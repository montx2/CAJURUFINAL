"""Painel desktop em Tkinter (sem dependências externas além da biblioteca padrão)."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from cajuru_a1 import __version__
from cajuru_a1.config import get_output_dir, load_config, save_config, validate_config
from cajuru_a1.exportacao import build_bundle
from cajuru_a1.pipeline import run_pipeline
from cajuru_a1.report import write_html_report, write_xlsx_report


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Cajuru A1 v{__version__} — Preparação para Jettax 360")
        self.geometry("960x720")
        self.minsize(820, 600)
        self.config_path = Path("config.yaml")
        self.cfg = load_config(self.config_path)
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build_ui()
        self._refresh_fields()
        self.after(150, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self, text="Configuração")
        top.pack(fill="x", **pad)

        self.pasta_var = tk.StringVar()
        self._picker_row(top, "Pasta CERTIFICADOS A1:", self.pasta_var, self._pick_folder, 0)

        self.planilhas_var = tk.StringVar()
        self._picker_row(top, "Planilha(s) de senha:", self.planilhas_var, self._pick_planilhas, 1, multi=True)

        self.saida_var = tk.StringVar()
        self._picker_row(top, "Pasta de saída:", self.saida_var, self._pick_saida, 2)

        opts = ttk.Frame(top)
        opts.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        self.senha_manual_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Senha em branco na planilha oficial (preencher à mão no Jettax)",
                        variable=self.senha_manual_var).pack(side="left")
        self.common_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Tentar senhas comuns (123456...)",
                        variable=self.common_var).pack(side="left", padx=12)

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Salvar configuração", command=self._save_cfg).pack(side="left")
        self.run_btn = ttk.Button(btns, text="▶ Processar e gerar lote", command=self._run)
        self.run_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Abrir pasta de saída", command=self._open_saida).pack(side="left")

        # KPIs
        kpi_frame = ttk.Frame(self)
        kpi_frame.pack(fill="x", **pad)
        self.kpi_vars = {}
        for label in ("Total", "Abertos", "Prontos", "Duplicatas", "Rejeitados"):
            box = ttk.LabelFrame(kpi_frame, text=label)
            box.pack(side="left", expand=True, fill="x", padx=4)
            var = tk.StringVar(value="—")
            ttk.Label(box, textvariable=var, font=("Segoe UI", 16, "bold")).pack(pady=4)
            self.kpi_vars[label] = var

        # Log
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)

        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill="x", side="bottom")

    def _picker_row(self, parent, label, var, command, row, multi=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(parent, text="Selecionar...", command=command).grid(row=row, column=2, padx=4)
        parent.columnconfigure(1, weight=1)

    # ------------------------------------------------------------ fields
    def _refresh_fields(self) -> None:
        self.pasta_var.set(self.cfg.get("pasta_certificados", "") or "")
        planilhas = self.cfg.get("planilhas_senha") or []
        self.planilhas_var.set(" ; ".join(str(p) for p in planilhas))
        self.saida_var.set(self.cfg.get("saida", "") or "")
        opcoes = self.cfg.get("opcoes") or {}
        self.senha_manual_var.set(bool(opcoes.get("senha_manual_planilha", True)))
        self.common_var.set(bool(opcoes.get("tentar_senhas_comuns", False)))

    def _collect_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg["pasta_certificados"] = self.pasta_var.get().strip()
        raw = self.planilhas_var.get().strip()
        cfg["planilhas_senha"] = [p.strip() for p in raw.replace(";", "\n").splitlines() if p.strip()] if raw else []
        cfg["saida"] = self.saida_var.get().strip()
        cfg.setdefault("opcoes", {})
        cfg["opcoes"]["senha_manual_planilha"] = self.senha_manual_var.get()
        cfg["opcoes"]["tentar_senhas_comuns"] = self.common_var.get()
        return cfg

    def _save_cfg(self) -> None:
        cfg = self._collect_cfg()
        try:
            save_config(cfg, self.config_path)
            self.cfg = cfg
            messagebox.showinfo("Salvo", f"Configuração salva em {self.config_path}")
        except Exception as exc:
            messagebox.showerror("Erro", str(exc))

    # ----------------------------------------------------------- pickers
    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Selecione a pasta CERTIFICADOS A1")
        if folder:
            self.pasta_var.set(folder)

    def _pick_planilhas(self) -> None:
        files = filedialog.askopenfilenames(
            title="Selecione as planilhas de senha",
            filetypes=[("Excel", "*.xlsx *.xlsm"), ("Todos", "*.*")],
        )
        if files:
            existing = [p.strip() for p in self.planilhas_var.get().replace(";", "\n").splitlines() if p.strip()]
            merged = list(dict.fromkeys([*existing, *files]))
            self.planilhas_var.set(" ; ".join(merged))

    def _pick_saida(self) -> None:
        folder = filedialog.askdirectory(title="Pasta de saída")
        if folder:
            self.saida_var.set(folder)

    def _open_saida(self) -> None:
        output_dir = get_output_dir(self._collect_cfg())
        try:
            import os
            import subprocess
            if Path(output_dir).exists():
                if hasattr(os, "startfile"):
                    os.startfile(output_dir)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(output_dir)])
        except Exception as exc:
            messagebox.showerror("Erro", str(exc))

    # --------------------------------------------------------------- run
    def _run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        cfg = self._collect_cfg()
        errors = validate_config(cfg)
        if errors:
            messagebox.showerror("Configuração inválida", "\n".join(errors))
            return
        try:
            save_config(cfg, self.config_path)
            self.cfg = cfg
        except Exception:
            pass

        self.run_btn.configure(state="disabled")
        self._clear_log()
        self._set_kpis({})
        self.status_var.set("Processando...")

        self._worker = threading.Thread(target=self._run_worker, args=(cfg,), daemon=True)
        self._worker.start()

    def _run_worker(self, cfg: dict) -> None:
        def say(message: str) -> None:
            self._queue.put(("log", message))
        try:
            opcoes = cfg.get("opcoes") or {}
            result = run_pipeline(
                cfg["pasta_certificados"],
                cfg["planilhas_senha"],
                years=opcoes.get("anos_senha"),
                try_common=bool(opcoes.get("tentar_senhas_comuns", False)),
                max_cert_mb=int(opcoes.get("max_certificado_mb", 30)),
                max_attempts=int(opcoes.get("max_tentativas_senha", 500)),
                max_files=int(opcoes.get("max_arquivos", 10000)),
                log_fn=say,
            )
            output_dir = get_output_dir(cfg)
            html_path = write_html_report(result, output_dir / "relatorio.html")
            xlsx_path = write_xlsx_report(result, output_dir / "relatorio.xlsx")
            self._queue.put(("kpis", result.stats))
            self._queue.put(("log", f"Relatório HTML: {html_path}"))
            self._queue.put(("log", f"Relatório Excel: {xlsx_path}"))

            if result.selected:
                bundle = build_bundle(
                    result.selected,
                    output_dir,
                    senha_manual=bool(opcoes.get("senha_manual_planilha", True)),
                    rejeitados=result.rejected + [(c, "SUBSTITUIDO", c.extra.get("motivo_substituicao", "")) for c in result.duplicates],
                )
                self._queue.put(("done", f"Lote gerado em {bundle['dir']} ({bundle['quantidade']} certificados)"))
                self._queue.put(("bundle", bundle))
            else:
                self._queue.put(("done", "Nenhum certificado válido para exportar. Confira o relatório."))
        except Exception as exc:
            self._queue.put(("error", str(exc)))

    # ------------------------------------------------------------- queue
    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "kpis":
                    self._set_kpis(payload)
                elif kind == "done":
                    self.status_var.set(payload)
                    self._append_log("\n" + payload)
                    self.run_btn.configure(state="normal")
                elif kind == "error":
                    self.status_var.set("Erro.")
                    self._append_log("\nERRO: " + payload)
                    messagebox.showerror("Erro", payload)
                    self.run_btn.configure(state="normal")
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_kpis(self, stats: dict) -> None:
        mapping = {
            "Total": stats.get("total_arquivos", "—"),
            "Abertos": stats.get("abertos", "—"),
            "Prontos": stats.get("selecionados", "—"),
            "Duplicatas": stats.get("duplicatas", "—"),
            "Rejeitados": stats.get("rejeitados", "—"),
        }
        for key, value in mapping.items():
            self.kpi_vars[key].set(str(value))


def launch() -> None:
    app = App()
    app.mainloop()
