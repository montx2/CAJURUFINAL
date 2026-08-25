"""Inventário de integridade e trilha de auditoria sem segredos."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cajuru_a1.dropbox_safe import InventoryChange, ReadOnlyDropbox


def build_manifest(paths: Iterable[Path]) -> dict[str, str]:
    return {str(Path(path).resolve()): ReadOnlyDropbox.hash_file(Path(path)) for path in paths}


def verify_manifest(manifest: dict[str, str]) -> tuple[bool, list[str]]:
    changed: list[str] = []
    for raw_path, expected in manifest.items():
        path = Path(raw_path)
        try:
            current = ReadOnlyDropbox.hash_file(path)
        except Exception:
            changed.append(f"{path} [ausente/ilegível]")
            continue
        if current != expected:
            changed.append(f"{path} [hash alterado]")
    return not changed, changed


def inventory_summary(changes: list[InventoryChange] | list[dict]) -> dict[str, int]:
    summary = {"created": 0, "deleted": 0, "modified": 0, "moved": 0}
    for change in changes:
        kind = change.kind if isinstance(change, InventoryChange) else str(change.get("kind", ""))
        if kind in summary:
            summary[kind] += 1
    return summary


def serialize_changes(changes: list[InventoryChange]) -> list[dict]:
    return [asdict(change) for change in changes]


def write_run_audit(
    dest: Path,
    *,
    action: str,
    stats: dict,
    manifest: dict[str, str] | None = None,
    dry_run: bool,
    inventory_before: dict | None = None,
    inventory_after: dict | None = None,
    changes: list[InventoryChange] | list[dict] | None = None,
    decisions: list | None = None,
    outcome: str = "ok",
    send_results: list[dict] | None = None,
) -> Path:
    """Escreve JSON atomicamente. Nunca recebe ou serializa senhas."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw_changes = changes or []
    serialized = [asdict(item) if isinstance(item, InventoryChange) else dict(item) for item in raw_changes]
    safe_decisions = []
    for decision in decisions or []:
        safe_decisions.append({
            "status": getattr(decision, "status", ""),
            "empresa": getattr(getattr(decision, "cliente", None), "razao_social", ""),
            "cnpj": getattr(getattr(decision, "cliente", None), "cnpj", ""),
            "arquivo": getattr(getattr(decision, "cert", None), "filename", ""),
            "metodo": getattr(decision, "metodo", ""),
            "confianca": getattr(decision, "confianca", 0.0),
            "motivo": getattr(decision, "motivo", ""),
            "evidencias": list(getattr(decision, "evidencias", []) or []),
        })
    payload = {
        "schema_version": 2,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "action": str(action),
        "outcome": str(outcome),
        "dry_run": bool(dry_run),
        "read_only_mode": True,
        "stats": {str(key): int(value) if isinstance(value, (bool, int)) else str(value) for key, value in (stats or {}).items()},
        "source_files": len(manifest or {}),
        "source_manifest": manifest or {},
        "inventory_entries_before": len(inventory_before or {}),
        "inventory_entries_after": len(inventory_after or {}),
        "integrity": {
            "ok": not serialized,
            "summary": inventory_summary(serialized),
            "changes": serialized,
        },
        "decisions": safe_decisions,
        "send_results": list(send_results or []),
        "hostname": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(temporary, dest)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
    return dest
