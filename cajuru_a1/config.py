"""Configuração YAML simples (sem segredos persistidos)."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "pasta_certificados": "",
    "planilhas_senha": [],
    "opcoes": {
        "anos_senha": ["26", "2026", "25", "2025"],
        "tentar_senhas_comuns": False,
        "senha_manual_planilha": True,
        "max_certificado_mb": 30,
        "max_tentativas_senha": 500,
        "max_arquivos": 10000,
    },
    "saida": "",
}


def _blank() -> dict[str, Any]:
    return deepcopy(DEFAULTS)


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    config = _blank()
    p = Path(path)
    if p.is_file():
        raw = p.read_text(encoding="utf-8-sig")
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError("config.yaml deve ser um mapa")
        config = _deep_merge(config, data)
    return config


def save_config(cfg: dict[str, Any], path: str | Path = "config.yaml") -> None:
    dump = deepcopy(cfg)
    destination = Path(path).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(dump, stream, allow_unicode=True, sort_keys=False)


def get_output_dir(cfg: dict) -> Path:
    configured = str(cfg.get("saida") or "").strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / "CajuruA1" / "output"
    else:
        path = Path.home() / ".local" / "share" / "cajuru_a1" / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_config(cfg: dict) -> list[str]:
    errors: list[str] = []
    pasta = str(cfg.get("pasta_certificados") or "").strip()
    if not pasta:
        errors.append("Configure a pasta CERTIFICADOS A1.")
    elif not Path(pasta).expanduser().is_dir():
        errors.append(f"Pasta de certificados não encontrada: {pasta}")
    planilhas = [str(p).strip() for p in (cfg.get("planilhas_senha") or []) if str(p).strip()]
    if not planilhas:
        errors.append("Configure ao menos uma planilha de senhas.")
    for raw in planilhas:
        p = Path(raw).expanduser()
        if not p.is_file():
            errors.append(f"Planilha não encontrada: {raw}")
        elif p.suffix.lower() not in {".xlsx", ".xlsm"}:
            errors.append(f"Formato de planilha não suportado: {raw}")
    opcoes = cfg.get("opcoes") or {}
    for key in ("max_certificado_mb", "max_tentativas_senha", "max_arquivos"):
        try:
            if int(opcoes.get(key, 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"opcoes.{key} deve ser um inteiro positivo.")
    return errors
