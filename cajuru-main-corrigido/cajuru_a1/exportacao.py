"""Exportação local de certificados A1 já abertos com sucesso.

Esta operação não acessa o Jettax. O ZIP principal é preparado especificamente
para o importador do Jettax 360: cada certificado recebe o nome
``CNPJ.pfx`` (14 dígitos, sem razão social, CPF, espaços ou sufixos). Itens que
não têm um CNPJ corporativo válido dentro do X.509 — e duplicatas do mesmo
CNPJ — ficam fora desse ZIP para não invalidar toda a importação.
"""
from __future__ import annotations

import csv
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path

from cajuru_a1.cnpjutil import format_cnpj, is_valid_cnpj, only_digits, pad_cnpj
from cajuru_a1.lote import build_planilha_importacao_certificados


def _safe_review_filename(name: object, used: set[str]) -> str:
    """Cria um nome legível e seguro para o ZIP que é só de revisão.

    O ZIP principal do Jettax nunca usa esta função: nele os nomes devem ser
    exatamente ``<CNPJ>.pfx``. Aqui preservar o nome de origem é útil para a
    pessoa localizar um item que precisa de conferência manual.
    """
    base = str(name or "certificado.pfx").replace("\x00", "_").replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(base).stem.strip().strip(".") or "certificado"
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


def _temp_source(cert) -> Path | None:
    raw_path = getattr(cert, "temp_path", None)
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_file() else None


def _cnpj_for_jettax(cert) -> str | None:
    """Retorna somente o CNPJ validado de dentro do certificado.

    O nome do arquivo é uma pista útil para a auditoria, mas não pode decidir o
    nome enviado ao Jettax. Um CPF (11 dígitos), um CNPJ malformado ou um CNPJ
    existente apenas no nome do arquivo não gera um ZIP importável.
    """
    document = pad_cnpj(only_digits(getattr(cert, "cnpj_cert", "") or ""))
    return document if is_valid_cnpj(document) else None


def _ineligible_reason(cert) -> str:
    raw_document = only_digits(getattr(cert, "cnpj_cert", "") or "")
    if len(raw_document) == 11:
        return "O certificado contém CPF; o importador do Jettax exige CNPJ válido de 14 dígitos."
    if raw_document:
        return "O CNPJ interno do certificado é inválido; o item não entrou no ZIP do Jettax."
    return "Não foi encontrado um CNPJ interno válido no certificado; o item não entrou no ZIP do Jettax."


def _timestamp(value) -> float:
    try:
        return float(value.timestamp())
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return -1.0


def _newest_first_key(cert) -> tuple:
    """Chave determinística para não criar dois arquivos com o mesmo CNPJ."""
    return (
        _timestamp(getattr(cert, "not_after", None)),
        _timestamp(getattr(cert, "not_before", None)),
        float(getattr(cert, "source_mtime", 0.0) or 0.0),
        int(getattr(cert, "size", 0) or 0),
        str(getattr(cert, "filename", "")).casefold(),
    )


def _select_jettax_certificates(certificates) -> tuple[list[tuple[object, str, Path]], list[tuple[object, str]]]:
    """Separa itens importáveis de itens que precisam de revisão.

    O Jettax não aceita nomes duplicados nem nomes que não sejam CNPJ. Quando
    há mais de um A1 para o mesmo CNPJ, o de validade mais longa/mais recente é
    usado no ZIP principal e o outro é preservado no ZIP de revisão.
    """
    by_cnpj: dict[str, list[tuple[object, Path]]] = {}
    skipped: list[tuple[object, str]] = []

    for cert in certificates or []:
        if not getattr(cert, "opened", False):
            skipped.append((cert, getattr(cert, "error", None) or "O certificado não foi aberto com uma senha validada."))
            continue
        source = _temp_source(cert)
        if source is None:
            skipped.append((cert, "A cópia temporária do certificado não está mais disponível; rode a auditoria novamente."))
            continue
        document = _cnpj_for_jettax(cert)
        if not document:
            skipped.append((cert, _ineligible_reason(cert)))
            continue
        by_cnpj.setdefault(document, []).append((cert, source))

    selected: list[tuple[object, str, Path]] = []
    for document in sorted(by_cnpj):
        group = sorted(by_cnpj[document], key=lambda item: _newest_first_key(item[0]), reverse=True)
        winner, source = group[0]
        selected.append((winner, document, source))
        for duplicate, _duplicate_source in group[1:]:
            skipped.append((
                duplicate,
                f"CNPJ duplicado no ZIP do Jettax ({format_cnpj(document)}). "
                f"Foi selecionado o certificado mais recente: {getattr(winner, 'filename', '—')}.",
            ))
    return selected, skipped


def _make_bundle_dir(output_dir: Path) -> Path:
    base = output_dir / "exportacoes"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for index in range(1_000):
        suffix = "" if index == 0 else f"_{index + 1}"
        bundle_dir = base / f"todos_certificados_{stamp}{suffix}"
        try:
            bundle_dir.mkdir(parents=True, exist_ok=False)
            return bundle_dir
        except FileExistsError:
            continue
    raise RuntimeError("Não foi possível reservar uma pasta exclusiva para a exportação.")


def export_all_opened(certificates, output_dir: Path) -> dict:
    """Cria uma exportação local segura, sem login nem acesso ao Jettax.

    ``todos_certificados_a1.zip`` contém exclusivamente arquivos com nome
    ``CNPJ.pfx`` e deve ser o único ZIP escolhido no Jettax. Os itens abertos
    que não podem entrar nesse ZIP são preservados em
    ``certificados_para_revisao.zip`` (se existirem), junto com a explicação
    em ``nao_exportados.csv``. Esse ZIP de revisão nunca deve ser importado.
    """
    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    all_certificates = list(certificates or [])
    opened = [cert for cert in all_certificates if getattr(cert, "opened", False) and getattr(cert, "temp_path", None)]
    if not opened:
        raise RuntimeError("Nenhum certificado foi aberto com uma senha validada; não há exportação segura para gerar.")

    bundle_dir = _make_bundle_dir(output_dir)
    try:
        bundle_dir.chmod(stat.S_IRWXU)
    except OSError:
        pass

    zip_path: Path | None = None
    review_zip_path: Path | None = None
    planilha_path: Path | None = None
    password_path = bundle_dir / "certificados_e_senhas.csv"
    skipped_path = bundle_dir / "nao_exportados.csv"

    try:
        selected, skipped = _select_jettax_certificates(all_certificates)

        if selected:
            zip_path = bundle_dir / "todos_certificados_a1.zip"
            rows: list[tuple[str, str, str, str, str]] = []
            with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
                for cert, document, source in selected:
                    # O Jettax exige CNPJ puro + .pfx. Mesmo um .p12 é PKCS#12
                    # válido; apenas a extensão dentro do ZIP é padronizada.
                    name = f"{document}.pfx"
                    archive.write(source, name)
                    validity = cert.not_after.strftime("%d/%m/%Y") if getattr(cert, "not_after", None) else ""
                    rows.append((
                        name,
                        format_cnpj(document),
                        getattr(cert, "company_from_cert", None) or "",
                        "" if getattr(cert, "password", None) is None else str(cert.password),
                        validity,
                    ))

            with password_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(["ARQUIVO_NO_ZIP", "CNPJ_INTERNO", "EMPRESA_NO_CERTIFICADO", "SENHA_CERTIFICADO", "VALIDADE"])
                writer.writerows(rows)

            # A planilha e o ZIP precisam ter exatamente o mesmo conjunto de
            # CNPJs; por isso ela recebe somente os certificados selecionados.
            planilha_nota: str | None = None
            try:
                planilha_path = build_planilha_importacao_certificados(
                    [cert for cert, _document, _source in selected], bundle_dir
                )
            except RuntimeError as exc:
                planilha_nota = f"Não foi possível gerar a planilha de importação Jettax: {exc}"
        else:
            planilha_nota = (
                "Nenhum certificado aberto tinha CNPJ interno válido para o Jettax. "
                "Veja nao_exportados.csv e certificados_para_revisao.zip."
            )

        with skipped_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["ARQUIVO", "MOTIVO"])
            for cert, reason in skipped:
                writer.writerow([getattr(cert, "filename", ""), reason])

        # Mantém cópia dos PFX que a senha abriu, mas que não podem ser enviados
        # no ZIP principal sem o Jettax rejeitar a importação inteira.
        review_items = [
            (cert, reason, _temp_source(cert))
            for cert, reason in skipped
            if getattr(cert, "opened", False)
        ]
        review_items = [(cert, reason, source) for cert, reason, source in review_items if source is not None]
        if review_items:
            review_zip_path = bundle_dir / "certificados_para_revisao.zip"
            used_review_names: set[str] = set()
            with zipfile.ZipFile(review_zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
                for cert, _reason, source in review_items:
                    archive.write(source, _safe_review_filename(getattr(cert, "filename", ""), used_review_names))

        if planilha_nota:
            (bundle_dir / "planilha_nao_gerada.txt").write_text(planilha_nota + "\n", encoding="utf-8")

        cleanup_paths = [password_path, skipped_path]
        if zip_path:
            cleanup_paths.append(zip_path)
        if review_zip_path:
            cleanup_paths.append(review_zip_path)
        if planilha_path:
            cleanup_paths.append(planilha_path)
        for path in cleanup_paths:
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

        readme_lines = [
            "EXPORTAÇÃO LOCAL DE CERTIFICADOS A1",
            "===================================",
            "",
            "Esta exportação foi gerada sem abrir ou alterar o Jettax.",
            "",
        ]
        if zip_path:
            readme_lines += [
                f"- {zip_path.name}: {len(selected)} certificado(s) pronto(s) para o Jettax.",
                "  Dentro dele, cada arquivo está nomeado exatamente como CNPJ.pfx (14 dígitos).",
                "  ESTE É O ÚNICO ZIP QUE DEVE SER SELECIONADO EM Jettax > Clientes > Importar.",
                "- certificados_e_senhas.csv: senhas correspondentes (SEGREDO).",
            ]
        else:
            readme_lines += [
                "- Não foi criado ZIP para o Jettax porque nenhum certificado tinha CNPJ interno válido.",
                "  Consulte nao_exportados.csv antes de tentar importar qualquer arquivo.",
            ]
        if planilha_path:
            readme_lines += [
                "- planilha_importacao_jettax.xlsx: modelo OFICIAL do Jettax com CNPJ + SENHA.",
                "  Leve esta planilha junto com todos_certificados_a1.zip na tela Importar.",
            ]
        if review_zip_path:
            readme_lines += [
                "- certificados_para_revisao.zip: PFX abertos, mas excluídos do lote por CPF,",
                "  CNPJ interno inválido/ausente ou duplicidade. NÃO importe este ZIP no Jettax.",
            ]
        readme_lines += [
            "- nao_exportados.csv: motivo de cada item que ficou fora do ZIP do Jettax.",
            "",
            "Guarde esta pasta em local seguro e apague-a quando não precisar mais.",
            "Ela contém certificados e, quando houver ZIP importável, senhas. O Dropbox foi usado somente para leitura.",
            "",
        ]
        readme = bundle_dir / "LEIA-ME.txt"
        readme.write_text("\n".join(readme_lines), encoding="utf-8")
        try:
            readme.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return {
            "dir": bundle_dir,
            "zip": zip_path,
            "senhas": password_path if zip_path else None,
            "nao_exportados": skipped_path,
            "planilha": planilha_path,
            "revisao": review_zip_path,
            "quantidade": len(selected),
            "quantidade_revisao": len(review_items),
            "quantidade_abertos": len(opened),
        }
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise
