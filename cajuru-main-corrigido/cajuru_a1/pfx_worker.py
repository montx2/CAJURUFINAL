"""Isolador de processo para a inspeção de PFX/P12.

O OpenSSL (usado pela biblioteca ``cryptography``) pode travar por tempo
indeterminado ao encontrar PKCS#12 corrompidos ou construídos de forma
hostil (MAC com contadores gigantes, ASN.1 malformado, loops internos etc.).
Como o travamento acontece em código nativo, ele **não** responde a
``threading.Event`` nem pode ser interrompido pelo Python — o job inteiro
parava e a interface ficava "no 69/502".

A solução aqui é rodar cada inspeção num processo filho curto e matar esse
processo se ele não responder em ``timeout`` segundos. O filho só faz
cálculo local (leitura do arquivo + chamadas ao OpenSSL); ele nunca toca
no Dropbox, Jettax ou em qualquer segredo persistido.

O loop do filho reaproveita o mesmo processo entre arquivos (evita o custo
de centenas de sub-interpretadores). Se um arquivo travar, o filho é
encerrado e um novo é criado no próximo arquivo.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from cajuru_a1.pfx import PfxInfo, inspect_file

# Tempo máximo, em segundos, que um único PFX pode consumir no OpenSSL.
# Acima disso o processo filho é morto e o certificado vai para revisão
# manual (não trava mais o lote inteiro).
DEFAULT_TIMEOUT = 90


def _json_default(value):
    # datetime chega aqui quando serializamos o PfxInfo (asdict inclui os
    # campos not_before/not_after). Devolvemos ISO-8601; o reconstrutor do
    # lado do pai transforma de volta em datetime.
    try:
        from datetime import datetime as _dt, date as _date
        if isinstance(value, (_dt, _date)):
            return value.isoformat()
    except Exception:
        pass
    return str(value)


def _send(pipe, message: dict) -> None:
    pipe.send_bytes(
        json.dumps(message, ensure_ascii=False, default=_json_default).encode("utf-8")
    )


def _recv_raw(pipe) -> dict:
    return json.loads(pipe.recv_bytes().decode("utf-8"))


def _coerce_field(key: str, value):
    """Converte campos serializados de volta aos tipos esperados."""
    if value is None:
        return None
    if key in ("not_before", "not_after") and isinstance(value, str):
        try:
            from datetime import datetime as _dt
            text = value
            if text.endswith("+00:00"):
                text = text[:-6] + "+00:00"
            return _dt.fromisoformat(text)
        except Exception:
            return None
    if key in ("source_mtime",):
        try:
            return float(value)
        except Exception:
            return 0.0
    if key in ("size", "attempts"):
        try:
            return int(value)
        except Exception:
            return 0
    if key in ("opened", "password_verified", "has_private_key",
               "expired", "not_yet_valid", "identity_conflict",
               "duplicate_sha256"):
        return bool(value)
    return value


def _worker_loop(conn) -> None:
    """Loop do processo filho: recebe pedidos e devolve resultados/progresso."""
    try:
        while True:
            request = _recv_raw(conn)
            command = request.get("cmd")
            if command == "stop":
                return
            if command != "inspect":
                continue
            source = Path(request["source"])
            temp = Path(request["temp"])
            candidates = [tuple(item) for item in request.get("candidates") or []]
            max_bytes = int(request.get("max_bytes", 30 * 1024 * 1024))
            max_attempts = int(request.get("max_attempts", 250))

            def progress(attempt, total, _name=None):
                try:
                    _send(conn, {"type": "progress", "attempt": int(attempt), "total": int(total)})
                except Exception:
                    pass

            try:
                info = inspect_file(
                    source, temp, candidates,
                    max_bytes=max_bytes, max_attempts=max_attempts, progress=progress,
                )
                _send(conn, {"type": "result", "info": asdict(info)})
            except Exception as exc:
                _send(conn, {
                    "type": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=3),
                })
    except (EOFError, BrokenPipeError, OSError):
        # Pai desligou ou morreu — encerra silenciosamente.
        return
    except Exception:
        # Nunca deixar o filho vivo em estado desconhecido.
        try:
            _send(conn, {"type": "fatal", "error": traceback.format_exc(limit=3)})
        except Exception:
            pass
        return


def _start_worker():
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(target=_worker_loop, args=(child_conn,), daemon=True)
    proc.start()
    # O filho herdou a ponta de escrita; fechamos aqui para que o poll/recv
    # do pai não fique com uma referência viva que impede detectar EOF.
    child_conn.close()
    return proc, parent_conn


class IsolatedPfxInspector:
    """Executa ``inspect_file`` num processo filho com timeout duro.

    Uso::

        with IsolatedPfxInspector(timeout=90) as inspector:
            info = inspector.inspect(source, temp, candidates, ...)

    Ou chame ``.close()`` no final. A classe pode ser reutilizada para
    vários arquivos; o filho é recriado automaticamente após timeout/morte.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = max(5.0, float(timeout or DEFAULT_TIMEOUT))
        self._proc = None
        self._conn = None
        self._spawn()

    def _spawn(self) -> None:
        self._proc, self._conn = _start_worker()

    def _kill(self) -> None:
        """Encerra o filho de forma forçada."""
        conn, self._conn = self._conn, None
        proc, self._proc = self._proc, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if proc is not None and proc.is_alive():
            try:
                proc.terminate()
                proc.join(timeout=3)
                if proc.is_alive() and hasattr(proc, "kill"):
                    proc.kill()
                    proc.join(timeout=2)
            except Exception:
                pass

    def _drain(self) -> None:
        """Descarta qualquer mensagem pendurada no pipe (ex.: progresso
        atrasado de um arquivo anterior)."""
        if self._conn is None:
            return
        try:
            while self._conn.poll():
                try:
                    self._conn.recv_bytes()
                except EOFError:
                    break
        except Exception:
            pass

    def close(self) -> None:
        self._kill()

    def __enter__(self) -> "IsolatedPfxInspector":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _restart(self) -> None:
        self._kill()
        self._spawn()

    def inspect(
        self,
        source: Path,
        temp: Path,
        candidates: list[tuple[str, str]],
        *,
        max_bytes: int = 30 * 1024 * 1024,
        max_attempts: int = 250,
        progress: Callable[[int, int], None] | None = None,
        filename: str = "",
    ) -> PfxInfo:
        import time as _time

        # As tuplas são enviadas como listas pelo JSON; normalizamos.
        payload_candidates = [
            [("" if pwd is None else str(pwd)), str(src or "")]
            for pwd, src in (candidates or [])
        ]
        request = {
            "cmd": "inspect",
            "source": str(source),
            "temp": str(temp),
            "candidates": payload_candidates,
            "max_bytes": int(max_bytes),
            "max_attempts": int(max_attempts),
        }

        # Garante um filho vivo e um pipe limpo.
        if self._proc is None or not self._proc.is_alive():
            self._spawn()
        self._drain()
        try:
            self._conn.send_bytes(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, OSError):
            # Conexão quebrada: recria UM filho e tenta de novo uma única
            # vez (não faz loop infinito se o PFX for o problema).
            self._restart()
            try:
                self._conn.send_bytes(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            except Exception:
                return self._timeout_info(source, temp, filename)

        deadline = _time.monotonic() + self.timeout
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = self._conn.poll(remaining)
            except Exception:
                ready = False
            if not ready:
                break
            try:
                message = _recv_raw(self._conn)
            except EOFError:
                message = None
            if message is None:
                # Filho morreu no meio do processamento.
                self._restart()
                return self._timeout_info(source, temp, filename)
            mtype = message.get("type")
            if mtype == "progress":
                if progress is not None:
                    try:
                        progress(int(message.get("attempt", 0)), int(message.get("total", 0)))
                    except Exception:
                        pass
                continue
            if mtype == "result":
                data = message.get("info") or {}
                info = PfxInfo(
                    source_path=str(data.get("source_path", source)),
                    temp_path=str(data.get("temp_path", temp)),
                    filename=str(data.get("filename", filename or source.name)),
                    sha256=str(data.get("sha256", "")),
                    size=int(data.get("size", 0) or 0),
                )
                for key in PfxInfo.__dataclass_fields__:
                    if key in data:
                        info.__dict__[key] = _coerce_field(key, data[key])
                if not isinstance(info.extra, dict):
                    info.extra = {}
                return info
            if mtype in ("error", "fatal"):
                # Erro no filho (não timeout): não derruba o lote; devolve
                # um PfxInfo de falha de inspeção.
                self._restart()
                from cajuru_a1.pfx import file_sha256
                sha = ""
                size = 0
                try:
                    sha = file_sha256(Path(temp))
                    size = Path(temp).stat().st_size
                except Exception:
                    pass
                return PfxInfo(
                    source_path=str(source),
                    temp_path=str(temp),
                    filename=filename or Path(source).name,
                    sha256=sha, size=size,
                    error=f"Falha isolada na inspeção: {message.get('error', 'erro desconhecido')}",
                    error_code="falha_inspecao",
                )

        # Timeout: mata o filho travado e devolve revisão manual.
        self._restart()
        return self._timeout_info(source, temp, filename)

    def _timeout_info(self, source, temp, filename: str) -> PfxInfo:
        from cajuru_a1.pfx import file_sha256
        sha = ""
        size = 0
        try:
            sha = file_sha256(Path(temp))
            size = Path(temp).stat().st_size
        except Exception:
            pass
        return PfxInfo(
            source_path=str(source),
            temp_path=str(temp),
            filename=filename or Path(source).name,
            sha256=sha,
            size=size,
            cnpj_filename=None,
            error=f"Timeout de {int(self.timeout)}s ao processar o PKCS#12 (travou no OpenSSL) — arquivo marcado para revisão manual",
            error_code="timeout_pfx",
            attempts=0,
        )


# Evita que o usuário veja o banner do Tkinter/Chrome quando o filho sobe
# no Windows com o contexto "spawn".
if sys.platform.startswith("win"):
    try:
        import ctypes  # type: ignore
        ctypes.windll.kernel32.SetDllDirectoryW(None)  # type: ignore[attr-defined]
    except Exception:
        pass

# Proteção adicional: alguns ambientes (macOS, Windows) exigem que o método
# de start seja definido antes de qualquer fork.
try:
    if not mp.get_start_method(allow_none=True):
        mp.set_start_method("spawn", force=False)
except RuntimeError:
    pass
