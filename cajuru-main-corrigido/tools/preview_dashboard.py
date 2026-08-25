"""Desenha uma PRÉVIA do dashboard como imagem (PNG).

Não é um screenshot: é um desenho do mesmo layout usando as MESMAS cores
(``gui.C``) e os MESMOS dados (``cajuru_a1.dashboard``) que a janela real
usa. Serve para revisar o visual em máquinas sem Tcl/Tk instalado.

    python tools/preview_dashboard.py saida.png
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cajuru_a1.dashboard import (  # noqa: E402
    build_health,
    build_kpis,
    build_readiness,
    build_steps,
)

C = {
    "bg": "#0A0D14", "surface": "#111621", "surface2": "#171E2C", "surface3": "#202939",
    "border": "#28324A", "border_soft": "#1A2130", "text": "#F1F4FA",
    "text_muted": "#98A2B8", "text_faint": "#6B7689", "accent": "#F4823F",
    "accent_hover": "#FF9752", "accent_soft": "#2E1D0F", "ok": "#3DD68C",
    "ok_soft": "#0F2E20", "warn": "#F5B544", "warn_soft": "#32250C",
    "danger": "#FF6B6B", "danger_soft": "#361A1C", "review": "#A78BFA",
    "review_soft": "#241E3D", "neutral": "#8792A8", "neutral_soft": "#1E2430",
}
TONE = {
    "ok": (C["ok"], C["ok_soft"]), "warn": (C["warn"], C["warn_soft"]),
    "danger": (C["danger"], C["danger_soft"]), "review": (C["review"], C["review_soft"]),
    "accent": (C["accent"], C["accent_soft"]), "neutral": (C["neutral"], C["neutral_soft"]),
}

W, H = 1380, 1010
FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
BOLDS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size: int, bold: bool = False):
    for path in (BOLDS if bold else FONTS):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _card(draw, box, *, fill=None, border=None, radius=16):
    draw.rounded_rectangle(box, radius=radius, fill=fill or C["surface"],
                           outline=border or C["border"], width=1)


def _wrap(draw, text, font, max_width):
    linhas, atual = [], ""
    for palavra in text.split():
        teste = f"{atual} {palavra}".strip()
        if draw.textlength(teste, font=font) <= max_width:
            atual = teste
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas


def _cert(cnpj, nome, *, opened=True, mtime=1.0, sha="a" * 64):
    return SimpleNamespace(
        cnpj=cnpj, cnpj_cert=cnpj, cnpj_filename=cnpj, filename=nome,
        source_path=__file__, temp_path=__file__, opened=opened,
        identity_conflict=False, not_after=None, not_before=None,
        source_mtime=mtime, size=1, sha256=sha, extra={},
    )


def build_demo():
    """Dados realistas, no espírito da lista que o Jettax recusou."""
    certs = [_cert(c, f"{c}.pfx", sha=str(i) * 64) for i, c in enumerate([
        "21260898000379", "21260898000107", "36878186000395", "36878186000123",
        "23325176000119", "23325176000208", "57010796000261", "57010796000180",
        "41470879000201", "41470879000112", "12345678000195",
    ])]
    certs.append(_cert("12345678000195", "empresa (1).pfx", mtime=99.0, sha="z" * 64))
    certs.append(_cert("10961162600", "PAULA KARINE FONSECA.pfx", sha="y" * 64))
    certs.append(_cert("02540221629", "IVAIR MONTEIRO CHAVES.pfx", sha="x" * 64))
    for nome in ("PAEX", "MG PAPEIS", "ATHAMAR MATRIZ"):
        certs.append(_cert("", f"{nome}.pfx", sha=nome.ljust(64, "0")[:64]))
    stats = {
        "pfx": 17, "pfx_abertos": 16, "pronto": 11, "sem_senha": 1,
        "revisao_manual": 2, "ambiguo": 1, "vencido": 1, "substituido": 1,
    }
    return certs, stats


def render(destino: Path) -> Path:
    certs, stats = build_demo()
    readiness = build_readiness(certs)
    kpis = build_kpis(stats)
    health = build_health(stats)
    steps = build_steps(tem_config=True, tem_analise=True, tem_clientes=True,
                        tem_matches=True, tem_lote=False)

    img = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(img)

    # ---------------------------------------------------------- Sidebar
    d.rectangle([0, 0, 252, H], fill=C["surface"])
    d.rounded_rectangle([24, 26, 62, 64], radius=10, fill=C["accent"])
    d.text((34, 36), "A1", font=_font(15, True), fill="#12161F")
    d.text((74, 32), "Cajuru A1", font=_font(17, True), fill=C["text"])
    d.text((74, 52), "Auditoria e conciliação A1", font=_font(10), fill=C["text_faint"])
    d.text((24, 92), "PAINEL DE CONTROLE", font=_font(9, True), fill=C["text_faint"])
    for i, (label, ativo) in enumerate([
        ("Dashboard", True), ("Certificados", False), ("Lotes manuais", False),
        ("Relatórios", False), ("Configuração", False), ("Log", False),
    ]):
        y = 114 + i * 48
        if ativo:
            d.rounded_rectangle([16, y, 236, y + 42], radius=10,
                                fill=C["accent_soft"], outline=C["accent"], width=1)
        d.text((34, y + 13), label, font=_font(12, True),
               fill=C["text"] if ativo else C["text_muted"])
    d.multiline_text((24, H - 96),
                     "SCAN LOCAL\nSomente leitura no Dropbox.\nNenhuma senha é gravada em\nrelatório, log ou banco.",
                     font=_font(9), fill=C["text_faint"], spacing=5)

    # ----------------------------------------------------------- Header
    d.rectangle([252, 0, W, 78], fill=C["surface"])
    d.text((280, 20), "Dashboard", font=_font(19, True), fill=C["text"])
    d.text((280, 46), "Visão geral da auditoria de certificados A1",
           font=_font(11), fill=C["text_muted"])
    d.rounded_rectangle([W - 168, 24, W - 28, 50], radius=99, fill=C["warn_soft"])
    d.text((W - 152, 31), "●  AGUARDANDO", font=_font(10, True), fill=C["warn"])

    x0, right = 268, W - 16
    y = 96

    # ------------------------------------------------------------- Hero
    hero_h = 196
    split = x0 + int((right - x0) * 0.6)
    accent, soft = TONE[readiness.tone]
    _card(d, [x0, y, split - 6, y + hero_h], fill=soft, border=accent)
    d.text((x0 + 24, y + 20), "PRONTIDÃO PARA IMPORTAÇÃO NO JETTAX",
           font=_font(9, True), fill=C["text_faint"])
    d.text((x0 + 24, y + 40), readiness.titulo, font=_font(23, True), fill=C["text"])
    for i, linha in enumerate(_wrap(d, readiness.detalhe, _font(11), split - x0 - 52)):
        d.text((x0 + 24, y + 74 + i * 16), linha, font=_font(11), fill=C["text_muted"])
    d.text((x0 + 24, y + 108), str(readiness.prontos), font=_font(42, True), fill=accent)
    largura_num = d.textlength(str(readiness.prontos), font=_font(42, True))
    d.text((x0 + 30 + largura_num, y + 134), "aceitos", font=_font(11), fill=C["text_muted"])
    if readiness.bloqueados:
        txt = f"{readiness.bloqueados} fora do lote"
        d.text((split - 26 - d.textlength(txt, font=_font(11, True)), y + 134),
               txt, font=_font(11, True), fill=C["danger"])
    bar_y = y + hero_h - 32
    d.rounded_rectangle([x0 + 24, bar_y, split - 30, bar_y + 10], radius=5, fill=C["surface3"])
    if readiness.pct:
        fim = x0 + 24 + (split - 54 - x0) * readiness.pct / 100
        d.rounded_rectangle([x0 + 24, bar_y, fim, bar_y + 10], radius=5, fill=C["ok"])

    _card(d, [split + 6, y, right, y + hero_h])
    d.text((split + 28, y + 20), "O QUE ESTÁ TRAVANDO", font=_font(9, True), fill=C["text_faint"])
    ry = y + 44
    for motivo, qtd in readiness.motivos[:4]:
        d.rounded_rectangle([split + 28, ry, right - 22, ry + 30], radius=9, fill=C["surface2"])
        d.text((split + 40, ry + 8), str(qtd), font=_font(13, True), fill=C["accent"])
        linhas = _wrap(d, motivo, _font(10), right - split - 110)
        d.text((split + 70, ry + 9 if len(linhas) == 1 else ry + 3),
               "\n".join(linhas[:2]), font=_font(10), fill=C["text_muted"], spacing=3)
        ry += 36
    y += hero_h + 12

    # ------------------------------------------------------------- KPIs
    kpi_w = (right - x0 - 20) / 3
    for idx, kpi in enumerate(kpis):
        col, row = idx % 3, idx // 3
        kx = x0 + col * (kpi_w + 10)
        ky = y + row * 112
        _card(d, [kx, ky, kx + kpi_w, ky + 102])
        cor = TONE[kpi.tone][0]
        d.rounded_rectangle([kx + 18, ky + 17, kx + 21, ky + 30], radius=2, fill=cor)
        d.text((kx + 29, ky + 17), kpi.label.upper(), font=_font(9, True), fill=C["text_muted"])
        d.text((kx + 18, ky + 36), str(kpi.value), font=_font(30, True), fill=cor)
        for i, linha in enumerate(_wrap(d, kpi.hint, _font(9), kpi_w - 36)[:2]):
            d.text((kx + 18, ky + 76 + i * 12), linha, font=_font(9), fill=C["text_faint"])
    y += 224 + 12

    # ------------------------------------------------------------ Saúde
    _card(d, [x0, y, right, y + 104])
    d.text((x0 + 22, y + 16), "COMPOSIÇÃO DO ACERVO", font=_font(9, True), fill=C["text_faint"])
    d.text((right - 22 - d.textlength("última execução: 25/08/2026 às 20:21", font=_font(9)), y + 16),
           "última execução: 25/08/2026 às 20:21", font=_font(9), fill=C["text_faint"])
    bx, bw = x0 + 22, right - x0 - 44
    gap, usable = 3, bw - 3 * (len(health) - 1)
    cx = bx
    for seg in health:
        w = max(4, usable * seg.pct / 100)
        d.rectangle([cx, y + 38, cx + w, y + 52], fill=TONE[seg.tone][0])
        cx += w + gap
    for idx, seg in enumerate(health):
        lx = x0 + 22 + (idx % 4) * ((right - x0 - 44) / 4)
        ly = y + 64 + (idx // 4) * 18
        d.rounded_rectangle([lx, ly + 3, lx + 9, ly + 12], radius=3, fill=TONE[seg.tone][0])
        d.text((lx + 16, ly + 1), f"{seg.label}  {seg.count} ({seg.pct:.0f}%)",
               font=_font(9), fill=C["text_muted"])
    y += 104 + 12

    # ------------------------------------------------------------ Passos
    _card(d, [x0, y, right, y + 116])
    d.text((x0 + 22, y + 16), "COMO CHEGAR NO LOTE", font=_font(9, True), fill=C["text_faint"])
    sw = (right - x0 - 44 - 4 * 8) / 5
    for i, step in enumerate(steps):
        sx = x0 + 22 + i * (sw + 8)
        sy = y + 38
        edge, fill, tcol = {
            "done": (C["ok"], C["ok_soft"], C["text"]),
            "current": (C["accent"], C["accent_soft"], C["text"]),
            "todo": (C["border"], C["surface2"], C["text_faint"]),
        }[step.state]
        d.rounded_rectangle([sx, sy, sx + sw, sy + 62], radius=12, fill=fill, outline=edge, width=1)
        d.rounded_rectangle([sx + 12, sy + 11, sx + 32, sy + 31], radius=9,
                            fill=edge if step.state != "todo" else C["surface3"])
        marca = "✓" if step.state == "done" else str(step.index)
        d.text((sx + 12 + (20 - d.textlength(marca, font=_font(11, True))) / 2, sy + 15),
               marca, font=_font(11, True),
               fill="#0A0D14" if step.state != "todo" else C["text_faint"])
        d.text((sx + 38, sy + 15), step.title, font=_font(10, True), fill=tcol)
        for j, linha in enumerate(_wrap(d, step.detail, _font(8), sw - 24)[:2]):
            d.text((sx + 12, sy + 38 + j * 11), linha, font=_font(8), fill=C["text_faint"])
    y += 116 + 12

    # ----------------------------------------------------------- Express
    _card(d, [x0, y, right, y + 96], fill=C["accent_soft"], border=C["accent"])
    d.text((x0 + 24, y + 20), "EXTRAÇÃO EXPRESSA · 100% OFF-LINE",
           font=_font(13, True), fill=C["accent"])
    for i, linha in enumerate(_wrap(
        d, "Lê a pasta do Dropbox, testa as senhas conhecidas e monta o pacote do Jettax: "
           "ZIP nomeado por CNPJ, planilha oficial preenchida e a lista do que ficou de fora.",
            _font(10), right - x0 - 300)):
        d.text((x0 + 24, y + 42 + i * 14), linha, font=_font(10), fill=C["text_muted"])
    d.text((x0 + 24, y + 74), "Sem navegador  ·  Sem login  ·  Sem escrever no Dropbox",
           font=_font(9, True), fill=C["ok"])
    d.rounded_rectangle([right - 234, y + 25, right - 24, y + 71], radius=11, fill=C["accent"])
    d.text((right - 234 + (210 - d.textlength("Gerar pacote agora", font=_font(12, True))) / 2, y + 41),
           "Gerar pacote agora", font=_font(12, True), fill="#12161F")
    y += 96 + 12

    # ------------------------------------------------------------ Ações
    _card(d, [x0, y, right, y + 120])
    d.text((x0 + 22, y + 16), "FLUXOS COM O JETTAX", font=_font(9, True), fill=C["text_faint"])
    bw2 = (right - x0 - 44 - 16) / 3
    for i, (label, cor) in enumerate([
        ("Fluxo completo", C["accent"]), ("Gerar lote manual", C["warn"]),
        ("Simular / enviar", C["danger"]),
    ]):
        bx2 = x0 + 22 + i * (bw2 + 8)
        d.rounded_rectangle([bx2, y + 34, bx2 + bw2, y + 78], radius=11,
                            fill=C["surface2"], outline=cor, width=1)
        d.text((bx2 + (bw2 - d.textlength(label, font=_font(11, True))) / 2, y + 50),
               label, font=_font(11, True), fill=C["text"])
    d.text((x0 + 22, y + 86), "PASSOS AVULSOS", font=_font(9, True), fill=C["text_faint"])

    img.save(destino)
    return destino


if __name__ == "__main__":
    saida = Path(sys.argv[1] if len(sys.argv) > 1 else "preview_dashboard.png")
    print(render(saida))
