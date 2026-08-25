from __future__ import annotations

import argparse
import logging

from cajuru_a1.config import get_output_dir, load_config
from cajuru_a1.matcher import match_all
from cajuru_a1.pipeline import analyze, enviar, finish, refresh_stats
from cajuru_a1.reattempt import reattempt_locked
from cajuru_a1.report import write_excel_report, write_html_report


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Cajuru A1 — auditoria conservadora de certificados")
    parser.add_argument("--analisar", action="store_true", help="Só lê e audita; não acessa o Jettax")
    parser.add_argument("--enviar", action="store_true", help="Envia somente se dry_run=false e após todas as barreiras")
    parser.add_argument("--gerar-lote-manual", action="store_true", help="Lista clientes no Jettax (leitura) e gera ZIP+planilha (senha em branco)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    # A interface principal é o painel desktop local (CustomTkinter). O painel
    # web foi removido: não há servidor HTTP, navegador nem dependência de CDN.
    if not (args.analisar or args.enviar or args.gerar_lote_manual):
        from cajuru_a1.gui import run_gui
        run_gui()
        return 0

    if args.gerar_lote_manual:
        cfg = load_config(args.config)
        from cajuru_a1.jettax import JettaxBot

        opts = cfg.get("opcoes", {})
        atualizar_todas = bool(opts.get("atualizar_todas_empresas", False))
        clientes_sem: list = []
        clientes_com: list = []
        bot = JettaxBot(cfg)
        try:
            bot.start()
            bot.login()
            if atualizar_todas:
                clientes_sem, clientes_com = bot.list_all_clients()
            else:
                clientes_sem = bot.list_without_certificate()
        finally:
            bot.close()

        result = analyze(cfg, clientes_sem=clientes_sem, clientes_com=clientes_com)
        try:
            reattempt_locked(result, cfg, clientes_sem)
            result.clientes_sem = clientes_sem
            result.clientes_com = clientes_com
            result.matches = match_all(
                result.certificados, clientes_sem, clientes_com,
                atualizar_todos=atualizar_todas,
                escolher_mais_novo=bool(opts.get("escolher_certificado_mais_novo", True)),
            )
            refresh_stats(result)
            output = get_output_dir(cfg)
            write_excel_report(result, output / "relatorio.xlsx")
            write_html_report(result, output / "relatorio.html")
            from cajuru_a1.diagnostico import build_diagnostico, write_diagnostico_excel, write_diagnostico_html
            diag = build_diagnostico(result)
            write_diagnostico_excel(diag, output / "diagnostico.xlsx")
            write_diagnostico_html(diag, output / "diagnostico.html")
            ready = [m for m in result.matches if m.pode_enviar]
            if not ready:
                print("Nenhum certificado PRONTO para o lote. Veja diagnostico.html.")
            else:
                from cajuru_a1.lote import build_persistent_bundle
                bundle = build_persistent_bundle(
                    ready, output,
                    senha_manual=bool(opts.get("lote_senha_manual", True)),
                    salvar_senhas_csv=bool(opts.get("salvar_senhas_csv", True)),
                )
                print(f"Lote manual gerado em: {bundle['dir']}")
                print(f"  ZIP:      {bundle['zip'].name}")
                print(f"  Planilha: {bundle['planilha'].name}")
                if bundle.get("csv_senhas"):
                    print(f"  Senhas:   {bundle['csv_senhas'].name}")
        finally:
            finish(result)
        return 0

    if args.analisar or args.enviar:
        cfg = load_config(args.config)
        result = None
        try:
            result = analyze(cfg)
            output = get_output_dir(cfg)
            write_excel_report(result, output / "relatorio.xlsx")
            write_html_report(result, output / "relatorio.html")
            print(f"Relatório: {output / 'relatorio.html'}")
            if args.enviar:
                from cajuru_a1.jettax import JettaxBot

                bot = JettaxBot(cfg)
                try:
                    bot.start()
                    bot.login()
                    clients = bot.list_without_certificate()
                finally:
                    bot.close()
                reattempt_locked(result, cfg, clients)
                result.clientes_sem = clients
                result.matches = match_all(result.certificados, clients, [])
                refresh_stats(result)
                write_excel_report(result, output / "relatorio.xlsx")
                write_html_report(result, output / "relatorio.html")
                enviar(cfg, result)
            return 0
        finally:
            if result is not None:
                finish(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
