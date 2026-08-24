from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

DEFAULTS: dict[str, Any] = {
    "dropbox": {
        "modo": "local",
        "pasta": "",
        "somente_leitura": True,
        "tentativas_copia": 2,
        # O inventário completo é deliberadamente limitado para evitar varrer
        # por engano uma árvore Dropbox inteira ou uma pasta desproporcional.
        "max_arquivos_inventario": 5000,
    },
    "excel": {"arquivos": []},
    "jettax": {
        "url": "https://admin.jettax360.com.br",
        "email": "",
        # Não persistir senha. Use login assistido ou JETTAX_PASSWORD.
        "senha": "",
        "headless": False,
        "login": "assisted",
    },
    "opcoes": {
        # Padrão conservador: só simula. O painel web nunca envia
        # automaticamente ao Jettax de qualquer forma (ele só concilia e
        # monta o lote para importação manual); este campo importa para a
        # GUI de mesa e para o CLI `--enviar`, que pedem confirmação antes de
        # gravar de verdade.
        "dry_run": True,
        "enviar_vencidos": False,
        "modo_envio": "lote",
        "tentar_todas_senhas_da_planilha": False,
        "tentar_senhas_comuns": False,
        "anos_senha": ["26", "2026", "25", "2025"],
        # Renovar/atualizar certificados de TODAS as empresas, inclusive as que
        # já possuem A1 no Jettax (modo "atualizar todos"). Quando False, o
        # comportamento histórico é preservado: só empresas SEM A1 são alvo.
        "atualizar_todas_empresas": False,
        # Quando há dois PFX diferentes para o mesmo CNPJ, escolher
        # automaticamente o mais atualizado (validade/início/mtime).
        "escolher_certificado_mais_novo": True,
        # Lote MANUAL: não preenche a senha na planilha de importação e salva
        # o ZIP + a planilha em output/lotes/ (não apaga em seguida). O usuário
        # preenche a senha à mão no Jettax. Quando a opção salvar_senhas_csv
        # está ativa, um CSV separado com as senhas validadas também é gerado.
        "lote_senha_manual": True,
        "salvar_senhas_csv": True,
    },
    "seguranca": {
        "max_certificado_mb": 30,
        "max_pdf_mb": 60,
        "max_tentativas_senha": 250,
        "permitir_varredura_global": False,
    },
    "pdf": {"habilitado": True, "max_paginas": 30, "ocr": False, "ocr_max_paginas": 3, "tesseract": "tesseract"},
    "armazenamento": {"saida": "", "estado": ""},
}


def _blank() -> dict[str, Any]:
    return deepcopy(DEFAULTS)


def effective_config(cfg: dict | None) -> dict[str, Any]:
    """Aplica defaults também a configurações montadas programaticamente."""
    return _deep_merge(_blank(), cfg or {})


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _win_path_yaml_safe(text: str) -> str:
    return text.replace("\\", "/")


def _parse_yaml_text(text: str) -> dict:
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml deve ser um mapa")
    return data


def load_yaml_mapping(text: str) -> tuple[dict, bool]:
    try:
        return _parse_yaml_text(text), False
    except yaml.YAMLError:
        fixed = _win_path_yaml_safe(text)
        return _parse_yaml_text(fixed), True


def find_config_path(path: str | Path | None = None) -> Path | None:
    if path is not None:
        candidate = Path(path)
        return candidate if candidate.exists() else None
    candidate = Path("config.yaml")
    return candidate if candidate.exists() else None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = _blank()
    found = find_config_path(path)
    if found:
        raw = found.read_text(encoding="utf-8-sig")
        data, _fixed_in_memory = load_yaml_mapping(raw)
        config = _deep_merge(config, data)
    url = str(config.get("jettax", {}).get("url") or "")
    if "admin.jetax360.com.br" in url.casefold() or not url.strip():
        config["jettax"]["url"] = "https://admin.jettax360.com.br"
    # Segredos por ambiente têm precedência e nunca voltam ao YAML.
    env_password = os.environ.get("JETTAX_PASSWORD")
    if env_password:
        config["jettax"]["senha"] = env_password
    return config


def _paths_forward(obj):
    if isinstance(obj, str):
        if "\\" in obj and (":" in obj or obj.startswith("\\\\")):
            return obj.replace("\\", "/")
        return obj
    if isinstance(obj, dict):
        return {key: _paths_forward(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_paths_forward(value) for value in obj]
    return obj


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _looks_inside_dropbox(path: Path) -> bool:
    return any(part.casefold() == "dropbox" or part.casefold().startswith("dropbox (") for part in path.resolve(strict=False).parts)


def _is_certificates_folder(path: Path) -> bool:
    """Aceita a pasta de trabalho, não a raiz do Dropbox nem um ancestral.

    Instalações antigas usam tanto ``CERTIFICADOS`` quanto ``CERTIFICADOS A1``;
    ambos são nomes diretos válidos da pasta de certificados.
    """
    return path.name.strip().casefold().startswith("certificados")


def save_config(cfg: dict[str, Any], path: str | Path = "config.yaml") -> None:
    destination = Path(path).expanduser().resolve(strict=False)
    source = str((cfg.get("dropbox") or {}).get("pasta") or "").strip()
    if (source and _is_within(destination, Path(source).expanduser())) or _looks_inside_dropbox(destination):
        raise ValueError("Configuração não pode ser gravada dentro de uma árvore Dropbox")
    dump = _paths_forward(deepcopy(cfg))
    dump.setdefault("dropbox", {})["somente_leitura"] = True
    # Senhas e tokens nunca são persistidos pelo aplicativo.
    dump.setdefault("jettax", {})["senha"] = ""
    dump.setdefault("dropbox", {}).pop("token", None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(dump, stream, allow_unicode=True, sort_keys=False)


def _app_data_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base).expanduser() / "CajuruA1" if base else Path.home() / "AppData" / "Local" / "CajuruA1"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "cajuru_a1"


def _safe_runtime_path(cfg: dict, key: str, fallback: str) -> Path:
    configured = str((cfg.get("armazenamento") or {}).get(key) or "").strip()
    path = (Path(configured).expanduser() if configured else _app_data_root() / fallback).resolve(strict=False)
    source = str((cfg.get("dropbox") or {}).get("pasta") or "").strip()
    if (source and _is_within(path, Path(source).expanduser())) or _looks_inside_dropbox(path):
        raise ValueError(f"Diretório de {key} não pode ficar dentro de uma árvore Dropbox: {path}")
    return path


def get_output_dir(cfg: dict) -> Path:
    return _safe_runtime_path(cfg, "saida", "output")


def get_state_dir(cfg: dict) -> Path:
    return _safe_runtime_path(cfg, "estado", "state")


def validate_config(cfg: dict) -> list[str]:
    errors: list[str] = []
    dropbox = cfg.get("dropbox") or {}
    if dropbox.get("somente_leitura") is not True:
        errors.append("READ_ONLY_MODE é obrigatório: dropbox.somente_leitura deve ser true.")
    if str(dropbox.get("modo") or "local").casefold() != "local":
        errors.append("Somente o modo local sincronizado, estritamente de leitura, é suportado.")
    source = str(dropbox.get("pasta") or "").strip()
    if not source:
        errors.append("Configure a pasta CERTIFICADOS A1 do Dropbox.")
    else:
        path = Path(source).expanduser()
        if not path.exists():
            errors.append(f"A pasta do Dropbox não existe: {source}")
        elif not path.is_dir():
            errors.append(f"A origem configurada não é um diretório: {source}")
        elif not _is_certificates_folder(path):
            errors.append(
                "Selecione diretamente a pasta CERTIFICADOS ou CERTIFICADOS A1; "
                "a raiz do Dropbox e pastas ancestrais não fazem parte do escopo."
            )
    excels = [str(item).strip() for item in ((cfg.get("excel") or {}).get("arquivos") or []) if str(item).strip()]
    if len(excels) < 2:
        errors.append("Configure as duas planilhas de senhas.")
    for raw in excels:
        path = Path(raw).expanduser()
        if not path.exists():
            errors.append(f"Planilha não encontrada: {raw}")
        elif not path.is_file():
            errors.append(f"O caminho da planilha não é um arquivo: {raw}")
        elif path.suffix.casefold() not in {".xlsx", ".xlsm"}:
            errors.append(f"Formato de planilha não suportado: {raw}")
    url = str((cfg.get("jettax") or {}).get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "admin.jettax360.com.br":
        errors.append("A URL do Jettax deve ser exatamente HTTPS no host admin.jettax360.com.br.")
    try:
        get_output_dir(cfg)
        get_state_dir(cfg)
    except ValueError as exc:
        errors.append(str(exc))
    security = cfg.get("seguranca") or {}
    for key in ("max_certificado_mb", "max_pdf_mb", "max_tentativas_senha"):
        try:
            if int(security.get(key, 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"seguranca.{key} deve ser um inteiro positivo.")
    try:
        if int(dropbox.get("max_arquivos_inventario", 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("dropbox.max_arquivos_inventario deve ser um inteiro positivo.")
    return errors
