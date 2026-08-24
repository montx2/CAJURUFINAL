"""Interface em português — clara, com simulação obrigatória antes do envio."""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from cajuru_a1.cnpjutil import format_cnpj
from cajuru_a1.config import _blank, get_output_dir, load_config, save_config, validate_config
from cajuru_a1.matcher import match_all
from cajuru_a1.models import JetaxClient, PipelineResult
from cajuru_a1.pipeline import analyze, enviar, finish, refresh_stats
from cajuru_a1.reattempt import reattempt_locked
from cajuru_a1.report import write_excel_report, write_html_report

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

STATUS_COLOR = {
    "pronto": "#1B9C85",
    "sem_senha": "#E0A100",
    "vencido": "#C0392B",
    "conflito": "#D35400",
    "sem_cert": "#7F8C8D",
    "sem_cert_novo": "#2E86C1",
    "ambiguo": "#8E44AD",
    "revisao_manual": "#AF7AC5",
    "extra_pfx": "#2980B9",
    "substituido": "#566573",
    "duplicado": "#7F8C8D",
    "nao_valido": "#8E6BBE",
    "invalido": "#922B21",
}


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Cajuru A1  ·  Certificados Jettax 360")
        self.geometry("1280x780")
        self.minsize(1080, 680)
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
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self._log("Pronto. Nada no Dropbox será alterado — só leitura e cópia temporária.")
        if getattr(self, "_config_error", ""):
            self._log("config.yaml tinha erro e foi ignorado. Use os botões … para escolher as pastas e Salvar.")
            self._log(self._config_error)
        else:
            pasta = (self.cfg.get("dropbox") or {}).get("pasta") or ""
            if pasta:
                self._log(f"Pasta Dropbox: {pasta}")

    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=280, corner_radius=0, fg_color="#0B1F3A")
        bar.grid(row=0, column=0, sticky="nsew")
        bar.grid_propagate(False)
        ctk.CTkLabel(
            bar,
            text="CAJURU A1",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(28, 4))
        ctk.CTkLabel(
            bar,
            text="Dropbox → senha → Jettax\nsomente leitura no Dropbox",
            font=ctk.CTkFont(size=12),
            text_color="#9AB",
        ).pack(pady=(0, 20))

        self.step_labels = []
        for i, t in enumerate(
            [
                "1  Pastas e planilhas",
                "2  Ler certificados",
                "3  Clientes sem A1",
                "4  Conciliar",
                "5  Simular / Enviar",
            ],
            start=1,
        ):
            lb = ctk.CTkLabel(bar, text=t, anchor="w", font=ctk.CTkFont(size=14))
            lb.pack(fill="x", padx=22, pady=4)
            self.step_labels.append(lb)

        ctk.CTkLabel(bar, text="").pack(expand=True)
        ctk.CTkButton(
            bar, text="Salvar configuração", command=self._save_cfg, fg_color="#1B9C85"
        ).pack(fill="x", padx=22, pady=8)
        ctk.CTkButton(
            bar, text="Abrir pasta de relatórios", command=self._open_output, fg_color="#16324F"
        ).pack(fill="x", padx=22, pady=(0, 24))

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color="#121212", corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        cfgf = ctk.CTkFrame(main, fg_color="#1A1A1A")
        cfgf.grid(row=0, column=0, sticky="ew", padx=16, pady=16)
        cfgf.grid_columnconfigure(1, weight=1)

        self.var_drop = ctk.StringVar(value=self.cfg.get("dropbox", {}).get("pasta") or "")
        self.var_xlsx1 = ctk.StringVar(value=_nth(self.cfg.get("excel", {}).get("arquivos"), 0))
        self.var_xlsx2 = ctk.StringVar(value=_nth(self.cfg.get("excel", {}).get("arquivos"), 1))
        self.var_url = ctk.StringVar(
            value=(self.cfg.get("jettax") or {}).get("url") or "https://admin.jettax360.com.br/"
        )
        self.var_dry = ctk.BooleanVar(value=bool(self.cfg.get("opcoes", {}).get("dry_run", True)))
        self.var_lote = ctk.BooleanVar(
            value=(self.cfg.get("opcoes", {}).get("modo_envio") or "lote") != "individual"
        )
        opts = self.cfg.get("opcoes", {})
        self.var_atualizar_todas = ctk.BooleanVar(value=bool(opts.get("atualizar_todas_empresas", False)))
        self.var_mais_novo = ctk.BooleanVar(value=bool(opts.get("escolher_certificado_mais_novo", True)))
        self.var_senha_manual = ctk.BooleanVar(value=bool(opts.get("lote_senha_manual", True)))
        self.var_csv_senhas = ctk.BooleanVar(value=bool(opts.get("salvar_senhas_csv", True)))

        self._row_path(cfgf, 0, "Pasta CERTIFICADOS A1 (Dropbox)", self.var_drop, self._pick_dir)
        self._row_path(cfgf, 1, "Planilha de senhas 1", self.var_xlsx1, self._pick_xlsx)
        self._row_path(cfgf, 2, "Planilha de senhas 2", self.var_xlsx2, self._pick_xlsx)
        ctk.CTkLabel(cfgf, text="URL do Jettax", width=240, anchor="w").grid(
            row=3, column=0, padx=10, pady=6, sticky="w"
        )
        ctk.CTkEntry(cfgf, textvariable=self.var_url).grid(row=3, column=1, sticky="ew", pady=6)

        ctk.CTkCheckBox(
            cfgf,
            text="Modo simulação (não envia nada ao Jettax) — deixe ligado na 1ª vez",
            variable=self.var_dry,
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 4))
        ctk.CTkCheckBox(
            cfgf,
            text="Importar em LOTE (recomendado) — ZIP oficial do Jettax, não abre empresa por empresa",
            variable=self.var_lote,
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ctk.CTkCheckBox(
            cfgf,
            text="Lote com senha MANUAL — planilha com senha em branco; ZIP+planilha+CSV ficam salvos em output/lotes",
            variable=self.var_senha_manual,
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ctk.CTkCheckBox(
            cfgf,
            text="Salvar CSV de senhas ao lado do lote (para digitar manualmente; apague após importar)",
            variable=self.var_csv_senhas,
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ctk.CTkCheckBox(
            cfgf,
            text="Escolher o certificado MAIS NOVO quando houver 2 PFX para o mesmo CNPJ",
            variable=self.var_mais_novo,
        ).grid(row=8, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
        ctk.CTkCheckBox(
            cfgf,
            text="Atualizar/renovar certificados de TODAS as empresas (inclusive as que já têm A1)",
            variable=self.var_atualizar_todas,
        ).grid(row=9, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 10))

        actions = ctk.CTkFrame(main, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=16)
        for i, (txt, cmd, color) in enumerate(
            [
                ("Fluxo completo", self._run_full, "#154360"),
                ("Ler Dropbox + senhas", self._run_analyze, "#1B4F72"),
                ("Buscar no Jettax (sem A1)", self._run_jettax, "#6C3483"),
                ("Conciliar", self._run_match, "#1B9C85"),
                ("Gerar lote MANUAL", self._run_manual_bundle, "#117A65"),
                ("Simular / Enviar", self._run_send, "#C0392B"),
                ("🌐 Painel web", self._open_web_panel, "#2874A6"),
            ]
        ):
            ctk.CTkButton(actions, text=txt, command=cmd, fg_color=color, width=160, height=38).grid(
                row=0, column=i, padx=6
            )

        body = ctk.CTkFrame(main, fg_color="#1A1A1A")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        self.stats = ctk.CTkLabel(body, text="Aguardando…", anchor="w")
        self.stats.grid(row=0, column=0, sticky="ew", padx=10, pady=8)

        self.table = ctk.CTkTextbox(body, font=ctk.CTkFont(family="Consolas", size=13))
        self.table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))

        self.logbox = ctk.CTkTextbox(main, height=150, font=ctk.CTkFont(family="Consolas", size=12))
        self.logbox.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))

    def _row_path(self, parent, row, label, var, picker) -> None:
        ctk.CTkLabel(parent, text=label, width=240, anchor="w").grid(
            row=row, column=0, padx=10, pady=6, sticky="w"
        )
        ctk.CTkEntry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=6)
        ctk.CTkButton(parent, text="…", width=40, command=lambda: picker(var)).grid(
            row=row, column=2, padx=8
        )

    def _pick_dir(self, var) -> None:
        p = filedialog.askdirectory(
            title="Selecione diretamente a pasta CERTIFICADOS ou CERTIFICADOS A1"
        )
        if p:
            selected = Path(p)
            if not selected.name.strip().casefold().startswith("certificados"):
                messagebox.showwarning(
                    "Escopo inválido",
                    "Não selecione a raiz do Dropbox nem uma pasta ancestral.\n\n"
                    "Abra o Dropbox e selecione diretamente a pasta CERTIFICADOS ou CERTIFICADOS A1.",
                )
                self._log("Seleção de Dropbox recusada: escolha direta de CERTIFICADOS/CERTIFICADOS A1 obrigatória.")
                return
            var.set(p)
            self._log("Escopo confirmado: somente a pasta CERTIFICADOS selecionada será lida.")

    def _pick_xlsx(self, var) -> None:
        p = filedialog.askopenfilename(
            title="Planilha de senhas",
            filetypes=[("Excel", "*.xlsx *.xlsm"), ("Todos", "*.*")],
        )
        if p:
            var.set(p)

    def _sync_cfg(self) -> None:
        self.cfg.setdefault("dropbox", {})["pasta"] = self.var_drop.get().strip()
        arquivos = [p for p in (self.var_xlsx1.get().strip(), self.var_xlsx2.get().strip()) if p]
        self.cfg.setdefault("excel", {})["arquivos"] = arquivos
        self.cfg.setdefault("jettax", {})["url"] = (
            self.var_url.get().strip() or "https://admin.jettax360.com.br/"
        )
        self.cfg.setdefault("opcoes", {})["dry_run"] = bool(self.var_dry.get())
        self.cfg.setdefault("opcoes", {})["modo_envio"] = "lote" if self.var_lote.get() else "individual"
        self.cfg.setdefault("opcoes", {})["tentar_todas_senhas_da_planilha"] = False
        self.cfg.setdefault("opcoes", {})["lote_senha_manual"] = bool(self.var_senha_manual.get())
        self.cfg.setdefault("opcoes", {})["salvar_senhas_csv"] = bool(self.var_csv_senhas.get())
        self.cfg.setdefault("opcoes", {})["escolher_certificado_mais_novo"] = bool(self.var_mais_novo.get())
        self.cfg.setdefault("opcoes", {})["atualizar_todas_empresas"] = bool(self.var_atualizar_todas.get())
        self.cfg.setdefault("seguranca", {})["permitir_varredura_global"] = False

    def _save_cfg(self) -> None:
        self._sync_cfg()
        errors = validate_config(self.cfg)
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
        import os
        import subprocess
        import sys

        if sys.platform.startswith("win"):
            os.startfile(out)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(out)])

    def _log(self, msg: str) -> None:
        logging.getLogger("cajuru_a1").info(msg)
        try:
            self.logbox.insert("end", str(msg) + "\n")
            self.logbox.see("end")
        except Exception:
            pass
        try:
            out = get_output_dir(self.cfg)
            out.mkdir(parents=True, exist_ok=True)
            with (out / "cajuru_a1.log").open("a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass
        try:
            self.update_idletasks()
        except Exception:
            pass

    def _busy_on(self) -> bool:
        if self._busy:
            messagebox.showinfo("Aguarde", "Já existe uma tarefa em andamento.")
            return False
        self._busy = True
        return True

    def _thread(self, fn) -> None:
        if not self._busy_on():
            return

        def wrap():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                err = f"{type(exc).__name__}: {exc}"
                self.after(0, lambda t=tb, e=err: self._show_error(e, t))
            finally:
                self._busy = False

        threading.Thread(target=wrap, daemon=True).start()

    def _show_error(self, err: str, tb: str) -> None:
        self._log(tb)
        messagebox.showerror("Erro", err + "\n\nDetalhes no log (output/cajuru_a1.log).")

    def _wait_login_dialog(self) -> None:
        done = threading.Event()

        def ask():
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

        def ask():
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

    def _run_manual_bundle(self) -> None:
        """Lê o Dropbox, escolhe o certificado mais novo e gera o ZIP +
        planilha (com senha em branco) + CSV de senhas em output/lotes/.
        Não acessa o Jettax — operação 100% manual."""
        self._sync_cfg()
        errors = validate_config(self.cfg)
        if errors:
            messagebox.showwarning("Configuração", "\n".join(errors))
            return

        def job():
            from cajuru_a1.lote import build_persistent_bundle
            from cajuru_a1.diagnostico import build_diagnostico, write_diagnostico_excel, write_diagnostico_html

            self.after(0, lambda: self._log("LOTE MANUAL — lendo Dropbox e auditando certificados…"))
            result = analyze(self.cfg, log_fn=lambda m: self.after(0, self._log, m))
            self.result = result
            out = get_output_dir(self.cfg)
            write_excel_report(result, out / "relatorio.xlsx")
            write_html_report(result, out / "relatorio.html")
            diag = build_diagnostico(result)
            write_diagnostico_excel(diag, out / "diagnostico.xlsx")
            write_diagnostico_html(diag, out / "diagnostico.html")
            ready = [m for m in result.matches if m.pode_enviar]
            if not ready:
                self.after(0, lambda: self._log(
                    "Nenhum certificado PRONTO. Veja diagnostico.html para os motivos."
                ))
                self.after(0, self._refresh_table)
                return
            opts = self.cfg.get("opcoes", {})
            bundle = build_persistent_bundle(
                ready, out,
                senha_manual=bool(opts.get("lote_senha_manual", True)),
                salvar_senhas_csv=bool(opts.get("salvar_senhas_csv", True)),
            )
            self.after(0, self._refresh_table)
            self.after(0, lambda: self._log(f"LOTE MANUAL pronto em: {bundle['dir']}"))
            self.after(0, lambda: self._log(f"  ZIP:      {bundle['zip'].name}"))
            self.after(0, lambda: self._log(f"  Planilha: {bundle['planilha'].name} (senha em branco)"))
            if bundle.get("csv_senhas"):
                self.after(0, lambda: self._log(
                    f"  Senhas:   {bundle['csv_senhas'].name} (digite manualmente no Jettax)"
                ))
            self.after(0, lambda: messagebox.showinfo(
                "Lote manual gerado",
                f"ZIP e planilha salvos em:\n{bundle['dir']}\n\n"
                "Leve os dois arquivos ao Jettax > Clientes > Importar.\n"
                "A coluna SENHA está em branco — digite-a manualmente.\n"
                "Use o CSV de senhas como referência e apague a pasta depois.",
            ))

        self._thread(job)

    def _open_web_panel(self) -> None:
        """Inicia o painel web (Flask) em background e abre no navegador."""
        self._sync_cfg()
        try:
            save_config(self.cfg)
        except Exception:
            pass

        def run():
            from cajuru_a1.webapp import run_server
            import webbrowser
            url = "http://127.0.0.1:8765"
            self.after(0, lambda: self._log(f"Painel web iniciado em {url}"))
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
            try:
                run_server(host="127.0.0.1", port=8765, config_path="config.yaml", open_browser=False)
            except Exception as exc:
                self.after(0, lambda e=exc: self._log(f"Falha no painel web: {e}"))

        threading.Thread(target=run, daemon=True).start()

    def _run_full(self) -> None:
        self._sync_cfg()
        errors = validate_config(self.cfg)
        if errors:
            messagebox.showwarning("Configuração", "\n".join(errors))
            return
        def job():
            from cajuru_a1.jettax import JettaxBot
            atualizar_todas = bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
            mais_novo = bool(self.cfg.get("opcoes", {}).get("escolher_certificado_mais_novo", True))
            self.after(0, lambda: self._log(
                "FLUXO COMPLETO — buscando empresas sem A1 no Jettax…"
                + (" (modo TODAS as empresas)" if atualizar_todas else "")
            ))
            bot = JettaxBot(self.cfg, log_fn=lambda m: self.after(0, self._log, m))
            clientes_sem: list[JetaxClient] = []
            clientes_com: list[JetaxClient] = []
            try:
                bot.start()
                bot.login(wait_fn=self._wait_login_dialog)
                if atualizar_todas:
                    clientes_sem, clientes_com = bot.list_all_clients()
                    self.after(0, lambda a=len(clientes_sem), b=len(clientes_com): self._log(
                        f"Jettax: {a} sem A1, {b} já com A1 (renovação total)."
                    ))
                else:
                    clientes_sem = bot.list_without_certificate()
                    self.after(0, lambda n=len(clientes_sem): self._log(f"Jettax: {n} empresas sem certificado."))
            finally:
                bot.close()
            self.clientes = clientes_sem
            self.after(0, lambda: self._log("FLUXO COMPLETO — lendo Dropbox, usando os nomes reais do Jettax para localizar senhas…"))
            result = analyze(
                self.cfg, log_fn=lambda m: self.after(0, self._log, m),
                clientes_sem=clientes_sem, clientes_com=clientes_com,
            )
            reattempt_locked(result, self.cfg, clientes_sem)
            result.clientes_sem = clientes_sem
            result.clientes_com = clientes_com
            result.matches = match_all(
                result.certificados, clientes_sem, clientes_com,
                atualizar_todos=atualizar_todas, escolher_mais_novo=mais_novo,
            )
            refresh_stats(result)
            self.result = result
            out = get_output_dir(self.cfg)
            write_excel_report(result, out / "relatorio.xlsx")
            write_html_report(result, out / "relatorio.html")
            self.after(0, self._refresh_table)
            self.after(0, lambda: self._log("FLUXO COMPLETO concluído. Confira o relatório antes de enviar."))
        self._thread(job)

    def _run_analyze(self) -> None:
        self._sync_cfg()

        def job():
            self.after(0, lambda: self._log("Iniciando leitura do Dropbox…"))
            result = analyze(self.cfg, log_fn=lambda m: self.after(0, self._log, m))
            self.result = result
            self.after(0, self._refresh_table)

        self._thread(job)

    def _run_jettax(self) -> None:
        self._sync_cfg()

        def job():
            from cajuru_a1.jettax import JettaxBot

            atualizar_todas = bool(self.cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
            self.after(0, lambda: self._log("Abrindo Jettax. Faça login se pedir."))
            bot = JettaxBot(self.cfg, log_fn=lambda m: self.after(0, self._log, m))
            clientes: list[JetaxClient] = []
            clientes_com: list[JetaxClient] = []
            try:
                bot.start()
                bot.login(wait_fn=self._wait_login_dialog)
                if atualizar_todas:
                    clientes, clientes_com = bot.list_all_clients()
                    self.after(0, lambda a=len(clientes), b=len(clientes_com): self._log(
                        f"{a} sem A1, {b} com A1 (renovação total)."
                    ))
                else:
                    clientes = bot.list_without_certificate()
            except Exception:
                try:
                    out = get_output_dir(self.cfg)
                    out.mkdir(parents=True, exist_ok=True)
                    bot.screenshot(out / "erro_jettax.png")
                    self.after(0, lambda: self._log(f"Print salvo em {out / 'erro_jettax.png'}"))
                except Exception:
                    pass
                raise
            finally:
                bot.close()
            self.clientes = clientes
            self.clientes_com = clientes_com
            self.after(0, lambda n=len(clientes): self._log(
                f"{n} empresas sem certificado." + (" Clique em Conciliar." if not atualizar_todas else " Clique em Conciliar.")
            ))

        self._thread(job)

    def _run_match(self) -> None:
        if not self.result:
            messagebox.showwarning("Falta passo 2", "Leia o Dropbox primeiro.")
            return
        if not self.clientes:
            messagebox.showwarning("Falta passo 3", "Busque os clientes sem certificado no Jettax.")
            return

        def job():
            clientes = [c for c in (self.clientes or []) if c is not None]
            clientes_com = [c for c in (getattr(self, "clientes_com", []) or []) if c is not None]
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
            out = get_output_dir(self.cfg)
            write_excel_report(self.result, out / "relatorio.xlsx")
            write_html_report(self.result, out / "relatorio.html")
            self.after(0, self._refresh_table)
            self.after(0, lambda: self._log("Relatórios em output/relatorio.xlsx e .html"))

        self._thread(job)

    def _on_close(self) -> None:
        if self._busy:
            messagebox.showwarning("Tarefa em andamento", "Espere a tarefa atual terminar antes de fechar.")
            return
        try:
            if self.result:
                finish(self.result)
        except Exception as exc:  # alerta de integridade/limpeza não pode ser silencioso
            self._log(f"ALERTA AO FECHAR: {type(exc).__name__}: {exc}")
            messagebox.showerror("Alerta de segurança", str(exc))
        finally:
            self.destroy()

    def _run_send(self) -> None:
        if not self.result or not self.result.matches:
            messagebox.showwarning("Falta conciliar", "Faça a conciliação antes.")
            return
        self._sync_cfg()
        prontos = sum(1 for m in self.result.matches if m.status == "pronto")
        dry = self.var_dry.get()
        if not dry:
            if prontos != int(self.result.stats.get("pronto", 0)):
                messagebox.showerror("Envio bloqueado", "A contagem de certificados prontos mudou. Refaça a conciliação antes de enviar.")
                return
            ok = messagebox.askyesno(
                "Enviar de verdade?",
                f"Isso vai gravar {prontos} certificado(s) no Jettax 360.\n"
                "O Dropbox NÃO será alterado.\n\nConfirma?",
            )
            if not ok:
                return

        def job():
            results = enviar(
                self.cfg,
                self.result,
                log_fn=lambda m: self.after(0, self._log, m),
                wait_login=self._wait_login_dialog,
                wait_import=self._wait_import_dialog,
            )
            # Sucesso pode vir do modo individual ("enviado") ou do modo lote
            # ("confirmado_pela_tela" / "confirmado_manualmente"); "simulado" é
            # dry_run e não conta nem como sucesso nem como falha.
            status_sucesso = {"enviado", "confirmado_pela_tela", "confirmado_manualmente"}
            enviados = sum(1 for _, status in (results or []) if status in status_sucesso)
            falhas = sum(1 for _, status in (results or []) if str(status).startswith("falha"))
            self.after(
                0,
                lambda: self._log(
                    f"Tarefa de envio/simulação concluída. Enviados: {enviados}  Falhas: {falhas}  Total: {len(results or [])}. "
                    "Detalhe por certificado em output/auditoria_ultima_execucao.json (campo send_results)."
                ),
            )

        self._thread(job)

    def _refresh_table(self) -> None:
        self.table.delete("1.0", "end")
        if not self.result:
            return
        st = self.result.stats
        self.stats.configure(
            text=(
                f"PFX {st.get('pfx', 0)}   abertos {st.get('pfx_abertos', 0)}   "
                f"sem A1 no Jettax {st.get('clientes_sem', 0)}   "
                f"PRONTOS {st.get('pronto', 0)}   revisão manual {st.get('revisao_manual', 0)}   "
                f"sem senha {st.get('sem_senha', 0)}   conflitos {st.get('conflito', 0)}   "
                f"substituídos {st.get('substituido', 0)}   "
                f"sem PFX {st.get('sem_cert', 0) + st.get('sem_cert_novo', 0)}   "
                f"vencidos {st.get('vencido', 0)}"
            )
        )
        header = f"{'STATUS':<12} {'CNPJ':<20} {'EMPRESA':<42} {'ARQUIVO':<36} MOTIVO\n"
        self.table.insert("end", header)
        self.table.insert("end", "-" * 140 + "\n")
        if self.result.matches:
            for m in self.result.matches:
                empresa = ((m.cliente.razao_social if m.cliente else None) or "—")[:40]
                cnpj = (
                    format_cnpj(m.cliente.cnpj)
                    if m.cliente is not None and getattr(m.cliente, "cnpj", None)
                    else "—"
                )
                arq = ((m.cert.filename if m.cert else None) or "—")[:34]
                self.table.insert(
                    "end",
                    f"{m.status:<12} {cnpj:<20} {empresa:<42} {arq:<36} {m.motivo}\n",
                )
        else:
            for c in self.result.certificados:
                flag = "OK" if c.opened else "SEM_SENHA"
                if c.expired:
                    flag = "VENCIDO"
                self.table.insert(
                    "end",
                    f"{flag:<12} {(format_cnpj(c.cnpj) if c.cnpj else '—'):<20} "
                    f"{'':<42} {c.filename[:34]:<36} {c.password_source or c.error or ''}\n",
                )

def _nth(seq, i) -> str:
    seq = seq or []
    return seq[i] if i < len(seq) else ""


def run_gui() -> None:
    app = App()
    # Não sobrescrever _on_close: ele impede encerramento no meio da tarefa e
    # garante inventário final + limpeza temporária.
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()
