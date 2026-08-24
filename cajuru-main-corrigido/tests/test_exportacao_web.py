"""Exportação local de certificados A1 sem acesso ao Jettax."""

from __future__ import annotations

import csv
import zipfile

from cajuru_a1.exportacao import export_all_opened
from cajuru_a1.passwords import PasswordVault, candidate_passwords
from cajuru_a1.pfx import inspect_file

from conftest import make_pfx


def test_exporta_todos_os_certificados_abertos_sem_jettax(tmp_path):
    source = tmp_path / "origem"
    source.mkdir()
    pfx = source / "empresa.pfx"
    make_pfx(pfx, password="senha-validada", cnpj="12345678000195", company="EMPRESA TESTE")
    candidates = candidate_passwords(
        vault=PasswordVault(), empresa="EMPRESA TESTE", cnpj="12345678000195", include_empty=True
    ) + [("senha-validada", "teste")]
    cert = inspect_file(pfx, pfx, candidates)
    assert cert.opened

    export = export_all_opened([cert], tmp_path / "saida")
    assert export["quantidade"] == 1
    with zipfile.ZipFile(export["zip"]) as archive:
        assert archive.namelist() == ["empresa.pfx"]
    with export["senhas"].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
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
