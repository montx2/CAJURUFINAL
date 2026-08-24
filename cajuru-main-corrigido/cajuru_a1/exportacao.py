"""Exportação local de todos os certificados A1 já abertos com sucesso.

Esta operação não acessa o Jettax. Ela cria um ZIP com todos os PFX/P12 que
foram validados durante a auditoria, uma planilha CSV com as respectivas
senhas e a planilha modelo OFICIAL do Jettax
(``planilha_importacao_jettax.xlsx``, no formato exato de
``modelo_import_certificados.xlsx``) com CNPJ + SENHA já preenchidos para
importação em Clientes > Importar — tudo dentro da pasta de saída local
protegida do Cajuru A1.
"""
from __future__ import annotations

import csv
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path

from cajuru_a1.cnpjutil import format_cnpj, only_digits, pad_cnpj
from cajuru_a1.lote import build_planilha_importacao_certificados


def _safe_filename(name: str, used: set[str]) -> str:
    """Preserva um nome legível, sem permitir caminho ou sobrescrita no ZIP."""
    base = Path(name).name.replace("\x00", "_")
    stem = Path(base).stem or "certificado"
    suffix = Path(base).suffix.casefold()
    if suffix not in {".pfx", ".p12"}:
        suffix = ".pfx"
    candidate = f"{stem}{suffix}"
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def export_all_opened(certificates, output_dir: Path) -> dict:
    """Cria uma exportação baixável de todos os PFX/P12 abertos na auditoria.

    Não exige conciliação nem login no Jettax. Arquivos que não abriram são
    listados em ``nao_exportados.csv`` sem revelar candidatos de senha.
    """
    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = output_dir / "exportacoes" / f"todos_certificados_{stamp}"
    bundle_dir.mkdir(parents=True, exist_ok=False)
    try:
        bundle_dir.chmod(stat.S_IRWXU)
    except OSError:
        pass

    opened = [c for c in certificates if getattr(c, "opened", False) and getattr(c, "temp_path", None)]
    if not opened:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise RuntimeError("Nenhum certificado foi aberto com uma senha validada; não há exportação segura para gerar.")

    zip_path = bundle_dir / "todos_certificados_a1.zip"
    password_path = bundle_dir / "certificados_e_senhas.csv"
    skipped_path = bundle_dir / "nao_exportados.csv"
    planilha_path: Path | None = None
    used: set[str] = set()
    rows: list[tuple] = []

    try:
        with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
            for cert in opened:
                source = Path(cert.temp_path)
                if not source.is_file():
                    continue
                name = _safe_filename(cert.filename, used)
                archive.write(source, name)
                doc = pad_cnpj(only_digits(getattr(cert, "cnpj_cert", "") or ""))
                validity = cert.not_after.strftime("%d/%m/%Y") if getattr(cert, "not_after", None) else ""
                rows.append((name, format_cnpj(doc) if doc else "", cert.company_from_cert or "", "" if cert.password is None else str(cert.password), validity))
        if not rows:
            raise RuntimeError("As cópias temporárias dos certificados não estão mais disponíveis. Rode a auditoria novamente.")

        with password_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["ARQUIVO_NO_ZIP", "CNPJ_CPF_INTERNO", "EMPRESA_NO_CERTIFICADO", "SENHA_CERTIFICADO", "VALIDADE"])
            writer.writerows(rows)
        skipped = [c for c in certificates if c not in opened]
        with skipped_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["ARQUIVO", "MOTIVO"])
            for cert in skipped:
                writer.writerow([getattr(cert, "filename", ""), getattr(cert, "error", "não foi possível abrir")])
        # Planilha modelo OFICIAL do Jettax (modelo_import_certificados.xlsx)
        # preenchida com CNPJ + SENHA de cada certificado aberto e válido. É o
        # arquivo que a pessoa leva ao Jettax > Clientes > Importar.
        planilha_nota: str | None = None
        try:
            planilha_path = build_planilha_importacao_certificados(opened, bundle_dir)
        except RuntimeError as exc:
            # Sem certificado com CNPJ válido não há planilha a criar; não é
            # fatal para a exportação (o ZIP/CSV existem), apenas registramos.
            planilha_nota = f"Não foi possível gerar a planilha de importação Jettax: {exc}"
        cleanup_paths = [zip_path, password_path, skipped_path]
        if planilha_path:
            cleanup_paths.append(planilha_path)
        for path in cleanup_paths:
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        if planilha_nota:
            (bundle_dir / "planilha_nao_gerada.txt").write_text(planilha_nota + "\n", encoding="utf-8")
        readme_lines = [
            "EXPORTAÇÃO LOCAL DE CERTIFICADOS A1",
            "===================================\n",
            "Esta exportação foi gerada sem abrir ou alterar o Jettax.",
            "- todos_certificados_a1.zip: certificados que abriram com senha validada",
            "- certificados_e_senhas.csv: senhas correspondentes (SEGREDO)",
        ]
        if planilha_path:
            readme_lines.append("- planilha_importacao_jettax.xlsx: modelo OFICIAL do Jettax (CNPJ + SENHA)")
            readme_lines.append("  para importar em Clientes > Importar")
        readme_lines += [
            "- nao_exportados.csv: arquivos que não puderam ser abertos",
            "",
            "Para importar no Jettax: leve o ZIP (todos_certificados_a1.zip) e, se existir,",
            "a planilha (planilha_importacao_jettax.xlsx) juntos na tela Importar.",
            "",
            "Guarde esta pasta em local seguro e apague-a quando não precisar mais.",
            "O Dropbox foi usado somente para leitura.",
            "",
        ]
        readme = bundle_dir / "LEIA-ME.txt"
        readme.write_text("\n".join(readme_lines), encoding="utf-8")
        try:
            readme.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return {"dir": bundle_dir, "zip": zip_path, "senhas": password_path, "nao_exportados": skipped_path, "planilha": planilha_path, "quantidade": len(rows)}
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise
