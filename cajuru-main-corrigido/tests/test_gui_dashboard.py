"""Monta a janela inteira do Cajuru A1 com tkinter falso.

Não existe display nem Tcl/Tk nestes testes: ``tests/gui_stubs`` substitui
``tkinter`` e ``customtkinter``. Isso pega justamente os erros que só
apareceriam ao abrir o programa — nome de atributo trocado, argumento que
não existe, callback quebrado — sem precisar de tela.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

STUBS = Path(__file__).resolve().parent / "gui_stubs"


@pytest.fixture()
def gui(monkeypatch, tmp_path):
    """Importa ``cajuru_a1.gui`` com as bibliotecas de tela falsificadas."""
    monkeypatch.syspath_prepend(str(STUBS))
    for name in [m for m in list(sys.modules) if m.split(".")[0] in {"tkinter", "customtkinter"}]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "cajuru_a1.gui", raising=False)

    import tests.gui_stubs.customtkinter as ctk_stub
    import tests.gui_stubs.tkinter as tk_stub

    monkeypatch.setitem(sys.modules, "customtkinter", ctk_stub)
    monkeypatch.setitem(sys.modules, "tkinter", tk_stub)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", tk_stub.ttk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk_stub.messagebox)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", tk_stub.filedialog)

    module = importlib.import_module("cajuru_a1.gui")
    module = importlib.reload(module)
    tk_stub.messagebox.calls.clear()

    saida = tmp_path / "saida"
    saida.mkdir()
    monkeypatch.setattr(module, "load_config", lambda *a, **kw: {
        "dropbox": {"pasta": str(tmp_path / "certs")},
        "saida": {"pasta": str(saida)},
        "opcoes": {},
    })
    monkeypatch.setattr(module, "get_output_dir", lambda cfg: saida)
    module._saida = saida
    module._messagebox = tk_stub.messagebox
    return module


@pytest.fixture()
def app(gui):
    instancia = gui.App()
    yield instancia


# --------------------------------------------------------------- Abertura

def test_janela_abre_sem_erro_e_mostra_o_dashboard(app):
    assert app._current_view == "dashboard"
    assert set(app._views) == {"dashboard", "certificados", "lotes", "relatorios", "config", "log"}


def test_todas_as_abas_da_lateral_abrem(app):
    for key in ("certificados", "lotes", "relatorios", "config", "log", "dashboard"):
        app._show_view(key)
        assert app._current_view == key


def test_dashboard_vazio_mostra_os_seis_kpis_zerados(app):
    assert len(app.kpi_cards) == 6
    for label in app.kpi_cards.values():
        assert label.cget("text") in {"—", "0"}


# -------------------------------------------------------------- Prontidão

def _cert(tmp_path, cnpj, nome, *, opened=True, mtime=1.0, sha="a" * 64):
    caminho = tmp_path / nome
    caminho.write_bytes(b"x")
    return SimpleNamespace(
        cnpj=cnpj, cnpj_cert=cnpj, cnpj_filename=cnpj, filename=nome,
        source_path=str(caminho), temp_path=str(caminho), opened=opened,
        identity_conflict=False, not_after=None, not_before=None,
        source_mtime=mtime, size=1, sha256=sha, extra={},
    )


def test_painel_reflete_lote_limpo(app, tmp_path):
    certs = [
        _cert(tmp_path, "12345678000195", "a.pfx"),
        _cert(tmp_path, "21260898000379", "b.pfx", sha="b" * 64),
    ]
    app.result = SimpleNamespace(
        stats={"pfx": 2, "pfx_abertos": 2, "pronto": 2}, certificados=certs, matches=[],
    )
    app._update_dashboard_stats()

    assert app.hero_ready.cget("text") == "2"
    assert app.hero_blocked.cget("text") == ""
    assert "Jettax" in app.hero_title.cget("text")
    assert app.kpi_cards["pronto"].cget("text") == "2"


def test_painel_lista_os_motivos_de_bloqueio(app, tmp_path):
    certs = [
        _cert(tmp_path, "12345678000195", "ok.pfx"),
        _cert(tmp_path, "12345678000195", "ok (1).pfx", mtime=9.0, sha="b" * 64),
        _cert(tmp_path, "10961162600", "cpf.pfx", sha="c" * 64),
        _cert(tmp_path, "", "PAEX.pfx", sha="d" * 64),
    ]
    app.result = SimpleNamespace(stats={"pfx": 4, "pfx_abertos": 4}, certificados=certs, matches=[])
    app._update_dashboard_stats()

    assert app.hero_ready.cget("text") == "1"
    assert "3 fora do lote" in app.hero_blocked.cget("text")
    textos = " | ".join(app.hero_reasons.texts())
    assert "duplicado" in textos
    assert "CPF" in textos
    assert "Sem CNPJ" in textos


def test_barra_de_saude_desenha_um_bloco_por_status(app):
    app.result = SimpleNamespace(
        stats={"pronto": 3, "sem_senha": 1, "vencido": 1}, certificados=[], matches=[],
    )
    app._update_dashboard_stats()
    assert len(app._health_segments_cache) == 3
    app._draw_health()
    assert len(app.health_canvas.rects()) == 3
    assert "PRONTO" in " ".join(app.health_legend_box.texts())


def test_barra_de_saude_vazia_nao_quebra(app):
    app.result = None
    app._update_dashboard_stats()
    app._draw_health()
    assert app._health_segments_cache == []


def test_trilha_de_passos_avanca_conforme_o_estado(app):
    app._update_dashboard_stats()
    inicio = [w.cget("text") for w in app._steps_rail.walk() if w.cget("text")]
    assert any("Configurar pastas" in t for t in inicio)

    app.result = SimpleNamespace(stats={"pronto": 2}, certificados=[object()], matches=[object()])
    app.clientes = [object()]
    app._update_dashboard_stats()
    assert "✓" in [w.cget("text") for w in app._steps_rail.walk()]


def test_painel_le_a_ultima_execucao_do_disco(app, gui):
    (gui._saida / "auditoria_ultima_execucao.json").write_text(
        json.dumps({"stats": {"pfx": 12, "pronto": 10, "sem_senha": 2}}), encoding="utf-8")
    app.result = None
    app._update_dashboard_stats()
    assert app.kpi_cards["pronto"].cget("text") == "10"
    assert "última execução" in app.health_updated.cget("text")


def test_atualizar_o_painel_duas_vezes_nao_duplica_widgets(app):
    app.result = SimpleNamespace(stats={"pronto": 2, "sem_senha": 1}, certificados=[], matches=[])
    app._update_dashboard_stats()
    primeiro = len(list(app.health_legend_box.walk())), len(list(app._steps_rail.walk()))
    app._update_dashboard_stats()
    app._update_dashboard_stats()
    assert (len(list(app.health_legend_box.walk())), len(list(app._steps_rail.walk()))) == primeiro


# ------------------------------------------------------------- Regressões

def test_lote_manual_usa_o_nome_certo_do_parametro(app, gui, monkeypatch):
    """Regressão: a tela chamava ``atualizar_todas`` e o matcher espera
    ``atualizar_todos`` — todo clique em "Gerar lote" quebrava."""
    capturado = {}

    def match_all_falso(certs, sem, com=None, **kwargs):
        capturado.update(kwargs)
        return []

    monkeypatch.setattr(gui, "match_all", match_all_falso)
    monkeypatch.setattr(gui, "validate_config", lambda cfg: [])
    monkeypatch.setattr(gui, "effective_config", lambda cfg: cfg)
    monkeypatch.setattr(gui, "analyze", lambda *a, **kw: SimpleNamespace(
        certificados=[], matches=[], stats={}, clientes_sem=[], clientes_com=[]))
    monkeypatch.setattr(gui, "reattempt_locked", lambda *a, **kw: None)
    monkeypatch.setattr(gui, "refresh_stats", lambda r: None)
    monkeypatch.setattr(gui, "_write_reports", lambda *a, **kw: None)
    monkeypatch.setattr(app, "_resolve_clientes", lambda say: ([], []))
    monkeypatch.setattr(app, "_thread", lambda fn: fn())

    import cajuru_a1.diagnostico as diag
    monkeypatch.setattr(diag, "build_diagnostico", lambda r: {})
    monkeypatch.setattr(diag, "write_diagnostico_excel", lambda *a, **kw: None)
    monkeypatch.setattr(diag, "write_diagnostico_html", lambda *a, **kw: None)

    app._run_manual_bundle()

    assert "atualizar_todos" in capturado, "o matcher não aceita 'atualizar_todas'"
    assert "atualizar_todas" not in capturado


def test_mensagem_da_exportacao_cita_os_arquivos_reais(app, gui, monkeypatch):
    """Regressão: o aviso final falava de ``todos_certificados_a1.zip`` e
    ``certificados_e_senhas.csv``, arquivos que não são mais gerados."""
    pasta = gui._saida / "lote"
    pasta.mkdir()
    bundle = {
        "dir": pasta,
        "zip": pasta / "certificados_jettax.zip",
        "outros_zip": pasta / "outros_certificados.zip",
        "senhas": pasta / "senhas_para_preenchimento_manual.csv",
        "nao_exportados": pasta / "nao_exportados.csv",
        "planilha": pasta / "planilha_importacao_jettax.xlsx",
        "quantidade": 7,
        "excluidos": 3,
    }
    import cajuru_a1.exportacao as exportacao
    monkeypatch.setattr(exportacao, "export_all_opened", lambda *a, **kw: bundle)
    monkeypatch.setattr(gui, "validate_config", lambda cfg: [])
    monkeypatch.setattr(gui, "effective_config", lambda cfg: cfg)
    monkeypatch.setattr(gui, "analyze", lambda *a, **kw: SimpleNamespace(
        certificados=[], matches=[], stats={}))
    monkeypatch.setattr(gui, "_open_path", lambda p: None)
    monkeypatch.setattr(app, "_thread", lambda fn: fn())

    app._run_export_all()

    texto = " ".join(m for _kind, _title, m in gui._messagebox.calls)
    assert "certificados_jettax.zip" in texto
    assert "nao_exportados.csv" in texto
    assert "3 arquivo(s)" in texto
    assert "todos_certificados_a1.zip" not in texto
    assert "certificados_e_senhas.csv" not in texto


def test_nenhum_botao_do_dashboard_esta_sem_acao(app):
    import tests.gui_stubs.customtkinter as ctk_stub

    botoes = [w for w in app._views["dashboard"].walk() if isinstance(w, ctk_stub.CTkButton)]
    assert len(botoes) >= 7, "o painel deve ter extração expressa + 3 fluxos + 3 passos"
    for botao in botoes:
        assert callable(botao.cget("command")), f"botão sem comando: {botao.cget('text')}"
