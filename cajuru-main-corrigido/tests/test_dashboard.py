"""Testes do modelo do dashboard (roda sem tkinter, sem janela)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from cajuru_a1.dashboard import (
    STATUS_LABEL,
    STATUS_TONE,
    build_health,
    build_kpis,
    build_model,
    build_readiness,
    build_steps,
    last_run_label,
    load_last_stats,
)


def _cert(tmp_path, *, cnpj, nome="c.pfx", opened=True, mtime=1000.0, sha="a" * 64):
    caminho = tmp_path / nome
    caminho.write_bytes(b"conteudo")
    return SimpleNamespace(
        cnpj=cnpj, cnpj_cert=cnpj, cnpj_filename=cnpj,
        filename=nome, source_path=str(caminho), temp_path=str(caminho),
        opened=opened, identity_conflict=False, not_after=None, not_before=None,
        source_mtime=mtime, size=8, sha256=sha, extra={},
    )


# ----------------------------------------------------------------- KPIs

def test_build_kpis_sempre_devolve_seis_cartoes_mesmo_sem_dados():
    kpis = build_kpis({})
    assert len(kpis) == 6
    assert [k.key for k in kpis] == [
        "pfx", "pfx_abertos", "pronto", "sem_senha", "revisao_manual", "vencido",
    ]
    assert all(k.value == 0 for k in kpis)


def test_build_kpis_soma_revisao_manual_com_ambiguo_e_calcula_percentual():
    kpis = {k.key: k for k in build_kpis({
        "pfx": 40, "pfx_abertos": 30, "pronto": 25,
        "sem_senha": 3, "revisao_manual": 2, "ambiguo": 4, "vencido": 1,
    })}
    assert kpis["revisao_manual"].value == 6
    assert kpis["pfx_abertos"].value == 30
    assert "75%" in kpis["pfx_abertos"].hint


def test_build_kpis_nao_divide_por_zero_sem_certificado():
    kpis = {k.key: k for k in build_kpis({"pfx": 0, "pfx_abertos": 0})}
    assert "0%" in kpis["pfx_abertos"].hint


# ----------------------------------------------------------------- Saúde

def test_build_health_devolve_segmentos_proporcionais_e_ordenados():
    segmentos = build_health({"pronto": 3, "sem_senha": 1})
    assert [s.key for s in segmentos] == ["pronto", "sem_senha"]
    assert segmentos[0].pct == 75.0
    assert segmentos[1].pct == 25.0
    assert abs(sum(s.pct for s in segmentos) - 100.0) < 0.1


def test_build_health_vazio_quando_nao_ha_nada_contado():
    assert build_health({}) == []
    assert build_health({"pronto": 0, "vencido": 0}) == []
    assert build_health(None) == []


def test_todo_status_tem_rotulo_e_tom_conhecidos():
    for key in STATUS_TONE:
        assert key in STATUS_LABEL
        assert STATUS_TONE[key] in {"ok", "warn", "danger", "review", "neutral"}


# ------------------------------------------------------------ Prontidão

def test_readiness_sem_certificados_fica_neutro():
    pronto = build_readiness([])
    assert pronto.prontos == 0 and pronto.bloqueados == 0
    assert pronto.tone == "neutral"
    assert pronto.pct == 0.0


def test_readiness_lote_limpo_reporta_100_por_cento(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="12345678000195", nome="a.pfx"),
        _cert(tmp_path, cnpj="21260898000379", nome="b.pfx", sha="b" * 64),
    ]
    pronto = build_readiness(certs)
    assert pronto.prontos == 2
    assert pronto.bloqueados == 0
    assert pronto.motivos == []
    assert pronto.tone == "ok"
    assert pronto.pct == 100.0


def test_readiness_agrupa_cnpj_duplicado_como_um_motivo(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="12345678000195", nome="a.pfx", mtime=1.0, sha="a" * 64),
        _cert(tmp_path, cnpj="12345678000195", nome="a (1).pfx", mtime=2.0, sha="b" * 64),
    ]
    pronto = build_readiness(certs)
    assert pronto.prontos == 1, "só o mais novo entra no lote"
    assert pronto.bloqueados == 1
    assert pronto.tone == "warn"
    motivos = dict(pronto.motivos)
    assert motivos == {"CNPJ duplicado (enviado o mais novo)": 1}


def test_readiness_separa_cpf_de_arquivo_sem_documento(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="10961162600", nome="paula.pfx", sha="c" * 64),
        _cert(tmp_path, cnpj="", nome="PAEX.pfx", sha="d" * 64),
    ]
    pronto = build_readiness(certs)
    assert pronto.prontos == 0
    assert pronto.tone == "danger"
    motivos = dict(pronto.motivos)
    assert motivos["Certificado de CPF (Jettax só aceita CNPJ)"] == 1
    assert motivos["Sem CNPJ legível no arquivo"] == 1


def test_readiness_conta_certificado_que_nao_abriu(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="12345678000195", nome="ok.pfx"),
        _cert(tmp_path, cnpj="21260898000379", nome="travado.pfx", opened=False, sha="e" * 64),
    ]
    pronto = build_readiness(certs)
    assert pronto.prontos == 1
    assert dict(pronto.motivos)["Senha não validada"] == 1


def test_readiness_filiais_do_mesmo_grupo_nao_sao_duplicatas(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="21260898000379", nome="matriz.pfx", sha="a" * 64),
        _cert(tmp_path, cnpj="21260898000107", nome="filial.pfx", sha="b" * 64),
    ]
    pronto = build_readiness(certs)
    assert pronto.prontos == 2
    assert pronto.bloqueados == 0


def test_readiness_motivos_vem_do_maior_para_o_menor(tmp_path):
    certs = [
        _cert(tmp_path, cnpj="", nome=f"sem{i}.pfx", sha=str(i) * 64) for i in range(3)
    ]
    certs.append(_cert(tmp_path, cnpj="10961162600", nome="cpf.pfx", sha="f" * 64))
    pronto = build_readiness(certs)
    assert pronto.motivos[0][1] >= pronto.motivos[-1][1]
    assert pronto.motivos[0][0] == "Sem CNPJ legível no arquivo"


# --------------------------------------------------------------- Passos

def test_build_steps_marca_apenas_um_passo_atual():
    steps = build_steps(tem_config=True, tem_analise=False, tem_clientes=False,
                        tem_matches=False, tem_lote=False)
    assert [s.state for s in steps] == ["done", "current", "todo", "todo", "todo"]
    assert sum(1 for s in steps if s.state == "current") == 1


def test_build_steps_tudo_pronto_nao_tem_passo_atual():
    steps = build_steps(tem_config=True, tem_analise=True, tem_clientes=True,
                        tem_matches=True, tem_lote=True)
    assert {s.state for s in steps} == {"done"}


def test_build_steps_do_zero_comeca_na_configuracao():
    steps = build_steps(tem_config=False, tem_analise=False, tem_clientes=False,
                        tem_matches=False, tem_lote=False)
    assert steps[0].state == "current"
    assert steps[0].key == "config"


# ------------------------------------------------------- Estado em disco

def test_load_last_stats_le_execucao_anterior(tmp_path):
    (tmp_path / "auditoria_ultima_execucao.json").write_text(
        json.dumps({"stats": {"pronto": 7}}), encoding="utf-8")
    assert load_last_stats(tmp_path) == {"pronto": 7}
    assert "/" in last_run_label(tmp_path)


def test_load_last_stats_tolera_json_quebrado_ou_ausente(tmp_path):
    assert load_last_stats(tmp_path) == {}
    assert last_run_label(tmp_path) == "nunca"
    (tmp_path / "auditoria_ultima_execucao.json").write_text("{quebrado", encoding="utf-8")
    assert load_last_stats(tmp_path) == {}


# ---------------------------------------------------------------- Modelo

def test_build_model_sem_resultado_usa_disco_e_traz_todos_os_blocos(tmp_path):
    (tmp_path / "auditoria_ultima_execucao.json").write_text(
        json.dumps({"stats": {"pfx": 5, "pronto": 4, "sem_senha": 1}}), encoding="utf-8")
    model = build_model(None, tmp_path)
    assert {"kpis", "health", "readiness", "steps", "stats", "ultima_execucao"} <= set(model)
    assert {k.key: k.value for k in model["kpis"]}["pronto"] == 4
    assert model["health"], "a barra de saúde deve refletir o json da última execução"


def test_build_model_prefere_o_resultado_em_memoria(tmp_path):
    (tmp_path / "auditoria_ultima_execucao.json").write_text(
        json.dumps({"stats": {"pronto": 1}}), encoding="utf-8")
    result = SimpleNamespace(stats={"pronto": 9, "pfx": 9}, certificados=[], matches=[])
    model = build_model(result, tmp_path)
    assert {k.key: k.value for k in model["kpis"]}["pronto"] == 9


def test_build_model_liga_os_passos_ao_estado_real(tmp_path):
    result = SimpleNamespace(
        stats={"pronto": 2}, certificados=[object()], matches=[object()],
    )
    steps = {s.key: s.state for s in build_model(result, tmp_path, clientes=3, tem_config=True)["steps"]}
    assert steps == {k: "done" for k in steps}
