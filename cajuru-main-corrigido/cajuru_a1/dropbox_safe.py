"""Fronteira explícita de leitura para a árvore sincronizada do Dropbox.

Este módulo não importa SDK do Dropbox e não oferece nenhuma primitiva de
escrita na origem. Listagem, ``os.open(..., O_RDONLY)`` e hash são as únicas
operações permitidas. Toda escrita ocorre em diretório temporário marcado e
fora da árvore configurada.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("cajuru_a1.dropbox")
READ_ONLY_MODE = True
CERT_EXT = {".pfx", ".p12"}
PDF_EXT = {".pdf"}
_MARKER = ".cajuru_a1_temp.json"


class DropboxWriteAttempt(RuntimeError):
    pass


class SourceChangedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    size: int
    mtime_ns: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class InventoryChange:
    kind: str  # created | deleted | modified | moved
    path: str
    other_path: str = ""
    detail: str = ""


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    last_exc: OSError | None = None
    for attempt in range(4):
        digest = hashlib.sha256()
        total = 0
        fd = None
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise ValueError(f"Arquivo excede o limite de {max_bytes} bytes")
                    digest.update(chunk)
            return digest.hexdigest()
        except ValueError:
            raise
        except OSError as exc:
            last_exc = exc
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            # Falhas transitórias no Windows (arquivo "somente online" do Dropbox
            # ainda baixando, antivírus com lock momentâneo, sincronização em
            # andamento) costumam se resolver em uma nova tentativa.
            time.sleep(0.5 * (attempt + 1))
            continue
    raise OSError(
        f"Não foi possível ler o arquivo com segurança após várias tentativas: {path}\n"
        f"Causa provável: o arquivo está marcado como 'somente online' no Dropbox (Smart Sync) "
        f"e não está totalmente baixado neste computador, ou está temporariamente bloqueado "
        f"por antivírus/sincronização. Clique com o botão direito na pasta CERTIFICADOS A1 no "
        f"Dropbox e escolha 'Sempre manter neste dispositivo', aguarde a sincronização completa "
        f"e tente novamente. Erro original: {last_exc}"
    ) from last_exc


def snapshot(path: Path) -> SourceSnapshot:
    file_stat = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(file_stat.st_mode):
        raise DropboxWriteAttempt(f"Link simbólico não é aceito como arquivo de origem: {path}")
    return SourceSnapshot(path, file_stat.st_size, file_stat.st_mtime_ns, stat.S_IMODE(file_stat.st_mode), sha256_file(path))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ReadOnlyDropbox:
    """Capacidade de leitura; operações de escrita falham de forma explícita."""

    read_only = True

    def __init__(self, root: str | Path):
        if not READ_ONLY_MODE:  # constante deliberadamente não configurável
            raise DropboxWriteAttempt("READ_ONLY_MODE precisa permanecer ativo")
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))
        if self.root == Path(self.root.anchor):
            raise ValueError("A raiz inteira do disco não pode ser configurada como Dropbox")
        self.rejected_files: list[tuple[Path, str]] = []

    def _guard_source(self, path: Path) -> Path:
        # strict resolve bloqueia symlink que escape da raiz.
        resolved = Path(path).expanduser().resolve(strict=True)
        if not _is_relative_to(resolved, self.root):
            raise DropboxWriteAttempt(f"Caminho fora da origem configurada: {path}")
        return resolved

    def _guard_destination(self, path: Path) -> Path:
        resolved = Path(path).expanduser().resolve(strict=False)
        if _is_relative_to(resolved, self.root):
            raise DropboxWriteAttempt(f"Tentativa de escrita dentro do Dropbox: {resolved}")
        return resolved

    # Barreiras explícitas inclusive contra chamadas acidentais por nome.
    def delete(self, *_args, **_kwargs):
        raise DropboxWriteAttempt("Exclusão no Dropbox é proibida por READ_ONLY_MODE")

    def move(self, *_args, **_kwargs):
        raise DropboxWriteAttempt("Movimentação no Dropbox é proibida por READ_ONLY_MODE")

    def rename(self, *_args, **_kwargs):
        raise DropboxWriteAttempt("Renomeação no Dropbox é proibida por READ_ONLY_MODE")

    def upload(self, *_args, **_kwargs):
        raise DropboxWriteAttempt("Upload no Dropbox é proibido por READ_ONLY_MODE")

    def write(self, *_args, **_kwargs):
        raise DropboxWriteAttempt("Escrita no Dropbox é proibida por READ_ONLY_MODE")

    @staticmethod
    def hash_file(path: Path) -> str:
        return sha256_file(Path(path))

    def _walk(self, *, include_hidden: bool = False) -> Iterable[tuple[Path, list[str], list[str]]]:
        skip = {".dropbox", ".dropbox.cache", ".git", "__pycache__", "node_modules"}
        for raw_dir, dirnames, filenames in os.walk(self.root, followlinks=False):
            if include_hidden:
                dirnames[:] = sorted(dirnames)
            else:
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.casefold() not in skip)
            yield Path(raw_dir), dirnames, sorted(filenames)

    def list_files(self, extensions: set[str], progress=None) -> list[Path]:
        files: list[Path] = []
        for directory, _dirs, names in self._walk():
            for name in names:
                if name.startswith((".", "~$")):
                    continue
                candidate = directory / name
                if candidate.suffix.casefold() not in extensions:
                    continue
                try:
                    resolved = self._guard_source(candidate)
                except FileNotFoundError:
                    # Arquivo sumiu entre a listagem e a resolução — típico de
                    # sincronização do Dropbox em andamento ("cópia em conflito"
                    # nascendo/morrendo). Não é rejeição de segurança: é
                    # transitório e o inventário tratará o estado final.
                    if progress:
                        progress(f"  {candidate.name} desapareceu durante a leitura (Dropbox sincronizando); ignorado.")
                    continue
                except (OSError, DropboxWriteAttempt) as exc:
                    self.rejected_files.append((candidate, type(exc).__name__))
                    if progress:
                        progress(f"Arquivo inseguro/ilegível bloqueado: {candidate.name} ({type(exc).__name__})")
                    continue
                if candidate.is_symlink():
                    self.rejected_files.append((candidate, "link_simbolico"))
                    if progress:
                        progress(f"Link simbólico bloqueado: {candidate.name}")
                    continue
                if resolved.is_file():
                    files.append(resolved)
                    if progress and len(files) % 25 == 0:
                        progress(f"  {len(files)} arquivo(s) encontrado(s)…")
        files.sort(key=lambda item: item.relative_to(self.root).as_posix().casefold())
        return files

    def list_certificates(self, progress=None) -> list[Path]:
        result = self.list_files(CERT_EXT, progress)
        if progress:
            progress(f"Total: {len(result)} certificado(s) PFX/P12.")
        return result

    def list_pdfs(self, progress=None) -> list[Path]:
        result = self.list_files(PDF_EXT, progress)
        if progress:
            progress(f"Total: {len(result)} documento(s) PDF de apoio.")
        return result

    def inventory(self, progress=None, *, max_files: int = 5000) -> dict[str, dict[str, Any]]:
        """Inventaria arquivos, diretórios e links, com caminhos relativos.

        Hash de todo arquivo permite detectar conteúdo alterado mesmo quando
        tamanho e data são preservados. Atime é ignorado porque a própria leitura
        pode atualizá-lo em alguns sistemas de arquivos. O limite padrão de
        5.000 arquivos é uma barreira de escopo contra inventariar por engano a
        raiz do Dropbox; informe um limite maior conscientemente se necessário.

        Arquivos/diretórios que desaparecem ou ficam ilegíveis DURANTE a
        varredura (sincronização do Dropbox criando/removendo "cópias em
        conflito", arquivos "somente online" ainda baixando, antivírus) são
        pulados com aviso em vez de derrubar o inventário inteiro.
        """
        if max_files <= 0:
            raise ValueError("O limite do inventário deve ser maior que zero")
        result: dict[str, dict[str, Any]] = {}
        count = 0
        skipped: list[str] = []

        def _skip(path: Path, why: str) -> None:
            skipped.append(f"{path.name} ({why})")
            if progress:
                progress(f"  inventário: entrada pulada — {path.name} ({why}).")

        for directory, dirnames, filenames in self._walk(include_hidden=True):
            for dirname in list(dirnames):
                path = directory / dirname
                rel = path.relative_to(self.root).as_posix() + "/"
                try:
                    st = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # A pasta sumiu entre a listagem e o stat (Dropbox
                    # sincronizando). Remove de dirnames para o os.walk não
                    # tentar deser nela em seguida.
                    dirnames.remove(dirname)
                    _skip(path, "sumiu durante a leitura (Dropbox sincronizando)")
                    continue
                except OSError as exc:
                    dirnames.remove(dirname)
                    _skip(path, f"ilegível: {type(exc).__name__}")
                    continue
                if stat.S_ISLNK(st.st_mode):
                    result[rel] = {"type": "symlink", "target": os.readlink(path), "mode": stat.S_IMODE(st.st_mode)}
                else:
                    result[rel] = {"type": "directory", "mode": stat.S_IMODE(st.st_mode)}
            for filename in filenames:
                path = directory / filename
                rel = path.relative_to(self.root).as_posix()
                try:
                    st = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    _skip(path, "sumiu durante a leitura (Dropbox sincronizando)")
                    continue
                except OSError as exc:
                    _skip(path, f"ilegível: {type(exc).__name__}")
                    continue
                if stat.S_ISLNK(st.st_mode):
                    result[rel] = {"type": "symlink", "target": os.readlink(path), "mode": stat.S_IMODE(st.st_mode)}
                    continue
                if not stat.S_ISREG(st.st_mode):
                    result[rel] = {"type": "other", "mode": stat.S_IMODE(st.st_mode)}
                    continue
                count += 1
                if count > max_files:
                    raise ValueError(
                        f"Inventário interrompido: a pasta selecionada excede o limite de "
                        f"{max_files:,} arquivos. Selecione diretamente CERTIFICADOS/CERTIFICADOS A1 "
                        "ou aumente dropbox.max_arquivos_inventario conscientemente."
                    )
                try:
                    digest = sha256_file(path)
                except OSError:
                    # Registrar sem hash deixaria o inventário instável entre
                    # duas passadas (arquivo 'somente online' pode ser lido na
                    # segunda). Pular e avisar mantém a comparação coerente.
                    count -= 1
                    _skip(path, "não pôde ser lido agora (provavelmente 'somente online' no Dropbox)")
                    continue
                result[rel] = {
                    "type": "file",
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "mode": stat.S_IMODE(st.st_mode),
                    "sha256": digest,
                }
                if progress and count % 50 == 0:
                    progress(f"  inventário: {count} arquivo(s) verificado(s)…")
        if progress:
            summary = f"Inventário de origem concluído: {count} arquivo(s), {len(result) - count} outra(s) entrada(s)."
            if skipped:
                summary += f" {len(skipped)} entrada(s) pulada(s) por sincronização/ilegibilidade."
            progress(summary)
        return dict(sorted(result.items()))

    def manifest(self, paths: list[Path] | None = None) -> dict[str, str]:
        selected = paths if paths is not None else self.list_certificates()
        return {str(self._guard_source(path)): sha256_file(self._guard_source(path)) for path in selected}

    def verify_inventory(
        self, expected: dict[str, dict[str, Any]], progress=None, *, max_files: int = 5000
    ) -> list[InventoryChange]:
        return compare_inventories(expected, self.inventory(progress=progress, max_files=max_files))

    def make_temp_root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="cajuru_a1_", dir=tempfile.gettempdir())).resolve()
        self._guard_destination(root)
        try:
            root.chmod(stat.S_IRWXU)
        except OSError:
            pass
        marker = {"owner": "cajuru_a1", "token": secrets.token_hex(16), "created": time.time()}
        (root / _MARKER).write_text(json.dumps(marker), encoding="utf-8")
        return root

    def unique_temp_name(self, source: Path, used: set[str]) -> str:
        # Inclui fragmento do caminho relativo para evitar colisão entre pastas.
        rel = source.relative_to(self.root).as_posix()
        base = source.name
        key = base.casefold()
        if key not in used:
            used.add(key)
            return base
        suffix = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:10]
        name = f"{source.stem}__{suffix}{source.suffix.lower()}"
        while name.casefold() in used:
            suffix = secrets.token_hex(5)
            name = f"{source.stem}__{suffix}{source.suffix.lower()}"
        used.add(name.casefold())
        return name

    def copy_one(self, source: Path, destination: Path, *, retries: int = 2) -> SourceSnapshot:
        source = self._guard_source(source)
        destination = self._guard_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(max(0, retries) + 1):
            try:
                return self._copy_once(source, destination)
            except SourceChangedError:
                raise
            except FileNotFoundError:
                # Origem sumiu no meio (Dropbox sincronizando): sem sentido
                # tentar de novo — sobe direto para virar 'falha_copia'.
                raise
            except OSError as exc:
                last_error = exc
                if isinstance(exc, FileExistsError):
                    break
                if attempt < retries:
                    time.sleep(0.15 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _copy_once(self, source: Path, destination: Path) -> SourceSnapshot:
        before = snapshot(source)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source, flags)
        created = False
        try:
            with os.fdopen(fd, "rb") as input_stream, destination.open("xb") as output_stream:
                created = True
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        except Exception:
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        try:
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        after = snapshot(source)
        if before != after:
            destination.unlink(missing_ok=True)
            raise SourceChangedError(f"Arquivo mudou durante a cópia: {source.name}")
        if sha256_file(destination) != before.sha256:
            destination.unlink(missing_ok=True)
            raise OSError(f"Falha de integridade na cópia temporária: {source.name}")
        return before

    def copy_to_temp(self, dest_dir: Path | None = None):
        temp_root = self._guard_destination(dest_dir) if dest_dir else self.make_temp_root()
        if dest_dir:
            temp_root.mkdir(parents=True, exist_ok=True)
            if not (temp_root / _MARKER).exists():
                (temp_root / _MARKER).write_text(json.dumps({"owner": "cajuru_a1", "token": secrets.token_hex(16)}), encoding="utf-8")
        pairs: list[tuple[Path, Path]] = []
        used: set[str] = set()
        for source in self.list_certificates():
            destination = temp_root / self.unique_temp_name(source, used)
            self.copy_one(source, destination)
            pairs.append((source, destination))
        return temp_root, pairs


def compare_inventories(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[InventoryChange]:
    changes: list[InventoryChange] = []
    deleted = set(before) - set(after)
    created = set(after) - set(before)

    # Reconhece movimento pelo hash, sem esconder a criação/exclusão quando há
    # vários arquivos idênticos e a associação seria ambígua.
    deleted_by_hash: dict[str, list[str]] = {}
    created_by_hash: dict[str, list[str]] = {}
    for path in deleted:
        digest = before[path].get("sha256")
        if digest:
            deleted_by_hash.setdefault(digest, []).append(path)
    for path in created:
        digest = after[path].get("sha256")
        if digest:
            created_by_hash.setdefault(digest, []).append(path)
    for digest in set(deleted_by_hash) & set(created_by_hash):
        old, new = deleted_by_hash[digest], created_by_hash[digest]
        if len(old) == len(new) == 1:
            deleted.remove(old[0])
            created.remove(new[0])
            changes.append(InventoryChange("moved", old[0], new[0], "mesmo SHA-256"))

    changes.extend(InventoryChange("deleted", path) for path in sorted(deleted))
    changes.extend(InventoryChange("created", path) for path in sorted(created))
    for path in sorted(set(before) & set(after)):
        left, right = before[path], after[path]
        if left != right:
            fields = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
            changes.append(InventoryChange("modified", path, detail=", ".join(fields)))
    return sorted(changes, key=lambda item: (item.kind, item.path, item.other_path))


def cleanup_temp(temp_root: Path) -> bool:
    """Remove somente diretório temporário criado e marcado por este módulo."""
    if not temp_root:
        return False
    path = Path(temp_root).expanduser().resolve(strict=False)
    if not path.exists():
        return True
    temp_base = Path(tempfile.gettempdir()).resolve()
    marker = path / _MARKER
    if not _is_relative_to(path, temp_base) or not path.name.startswith("cajuru_a1_") or not marker.is_file():
        log.error("Limpeza recusada: diretório sem marcador seguro: %s", path)
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    if payload.get("owner") != "cajuru_a1" or not payload.get("token"):
        return False
    shutil.rmtree(path)
    return not path.exists()
