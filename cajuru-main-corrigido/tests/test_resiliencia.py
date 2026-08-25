"""Regressão dos erros reais reportados no escritório.

1. Inventário não pode derrubar quando o Dropbox cria/apaga 'cópia em
   conflito' durante a varredura (FileNotFoundError no meio do os.walk).
2. O envio/leitura do Jettax não pode derrubar o lote quando a janela do
   Chrome é fechada — erro precisa ser reconhecido como 'navegador fechado'.
3. Pastas de origem com nome diferente de CERTIFICADOS são aceitas (raiz do
   disco/Dropbox continua proibida).
"""
from __future__ import annotations

import os
from pathlib import Path

from cajuru_a1 import dropbox_safe
from cajuru_a1.config import effective_config, validate_config, validate_output_path
from cajuru_a1.dropbox_safe import ReadOnlyDropbox, compare_inventories
from cajuru_a1.jettax import _is_closed_error
from cajuru_a1.pipeline import _change_touches_certificate
from cajuru_a1.audit import InventoryChange


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "CERTIFICADOS A1"
    (root / "sub").mkdir(parents=True)
    (root / "ok.pfx").write_bytes(b"certificado")
    (root / "sub" / "planilha.xlsx").write_bytes(b"planilha")
    return root


def test_inventory_pula_arquivo_que_sumiu_durante_walk(tmp_path, monkeypatch):
    """Arquivo listado pelo os.walk mas inexistente no stat: pular, não travar."""
    root = _make_root(tmp_path)
    dbx = ReadOnlyDropbox(root)
    mensagens: list[str] = []

    real_walk = os.walk

    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        for d, dirs, files in real_walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks):
            files = list(files)
            if Path(d) == root and "ok.pfx" in files:
                # 'Cópia em conflito' que o Dropbox criou e já removeu — o
                # cenário exato do FileNotFoundError reportado.
                files.append("SENHAS (Cópia em conflito de Contabilidade Cajuru 2026-08-25 1).xlsx")
            yield d, dirs, files

    monkeypatch.setattr(dropbox_safe.os, "walk", fake_walk)
    inv = dbx.inventory(progress=mensagens.append)

    assert inv["ok.pfx"]["type"] == "file"
    assert inv["sub/planilha.xlsx"]["type"] == "file"
    fantasmas = [k for k in inv if "Cópia em conflito" in k]
    assert fantasmas == []
    assert any("pulada" in m for m in mensagens)


def test_inventory_pula_pasta_que_sumiu(tmp_path, monkeypatch):
    root = _make_root(tmp_path)
    dbx = ReadOnlyDropbox(root)

    real_walk = os.walk

    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        first = True
        for d, dirs, files in real_walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks):
            dirs = list(dirs)
            if first and Path(d) == root:
                dirs.append("PASTA FANTASMA")  # não existe no disco
                first = False
            yield d, dirs, files

    monkeypatch.setattr(dropbox_safe.os, "walk", fake_walk)
    inv = dbx.inventory()
    assert all("FANTASMA" not in k for k in inv)
    assert "sub/planilha.xlsx" in inv  # resto da árvore continua normal


def test_inventory_pula_arquivo_ilegivel(tmp_path, monkeypatch):
    """Arquivo 'somente online'/bloqueado não pode abortar o inventário."""
    root = _make_root(tmp_path)
    (root / "online.xlsx").write_bytes(b"x")
    dbx = ReadOnlyDropbox(root)
    real_sha = dropbox_safe.sha256_file

    def sha_seletivo(path, *args, **kwargs):
        if Path(path).name == "online.xlsx":
            raise OSError("arquivo somente online no Dropbox")
        return real_sha(path, *args, **kwargs)

    monkeypatch.setattr(dropbox_safe, "sha256_file", sha_seletivo)
    inv = dbx.inventory()
    assert "online.xlsx" not in inv
    assert "ok.pfx" in inv
    assert "sub/planilha.xlsx" in inv


def test_list_files_ignora_arquivo_que_desapareceu(tmp_path, monkeypatch):
    root = _make_root(tmp_path)
    dbx = ReadOnlyDropbox(root)

    real_guard = dbx._guard_source
    alvo = root / "ok.pfx"

    def guard_quebrado(path):
        if Path(path) == alvo:
            raise FileNotFoundError("sumiu")
        return real_guard(path)

    monkeypatch.setattr(dbx, "_guard_source", guard_quebrado)
    resultado = dbx.list_certificates()
    assert resultado == []
    assert dbx.rejected_files == []  # sumiu ≠ rejeitado por segurança


def test_is_closed_error_detecta_target_closed():
    class TargetClosedError(Exception):
        pass

    assert _is_closed_error(
        TargetClosedError("Page.goto: Target page, context or browser has been closed")
    )
    assert _is_closed_error(Exception("Target page, context or browser has been closed"))
    assert _is_closed_error(Exception("Browser has been closed"))
    assert not _is_closed_error(TimeoutError("Timeout 90000ms exceeded"))
    assert not _is_closed_error(RuntimeError("outro problema"))


def test_goto_safe_reabre_navegador_fechado():
    """Cenário exato do log: primeiro goto morre com TargetClosedError.

    O robô deve reabrir o navegador sozinho e navegar de novo, em vez de
    derrubar o lote inteiro.
    """
    from cajuru_a1.jettax import JettaxBot

    bot = JettaxBot({"jettax": {"url": "https://admin.jettax360.com.br"}}, log_fn=lambda m: None)
    eventos: list[str] = []

    class FakePage:
        def __init__(self, falha_inicial: bool):
            self._falha = falha_inicial

        def goto(self, url, wait_until=None, timeout=None):
            eventos.append(f"goto:{wait_until}")
            if self._falha:
                raise RuntimeError(
                    "Page.goto: Target page, context or browser has been closed"
                )

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]

    primeira = FakePage(falha_inicial=True)
    bot.context = FakeContext(primeira)
    bot.page = primeira

    def fake_restart(motivo):
        eventos.append(f"restart:{motivo}")
        nova = FakePage(falha_inicial=False)
        bot.context = FakeContext(nova)
        bot.page = nova
        return True

    bot._restart_browser = fake_restart
    bot._browser_alive = lambda: True

    bot._goto_safe("https://admin.jettax360.com.br/")

    assert eventos[0] == "goto:domcontentloaded"
    assert eventos[1].startswith("restart:")
    assert "goto:domcontentloaded" in eventos[2]
    assert sum(1 for e in eventos if e.startswith("restart:")) == 1


def test_goto_safe_erro_amigavel_quando_nao_reabre():
    from cajuru_a1.jettax import JettaxBot

    bot = JettaxBot({"jettax": {"url": "https://admin.jettax360.com.br"}}, log_fn=lambda m: None)

    class FakePage:
        def goto(self, url, wait_until=None, timeout=None):
            raise RuntimeError("Page.goto: Target page, context or browser has been closed")

    bot.context = type("C", (), {"pages": []})()
    bot.page = FakePage()
    bot._browser_alive = lambda: True
    bot._restart_browser = lambda motivo: False  # não consegue reabrir

    try:
        bot._goto_safe("https://admin.jettax360.com.br/")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "Não feche essa janela" in str(exc) or "não foi possível reabrí-la" in str(exc)
    assert raised


def test_pasta_origem_com_outro_nome_e_aceita(tmp_path):
    """O usuário pode trocar a pasta de origem para um nome qualquer.

    Só a raiz do disco e a raiz do Dropbox continuam proibidas.
    """
    pasta = tmp_path / "A1 Atualizados 2026"
    pasta.mkdir()
    x1 = tmp_path / "senhas1.xlsx"
    x2 = tmp_path / "senhas2.xlsx"
    for x in (x1, x2):
        x.write_bytes(b"xlsx")
    cfg = effective_config({
        "dropbox": {"pasta": str(pasta)},
        "excel": {"arquivos": [str(x1), str(x2)]},
        "jettax": {"url": "https://admin.jettax360.com.br"},
    })
    erros = [e for e in validate_config(cfg) if "raiz do disco" in e or "raiz do Dropbox" in e]
    assert erros == []

    raiz_dropbox = tmp_path / "Dropbox (Pessoal)"
    raiz_dropbox.mkdir()
    cfg_ruim = effective_config({
        "dropbox": {"pasta": str(raiz_dropbox)},
        "excel": {"arquivos": [str(x1), str(x2)]},
        "jettax": {"url": "https://admin.jettax360.com.br"},
    })
    erros_ruim = [e for e in validate_config(cfg_ruim) if "raiz do Dropbox" in e]
    assert erros_ruim, "raiz do Dropbox deve continuar proibida"


def test_validate_output_path_troca_pasta_e_recusa_dropbox(tmp_path):
    destino = tmp_path / "saida"
    destino.mkdir()
    cfg = effective_config({"armazenamento": {"saida": str(destino)}})
    assert validate_output_path(cfg, str(destino)) == destino.resolve()

    dentro_dropbox = tmp_path / "Dropbox" / "output"
    dentro_dropbox.mkdir(parents=True)
    try:
        validate_output_path(cfg, str(dentro_dropbox))
        raised = False
    except ValueError:
        raised = True
    assert raised, "pasta de saída dentro do Dropbox deve ser recusada"


def test_mudanca_em_certificado_bloqueia_mas_planilha_nao():
    cert = InventoryChange("deleted", "empresa.pfx")
    planilha = InventoryChange("created", "SENHAS (Cópia em conflito).xlsx")
    movido = InventoryChange("moved", "a.xlsx", "b.pfx")
    assert _change_touches_certificate(cert)
    assert _change_touches_certificate(movido)  # destino do movimento é PFX
    assert not _change_touches_certificate(planilha)


def test_compare_inventories_sem_falsa_mudanca_para_pulados():
    """Mesma pasta inventariada duas vezes com o mesmo pulado = zero mudanças."""
    inv = {
        "a.pfx": {"type": "file", "size": 3, "mtime_ns": 1, "mode": 420, "sha256": "x"},
        "sub/": {"type": "directory", "mode": 493},
    }
    assert compare_inventories(inv, dict(inv)) == []
