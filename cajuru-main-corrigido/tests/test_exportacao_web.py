"""Exportação local de certificados A1 sem acesso ao Jettax.

Estes testes travam o contrato exigido pelo importador do Jettax:
* cada arquivo do ZIP se chama exatamente ``<14 dígitos do CNPJ>.pfx``
  (senão: "Nome do arquivo não é um CNPJ válido");
* a planilha tem UMA linha por CNPJ (senão: "CNPJ duplicado na planilha").
"""

from __future__ import annotations

import csv
import zipfile

import pytest

from cajuru_a1.exportacao import export_all_opened, selecionar_para_jettax
from cajuru_a1.passwords import PasswordVault, candidate_passwords
from cajuru_a1.pfx import inspect_file

from conftest import make_pfx


def _abrir(path, senha, cnpj="12345678000195", empresa="EMPRESA TESTE"):
    candidates = candidate_passwords(
        vault=PasswordVault(), empresa=empresa, cnpj=cnpj, include_empty=True
    ) + [(senha, "teste")]
    cert = inspect_file(path, path, candidates)
    assert cert.opened
    return cert


def test_exporta_todos_os_certificados_abertos_sem_jettax(tmp_path):
    source = tmp_path / "origem"
    source.mkdir()
    pfx = source / "empresa.pfx"
    make_pfx(pfx, password="senha-validada", cnpj="12345678000195", company="EMPRESA TESTE")
    cert = _abrir(pfx, "senha-validada")

    export = export_all_opened([cert], tmp_path / "saida")
    assert export["quantidade"] == 1
    # O Jettax recusa qualquer nome que não seja o CNPJ puro.
    with zipfile.ZipFile(export["zip"]) as archive:
        assert archive.namelist() == ["12345678000195.pfx"]
    with export["senhas"].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    assert rows[0][:2] == ["ARQUIVO_NO_ZIP", "CNPJ"]
    assert rows[1][0] == "12345678000195.pfx"
    assert rows[1][3] == "senha-validada"

    # A exportação também gera a planilha modelo OFICIAL do Jettax (aba
    # "Certificados") com CNPJ + SENHA preenchidos, no formato exato do
    # modelo_import_certificados.xlsx.
    assert export.get("planilha") and export["planilha"].is_file()
    import openpyxl
    workbook = openpyxl.load_workbook(export["planilha"])
    assert workbook.sheetnames == ["Leia-me", "Certificados", "Regimes"]
    sheet = workbook["Certificados"]
    assert sheet.cell(1, 1).value == "CNPJ *"
    assert sheet.cell(1, 2).value == "Senha Certificado *"
    assert sheet.cell(2, 1).value == "12.345.678/0001-95"
    assert sheet.cell(2, 2).value == "senha-validada"
    # Demais colunas do modelo permanecem em branco (sem o exemplo residual).
    assert all(sheet.cell(2, col).value in (None, "") for col in range(3, sheet.max_column + 1))
    workbook.close()


def test_zip_usa_cnpj_mesmo_com_nome_humano_e_extensao_p12(tmp_path):
    """Nome legível e .p12 viram <CNPJ>.pfx — a causa do erro do importador."""
    source = tmp_path / "origem"
    source.mkdir()
    pfx = source / "WM GESTAO EMPRESARIAL LTDA_68620019000174.p12"
    make_pfx(pfx, password="pw", cnpj="12345678000195", company="WM GESTAO")
    cert = _abrir(pfx, "pw")

    export = export_all_opened([cert], tmp_path / "saida")
    with zipfile.ZipFile(export["zip"]) as archive:
        nomes = archive.namelist()
    assert nomes == ["12345678000195.pfx"]
    for nome in nomes:
        stem = nome[:-4]
        assert stem.isdigit() and len(stem) == 14


def test_mesmo_cnpj_em_arquivos_diferentes_gera_uma_unica_linha(tmp_path):
    """Duplicata de CNPJ: vence o mais novo, o outro vai para nao_exportados."""
    source = tmp_path / "origem"
    source.mkdir()
    antigo = source / "ACME (1).pfx"
    novo = source / "ACME - Copia.pfx"
    make_pfx(antigo, password="pw", cnpj="12345678000195", company="ACME", days_valid=60)
    make_pfx(novo, password="pw", cnpj="12345678000195", company="ACME", days_valid=700)
    certs = [_abrir(antigo, "pw"), _abrir(novo, "pw")]

    elegiveis, excluidos = selecionar_para_jettax(certs)
    assert [doc for doc, _ in elegiveis] == ["12345678000195"]
    assert elegiveis[0][1].filename == "ACME - Copia.pfx"  # validade mais distante
    assert len(excluidos) == 1
    assert "duplicado" in excluidos[0][1]

    export = export_all_opened(certs, tmp_path / "saida")
    assert export["quantidade"] == 1
    with zipfile.ZipFile(export["zip"]) as archive:
        assert archive.namelist() == ["12345678000195.pfx"]

    import openpyxl
    workbook = openpyxl.load_workbook(export["planilha"])
    sheet = workbook["Certificados"]
    cnpjs = [sheet.cell(row, 1).value for row in range(2, sheet.max_row + 1) if sheet.cell(row, 1).value]
    assert cnpjs == ["12.345.678/0001-95"]  # sem repetição
    workbook.close()

    with export["nao_exportados"].open(encoding="utf-8-sig", newline="") as handle:
        linhas = list(csv.reader(handle, delimiter=";"))
    assert linhas[0] == ["ARQUIVO", "CNPJ_CPF_DETECTADO", "MOTIVO"]
    assert any("ACME (1).pfx" == linha[0] for linha in linhas[1:])


def test_filiais_diferentes_do_mesmo_grupo_nao_sao_agrupadas(tmp_path):
    """CNPJs que só diferem na filial são clientes distintos: os dois entram."""
    source = tmp_path / "origem"
    source.mkdir()
    matriz = source / "GRUPO MATRIZ.pfx"
    filial = source / "GRUPO FILIAL.pfx"
    make_pfx(matriz, password="pw", cnpj="21260898000107", company="GRUPO")
    make_pfx(filial, password="pw", cnpj="21260898000379", company="GRUPO")
    certs = [
        _abrir(matriz, "pw", cnpj="21260898000107"),
        _abrir(filial, "pw", cnpj="21260898000379"),
    ]

    export = export_all_opened(certs, tmp_path / "saida")
    assert export["quantidade"] == 2
    with zipfile.ZipFile(export["zip"]) as archive:
        assert sorted(archive.namelist()) == ["21260898000107.pfx", "21260898000379.pfx"]


def test_certificado_de_cpf_fica_fora_do_lote_jettax(tmp_path):
    """CPF não é aceito pelo importador; sai do ZIP e vira linha com motivo."""
    source = tmp_path / "origem"
    source.mkdir()
    empresa = source / "empresa.pfx"
    pessoa = source / "PAULA KARINE FERREIRA FONSECA_10961162600.pfx"
    make_pfx(empresa, password="pw", cnpj="12345678000195", company="EMPRESA")
    make_pfx(pessoa, password="pw", cnpj=None, company="PAULA KARINE")
    certs = [_abrir(empresa, "pw"), _abrir(pessoa, "pw", cnpj="10961162600", empresa="PAULA")]

    export = export_all_opened(certs, tmp_path / "saida")
    assert export["quantidade"] == 1
    with zipfile.ZipFile(export["zip"]) as archive:
        assert archive.namelist() == ["12345678000195.pfx"]
    # O arquivo excluído continua disponível localmente, com o nome original.
    assert export["outros_zip"] and export["outros_zip"].is_file()
    with zipfile.ZipFile(export["outros_zip"]) as archive:
        assert archive.namelist() == ["PAULA KARINE FERREIRA FONSECA_10961162600.pfx"]
    with export["nao_exportados"].open(encoding="utf-8-sig", newline="") as handle:
        motivos = "\n".join(";".join(linha) for linha in csv.reader(handle, delimiter=";"))
    assert "PAULA KARINE" in motivos


def test_certificado_sem_documento_legivel_fica_fora(tmp_path):
    source = tmp_path / "origem"
    source.mkdir()
    empresa = source / "empresa.pfx"
    anonimo = source / "PAEX.pfx"
    make_pfx(empresa, password="pw", cnpj="12345678000195", company="EMPRESA")
    make_pfx(anonimo, password="pw", cnpj=None, company="PAEX")
    certs = [_abrir(empresa, "pw"), _abrir(anonimo, "pw", cnpj="", empresa="PAEX")]

    export = export_all_opened(certs, tmp_path / "saida")
    assert export["quantidade"] == 1
    assert export["excluidos"] == 1
    with zipfile.ZipFile(export["zip"]) as archive:
        assert archive.namelist() == ["12345678000195.pfx"]


def test_exportacao_falha_quando_nenhum_cnpj_e_valido(tmp_path):
    source = tmp_path / "origem"
    source.mkdir()
    anonimo = source / "MG PAPEIS.pfx"
    make_pfx(anonimo, password="pw", cnpj=None, company="MG PAPEIS")
    cert = _abrir(anonimo, "pw", cnpj="", empresa="MG PAPEIS")

    with pytest.raises(RuntimeError, match="CNPJ"):
        export_all_opened([cert], tmp_path / "saida")
