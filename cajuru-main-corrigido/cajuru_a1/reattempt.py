"""Segunda passagem restrita usando identidade do cliente Jettax como pista."""

from __future__ import annotations

from pathlib import Path

from cajuru_a1.config import effective_config
from cajuru_a1.excel_passwords import load_excel_files
from cajuru_a1.models import JetaxClient, PipelineResult
from cajuru_a1.passwords import candidate_passwords
from cajuru_a1.pfx import inspect_file


def reattempt_locked(result: PipelineResult, cfg: dict, clientes: list[JetaxClient]) -> PipelineResult:
    cfg = effective_config(cfg)
    locked = [cert for cert in result.certificados if not cert.opened and cert.temp_path]
    if not locked or not clientes:
        return result
    vault = load_excel_files(cfg.get("excel", {}).get("arquivos") or [])
    options = cfg.get("opcoes") or {}
    security = cfg.get("seguranca") or {}
    max_attempts = int(security.get("max_tentativas_senha", 250))
    max_bytes = int(security.get("max_certificado_mb", 30)) * 1024 * 1024
    global_scan = bool(options.get("tentar_todas_senhas_da_planilha", False)) and bool(security.get("permitir_varredura_global", False))
    by_document = {client.cnpj: client for client in clientes if client and client.cnpj}

    for cert in locked:
        names: list[str] = []
        if cert.cnpj_filename and cert.cnpj_filename in by_document:
            names.append(by_document[cert.cnpj_filename].razao_social)
        names.append(Path(cert.filename).stem)
        candidates = candidate_passwords(
            vault=vault,
            empresa=names[0],
            cnpj=cert.cnpj_filename or "",
            extra_names=names,
            years=options.get("anos_senha"),
            include_all_sheet=global_scan,
            include_common=bool(options.get("tentar_senhas_comuns", False)),
            max_candidates=max_attempts,
        )
        new = inspect_file(Path(cert.source_path), Path(cert.temp_path), candidates, max_bytes=max_bytes, max_attempts=max_attempts)
        cert.attempts += new.attempts
        if new.opened:
            for attribute in (
                "opened", "password", "password_verified", "has_private_key", "password_source",
                "cnpj_cert", "company_from_cert", "not_before", "not_after", "expired",
                "not_yet_valid", "identity_conflict", "error", "error_code", "extra",
            ):
                setattr(cert, attribute, getattr(new, attribute))
            cert.password_source = (cert.password_source or "candidata") + "+segunda-passagem"
    return result
