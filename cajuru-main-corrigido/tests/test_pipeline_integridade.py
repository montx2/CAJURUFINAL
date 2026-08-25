"""Teste de ponta a ponta: mudança em planilha durante a análise NÃO derruba mais.

Cenário real do escritório: o Dropbox cria 'cópia em conflito' da planilha de
senhas dentro da pasta CERTIFICADOS A1 enquanto a análise roda. Antes, isso
estourava RuntimeError e jogava fora a análise inteira; agora a análise
conclui com alerta, ``safety_ok=False`` (envio bloqueado até nova análise) e
as mudanças registradas em ``integrity_changes``.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from cajuru_a1.audit import InventoryChange
from cajuru_a1.config import effective_config
from cajuru_a1.dropbox_safe import ReadOnlyDropbox
from cajuru_a1.pipeline import analyze
from tests.conftest import make_pfx


def _xlsx(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "ECNPJ"
    ws.append(["CNPJ", "EMPRESA", "SENHA"])
    ws.append(["12.345.678/0001-95", "EMPRESA TESTE LTDA", "123456"])
    wb.save(path)
    return path


def test_analyze_tolera_mudanca_somente_em_planilha(tmp_path, monkeypatch):
    root = tmp_path / "CERTIFICADOS A1"
    root.mkdir()
    make_pfx(root / "12345678000195 EMPRESA TESTE.pfx", password="123456")
    x1 = _xlsx(tmp_path / "senhas1.xlsx")
    x2 = _xlsx(tmp_path / "senhas2.xlsx")
    out = tmp_path / "output"
    state = tmp_path / "state"
    cfg = effective_config({
        "dropbox": {"pasta": str(root)},
        "excel": {"arquivos": [str(x1), str(x2)]},
        "armazenamento": {"saida": str(out), "estado": str(state)},
        "pdf": {"habilitado": False},
    })

    def verify_fake(self, expected, progress=None, max_files=5000):
        return [InventoryChange("created", "SENHAS (Cópia em conflito de Contabilidade Cajuru 2026-08-25 1).xlsx")]

    monkeypatch.setattr(ReadOnlyDropbox, "verify_inventory", verify_fake)

    mensagens: list[str] = []
    result = analyze(cfg, log_fn=mensagens.append)

    assert result.certificados and result.certificados[0].opened
    assert result.safety_ok is False
    assert result.integrity_changes, "a mudança precisa ficar registrada"
    assert any("AVISO" in m for m in mensagens)
    assert (out / "auditoria_ultima_execucao.json").is_file()
    assert (out / "diagnostico.html").is_file()


def test_analyze_bloqueia_mudanca_em_certificado(tmp_path, monkeypatch):
    root = tmp_path / "CERTIFICADOS A1"
    root.mkdir()
    make_pfx(root / "12345678000195 EMPRESA TESTE.pfx", password="123456")
    x1 = _xlsx(tmp_path / "senhas1.xlsx")
    x2 = _xlsx(tmp_path / "senhas2.xlsx")
    cfg = effective_config({
        "dropbox": {"pasta": str(root)},
        "excel": {"arquivos": [str(x1), str(x2)]},
        "armazenamento": {"saida": str(tmp_path / "output"), "estado": str(tmp_path / "state")},
        "pdf": {"habilitado": False},
    })

    def verify_fake(self, expected, progress=None, max_files=5000):
        return [InventoryChange("deleted", "12345678000195 EMPRESA TESTE.pfx")]

    monkeypatch.setattr(ReadOnlyDropbox, "verify_inventory", verify_fake)

    mensagens: list[str] = []
    try:
        analyze(cfg, log_fn=mensagens.append)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "certificados foram afetados" in str(exc)
    assert raised, "mudança em .pfx/.p12 precisa bloquear a análise"
