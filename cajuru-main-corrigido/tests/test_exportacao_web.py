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


def test_rota_cancelamento_de_job(tmp_path):
    from cajuru_a1.webapp import JOBS, create_app

    cfg = tmp_path / "config.yaml"
    cfg.write_text("dropbox:\n  pasta: ''\nexcel:\n  arquivos: []\n", encoding="utf-8")
    app = create_app(cfg)
    job = JOBS.create("teste")
    response = app.test_client().post(f"/api/job/{job.id}/cancelar")
    assert response.status_code == 200
    assert response.get_json()["job"]["cancel_requested"] is True
