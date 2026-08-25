"""Pipeline principal: varre a pasta de certificados, abre os PFX e concilia."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cajuru_a1.cnpjutil import is_valid_doc, only_digits, pad_cnpj
from cajuru_a1.excel_passwords import load_excel_files
from cajuru_a1.passwords import PasswordVault, candidate_passwords
from cajuru_a1.pfx import PfxInfo, inspect_file, pick_newest

log = logging.getLogger("cajuru_a1.pipeline")

CERT_EXTENSIONS = {".pfx", ".p12"}


@dataclass
class PipelineResult:
    certificates: list[PfxInfo] = field(default_factory=list)
    """Todos os PFX/P12 inspecionados."""

    selected: dict[str, PfxInfo] = field(default_factory=dict)
    """CNPJ -> certificado escolhido (mais novo/válido) para exportação."""

    duplicates: list[PfxInfo] = field(default_factory=list)
    """Certificados que perderam o desempate para o mesmo CNPJ."""

    rejected: list[tuple[PfxInfo | None, str, str]] = field(default_factory=list)
    """(cert, código, motivo) — itens que não podem ser exportados."""

    vault: PasswordVault | None = None
    stats: dict[str, int] = field(default_factory=dict)
    source_root: str = ""


def find_certificates(folder: str | Path, max_files: int = 10000) -> list[Path]:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Pasta de certificados não encontrada: {root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in CERT_EXTENSIONS:
            files.append(path)
            if len(files) > max_files:
                raise RuntimeError(
                    f"Mais de {max_files} certificados encontrados; verifique se a pasta "
                    "selecionada é a pasta CERTIFICADOS (não a raiz do Dropbox)."
                )
    files.sort(key=lambda p: str(p).casefold())
    return files


def run_pipeline(
    cert_folder: str | Path,
    excel_files: list[str | Path],
    *,
    years: list[str] | None = None,
    try_common: bool = False,
    max_cert_mb: int = 30,
    max_attempts: int = 500,
    max_files: int = 10000,
    log_fn=None,
) -> PipelineResult:
    say = log_fn or (lambda message: log.info(message))
    result = PipelineResult(source_root=str(Path(cert_folder).expanduser()))
    result.vault = load_excel_files(excel_files, log_fn=say)

    files = find_certificates(cert_folder, max_files=max_files)
    say(f"Encontrados {len(files)} arquivo(s) PFX/P12 em {cert_folder}")
    result.stats["total_arquivos"] = len(files)

    max_bytes = max_cert_mb * 1024 * 1024
    opened_count = 0

    for index, path in enumerate(files, 1):
        say(f"[{index}/{len(files)}] {path.name}")
        # Candidatas de senha baseadas no CNPJ do nome do arquivo e no próprio nome.
        name_doc = None
        from cajuru_a1.cnpjutil import best_doc_from_filename
        name_doc = best_doc_from_filename(path.name)
        candidates = candidate_passwords(
            vault=result.vault,
            empresa=path.stem,
            cnpj=name_doc or "",
            extra_names=[path.stem],
            years=years,
            include_common=try_common,
            max_candidates=max_attempts,
        )
        info = inspect_file(path, candidates, max_bytes=max_bytes, max_attempts=max_attempts)
        result.certificates.append(info)
        if info.opened:
            opened_count += 1
        say(f"    -> {'aberto' if info.opened else 'falhou'}: {info.error or info.company_from_cert or ''}")

    result.stats["abertos"] = opened_count
    _reconcile(result, say)
    _summarize(result)
    return result


def _reconcile(result: PipelineResult, say) -> None:
    """Agrupa por CNPJ interno, escolhe o mais novo e classifica rejeições."""
    by_cnpj: dict[str, list[PfxInfo]] = {}
    sha_seen: dict[str, PfxInfo] = {}

    for cert in result.certificates:
        # Conteúdo binário idêntico: duplicata exata.
        if cert.sha256 in sha_seen:
            cert.duplicate_sha256 = True
            cert.extra["duplicata_de"] = sha_seen[cert.sha256].filename
            result.rejected.append((cert, "DUPLICADO", f"Conteúdo idêntico a {sha_seen[cert.sha256].filename}"))
            continue
        sha_seen[cert.sha256] = cert

        if not cert.opened:
            result.rejected.append((cert, "SEM_SENHA", cert.error or "Não foi possível abrir o certificado"))
            continue
        if cert.identity_conflict:
            result.rejected.append((cert, "CONFLITO", cert.error or "Conflito de identidade no certificado"))
            continue
        if cert.not_yet_valid:
            result.rejected.append((cert, "AINDA_NAO_VALIDO", "Certificado ainda não está válido"))
            continue

        doc = pad_cnpj(only_digits(cert.cnpj_cert or ""))
        if not doc or not is_valid_doc(doc):
            result.rejected.append((cert, "SEM_CNPJ", "Certificado não contém CNPJ/CPF interno válido"))
            continue

        # CNPJ do nome divergente do interno só faz sentido registrar, mas o
        # interno é o que vale. Já é tratado como conflito em pfx.py.
        by_cnpj.setdefault(doc, []).append(cert)

    for doc, certs in by_cnpj.items():
        valid = [c for c in certs if not c.expired]
        pool = valid or certs  # se todos vencidos, mantém no grupo mas rejeita depois
        winner, losers = pick_newest(pool)
        if winner is None:
            for c in certs:
                result.rejected.append((c, "SEM_SENHA", "Nenhum PFX do grupo pôde ser aberto"))
            continue
        result.selected[doc] = winner
        for loser in losers:
            loser.extra.setdefault("motivo_substituicao", "Substituído por cópia mais nova do mesmo CNPJ")
            result.duplicates.append(loser)
            say(f"    {doc}: {loser.filename} substituído por {winner.filename}")

    # Rejeita vencidos que foram selecionados como último recurso.
    for doc, cert in list(result.selected.items()):
        if cert.expired:
            result.rejected.append((cert, "VENCIDO",
                                    f"Certificado vencido em {cert.not_after.strftime('%d/%m/%Y') if cert.not_after else '?'}"))
            del result.selected[doc]


def _summarize(result: PipelineResult) -> None:
    result.stats.update({
        "selecionados": len(result.selected),
        "duplicatas": len(result.duplicates),
        "rejeitados": len(result.rejected),
        "com_senha_planilha": sum(1 for c in result.certificates if c.opened and c.password_source and c.password_source.startswith("planilha")),
    })
