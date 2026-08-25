"""Exportação local de todos os certificados A1 já abertos com sucesso.

Esta operação não acessa o Jettax. Ela cria:

* ``certificados_jettax.zip`` — só os certificados que o importador do Jettax
  aceita, com cada arquivo nomeado EXATAMENTE ``<14 dígitos do CNPJ>.pfx``
  (o Jettax rejeita qualquer outro nome com "Nome do arquivo não é um CNPJ
  válido");
* ``planilha_importacao_jettax.xlsx`` — modelo OFICIAL do Jettax
  (``modelo_import_certificados.xlsx``) com UMA linha por CNPJ (CNPJ repetido
  invalida as duas linhas: "CNPJ duplicado na planilha");
* ``certificados_e_senhas.csv`` — senhas validadas (segredo, uso local);
* ``nao_exportados.csv`` — tudo que ficou de fora, com o motivo;
* ``outros_certificados.zip`` — os arquivos não importáveis (CPF, sem CNPJ
  legível ou versões substituídas), guardados com o nome original.

Quando dois arquivos diferentes carregam o MESMO CNPJ, apenas o mais atual
(mesma política do conciliador: ``pfx.pick_newest``) entra no lote; os demais
viram linha no ``nao_exportados.csv``. Tudo dentro da pasta de saída local
protegida do Cajuru A1 — o Dropbox continua somente-leitura.
"""
from __future__ import annotations

import csv
import shutil
import stat
import zipfile
from datetime import datetime
from pathlib import Path

from cajuru_a1.cnpjutil import format_cnpj, is_valid_cnpj, is_valid_cpf, only_digits, pad_cnpj
from cajuru_a1.lote import build_planilha_importacao_certificados
from cajuru_a1.pfx import pick_newest

# Motivos padronizados para o relatório de exclusão (nunca contêm senha).
MOTIVO_SEM_DOCUMENTO = (
    "Sem CNPJ legível no certificado nem no nome do arquivo — o Jettax exige "
    "que o arquivo se chame <CNPJ>.pfx"
)
MOTIVO_CPF = "Certificado de pessoa física (CPF) — o importador do Jettax só aceita CNPJ"
MOTIVO_CNPJ_INVALIDO = "CNPJ inválido (dígito verificador não confere)"
MOTIVO_NAO_ABRIU = "Não foi possível abrir com uma senha validada"
MOTIVO_SEM_COPIA = "A cópia temporária do certificado não está mais disponível — rode a auditoria novamente"


def _safe_filename(name: str, used: set[str]) -> str:
    """Preserva um nome legível, sem permitir caminho ou sobrescrita no ZIP.

    Usado apenas no ZIP de arquivos NÃO importáveis (``outros_certificados.zip``).
    O ZIP que vai para o Jettax nunca usa nome legível — veja ``_zip_name``.
    """
    base = Path(name or "certificado").name.replace("\x00", "_")
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


def _zip_name(document: str) -> str:
    """Nome exigido pelo importador do Jettax: só os 14 dígitos do CNPJ + .pfx.

    O ``.p12`` é o mesmo formato binário do ``.pfx``; renomear é seguro e o
    Jettax só documenta a extensão ``.pfx``.
    """
    return f"{only_digits(document)}.pfx"


def _documento(cert) -> str:
    """Documento do certificado: identidade interna primeiro, nome depois."""
    raw = getattr(cert, "cnpj", None)
    if not raw:
        raw = getattr(cert, "cnpj_cert", None) or getattr(cert, "cnpj_filename", None)
    return pad_cnpj(only_digits(raw or ""))


def _validade(cert) -> str:
    not_after = getattr(cert, "not_after", None)
    return not_after.strftime("%d/%m/%Y") if not_after else ""


def selecionar_para_jettax(certificates) -> tuple[list[tuple[str, object]], list[tuple[object, str]]]:
    """Separa os certificados abertos entre "vai para o Jettax" e "fica de fora".

    Devolve ``(elegiveis, excluidos)`` onde ``elegiveis`` é uma lista de
    ``(cnpj_14_digitos, cert)`` com UM único registro por CNPJ (o mais atual)
    e ``excluidos`` é uma lista de ``(cert, motivo)``.

    Regras (as mesmas que o importador do Jettax cobra):
    1. só CNPJ — CPF e arquivos sem documento legível ficam de fora;
    2. CNPJ tem que passar no dígito verificador;
    3. um CNPJ só pode aparecer uma vez: entre arquivos repetidos vence o
       certificado mais novo (``pfx.pick_newest``), igual ao conciliador.
    """
    excluidos: list[tuple[object, str]] = []
    por_documento: dict[str, list] = {}

    for cert in certificates or []:
        if not getattr(cert, "opened", False):
            continue
        temp_path = getattr(cert, "temp_path", None)
        if not temp_path or not Path(temp_path).is_file():
            excluidos.append((cert, MOTIVO_SEM_COPIA))
            continue
        documento = _documento(cert)
        if not documento:
            excluidos.append((cert, MOTIVO_SEM_DOCUMENTO))
            continue
        if len(documento) == 11 or is_valid_cpf(documento):
            excluidos.append((cert, f"{MOTIVO_CPF} ({format_cnpj(documento)})"))
            continue
        if not is_valid_cnpj(documento):
            excluidos.append((cert, f"{MOTIVO_CNPJ_INVALIDO}: {documento}"))
            continue
        por_documento.setdefault(documento, []).append(cert)

    elegiveis: list[tuple[str, object]] = []
    for documento in sorted(por_documento):
        candidatos = por_documento[documento]
        if len(candidatos) == 1:
            elegiveis.append((documento, candidatos[0]))
            continue
        escolhido, substituidos = pick_newest(candidatos)
        if escolhido is None:
            # Nenhum candidato confiável: não arrisca mandar o arquivo errado.
            for cert in candidatos:
                excluidos.append((
                    cert,
                    f"CNPJ {format_cnpj(documento)} repetido em {len(candidatos)} arquivos e não foi possível "
                    "eleger o mais atual — revise manualmente",
                ))
            continue
        elegiveis.append((documento, escolhido))
        vencedor = getattr(escolhido, "filename", "") or _zip_name(documento)
        validade = _validade(escolhido)
        for cert in substituidos:
            excluidos.append((
                cert,
                f"CNPJ {format_cnpj(documento)} duplicado — enviado o mais atual "
                f"({vencedor}{f', validade {validade}' if validade else ''})",
            ))
    return elegiveis, excluidos


def export_all_opened(certificates, output_dir: Path, *, senha_manual: bool = False) -> dict:
    """Cria uma exportação baixável pronta para o Jettax > Clientes > Importar.

    Não exige conciliação nem login no Jettax. Arquivos que não abriram, que
    são de CPF, que não têm CNPJ legível ou que são versões antigas de um
    mesmo CNPJ são listados em ``nao_exportados.csv`` (com o motivo, sem
    revelar candidatos de senha) e guardados em ``outros_certificados.zip``.
    """
    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = output_dir / "exportacoes" / f"todos_certificados_{stamp}"
    bundle_dir.mkdir(parents=True, exist_ok=False)
    try:
        bundle_dir.chmod(stat.S_IRWXU)
    except OSError:
        pass

    abertos = [c for c in (certificates or []) if getattr(c, "opened", False) and getattr(c, "temp_path", None)]
    if not abertos:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise RuntimeError("Nenhum certificado foi aberto com uma senha validada; não há exportação segura para gerar.")

    elegiveis, excluidos = selecionar_para_jettax(certificates)
    nao_abertos = [
        (c, getattr(c, "error", None) or MOTIVO_NAO_ABRIU)
        for c in (certificates or [])
        if not getattr(c, "opened", False)
    ]

    zip_path = bundle_dir / "certificados_jettax.zip"
    outros_zip_path = bundle_dir / "outros_certificados.zip"
    password_path = bundle_dir / "certificados_e_senhas.csv"
    skipped_path = bundle_dir / "nao_exportados.csv"
    planilha_path: Path | None = None
    rows: list[tuple] = []

    try:
        if not elegiveis:
            raise RuntimeError(
                "Nenhum certificado aberto tem CNPJ válido para importar no Jettax "
                f"({len(excluidos)} arquivo(s) ficaram de fora — veja nao_exportados.csv)."
            )

        # ZIP oficial: um arquivo por CNPJ, nomeado <14 dígitos>.pfx.
        with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
            for documento, cert in elegiveis:
                nome = _zip_name(documento)
                archive.write(Path(cert.temp_path), nome)
                rows.append((
                    nome,
                    format_cnpj(documento),
                    getattr(cert, "company_from_cert", "") or "",
                    "" if getattr(cert, "password", None) is None else str(cert.password),
                    _validade(cert),
                    getattr(cert, "filename", "") or "",
                ))

        # ZIP auxiliar: o que não pode ir para o Jettax, com o nome original.
        excluidos_com_arquivo = [
            (cert, motivo)
            for cert, motivo in excluidos
            if getattr(cert, "temp_path", None) and Path(cert.temp_path).is_file()
        ]
        if excluidos_com_arquivo:
            usados: set[str] = set()
            with zipfile.ZipFile(outros_zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
                for cert, _motivo in excluidos_com_arquivo:
                    archive.write(Path(cert.temp_path), _safe_filename(getattr(cert, "filename", ""), usados))
        else:
            outros_zip_path = None  # type: ignore[assignment]

        with password_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow([
                "ARQUIVO_NO_ZIP", "CNPJ", "EMPRESA_NO_CERTIFICADO", "SENHA_CERTIFICADO", "VALIDADE", "ARQUIVO_ORIGEM",
            ])
            writer.writerows(rows)

        with skipped_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["ARQUIVO", "CNPJ_CPF_DETECTADO", "MOTIVO"])
            for cert, motivo in nao_abertos + excluidos:
                documento = _documento(cert)
                writer.writerow([
                    getattr(cert, "filename", ""),
                    format_cnpj(documento) if documento else "",
                    motivo,
                ])

        # Planilha modelo OFICIAL do Jettax (modelo_import_certificados.xlsx)
        # com UMA linha por CNPJ, na mesma ordem do ZIP. É o arquivo que a
        # pessoa leva ao Jettax > Clientes > Importar.
        planilha_nota: str | None = None
        try:
            planilha_path = build_planilha_importacao_certificados(
                [cert for _doc, cert in elegiveis], bundle_dir, senha_manual=senha_manual
            )
        except RuntimeError as exc:
            # Sem certificado com CNPJ válido não há planilha a criar; não é
            # fatal para a exportação (o ZIP/CSV existem), apenas registramos.
            planilha_nota = f"Não foi possível gerar a planilha de importação Jettax: {exc}"

        cleanup_paths = [zip_path, password_path, skipped_path, outros_zip_path, planilha_path]
        for path in cleanup_paths:
            if not path:
                continue
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
            f"Certificados prontos para importar: {len(rows)}",
            f"Arquivos fora do lote (veja o motivo): {len(nao_abertos) + len(excluidos)}",
            "",
            "LEVE PARA O JETTAX (Clientes > Importar):",
            "- certificados_jettax.zip: um .pfx por CNPJ, já nomeado <CNPJ>.pfx",
        ]
        if planilha_path:
            readme_lines.append("- planilha_importacao_jettax.xlsx: modelo OFICIAL do Jettax (CNPJ + SENHA),")
            readme_lines.append("  uma única linha por CNPJ")
        readme_lines += [
            "",
            "SÓ PARA CONSULTA LOCAL (não envie ao Jettax):",
            "- certificados_e_senhas.csv: senhas correspondentes (SEGREDO)",
            "- nao_exportados.csv: o que ficou de fora e por quê",
        ]
        if outros_zip_path:
            readme_lines.append("- outros_certificados.zip: CPF, sem CNPJ legível ou versão substituída")
        readme_lines += [
            "",
            "Por que alguns arquivos ficam de fora: o Jettax recusa nome de arquivo",
            "que não seja o CNPJ ('Nome do arquivo não é um CNPJ válido') e recusa a",
            "planilha inteira quando um CNPJ aparece duas vezes ('CNPJ duplicado na",
            "planilha'). Por isso o lote traz um único certificado — o mais atual —",
            "para cada CNPJ, e nenhum certificado de CPF.",
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
        return {
            "dir": bundle_dir,
            "zip": zip_path,
            "outros_zip": outros_zip_path,
            "senhas": password_path,
            "nao_exportados": skipped_path,
            "planilha": planilha_path,
            "quantidade": len(rows),
            "excluidos": len(nao_abertos) + len(excluidos),
        }
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise
