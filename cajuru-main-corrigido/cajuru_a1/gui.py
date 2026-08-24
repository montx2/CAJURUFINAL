"""Interface de mesa do Cajuru A1 — elegante, minimalista e 100% local.

Este é o painel principal do produto. Ele substitui o antigo painel web:

- Não abre navegador nem servidor HTTP.
- Não busca nada na internet (CSS, fontes, scripts ou CDNs).
- Os únicos acessos externos opcionais são as chamadas explícitas ao Jettax
  (login assistido e leitura dos clientes), nunca automáticas.
- Tudo roda numa janela desktop única, com navegação lateral, dashboard,
  certificados, lotes manuais, relatórios, configuração e log em tempo real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import Canvas, filedialog, messagebox, ttk

import customtkinter as ctk

from cajuru_a1.cnpjutil import format_cnpj
from cajuru_a1.config import (
    _blank,
    effective_config,
    get_output_dir,
    load_config,
    save_config,
    validate_config,
)
from cajuru_a1.matcher import match_all
from cajuru_a1.models import JetaxClient, PipelineResult
from cajuru_a1.pipeline import analyze, enviar, finish, refresh_stats
from cajuru_a1.reattempt import reattempt_locked
from cajuru_a1.report import write_excel_report, write_html_report

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ---------------------------------------------------------------- Design tokens
C = {
    "bg": "#0E1116",
    "surface": "#151A22",
    "surface2": "#1B222D",
    "surface3": "#222B38",
    "border": "#27303E",
    "border_soft": "#1C232E",
    "text": "#E9ECF2",
    "text_muted": "#8B93A3",
    "text_faint": "#616B7B",
    "accent": "#5B7DF0",
    "accent_hover": "#6B8BFF",
    "accent_soft": "#202B45",
    "ok": "#2FA968",
    "ok_soft": "#183426",
    "warn": "#D3941F",
    "warn_soft": "#3A2C13",
    "danger": "#DA5257",
    "danger_soft": "#3A1D20",
    "review": "#8B6FE0",
    "review_soft": "#2C2341",
    "neutral": "#7C8494",
    "neutral_soft": "#252B34",
}

STATUS_COLOR = {
    "pronto": "#2FA968",
    "sem_senha": "#D3941F",
    "vencido": "#DA5257",
    "nao_valido": "#8B6FE0",
    "invalido": "#DA5257",
    "conflito": "#DA5257",
    "ambiguo": "#8B6FE0",
    "revisao_manual": "#8B6FE0",
    "substituido": "#7C8494",
    "duplicado": "#7C8494",
    "sem_cert": "#7C8494",
    "sem_cert_novo": "#7C8494",
    "extra_pfx": "#7C8494",
}

STATUS_LABEL = {
    "pronto": "PRONTO",
    "vencido": "VENCIDO",
    "nao_valido": "AINDA NAO VALIDO",
    "sem_senha": "SEM SENHA",
    "invalido": "INVALIDO",
    "conflito": "CONFLITO",
    "ambiguo": "AMBIGUO",
    "revisao_manual": "REVISAO MANUAL",
    "substituido": "SUBSTITUIDO",
    "duplicado": "DUPLICADO",
    "sem_cert": "SEM PFX",
    "sem_cert_novo": "SEM PFX NOVO",
    "extra_pfx": "PFX SEM CLIENTE",
}

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"

HEALTH_ORDER = [
    "pronto", "substituido", "revisao_manual", "ambiguo", "sem_senha",
    "nao_valido", "vencido", "invalido", "conflito", "duplicado",
    "extra_pfx", "sem_cert", "sem_cert_novo",
]


def _now_stamp() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _open_path(path: Path) -> None:
    path = Path(path).expanduser()
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        messagebox.showwarning("Abrir pasta", f"Não foi possível abrir:\n{path}")


def _health_segments(stats: dict | None) -> list[dict]:
    stats = stats or {}
    total = sum(int(stats.get(key, 0) or 0) for key in HEALTH_ORDER)
    if total <= 0:
        return []
    out = []
    for key in HEALTH_ORDER:
        count = int(stats.get(key, 0) or 0)
        if count <= 0:
            continue
        out.append({
            "key": key,
            "label": STATUS_LABEL.get(key, key),
            "color": STATUS_COLOR.get(key, C["neutral"]),
            "count": count,
            "pct": round(count / total * 100, 1),
        })
    return out


def _list_reports(output: Path) -> list[dict]:
    if not output.exists():
        return []
    names = [
        "relatorio.html", "relatorio.xlsx",
        "diagnostico.html", "diagnostico.xlsx",
        "auditoria_ultima_execucao.json",
    ]
    reports = []
    for name in names:
        path = output / name
        if path.is_file():
            st = path.stat()
            reports.append({
                "name": name,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M"),
            })
    return reports


def _list_bundles(output: Path) -> list[dict]:
    base = output / "lotes"
    if not base.exists():
        return []
    items = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        files = {}
        for name in (
            "certificados_jettax.zip",
            "planilha_importacao_jettax.xlsx",
            "senhas_para_preenchimento_manual.csv",
            "LEIA-ME.txt",
        ):
            p = d / name
            if p.is_file():
                files[name] = round(p.stat().st_size / 1024, 1)
        if files:
            items.append({
                "name": d.name,
                "path": str(d),
                "files": files,
                "mtime": datetime.fromtimestamp(d.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
            })
    return items


class App(ctk.CTk):
    """Janela principal do painel desktop Cajuru A1."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Cajuru A1 · Certificados Jettax 360")
        self.geometry("1380x860")
        self.minsize(1180, 720)

        try:
            self.cfg = load_config()
            self._config_error = ""
        except Exception as exc:  # noqa: BLE001
            self.cfg = _blank()
            self._config_error = str(exc)

        self.result: PipelineResult | None = None
        self.clientes: list[JetaxClient] = []
        self.clientes_com: list[JetaxClient] = []
        self._busy = False
        self._current_view = "dashboard"
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._views: dict[str, ctk.CTkFrame] = {}
        self._log_boxes: list = []

        self.grid_columnconfigure(0, minsize=252)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_content()
        self._build_statusbar()
        self._show_view("dashboard")

        self._log("Pronto. Nenhum arquivo do Dropbox será alterado — somente leitura e cópia temporária.")
        if self._config_error:
            self._log("config.yaml tinha erro e foi ignorado. Use a tela Configuração para corrigir.")
        else:
            pasta = (self.cfg.get("dropbox") or {}).get("pasta") or ""
            if pasta:
                self._log(f"Pasta de certificados: {pasta}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- Layout
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=252, corner_radius=0, fg_color=C["surface"])
        bar.grid(row=0, column=0, sticky="nsew")
        bar.grid_propagate(False)

        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.pack(fill="x", padx=24, pady=(26, 20))
        logo = ctk.CTkFrame(brand, width=38, height=38, corner_radius=10, fg_color=C["accent"])
        logo.pack(side="left", padx=(0, 12))
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="A1", font=ctk.CTkFont(FONT_UI, 15, "bold"), text_color="#FFFFFF").pack(expand=True)

        title_frame = ctk.CTkFrame(brand, fg_color="transparent")
        title_frame.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title_frame, text="Cajuru A1", font=ctk.CTkFont(FONT_UI, 17, "bold"),
                     text_color=C["text"], anchor="w").pack(anchor="w")
        ctk.CTkLabel(title_frame, text="Auditoria e conciliação A1",
                     font=ctk.CTkFont(FONT_UI, 11), text_color=C["text_faint"], anchor="w").pack(anchor="w")

        ctk.CTkLabel(bar, text="PAINEL DE CONTROLE", font=ctk.CTkFont(FONT_UI, 10, "bold"),
                     text_color=C["text_faint"], anchor="w").pack(fill="x", padx=24, pady=(0, 4))

        navs = [
            ("dashboard", "Dashboard"),
            ("certificados", "Certificados"),
            ("lotes", "Lotes manuais"),
            ("relatorios", "Relatórios"),
            ("config", "Configuração"),
            ("log", "Log"),
        ]
        for key, label in navs:
            button = ctk.CTkButton(
                bar,
                text=f"   {label}",
                command=lambda k=key: self._show_view(k),
                fg_color="transparent",
                hover_color=C["surface2"],
                text_color=C["text_muted"],
                anchor="w",
                height=42,
                corner_radius=10,
                font=ctk.CTkFont(FONT_UI, 13, "bold"),
                border_width=1,
                border_color=C["border_soft"],
            )
            button.pack(fill="x", padx=16, pady=3)
            self._nav_buttons[key] = button

        spacer = ctk.CTkFrame(bar, height=10, fg_color="transparent")
        spacer.pack(fill="x", expand=True)

        note = ctk.CTkFrame(bar, fg_color="transparent")
        note.pack(side="bottom", fill="x", padx=20, pady=(0, 18))
        ctk.CTkLabel(
            note,
            text="SCAN LOCAL\nSomente leitura no Dropbox.\nNenhuma senha é gravada em\nrelatório, log ou banco.",
            font=ctk.CTkFont(FONT_UI, 10),
            text_color=C["text_faint"],
            justify="left",
            anchor="w",
        ).pack(fill="x")

    def _build_content(self) -> None:
        container = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        container.grid(row=0, column=1, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1)

        self.header = ctk.CTkFrame(container, height=78, fg_color=C["surface"], corner_radius=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.grid_propagate(False)
        self.header.grid_columnconfigure(0, weight=1)
        self.header.grid_columnconfigure(1, weight=0)

        self.title_label = ctk.CTkLabel(self.header, text="Dashboard", font=ctk.CTkFont(FONT_UI, 19, "bold"),
                                        text_color=C["text"], anchor="w")
        self.title_label.grid(row=0, column=0, rowspan=2, sticky="w", padx=28, pady=8)
        self.subtitle_label = ctk.CTkLabel(self.header, text="Visão geral da auditoria de certificados A1",
                                           font=ctk.CTkFont(FONT_UI, 12), text_color=C["text_muted"], anchor="w")
        self.subtitle_label.grid(row=1, column=0, sticky="w", padx=28, pady=(0, 12))

        self.status_pill = ctk.CTkLabel(
            self.header, text="●  AGUARDANDO", font=ctk.CTkFont(FONT_UI, 11, "bold"),
            text_color=C["warn"], fg_color=C["warn_soft"], corner_radius=99,
        )
        self.status_pill.grid(row=0, column=1, sticky="e", padx=28, pady=(16, 4))

        self.progress = ctk.CTkProgressBar(container, fg_color=C["surface2"], progress_color=C["accent"], height=6)
        self.progress.grid(row=2, column=0, sticky="ew")
        self.progress.set(0)

        self.body = ctk.CTkFrame(container, fg_color=C["bg"], corner_radius=0)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        self._build_dashboard()
        self._build_certificates()
        self._build_bundles()
        self._build_reports()
        self._build_config()
        self._build_log()

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=30, fg_color=C["surface"], corner_radius=0)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        self.status_text = ctk.CTkLabel(
            bar, text="Sem tarefa em andamento", font=ctk.CTkFont(FONT_UI, 10),
            text_color=C["text_muted"], anchor="w",
        )
        self.status_text.pack(side="left", padx=18)
        self.clock_label = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(FONT_UI, 10),
                                        text_color=C["text_faint"])
        self.clock_label.pack(side="right", padx=18)
        self._tick_clock()

    def _tick_clock(self) -> None:
        try:
            if not self.winfo_exists():
                return
            self.clock_label.configure(text=datetime.now().strftime("%d/%m/%Y  %H:%M:%S"))
            self.after(1000, self._tick_clock)
        except Exception:
            pass

    # ---------------------------------------------------------- Navigation
    def _show_view(self, key: str) -> None:
        self._current_view = key
        titles = {
            "dashboard": ("Dashboard", "Visão geral da auditoria de certificados A1"),
            "certificados": ("Certificados", "Resultado da última leitura e conciliação"),
            "lotes": ("Lotes manuais", "ZIP + planilha + CSV de senhas prontos para importação"),
            "relatorios": ("Relatórios", "Arquivos gerados na pasta de saída local"),
            "config": ("Configuração", "Origem, planilhas e opções de segurança"),
            "log": ("Log", "Eventos da execução atual"),
        }
        title, subtitle = titles.get(key, ("", ""))
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)

        for view in self._views.values():
            view.pack_forget()
        if key == "dashboard":
            self._views[key].pack(fill="both", expand=True)
        else:
            self._views[key].pack(fill="both", expand=True)

        for nav_key, btn in self._nav_buttons.items():
            active = nav_key == key
            btn.configure(
                fg_color=C["accent_soft"] if active else "transparent",
                text_color="#FFFFFF" if active else C["text_muted"],
                border_color=C["border"] if active else C["border_soft"],
            )

    # ------------------------------------------------------- Dashboard view
    def _build_dashboard(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color="transparent")
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(0, weight=1)
        self._views["dashboard"] = view

        canvas = ctk.CTkScrollableFrame(view, fg_color=C["bg"], corner_radius=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        canvas.grid_columnconfigure(0, weight=1)

        self.kpi_cards: dict[str, ctk.CTkLabel] = {}
        cards = ctk.CTkFrame(canvas, fg_color="transparent")
        cards.pack(fill="x", padx=16, pady=(18, 8))
        
        # Configure 3 columns for 2x3 card grid
        for idx in range(3):
            cards.grid_columnconfigure(idx, weight=1)

        kpis = [
            ("pfx", "PFX Lidos", "arquivos .pfx/.p12 localizados", C["accent"]),
            ("pfx_abertos", "Abertos", "certificados válidos com senha", C["ok"]),
            ("pronto", "Prontos para Lote", "conciliados com sucesso", C["ok"]),
            ("revisao_manual", "Revisão Manual", "precisam de atenção/ajuste", C["review"]),
            ("sem_senha", "Sem Senha", "senha não encontrada/inválida", C["warn"]),
            ("clientes_sem", "Sem A1 no Jettax", "empresas pendentes no Jettax", C["neutral"]),
        ]
        
        for idx, (key, title, hint, color) in enumerate(kpis):
            row = idx // 3
            col = idx % 3
            card = ctk.CTkFrame(cards, fg_color=C["surface"], corner_radius=16, border_width=1,
                                border_color=C["border"])
            card.grid(row=row, column=col, sticky="nsew", padx=8, pady=8)
            
            ctk.CTkLabel(card, text=title.upper(), font=ctk.CTkFont(FONT_UI, 11, "bold"),
                         text_color=C["text_muted"], anchor="w").pack(padx=20, pady=(18, 0), anchor="w")
            value = ctk.CTkLabel(card, text="—", font=ctk.CTkFont(FONT_UI, 36, "bold"),
                                 text_color=color, anchor="w")
            value.pack(padx=20, pady=(4, 0), anchor="w")
            self.kpi_cards[key] = value
            ctk.CTkLabel(card, text=hint, font=ctk.CTkFont(FONT_UI, 11), text_color=C["text_faint"],
                         anchor="w").pack(padx=20, pady=(2, 18), anchor="w")

        health = ctk.CTkFrame(canvas, fg_color=C["surface"], corner_radius=14, border_width=1,
                              border_color=C["border"])
        health.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(health, text="SAÚDE DA PASTA & CONCILIAÇÃO", font=ctk.CTkFont(FONT_UI, 11, "bold"),
                     text_color=C["text_muted"], anchor="w").pack(padx=18, pady=(16, 4))
        self.health_canvas = Canvas(health, height=16, bg=C["surface"], highlightthickness=0, borderwidth=0)
        self.health_canvas.pack(fill="x", padx=18, pady=8)
        self.health_legend = ctk.CTkLabel(health, text="Sem dados ainda — rode uma análise para ver o resumo.",
                                          font=ctk.CTkFont(FONT_UI, 11), text_color=C["text_faint"], anchor="w")
        self.health_legend.pack(fill="x", padx=18, pady=(0, 16))
        self.health_canvas.bind("<Configure>", lambda _e: self._draw_health())

        # Express local extraction (User's special request!)
        quick_card = ctk.CTkFrame(canvas, fg_color=C["accent_soft"], corner_radius=16, border_width=1,
                                  border_color=C["accent"])
        quick_card.pack(fill="x", padx=16, pady=12)
        quick_card.grid_columnconfigure(0, weight=1)
        quick_card.grid_columnconfigure(1, weight=0)
        
        lbl_frame = ctk.CTkFrame(quick_card, fg_color="transparent")
        lbl_frame.grid(row=0, column=0, sticky="w", padx=24, pady=18)
        
        ctk.CTkLabel(lbl_frame, text="⚡ EXTRAÇÃO EXPRESSA OFF-LINE (SEM JETTAX)", font=ctk.CTkFont(FONT_UI, 14, "bold"),
                     text_color=C["text"], anchor="w").pack(anchor="w")
        ctk.CTkLabel(lbl_frame, text="Lê a pasta de certificados do Dropbox, testa todas as senhas locais e extrai tudo em uma pasta limpa.",
                     font=ctk.CTkFont(FONT_UI, 11), text_color=C["text_muted"], anchor="w").pack(anchor="w", pady=(2, 0))
        ctk.CTkLabel(lbl_frame, text="✓ 100% Off-line  ·  ✓ Sem abrir Jettax  ·  ✓ Salva um arquivo ZIP e a lista de senhas correspondente",
                     font=ctk.CTkFont(FONT_UI, 10, "bold"), text_color=C["ok"], anchor="w").pack(anchor="w", pady=(4, 0))
                     
        btn_quick = ctk.CTkButton(
            quick_card,
            text="Pegar Certificados + Senhas",
            command=self._run_export_all,
            width=240,
            height=46,
            corner_radius=10,
            fg_color=C["accent"],
            hover_color=C["accent_hover"],
            text_color="#FFFFFF",
            font=ctk.CTkFont(FONT_UI, 13, "bold")
        )
        btn_quick.grid(row=0, column=1, padx=24, pady=18, sticky="e")

        actions = ctk.CTkFrame(canvas, fg_color=C["surface"], corner_radius=14, border_width=1,
                               border_color=C["border"])
        actions.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(actions, text="INTEGRAÇÃO COM JETTAX (FLUXOS & PASSOS)", font=ctk.CTkFont(FONT_UI, 11, "bold"),
                     text_color=C["text_muted"], anchor="w").pack(padx=18, pady=(16, 6))
        
        grid = ctk.CTkFrame(actions, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(4, 16))
        
        for idx in range(3):
            grid.grid_columnconfigure(idx, weight=1)

        # Main flows row
        ctk.CTkLabel(grid, text="FLUXOS PRINCIPAIS:", font=ctk.CTkFont(FONT_UI, 10, "bold"),
                     text_color=C["text_faint"], anchor="w").grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 4))
        
        btn_full = ctk.CTkButton(grid, text="Executar Fluxo Completo Jettax", command=self._run_full, height=44, corner_radius=10,
                                 fg_color=C["accent_soft"], hover_color=C["accent"], text_color="#FFFFFF",
                                 border_width=1, border_color=C["accent"],
                                 font=ctk.CTkFont(FONT_UI, 12, "bold"))
        btn_full.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        
        btn_lote = ctk.CTkButton(grid, text="Gerar Lote de Importação Manual", command=self._run_manual_bundle, height=44, corner_radius=10,
                                 fg_color=C["warn_soft"], hover_color=C["warn"], text_color="#FFFFFF",
                                 border_width=1, border_color=C["warn"],
                                 font=ctk.CTkFont(FONT_UI, 12, "bold"))
        btn_lote.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        
        btn_envio = ctk.CTkButton(grid, text="Simular / Enviar ao Jettax", command=self._run_send, height=44, corner_radius=10,
                                  fg_color=C["danger_soft"], hover_color=C["danger"], text_color="#FFFFFF",
                                  border_width=1, border_color=C["danger"],
                                  font=ctk.CTkFont(FONT_UI, 12, "bold"))
        btn_envio.grid(row=1, column=2, sticky="ew", padx=6, pady=4)

        # Individual steps row
        ctk.CTkLabel(grid, text="PASSOS INDIVIDUAIS / DEPURAÇÃO:", font=ctk.CTkFont(FONT_UI, 10, "bold"),
                     text_color=C["text_faint"], anchor="w").grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(16, 4))

        btn_step1 = ctk.CTkButton(grid, text="Passo 1: Apenas Ler Dropbox", command=self._run_analyze, height=38, corner_radius=10,
                                  fg_color=C["surface3"], hover_color=C["surface2"], text_color=C["text"],
                                  border_width=1, border_color=C["border"],
                                  font=ctk.CTkFont(FONT_UI, 11, "bold"))
        btn_step1.grid(row=3, column=0, sticky="ew", padx=6, pady=4)
        
        btn_step2 = ctk.CTkButton(grid, text="Passo 2: Buscar Clientes no Jettax", command=self._run_jettax, height=38, corner_radius=10,
                                  fg_color=C["surface3"], hover_color=C["surface2"], text_color=C["text"],
                                  border_width=1, border_color=C["border"],
                                  font=ctk.CTkFont(FONT_UI, 11, "bold"))
        btn_step2.grid(row=3, column=1, sticky="ew", padx=6, pady=4)
        
        btn_step3 = ctk.CTkButton(grid, text="Passo 3: Executar Conciliação Cruzada", command=self._run_match, height=38, corner_radius=10,
                                  fg_color=C["surface3"], hover_color=C["surface2"], text_color=C["text"],
                                  border_width=1, border_color=C["border"],
                                  font=ctk.CTkFont(FONT_UI, 11, "bold"))
        btn_step3.grid(row=3, column=2, sticky="ew", padx=6, pady=4)

        self.dashboard_note = ctk.CTkLabel(
            canvas,
            text="O Dropbox é tratado como origem somente leitura. O programa não envia nada ao Jettax "
                 "automaticamente; ele concilia e prepara o lote para você importar.",
            font=ctk.CTkFont(FONT_UI, 11),
            text_color=C["text_muted"],
            anchor="w",
            justify="left",
        )
        self.dashboard_note.pack(fill="x", padx=18, pady=(0, 18))

    def _draw_health(self) -> None:
        if not hasattr(self, "health_canvas"):
            return
        canvas = self.health_canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        if width < 20:
            return
        cfg = self.cfg or {}
        output = get_output_dir(cfg)
        last = output / "auditoria_ultima_execucao.json"
        stats = {}
        try:
            import json
            if last.exists():
                stats = json.loads(last.read_text(encoding="utf-8")).get("stats") or {}
        except Exception:
            stats = {}
        if self.result and self.result.stats:
            stats = self.result.stats
        segments = _health_segments(stats)
        if not segments:
            canvas.create_rectangle(0, 4, width, 12, fill=C["surface2"], outline="")
            return
        x = 0.0
        gap = 2
        usable = max(1, width - gap * (len(segments) - 1))
        for seg in segments:
            w = max(3.0, usable * seg["pct"] / 100.0)
            canvas.create_rectangle(x, 4, x + w, 12, fill=seg["color"], outline="")
            x += w + gap
        self.health_legend.configure(
            text="   ".join(
                f"{seg['label']} {seg['count']}  ({seg['pct']:.0f}%)"
                for seg in segments
            ) or "Sem dados"
        )

    def _update_dashboard_stats(self) -> None:
        stats = (self.result.stats if self.result else {}) or {}
        mapping = {
            "pfx": "pfx", "pfx_abertos": "pfx_abertos", "pronto": "pronto",
            "revisao_manual": "revisao_manual", "sem_senha": "sem_senha",
            "clientes_sem": "clientes_sem",
        }
        for key, kpi in mapping.items():
            label = self.kpi_cards.get(kpi)
            if label:
                label.configure(text=str(int(stats.get(key, 0) or 0)))
        if hasattr(self, "health_canvas"):
            self._draw_health()

    # -------------------------------------------------- Certificates view
    def _build_certificates(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color=C["bg"], corner_radius=0)
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)
        view.grid_rowconfigure(2, weight=0)
        self._views["certificados"] = view

        top = ctk.CTkFrame(view, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        self.certs_summary = ctk.CTkLabel(top, text="Nenhuma análise ainda.", font=ctk.CTkFont(FONT_UI, 12),
                                          text_color=C["text_muted"], anchor="w")
        self.certs_summary.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(top, text="Analisar agora", command=self._run_analyze, width=150, height=38,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      font=ctk.CTkFont(FONT_UI, 12, "bold")).pack(side="right")

        table_frame = ctk.CTkFrame(view, fg_color=C["surface"], corner_radius=14, border_width=1,
                                   border_color=C["border_soft"])
        table_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        self._configure_tree_style()
        columns = ("status", "cnpj", "empresa", "arquivo", "motivo")
        self.cert_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=14)
        for col, heading, width in (
            ("status", "STATUS", 130),
            ("cnpj", "CNPJ", 170),
            ("empresa", "EMPRESA", 300),
            ("arquivo", "ARQUIVO", 240),
            ("motivo", "MOTIVO", 360),
        ):
            self.cert_tree.heading(col, text=heading)
            self.cert_tree.column(col, width=width, minwidth=70, anchor="w")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.cert_tree.yview)
        self.cert_tree.configure(yscrollcommand=vsb.set)
        self.cert_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        for key in STATUS_COLOR:
            self.cert_tree.tag_configure(key, foreground=STATUS_COLOR.get(key, C["text"]),
                                         background=C["surface2"])
        self.cert_tree.tag_configure("even", background=C["surface"])
        self.cert_tree.tag_configure("odd", background=C["surface2"])

        bottom = ctk.CTkFrame(view, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        self.certs_count = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(FONT_UI, 11),
                                        text_color=C["text_faint"], anchor="w")
        self.certs_count.pack(side="left")

    def _configure_tree_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview",
            background=C["surface2"],
            fieldbackground=C["surface2"],
            foreground=C["text"],
            borderwidth=0,
            rowheight=30,
            font=(FONT_UI, 11),
        )
        style.configure(
            "Treeview.Heading",
            background=C["surface"],
            foreground=C["text_faint"],
            relief="flat",
            font=(FONT_UI, 10, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", C["surface3"])],
        )
        style.map(
            "Treeview",
            background=[("selected", C["accent_soft"])],
            foreground=[("selected", "#FFFFFF")],
        )

    def _refresh_certificates(self) -> None:
        for item in self.cert_tree.get_children():
            self.cert_tree.delete(item)
        if not self.result:
            self.certs_summary.configure(text="Nenhuma análise ainda.")
            self.certs_count.configure(text="")
            return
        stats = self.result.stats or {}
        self.certs_summary.configure(
            text=f"    {stats.get('pfx', 0)} PFX   ·   {stats.get('pfx_abertos', 0)} abertos   ·   "
                 f"{stats.get('pronto', 0)} prontos   ·   {stats.get('revisao_manual', 0)} em revisão"
        )
        rows = []
        if self.result.matches:
            for i, m in enumerate(self.result.matches):
                empresa = (m.cliente.razao_social if m.cliente else None) or "—"
                cnpj = format_cnpj(m.cliente.cnpj) if m.cliente is not None and getattr(m.cliente, "cnpj", None) else "—"
                arq = (m.cert.filename if m.cert else None) or "—"
                rows.append((i, m.status, STATUS_LABEL.get(m.status, m.status.upper()), cnpj, empresa, arq, m.motivo))
        else:
            for i, c in enumerate(self.result.certificados):
                flag = "ABERTO" if c.opened else "SEM SENHA"
                status_key = "pronto" if c.opened else "sem_senha"
                if c.expired:
                    flag = "VENCIDO"
                    status_key = "vencido"
                rows.append((i, status_key, flag, format_cnpj(c.cnpj) if c.cnpj else "—", "", c.filename[:48],
                             c.password_source or c.error or ""))
        for idx, (row_idx, status_key, status, cnpj, empresa, arq, motivo) in enumerate(rows):
            row_tag = "odd" if row_idx % 2 else "even"
            self.cert_tree.insert("", "end", tags=(status_key, row_tag),
                                  values=(status, cnpj, empresa, arq, motivo))
        self.certs_count.configure(text=f"{len(rows)} registro(s)")

    # -------------------------------------------------------- Bundles view
    def _build_bundles(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color=C["bg"], corner_radius=0)
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)
        self._views["lotes"] = view

        top = ctk.CTkFrame(view, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        ctk.CTkLabel(top, text="Lotes manuais gerados. A importação no Jettax é sempre feita por você.",
                     font=ctk.CTkFont(FONT_UI, 12), text_color=C["text_muted"], anchor="w").pack(side="left", fill="x", expand=True)
        ctk.CTkButton(top, text="Gerar lote manual", command=self._run_manual_bundle, width=170, height=38,
                      fg_color=C["warn"], hover_color=C["warn"], text_color="#1A1300",
                      font=ctk.CTkFont(FONT_UI, 12, "bold")).pack(side="right")

        self.bundles_frame = ctk.CTkScrollableFrame(view, fg_color=C["bg"], corner_radius=0)
        self.bundles_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self._refresh_bundles()

    def _refresh_bundles(self) -> None:
        for child in self.bundles_frame.winfo_children():
            child.destroy()
        output = get_output_dir(self.cfg)
        bundles = _list_bundles(output)
        if not bundles:
            ctk.CTkLabel(self.bundles_frame, text="Nenhum lote manual gerado ainda.",
                         font=ctk.CTkFont(FONT_UI, 12), text_color=C["text_faint"]).pack(pady=20)
            return
        for bundle in bundles:
            card = ctk.CTkFrame(self.bundles_frame, fg_color=C["surface"], corner_radius=12,
                                border_width=1, border_color=C["border_soft"])
            card.pack(fill="x", pady=6)
            info = ctk.CTkFrame(card, fg_color="transparent")
            info.pack(side="left", fill="both", expand=True, padx=16, pady=12)
            ctk.CTkLabel(info, text=bundle["name"], font=ctk.CTkFont(FONT_UI, 13, "bold"),
                         text_color=C["text"], anchor="w").pack(anchor="w")
            file_desc = "   ·   ".join(f"{name}: {kb} KB" for name, kb in bundle["files"].items())
            ctk.CTkLabel(info, text=file_desc, font=ctk.CTkFont(FONT_UI, 11),
                         text_color=C["text_muted"], anchor="w").pack(anchor="w", pady=(2, 0))
            ctk.CTkLabel(info, text=bundle["mtime"], font=ctk.CTkFont(FONT_UI, 10),
                         text_color=C["text_faint"], anchor="w").pack(anchor="w", pady=(2, 0))
            b1 = ctk.CTkButton(card, text="Abrir pasta", command=lambda b=bundle: _open_path(Path(b["path"])),
                               width=110, height=34, fg_color=C["surface3"], hover_color=C["surface3"],
                               font=ctk.CTkFont(FONT_UI, 11, "bold"))
            b1.pack(side="right", padx=(6, 16), pady=12)

    # ------------------------------------------------------- Reports view
    def _build_reports(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color=C["bg"], corner_radius=0)
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)
        self._views["relatorios"] = view

        top = ctk.CTkFrame(view, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        ctk.CTkLabel(top, text="Relatórios e diagnósticos da última execução.",
                     font=ctk.CTkFont(FONT_UI, 12), text_color=C["text_muted"], anchor="w").pack(side="left", fill="x", expand=True)
        ctk.CTkButton(top, text="Abrir pasta", command=self._open_output, width=140, height=38,
                      fg_color=C["surface3"], hover_color=C["surface3"],
                      font=ctk.CTkFont(FONT_UI, 12, "bold")).pack(side="right")

        self.reports_frame = ctk.CTkScrollableFrame(view, fg_color=C["bg"], corner_radius=0)
        self.reports_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self._refresh_reports()

    def _refresh_reports(self) -> None:
        for child in self.reports_frame.winfo_children():
            child.destroy()
        output = get_output_dir(self.cfg)
        reports = _list_reports(output)
        if not reports:
            ctk.CTkLabel(self.reports_frame, text="Nenhum relatório encontrado. Rode uma análise.",
                         font=ctk.CTkFont(FONT_UI, 12), text_color=C["text_faint"]).pack(pady=20)
            return
        for report in reports:
            card = ctk.CTkFrame(self.reports_frame, fg_color=C["surface"], corner_radius=12,
                                border_width=1, border_color=C["border_soft"])
            card.pack(fill="x", pady=6)
            info = ctk.CTkFrame(card, fg_color="transparent")
            info.pack(side="left", fill="both", expand=True, padx=16, pady=12)
            ctk.CTkLabel(info, text=report["name"], font=ctk.CTkFont(FONT_UI, 13, "bold"),
                         text_color=C["text"], anchor="w").pack(anchor="w")
            ctk.CTkLabel(info, text=f"{report['size_kb']} KB   ·   {report['mtime']}",
                         font=ctk.CTkFont(FONT_UI, 11), text_color=C["text_muted"], anchor="w").pack(anchor="w", pady=(2, 0))
            ctk.CTkButton(card, text="Abrir", command=lambda r=report: _open_path(output / r["name"]),
                          width=80, height=32, fg_color=C["accent"], hover_color=C["accent_hover"],
                          font=ctk.CTkFont(FONT_UI, 11, "bold")).pack(side="right", padx=(6, 16), pady=12)

    # --------------------------------------------------------- Config view
    def _build_config(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color=C["bg"], corner_radius=0)
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_columnconfigure(1, weight=0)
        view.grid_rowconfigure(0, weight=1)
        self._views["config"] = view

        frame = ctk.CTkScrollableFrame(view, fg_color=C["bg"], corner_radius=0)
        frame.grid(row=0, column=0, sticky="nsew", padx=24, pady=18)

        paths = ctk.CTkFrame(frame, fg_color=C["surface"], corner_radius=14, border_width=1,
                             border_color=C["border_soft"])
        paths.pack(fill="x", pady=8)
        ctk.CTkLabel(paths, text="CAMINHOS", font=ctk.CTkFont(FONT_UI, 11, "bold"),
                     text_color=C["text_muted"], anchor="w").pack(padx=18, pady=(16, 4))

        self.var_drop = ctk.StringVar(value=(self.cfg.get("dropbox") or {}).get("pasta") or "")
        self.var_xlsx1 = ctk.StringVar(value=_nth((self.cfg.get("excel") or {}).get("arquivos"), 0))
        self.var_xlsx2 = ctk.StringVar(value=_nth((self.cfg.get("excel") or {}).get("arquivos"), 1))
        self.var_url = ctk.StringVar(value=(self.cfg.get("jettax") or {}).get("url")
                                     or "https://admin.jettax360.com.br")

        self._config_row(paths, "Pasta CERTIFICADOS A1 (Dropbox)", self.var_drop, self._pick_dir)
        self._config_row(paths, "Planilha de senhas 1", self.var_xlsx1, self._pick_xlsx)
        self._config_row(paths, "Planilha de senhas 2", self.var_xlsx2, self._pick_xlsx)
        self._config_row(paths, "URL do Jettax", self.var_url, None)

        opts = self.cfg.get("opcoes") or {}
        self.var_dry = ctk.BooleanVar(value=bool(opts.get("dry_run", True)))
        self.var_lote = ctk.BooleanVar(value=(opts.get("modo_envio") or "lote") != "individual")
        self.var_atualizar_todas = ctk.BooleanVar(value=bool(opts.get("atualizar_todas_empresas", False)))
        self.var_mais_novo = ctk.BooleanVar(value=bool(opts.get("escolher_certificado_mais_novo", True)))
        self.var_senha_manual = ctk.BooleanVar(value=bool(opts.get("lote_senha_manual", True)))
        self.var_csv_senhas = ctk.BooleanVar(value=bool(opts.get("salvar_senhas_csv", True)))
        self.var_senhas_comuns = ctk.BooleanVar(value=bool(opts.get("tentar_senhas_comuns", False)))
        self.var_varredura_global = ctk.BooleanVar(value=bool(opts.get("tentar_todas_senhas_da_planilha", False)))

        security = ctk.CTkFrame(frame, fg_color=C["surface"], corner_radius=14, border_width=1,
                                border_color=C["border_soft"])
        security.pack(fill="x", pady=8)
        ctk.CTkLabel(security, text="OPÇÕES DE EXECUÇÃO", font=ctk.CTkFont(FONT_UI, 11, "bold"),
                     text_color=C["text_muted"], anchor="w").pack(padx=18, pady=(16, 4))
        checks = [
            ("Modo simulação (não grava nada no Jettax) — mantenha na 1ª vez", self.var_dry),
            ("Importar em LOTE (recomendado)", self.var_lote),
            ("Escolher o certificado mais novo quando houver 2 PFX", self.var_mais_novo),
            ("Atualizar/renovar empresas que já possuem A1", self.var_atualizar_todas),
            ("Lote manual com senha em branco", self.var_senha_manual),
            ("Salvar CSV de senhas junto ao lote", self.var_csv_senhas),
            ("Aceitar marcas comuns + ano como candidatas", self.var_senhas_comuns),
        ]
        for text, var in checks:
            ctk.CTkCheckBox(security, text=text, variable=var, text_color=C["text_muted"],
                            fg_color=C["accent"], hover_color=C["accent_hover"],
                            font=ctk.CTkFont(FONT_UI, 12)).pack(fill="x", padx=18, pady=5)

        ctk.CTkCheckBox(security, text="Varredura global de todas as senhas da planilha (arriscado, desativa padrão)",
                        variable=self.var_varredura_global, text_color=C["warn"],
                        fg_color=C["accent"], hover_color=C["accent_hover"],
                        font=ctk.CTkFont(FONT_UI, 12)).pack(fill="x", padx=18, pady=(5, 15))

        bottom = ctk.CTkFrame(view, fg_color="transparent")
        bottom.grid(row=0, column=1, sticky="ns", padx=(0, 24), pady=18)
        ctk.CTkButton(bottom, text="Salvar configuração", command=self._save_cfg, width=190, height=42,
                      fg_color=C["accent"], hover_color=C["accent_hover"],
                      font=ctk.CTkFont(FONT_UI, 12, "bold")).pack(pady=(0, 10))
        ctk.CTkButton(bottom, text="Abrir pasta de saída", command=self._open_output, width=190, height=42,
                      fg_color=C["surface3"], hover_color=C["surface3"],
                      font=ctk.CTkFont(FONT_UI, 12, "bold")).pack()

    def _config_row(self, parent, label, var, picker) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(row, text=label, width=250, anchor="w", font=ctk.CTkFont(FONT_UI, 12),
                     text_color=C["text_muted"]).pack(side="left")
        entry = ctk.CTkEntry(row, textvariable=var, font=ctk.CTkFont(FONT_UI, 12),
                             fg_color=C["surface2"], border_color=C["border"], height=36)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        if picker:
            ctk.CTkButton(row, text="…", width=38, height=36,
                          command=lambda v=var: picker(v),
                          fg_color=C["surface3"], hover_color=C["surface3"]).pack(side="right")

    def _pick_dir(self, var) -> None:
        p = filedialog.askdirectory(title="Selecione diretamente a pasta CERTIFICADOS ou CERTIFICADOS A1")
        if not p:
            return
        selected = Path(p)
        if not selected.name.strip().casefold().startswith("certificados"):
            messagebox.showwarning(
                "Escopo inválido",
                "Não selecione a raiz do Dropbox nem uma pasta ancestral.\n\n"
                "Abra o Dropbox e selecione diretamente a pasta CERTIFICADOS ou CERTIFICADOS A1.",
            )
            self._log("Seleção recusada: a pasta direta CERTIFICADOS/CERTIFICADOS A1 é obrigatória.")
            return
        var.set(p)

    def _pick_xlsx(self, var) -> None:
        p = filedialog.askopenfilename(title="Planilha de senhas",
                                       filetypes=[("Excel", "*.xlsx *.xlsm"), ("Todos", "*.*")])
        if p:
            var.set(p)

    def _sync_cfg(self) -> None:
        self.cfg.setdefault("dropbox", {})["pasta"] = self.var_drop.get().strip()
        arquivos = [p for p in (self.var_xlsx1.get().strip(), self.var_xlsx2.get().strip()) if p]
        self.cfg.setdefault("excel", {})["arquivos"] = arquivos
        self.cfg.setdefault("jettax", {})["url"] = self.var_url.get().strip() or "https://admin.jettax360.com.br"
        self.cfg.setdefault("opcoes", {})["dry_run"] = bool(self.var_dry.get())
        self.cfg.setdefault("opcoes", {})["modo_envio"] = "lote" if self.var_lote.get() else "individual"
        self.cfg.setdefault("opcoes", {})["atualizar_todas_empresas"] = bool(self.var_atualizar_todas.get())
        self.cfg.setdefault("opcoes", {})["escolher_certificado_mais_novo"] = bool(self.var_mais_novo.get())
        self.cfg.setdefault("opcoes", {})["lote_senha_manual"] = bool(self.var_senha_manual.get())
        self.cfg.setdefault("opcoes", {})["salvar_senhas_csv"] = bool(self.var_csv_senhas.get())
        self.cfg.setdefault("opcoes", {})["tentar_senhas_comuns"] = bool(self.var_senhas_comuns.get())
        varredura = bool(self.var_varredura_global.get())
        self.cfg.setdefault("opcoes", {})["tentar_todas_senhas_da_planilha"] = varredura
        self.cfg.setdefault("seguranca", {})["permitir_varredura_global"] = varredura
        self.cfg.setdefault("dropbox", {})["somente_leitura"] = True

    def _save_cfg(self) -> None:
        self._sync_cfg()
        errors = validate_config(effective_config(self.cfg))
        if errors:
            messagebox.showwarning("Configuração incompleta", "\n".join(errors))
            self._log("Configuração ainda possui pendências: " + " | ".join(errors))
            return
        save_config(self.cfg)
        self._log("config.yaml salvo e validado.")

    def _open_output(self) -> None:
        self._sync_cfg()
        out = get_output_dir(self.cfg)
        out.mkdir(parents=True, exist_ok=True)
        _open_path(out)

    # ------------------------------------------------------------- Log view
    def _build_log(self) -> None:
        view = ctk.CTkFrame(self.body, fg_color=C["bg"], corner_radius=0)
        view.pack(fill="both", expand=True)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(0, weight=1)
        self._views["log"] = view

        box = ctk.CTkTextbox(view, font=ctk.CTkFont(FONT_MONO, 12), fg_color=C["surface"],
                             text_color=C["text"], border_color=C["border_soft"],
                             border_width=1, corner_radius=12)
        box.pack(fill="both", expand=True, padx=24, pady=18)
        self._log_boxes.append(box)

    # ------------------------------------------------------------- Utilities
    def _busy_on(self) -> bool:
        if self._busy:
            messagebox.showinfo("Aguarde", "Já existe uma tarefa em andamento.")
            return False
        self._busy = True
        self.status_pill.configure(text="●  TRABALHANDO", text_color=C["accent"],
                                   fg_color=C["accent_soft"])
        self.status_text.configure(text="Executando tarefa…")
        self.progress.configure(mode="indeterminate")
        try:
            self.progress.start()
        except Exception:
            pass
        return True

    def _busy_off(self) -> None:
        self._busy = False
        self.status_pill.configure(text="●  AGUARDANDO", text_color=C["warn"], fg_color=C["warn_soft"])
        self.status_text.configure(text="Sem tarefa em andamento")
        try:
            self.progress.stop()
            self.progress.set(0)
        except Exception:
            pass

    def _thread(self, fn) -> None:
        if not self._busy_on():
            return

        def wrap() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                self.after(0, lambda e=exc, t=tb: self._show_error(e, t))
            finally:
                self.after(0, self._busy_off)

        threading.Thread(target=wrap, daemon=True).start()

    def _show_error(self, err: Exception, tb: str) -> None:
        self._log(tb)
        messagebox.showerror("Erro", f"{type(err).__name__}: {err}\n\nDetalhes em Log / pasta de saída.")

    def _log(self, msg: str) -> None:
        line = f"[{_now_stamp()}] {msg}"
        try:
            print(line)
        except Exception:
            pass
        try:
            for box in self._log_boxes:
                box.insert("end", str(msg) + "\n")
                box.see("end")
        except Exception:
            pass
        try:
            out = get_output_dir(self.cfg)
            out.mkdir(parents=True, exist_ok=True)
            with (out / "cajuru_a1.log").open("a", encoding="utf-8") as handle:
                handle.write(str(msg) + "\n")
        except Exception:
            pass

    def _wait_login_dialog(self) -> None:
        done = threading.Event()

        def ask() -> None:
            messagebox.showinfo(
                "Login no Jettax",
                "Na janela do Chrome, entre no Jettax 360.\n\n"
                "Quando aparecer a lista de CLIENTES, volte nesta tela e clique OK.",
            )
            done.set()

        self.after(0, ask)
        if not done.wait(timeout=900):
            raise TimeoutError("Login no Jettax não foi confirmado (15 min).")

    def _wait_import_dialog(self) -> None:
        done = threading.Event()
        answer = {"ok": False}

        def ask() -> None:
            answer["ok"] = messagebox.askyesno(
                "Confirmar importação",
                "Confira a janela do Jettax. O sistema exibiu que o lote foi recebido/importado com sucesso?\n\n"
                "Clique SIM somente após confirmar no Jettax. O arquivo transitório com senhas será apagado em seguida.",
            )
            done.set()

        self.after(0, ask)
        if not done.wait(timeout=1800):
            raise TimeoutError("Importação não foi confirmada em 30 minutos")
        if not answer["ok"]:
            raise RuntimeError("Usuário não confirmou a importação do lote")

    def after_log(self, msg: str):
        self.after(0, lambda: self._log(msg))

    def _after_log(self, msg: str):
        self.after(0, lambda: self._log(msg))

    def _resolve_clientes(self, say) -> tuple[list[JetaxClient], list[JetaxClient]]:
        """Retorna os clientes Jettax já carregados, ou os lista.

        Se a tela 'Buscar no Jettax' já foi usada nesta sessão, aproveita a
        lista para não abrir o Chrome de novo sem necessidade.
        """
        if self.clientes:
            say("Usando os clientes Jettax já carregados nesta sessão.")
            return list(self.clientes), list(self.clientes_com or [])

        from cajuru_a1.jettax import JettaxBot

        atualizar_todas = bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
        say("Abrindo o Jettax para listar os clientes reais (somente leitura)…")
        bot = JettaxBot(self.cfg, log_fn=say)
        clientes_sem: list[JetaxClient] = []
        clientes_com: list[JetaxClient] = []
        try:
            bot.start()
            bot.login(wait_fn=self._wait_login_dialog)
            if atualizar_todas:
                clientes_sem, clientes_com = bot.list_all_clients()
                say(f"Jettax: {len(clientes_sem)} sem A1, {len(clientes_com)} com A1.")
            else:
                clientes_sem = bot.list_without_certificate()
                say(f"Jettax: {len(clientes_sem)} empresa(s) sem certificado.")
        finally:
            bot.close()
        return clientes_sem, clientes_com

    # ------------------------------------------------------------- Operations
    def _run_full(self) -> None:
        self._sync_cfg()
        errors = validate_config(effective_config(self.cfg))
        if errors:
            messagebox.showwarning("Configuração", "\n".join(errors))
            return

        def job() -> None:
            from cajuru_a1.jettax import JettaxBot

            atualizar_todas = bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
            mais_novo = bool(self.cfg.get("opcoes", {}).get("escolher_certificado_mais_novo", True))
            say = lambda m: self.after_log(m)
            say("FLUXO COMPLETO — abrindo o Chrome do Jettax para leitura (sem gravar nada)…")
            bot = JettaxBot(self.cfg, log_fn=say)
            clientes_sem: list[JetaxClient] = []
            clientes_com: list[JetaxClient] = []
            try:
                bot.start()
                bot.login(wait_fn=self._wait_login_dialog)
                if atualizar_todas:
                    clientes_sem, clientes_com = bot.list_all_clients()
                    say(f"Jettax: {len(clientes_sem)} sem A1, {len(clientes_com)} já com A1 (renovação total).")
                else:
                    clientes_sem = bot.list_without_certificate()
                    say(f"Jettax: {len(clientes_sem)} empresa(s) sem certificado.")
            finally:
                bot.close()

            self.clientes = clientes_sem
            self.clientes_com = clientes_com
            say("FLUXO COMPLETO — lendo Dropbox e validando senhas…")
            result = analyze(self.cfg, log_fn=say, clientes_sem=clientes_sem, clientes_com=clientes_com)
            reattempt_locked(result, self.cfg, clientes_sem)
            result.clientes_sem = clientes_sem
            result.clientes_com = clientes_com
            result.matches = match_all(
                result.certificados, clientes_sem, clientes_com,
                atualizar_todos=atualizar_todas, escolher_mais_novo=mais_novo,
            )
            refresh_stats(result)
            self.result = result
            self._safe_reports(result)
            self.after(0, self._refresh_certificates)
            self.after(0, self._update_dashboard_stats)
            self.after(0, self._refresh_bundles)
            self.after(0, self._refresh_reports)
            say("FLUXO COMPLETO concluído. Confira relatório e diagnóstico antes de importar.")

        self._thread(job)

    def _run_analyze(self) -> None:
        self._sync_cfg()

        def job() -> None:
            say = lambda m: self.after_log(m)
            say("Iniciando leitura e auditoria do Dropbox (sem conectar ao Jettax)…")
            result = analyze(self.cfg, log_fn=say)
            self.result = result
            self._safe_reports(result)
            self.after(0, self._refresh_certificates)
            self.after(0, self._update_dashboard_stats)
            self.after(0, self._refresh_reports)
            say("Leitura concluída.")

        self._thread(job)

    def _run_jettax(self) -> None:
        self._sync_cfg()

        def job() -> None:
            from cajuru_a1.jettax import JettaxBot

            atualizar_todas = bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
            say = lambda m: self.after_log(m)
            say("Abrindo Jettax. Faça login se solicitado.")
            bot = JettaxBot(self.cfg, log_fn=say)
            clientes: list[JetaxClient] = []
            clientes_com: list[JetaxClient] = []
            try:
                bot.start()
                bot.login(wait_fn=self._wait_login_dialog)
                if atualizar_todas:
                    clientes, clientes_com = bot.list_all_clients()
                    say(f"{len(clientes)} sem A1, {len(clientes_com)} com A1 (renovação total).")
                else:
                    clientes = bot.list_without_certificate()
                    say(f"{len(clientes)} empresa(s) sem certificado.")
            except Exception:
                try:
                    out = get_output_dir(self.cfg)
                    out.mkdir(parents=True, exist_ok=True)
                    bot.screenshot(out / "erro_jettax.png")
                    say(f"Print salvo em {out / 'erro_jettax.png'}")
                except Exception:
                    pass
                raise
            finally:
                bot.close()
            self.clientes = clientes
            self.clientes_com = clientes_com
            say("Clientes carregados. Use Conciliar para gerar relatórios.")

        self._thread(job)

    def _run_match(self) -> None:
        if not self.result:
            messagebox.showwarning("Falta passo", "Leia o Dropbox antes de conciliar.")
            return
        if not self.clientes:
            messagebox.showwarning("Falta passo", "Busque os clientes sem certificado no Jettax antes de conciliar.")
            return

        def job() -> None:
            clientes = [c for c in (self.clientes or []) if c is not None]
            clientes_com = [c for c in (self.clientes_com or []) if c is not None]
            certs = [c for c in (self.result.certificados or []) if c is not None]
            reattempt_locked(self.result, self.cfg, clientes)
            self.result.clientes_sem = clientes
            self.result.clientes_com = clientes_com
            self.result.certificados = certs
            opts = self.cfg.get("opcoes", {})
            self.result.matches = match_all(
                certs, clientes, clientes_com,
                atualizar_todos=bool(opts.get("atualizar_todas_empresas", False)),
                escolher_mais_novo=bool(opts.get("escolher_certificado_mais_novo", True)),
            )
            refresh_stats(self.result)
            self._safe_reports(self.result)
            self.after(0, self._refresh_certificates)
            self.after(0, self._update_dashboard_stats)
            self.after(0, self._refresh_reports)
            self._after_log("Conciliação concluída. Relatórios atualizados.")

        self._thread(job)

    def _run_manual_bundle(self) -> None:
        self._sync_cfg()
        errors = validate_config(effective_config(self.cfg))
        if errors:
            messagebox.showwarning("Configuração", "\n".join(errors))
            return

        def job() -> None:
            from cajuru_a1.diagnostico import build_diagnostico, write_diagnostico_excel, write_diagnostico_html
            from cajuru_a1.jettax import JettaxBot
            from cajuru_a1.lote import build_persistent_bundle

            say = lambda m: self.after_log(m)
            clientes_sem, clientes_com = self._resolve_clientes(say)
            say("LOTE MANUAL — lendo Dropbox e auditando certificados…")
            result = analyze(self.cfg, log_fn=say, clientes_sem=clientes_sem, clientes_com=clientes_com)
            reattempt_locked(result, self.cfg, clientes_sem)
            result.clientes_sem = clientes_sem
            result.clientes_com = clientes_com
            result.matches = match_all(
                result.certificados, clientes_sem, clientes_com,
                atualizar_todas=bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False)),
                escolher_mais_novo=bool(self.cfg.get("opcoes", {}).get("escolher_certificado_mais_novo", True)),
            )
            refresh_stats(result)
            self.result = result
            self.clientes = clientes_sem
            self.clientes_com = clientes_com
            out = get_output_dir(self.cfg)
            _write_reports(result, out)
            diag = build_diagnostico(result)
            write_diagnostico_excel(diag, out / "diagnostico.xlsx")
            write_diagnostico_html(diag, out / "diagnostico.html", stats=result.stats)
            ready = [m for m in result.matches if m.pode_enviar]
            if not ready:
                self.after(0, self._refresh_certificates)
                self.after(0, self._update_dashboard_stats)
                say("Nenhum certificado PRONTO. Veja diagnostico.html para os motivos.")
                return
            opts = self.cfg.get("opcoes", {})
            bundle = build_persistent_bundle(
                ready, out,
                senha_manual=bool(opts.get("lote_senha_manual", True)),
                salvar_senhas_csv=bool(opts.get("salvar_senhas_csv", True)),
            )
            self.after(0, self._refresh_certificates)
            self.after(0, self._update_dashboard_stats)
            self.after(0, self._refresh_bundles)
            say(f"LOTE MANUAL pronto: {bundle['dir']}")
            say(f"  ZIP: {bundle['zip'].name}  ·  Planilha: {bundle['planilha'].name}")
            if bundle.get("csv_senhas"):
                say(f"  Senhas: {bundle['csv_senhas'].name}")
            self.after(0, lambda: messagebox.showinfo(
                "Lote manual gerado",
                f"ZIP e planilha salvos em:\n{bundle['dir']}\n\n"
                "Leve os dois arquivos ao Jettax > Clientes > Importar.\n"
                "A coluna SENHA está em branco — digite-a manualmente.\n"
                "Use o CSV de senhas como referência e apague a pasta depois.",
            ))

        self._thread(job)

    def _run_export_all(self) -> None:
        self._sync_cfg()
        errors = validate_config(effective_config(self.cfg))
        if errors:
            messagebox.showwarning("Configuração", "\n".join(errors))
            return

        def job() -> None:
            from cajuru_a1.exportacao import export_all_opened

            say = lambda m: self.after_log(m)
            say("Lendo certificados e validando senhas, sem conectar ao Jettax…")
            result = analyze(self.cfg, log_fn=say)
            self.result = result
            say("Criando ZIP e CSV de senhas…")
            try:
                bundle = export_all_opened(result.certificados, get_output_dir(self.cfg))
                self.after(0, self._refresh_certificates)
                self.after(0, self._update_dashboard_stats)
                self.after(0, self._refresh_reports)
                say(f"Exportação concluída: {bundle['quantidade']} certificado(s) em {bundle['dir']}")
                
                # Abre a pasta de exportação automaticamente
                self.after(0, lambda: _open_path(Path(bundle['dir'])))
                
                self.after(0, lambda: messagebox.showinfo(
                    "Exportação Concluída",
                    f"Sucesso! Foram exportados {bundle['quantidade']} certificado(s) com senhas validadas.\n\n"
                    f"A pasta de destino foi aberta automaticamente:\n{bundle['dir']}\n\n"
                    "Lá você encontrará:\n"
                    "- todos_certificados_a1.zip (com os arquivos .pfx/.p12)\n"
                    "- certificados_e_senhas.csv (com as senhas correspondentes!)"
                ))
            except Exception as e:
                say(f"Erro na exportação: {e}")
                raise

        self._thread(job)

    def _run_send(self) -> None:
        if not self.result or not self.result.matches:
            messagebox.showwarning("Falta conciliar", "Faça a conciliação antes.")
            return
        self._sync_cfg()
        prontos = sum(1 for m in self.result.matches if m.status == "pronto")
        dry = self.var_dry.get()
        if not dry:
            if prontos != int(self.result.stats.get("pronto", 0)):
                messagebox.showerror("Envio bloqueado",
                                     "A contagem de certificados prontos mudou. Refaça a conciliação antes de enviar.")
                return
            ok = messagebox.askyesno(
                "Enviar de verdade?",
                f"Isso vai gravar {prontos} certificado(s) no Jettax 360.\n"
                "O Dropbox NÃO será alterado.\n\nConfirma?",
            )
            if not ok:
                return

        def job() -> None:
            say = lambda m: self.after_log(m)
            results = enviar(
                self.cfg, self.result, log_fn=say,
                wait_login=self._wait_login_dialog, wait_import=self._wait_import_dialog,
            )
            sucesso = {"enviado", "confirmado_pela_tela", "confirmado_manualmente"}
            enviados = sum(1 for _, status in (results or []) if status in sucesso)
            falhas = sum(1 for _, status in (results or []) if str(status).startswith("falha"))
            say(f"Envio/simulação concluído. Enviados: {enviados}  Falhas: {falhas}  Total: {len(results or [])}.")
            say("Detalhe por certificado em output/auditoria_ultima_execucao.json (campo send_results).")

        self._thread(job)

    def _safe_reports(self, result: PipelineResult) -> None:
        out = get_output_dir(self.cfg)
        try:
            _write_reports(result, out)
        except Exception:
            self._log("Atenção: não foi possível gravar os relatórios desta execução.")

    def _on_close(self) -> None:
        if self._busy:
            messagebox.showwarning("Tarefa em andamento", "Espere a tarefa atual terminar antes de fechar.")
            return
        try:
            if self.result:
                finish(self.result)
        except Exception as exc:  # noqa: BLE001
            self._log(f"ALERTA AO FECHAR: {type(exc).__name__}: {exc}")
            messagebox.showerror("Alerta de segurança", str(exc))
        finally:
            self.destroy()


def _write_reports(result: PipelineResult, out: Path) -> None:
    write_excel_report(result, out / "relatorio.xlsx")
    write_html_report(result, out / "relatorio.html")


def _nth(seq, i) -> str:
    seq = seq or []
    return seq[i] if i < len(seq) else ""


def run_gui() -> None:
    app = App()
    app.mainloop()
