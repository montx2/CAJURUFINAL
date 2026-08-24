"""Testes da seleção do certificado mais novo e do matcher."""
from __future__ import annotations

from pathlib import Path

import pytest

from cajuru_a1.matcher import match_all
from cajuru_a1.models import JetaxClient
from cajuru_a1.pfx import PfxInfo, inspect_file
from cajuru_a1.passwords import PasswordVault
from cajuru_a1.passwords import candidate_passwords

from conftest import make_pfx


def _candidates(vault, empresa, cnpj, senha):
    return candidate_passwords(
        vault=vault, empresa=empresa, cnpj=cnpj,
        extra_names=[empresa], include_empty=True,
    ) + [(senha, "teste")]


def test_pick_newest_escolhe_validade_mais_longa(tmp_path):
    from cajuru_a1.pfx import pick_newest
    antigo = PfxInfo("", "", "antigo.pfx", "a"*64, 10, opened=True,
                    not_before=_dt(-300), not_after=_dt(-10))
    novo = PfxInfo("", "", "novo.pfx", "b"*64, 10, opened=True,
                   not_before=_dt(-5), not_after=_dt(360))
    # antigo vencido, novo válido -> vence o novo
    winner, losers = pick_newest([antigo, novo])
    assert winner is novo
    assert antigo in losers
    assert "Substituído" in (antigo.extra.get("motivo_substituicao") or "")


def _dt(days):
    import datetime
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)


def test_matcher_escolhe_mais_novo_automaticamente(tmp_path):
    (tmp_path / "src").mkdir()
    """Dois PFX do mesmo CNPJ: um vencido e um válido -> escolhe o novo."""
    p1 = tmp_path / "src" / "velho.pfx"
    p2 = tmp_path / "src" / "novo.pfx"
    make_pfx(p1, password="123", cnpj="12345678000195", company="VELHA LTDA", days_valid=-10)
    make_pfx(p2, password="456", cnpj="12345678000195", company="NOVA LTDA", days_valid=400)

    # inspeciona
    vault = PasswordVault()
    c1 = inspect_file(p1, p1, _candidates(vault, "VELHA", "12345678000195", "123"))
    c2 = inspect_file(p2, p2, _candidates(vault, "NOVA", "12345678000195", "456"))
    assert c1.opened and c2.opened
    assert c1.expired and not c2.expired

    client = JetaxClient(razao_social="NOVA LTDA", cnpj="12345678000195")
    matches = match_all([c1, c2], [client], [], escolher_mais_novo=True)

    prontos = [m for m in matches if m.status == "pronto"]
    subs = [m for m in matches if m.status == "substituido"]
    assert len(prontos) == 1
    assert prontos[0].cert is c2
    assert len(subs) == 1
    assert subs[0].cert is c1


def test_matcher_modo_legado_marca_ambiguo(tmp_path):
    (tmp_path / "src").mkdir()
    """Com escolher_mais_novo=False, preserva comportamento antigo."""
    p1 = tmp_path / "src" / "a.pfx"
    p2 = tmp_path / "src" / "b.pfx"
    make_pfx(p1, password="1", cnpj="12345678000195", company="X", days_valid=100)
    make_pfx(p2, password="2", cnpj="12345678000195", company="X", days_valid=400)
    vault = PasswordVault()
    c1 = inspect_file(p1, p1, _candidates(vault, "X", "12345678000195", "1"))
    c2 = inspect_file(p2, p2, _candidates(vault, "X", "12345678000195", "2"))
    client = JetaxClient(razao_social="X", cnpj="12345678000195")
    matches = match_all([c1, c2], [client], [], escolher_mais_novo=False)
    assert [m.status for m in matches].count("ambiguo") == 2


def test_ambiguo_desempata_por_nome_quando_cnpj_duplicado_no_jettax(tmp_path):
    """Dois clientes Jettax com o mesmo CNPJ (erro de cadastro): o nome do
    certificado, quando bate com folga em um único candidato, resolve
    automaticamente em vez de travar em AMBÍGUO."""
    (tmp_path / "src").mkdir()
    p = tmp_path / "src" / "cert.pfx"
    make_pfx(p, password="pw", cnpj="12345678000195", company="CAJURU CONTABILIDADE LTDA", days_valid=400)
    vault = PasswordVault()
    cert = inspect_file(p, p, _candidates(vault, "CAJURU CONTABILIDADE", "12345678000195", "pw"))

    certo = JetaxClient(razao_social="CAJURU CONTABILIDADE LTDA", cnpj="12345678000195")
    duplicado = JetaxClient(razao_social="OUTRA EMPRESA SEM RELACAO LTDA", cnpj="12345678000195")
    matches = match_all([cert], [certo, duplicado], [])

    prontos = [m for m in matches if m.status == "pronto"]
    assert len(prontos) == 1
    assert prontos[0].cliente is certo
    assert prontos[0].pode_enviar
    assert any("nome do certificado" in ev for ev in prontos[0].evidencias)


def test_ambiguo_permanece_quando_nomes_nao_desempatam(tmp_path):
    """Se o nome do certificado não aponta claramente para nenhum dos
    candidatos com o mesmo CNPJ, o caso continua AMBÍGUO (revisão manual)."""
    (tmp_path / "src").mkdir()
    p = tmp_path / "src" / "cert.pfx"
    make_pfx(p, password="pw", cnpj="12345678000195", company="ZZZ TOTALMENTE DIFERENTE LTDA", days_valid=400)
    vault = PasswordVault()
    cert = inspect_file(p, p, _candidates(vault, "ZZZ TOTALMENTE DIFERENTE", "12345678000195", "pw"))

    cliente_a = JetaxClient(razao_social="CAJURU CONTABILIDADE LTDA", cnpj="12345678000195")
    cliente_b = JetaxClient(razao_social="CAJURU SERVICOS LTDA", cnpj="12345678000195")
    matches = match_all([cert], [cliente_a, cliente_b], [])

    assert [m.status for m in matches].count("ambiguo") == 1
    assert not any(m.status == "pronto" for m in matches)


def test_atualizar_todas_marca_pronto_para_quem_ja_tem_a1(tmp_path):
    (tmp_path / "src").mkdir()
    p = tmp_path / "src" / "novo.pfx"
    make_pfx(p, password="pw", cnpj="55555555000191", company="ALPHA LTDA", days_valid=500)
    vault = PasswordVault()
    cert = inspect_file(p, p, _candidates(vault, "ALPHA", "55555555000191", "pw"))
    sem = []  # ninguém sem A1
    com = [JetaxClient(razao_social="ALPHA LTDA", cnpj="55555555000191", tem_certificado=True)]
    # sem atualizar_todas: vira extra_pfx
    m1 = match_all([cert], sem, com, atualizar_todos=False)
    assert m1[0].status == "extra_pfx"
    # com atualizar_todas: vira pronto (renovação)
    m2 = match_all([cert], sem, com, atualizar_todos=True, escolher_mais_novo=True)
    prontos = [m for m in m2 if m.status == "pronto"]
    assert len(prontos) == 1
    assert prontos[0].pode_enviar
