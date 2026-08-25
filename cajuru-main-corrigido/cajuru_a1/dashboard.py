"""Modelo de dados do dashboard — sem nenhuma dependência de interface.

A tela (``gui.py``) só desenha o que este módulo calcula. Isso mantém a
lógica do painel testável sem abrir janela, sem tkinter e sem display.

O painel responde, em ordem, às três perguntas que interessam:

1. **Dá para importar no Jettax agora?** (``build_readiness``) — quantos
   certificados passam nas regras do importador e quantos travam, com o
   motivo agrupado.
2. **Qual é o estado do acervo?** (``build_kpis`` e ``build_health``).
3. **Qual é o próximo passo?** (``build_steps``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ Status

STATUS_LABEL = {
    "pronto": "PRONTO",
    "vencido": "VENCIDO",
    "nao_valido": "AINDA NÃO VÁLIDO",
    "sem_senha": "SEM SENHA",
    "invalido": "INVÁLIDO",
    "conflito": "CONFLITO",
    "ambiguo": "AMBÍGUO",
    "revisao_manual": "REVISÃO MANUAL",
    "substituido": "SUBSTITUÍDO",
    "duplicado": "DUPLICADO",
    "sem_cert": "SEM PFX",
    "sem_cert_novo": "SEM PFX NOVO",
    "extra_pfx": "PFX SEM CLIENTE",
}

# Tom semântico de cada status (a paleta concreta vive na interface).
STATUS_TONE = {
    "pronto": "ok",
    "sem_senha": "warn",
    "vencido": "danger",
    "invalido": "danger",
    "conflito": "danger",
    "nao_valido": "review",
    "ambiguo": "review",
    "revisao_manual": "review",
    "substituido": "neutral",
    "duplicado": "neutral",
    "sem_cert": "neutral",
    "sem_cert_novo": "neutral",
    "extra_pfx": "neutral",
}

HEALTH_ORDER = [
    "pronto", "substituido", "revisao_manual", "ambiguo", "sem_senha",
    "nao_valido", "vencido", "invalido", "conflito", "duplicado",
    "extra_pfx", "sem_cert", "sem_cert_novo",
]


@dataclass
class Kpi:
    key: str
    label: str
    value: int
    hint: str
    tone: str = "neutral"


@dataclass
class Segment:
    key: str
    label: str
    tone: str
    count: int
    pct: float


@dataclass
class Step:
    key: str
    index: int
    title: str
    detail: str
    state: str  # "done" | "current" | "todo"


@dataclass
class Readiness:
    """Resumo de "o Jettax vai aceitar este lote?"."""

    prontos: int = 0
    bloqueados: int = 0
    motivos: list[tuple[str, int]] = field(default_factory=list)
    tone: str = "neutral"
    titulo: str = "Sem análise"
    detalhe: str = "Rode uma leitura da pasta de certificados para começar."

    @property
    def total(self) -> int:
        return self.prontos + self.bloqueados

    @property
    def pct(self) -> float:
        return round(self.prontos / self.total * 100, 1) if self.total else 0.0


# ------------------------------------------------------------------ Helpers

def _int(stats: dict | None, key: str) -> int:
    try:
        return int((stats or {}).get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def load_last_stats(output_dir: Path) -> dict:
    """Lê as estatísticas da última execução gravadas na pasta de saída."""
    path = Path(output_dir) / "auditoria_ultima_execucao.json"
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("stats") or {}
    except (OSError, ValueError):
        pass
    return {}


def last_run_label(output_dir: Path) -> str:
    path = Path(output_dir) / "auditoria_ultima_execucao.json"
    try:
        if path.is_file():
            return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y às %H:%M")
    except OSError:
        pass
    return "nunca"


# ------------------------------------------------------------------ Blocos

def build_kpis(stats: dict | None) -> list[Kpi]:
    """Os seis números que resumem a execução."""
    stats = stats or {}
    lidos = _int(stats, "pfx")
    abertos = _int(stats, "pfx_abertos")
    return [
        Kpi("pfx", "Certificados lidos", lidos, "arquivos .pfx/.p12 na pasta", "accent"),
        Kpi("pfx_abertos", "Senha validada", abertos,
            f"{round(abertos / lidos * 100) if lidos else 0}% do acervo abriu", "ok"),
        Kpi("pronto", "Prontos para o Jettax", _int(stats, "pronto"),
            "conciliados e liberados", "ok"),
        Kpi("sem_senha", "Sem senha", _int(stats, "sem_senha"),
            "nenhuma senha candidata funcionou", "warn"),
        Kpi("revisao_manual", "Revisão manual", _int(stats, "revisao_manual") + _int(stats, "ambiguo"),
            "exigem conferência humana", "review"),
        Kpi("vencido", "Vencidos", _int(stats, "vencido"),
            "fora da validade — precisam renovar", "danger"),
    ]


def build_health(stats: dict | None) -> list[Segment]:
    """Barra proporcional com a composição do acervo por status."""
    stats = stats or {}
    total = sum(_int(stats, key) for key in HEALTH_ORDER)
    if total <= 0:
        return []
    segments: list[Segment] = []
    for key in HEALTH_ORDER:
        count = _int(stats, key)
        if count <= 0:
            continue
        segments.append(Segment(
            key=key,
            label=STATUS_LABEL.get(key, key.upper()),
            tone=STATUS_TONE.get(key, "neutral"),
            count=count,
            pct=round(count / total * 100, 1),
        ))
    return segments


def build_readiness(certificados) -> Readiness:
    """Aplica as regras do importador do Jettax e resume o resultado.

    É o bloco mais importante do painel: mostra, ANTES de gerar o lote,
    quantos certificados o Jettax vai aceitar e por que os outros travam
    (CNPJ duplicado, certificado de CPF, arquivo sem CNPJ legível…).
    """
    certificados = [c for c in (certificados or []) if c is not None]
    if not certificados:
        return Readiness()

    # Importação tardia: evita ciclo e mantém este módulo leve.
    from cajuru_a1.exportacao import selecionar_para_jettax

    elegiveis, excluidos = selecionar_para_jettax(certificados)
    nao_abriram = [c for c in certificados if not getattr(c, "opened", False)]

    contagem: dict[str, int] = {}
    for _cert, motivo in excluidos:
        chave = _resumir_motivo(motivo)
        contagem[chave] = contagem.get(chave, 0) + 1
    if nao_abriram:
        contagem["Senha não validada"] = contagem.get("Senha não validada", 0) + len(nao_abriram)

    motivos = sorted(contagem.items(), key=lambda item: (-item[1], item[0]))
    prontos = len(elegiveis)
    bloqueados = len(excluidos) + len(nao_abriram)

    if prontos and not bloqueados:
        tone, titulo = "ok", "Lote 100% aceito pelo Jettax"
        detalhe = f"{prontos} certificado(s) com CNPJ único e nome de arquivo no padrão exigido."
    elif prontos:
        tone, titulo = "warn", f"{prontos} de {prontos + bloqueados} prontos para importar"
        detalhe = "Os bloqueados ficam listados em nao_exportados.csv e não impedem a importação dos demais."
    else:
        tone, titulo = "danger", "Nenhum certificado importável"
        detalhe = "Todos os arquivos travaram nas regras do Jettax. Veja os motivos ao lado."
    return Readiness(prontos=prontos, bloqueados=bloqueados, motivos=motivos,
                     tone=tone, titulo=titulo, detalhe=detalhe)


def _resumir_motivo(motivo: str) -> str:
    """Agrupa mensagens detalhadas em famílias curtas para o painel."""
    texto = (motivo or "").casefold()
    if "duplicado" in texto or "repetido" in texto:
        return "CNPJ duplicado (enviado o mais novo)"
    if "cpf" in texto or "pessoa física" in texto:
        return "Certificado de CPF (Jettax só aceita CNPJ)"
    if "sem cnpj" in texto:
        return "Sem CNPJ legível no arquivo"
    if "inválido" in texto:
        return "CNPJ inválido"
    if "cópia temporária" in texto:
        return "Cópia temporária perdida (refaça a leitura)"
    return "Outros"


def build_steps(*, tem_config: bool, tem_analise: bool, tem_clientes: bool,
                tem_matches: bool, tem_lote: bool) -> list[Step]:
    """Trilha de progresso do fluxo, do zero até o lote pronto."""
    definidos = [
        ("config", "Configurar pastas", "Pasta do Dropbox e planilhas de senha", tem_config),
        ("analise", "Ler e validar", "Abre cada .pfx testando as senhas conhecidas", tem_analise),
        ("clientes", "Buscar clientes", "Lista as empresas do Jettax (somente leitura)", tem_clientes),
        ("conciliacao", "Conciliar", "Cruza certificado × empresa e classifica cada caso", tem_matches),
        ("lote", "Gerar lote", "ZIP nomeado por CNPJ + planilha oficial", tem_lote),
    ]
    steps: list[Step] = []
    current_marcado = False
    for index, (key, title, detail, done) in enumerate(definidos, start=1):
        if done:
            state = "done"
        elif not current_marcado:
            state, current_marcado = "current", True
        else:
            state = "todo"
        steps.append(Step(key=key, index=index, title=title, detail=detail, state=state))
    return steps


def build_model(result, output_dir: Path, *, clientes: int = 0, tem_config: bool = False) -> dict:
    """Junta todos os blocos num único dicionário para a interface desenhar."""
    stats = (getattr(result, "stats", None) or {}) if result else {}
    if not stats:
        stats = load_last_stats(output_dir)
    certificados = getattr(result, "certificados", None) if result else None
    matches = getattr(result, "matches", None) if result else None
    return {
        "kpis": build_kpis(stats),
        "health": build_health(stats),
        "readiness": build_readiness(certificados),
        "steps": build_steps(
            tem_config=tem_config,
            tem_analise=bool(certificados),
            tem_clientes=bool(clientes),
            tem_matches=bool(matches),
            tem_lote=bool(_int(stats, "pronto")),
        ),
        "stats": stats,
        "ultima_execucao": last_run_label(output_dir),
    }
