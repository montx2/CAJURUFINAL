"""Gerenciador de jobs com persistência em disco.

Os jobs (rodar tudo, pré-checagem, exportar, lote manual) rodam numa thread
do servidor. Antes eles viviam só em memória: se o navegador fosse fechado,
o polling parava mas o job continuava — e ao reabrir o painel o último job
não era recuperado. Também podia haver perda do log em travamentos do
navegador.

Aqui cada job é espelhado num arquivo JSON em ``<state>/jobs/<id>.json``.
- O log é incremental (append no arquivo a cada linha), então não se perde
  se o navegador fechar ou o painel reiniciar.
- Quando o painel sobe, os jobs "running" do último processo são marcados
  como interrompidos (o processo antigo morreu junto).
- ``latest(kind)`` lê primeiro a memória; se não houver, procura em disco.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Número máximo de linhas de log entregues por polling. O log completo fica
# em disco; isto evita JSONs grandes a cada 900 ms.
MAX_LOG_LINES = 500


def _now_stamp() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error | cancelled
    message: str = ""
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    logs: list[str] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    result: Any = None
    error: str = ""
    # Caminho do JSON persistido.
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "logs": list(self.logs[-MAX_LOG_LINES:]),
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "result": self.result,
        }


class JobManager:
    """Mantém jobs em memória e espelha o essencial em disco."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._state_dir: Path | None = None
        if state_dir:
            self.set_state_dir(Path(state_dir))

    def set_state_dir(self, path: Path) -> None:
        with self._lock:
            self._state_dir = path
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._recover_orphans()

    def _jobs_dir(self) -> Path | None:
        if self._state_dir is None:
            return None
        d = self._state_dir / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _recover_orphans(self) -> None:
        """Marca jobs 'running' deixados por um processo anterior."""
        d = self._jobs_dir()
        if d is None:
            return
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") == "running":
                data["status"] = "error"
                data["finished"] = _now_stamp()
                data["error"] = (
                    "O processo do painel foi encerrado enquanto este job rodava. "
                    "Os arquivos do Dropbox não foram alterados (modo somente leitura); "
                    "rode novamente para concluir."
                )
                data["message"] = data["error"]
                logs = data.get("logs") or []
                logs.append(f"[{_now_stamp()}] {data['error']}")
                data["logs"] = logs[-MAX_LOG_LINES:]
                try:
                    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

    def _path_for(self, jid: str) -> Path | None:
        d = self._jobs_dir()
        if d is None:
            return None
        return d / f"{jid}.json"

    def _persist(self, job: Job) -> None:
        path = self._path_for(job.id)
        if path is None:
            return
        tmp = path.with_suffix(".json.tmp")
        # Montamos o dicionário na mão (em vez de ``dataclasses.asdict``)
        # porque ``cancel_event`` é um ``threading.Event`` que não é
        # serializavel/picklável e derrubava o asdict.
        data = {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "message": job.message,
            "cancel_requested": job.cancel_requested,
            "logs": list(job.logs[-MAX_LOG_LINES:]),
            "started": job.started,
            "finished": job.finished,
            "result": job.result,
            "error": job.error,
        }
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception:
            # Persistência é best-effort; nunca deve derrubar o job.
            pass

    def create(self, kind: str) -> Job:
        jid = datetime.now().strftime("%Y%m%d%H%M%S%f") + "-" + uuid.uuid4().hex[:6]
        job = Job(id=jid, kind=kind, started=_now_stamp())
        path = self._path_for(jid)
        if path:
            job.path = str(path)
        with self._lock:
            self._jobs[jid] = job
            self._persist(job)
        return job

    def get(self, jid: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(jid)
            if job:
                return job
            # Recupera do disco (ex.: navegador fechado e painel reiniciado).
            return self._load_from_disk(jid)

    def _load_from_disk(self, jid: str) -> Job | None:
        path = self._path_for(jid) if self._state_dir else None
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        job = Job(
            id=str(data.get("id") or jid),
            kind=str(data.get("kind") or ""),
            status=str(data.get("status") or "running"),
            message=str(data.get("message") or ""),
            cancel_requested=bool(data.get("cancel_requested")),
            logs=list(data.get("logs") or []),
            started=str(data.get("started") or ""),
            finished=str(data.get("finished") or ""),
            result=data.get("result"),
            error=str(data.get("error") or ""),
            path=str(path),
        )
        # Jobs recuperados do disco não podem ser cancelados por Event (o
        # processo original já morreu); mantemos o estado como está.
        self._jobs[jid] = job
        return job

    def append_log(self, job: Job, line: str) -> None:
        with self._lock:
            job.logs.append(line)
            self._persist(job)

    def update(self, job: Job, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            self._persist(job)

    def cancel(self, jid: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(jid) or self._load_from_disk(jid)
            if not job or job.status != "running":
                return job
            job.cancel_requested = True
            # Só sinaliza o Event se o job está de fato rodando neste processo.
            if job.cancel_event is not None:
                try:
                    job.cancel_event.set()
                except Exception:
                    pass
            job.message = "Cancelamento solicitado; finalizando com segurança a etapa atual…"
            job.logs.append(f"[{_now_stamp()}] Cancelamento solicitado pelo usuário.")
            self._persist(job)
            return job

    def latest(self, kind: str | None = None) -> Job | None:
        with self._lock:
            items = list(self._jobs.values())
            if not items and self._state_dir:
                # Procura no disco pelo arquivo mais recente.
                d = self._jobs_dir()
                if d is not None:
                    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for f in files[:20]:
                        loaded = self._load_from_disk(f.stem)
                        if loaded:
                            items.append(loaded)
            if kind:
                items = [j for j in items if j.kind == kind]
            return items[-1] if items else None

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            items = list(self._jobs.values())
            if self._state_dir and len(items) < limit:
                d = self._jobs_dir()
                if d is not None:
                    known = {j.id for j in items}
                    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for f in files:
                        if f.stem in known:
                            continue
                        loaded = self._load_from_disk(f.stem)
                        if loaded:
                            items.append(loaded)
                        if len(items) >= limit:
                            break
            items.sort(key=lambda j: j.started or "")
            return items[-limit:]


# Instância global. O state_dir é definido quando o app Flask sobe
# (create_app), já que na hora da importação ainda não temos config.
JOBS = JobManager()
