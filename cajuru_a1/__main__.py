"""Interface de linha de comando do Cajuru A1."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from cajuru_a1.config import get_output_dir, load_config, save_config, validate_config
from cajuru_a1.exportacao import build_bundle
from cajuru_a1.pipeline import run_pipeline
from cajuru_a1.report import write_html_report, write_xlsx_report


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


def _say_factory():
    def say(message: str) -> None:
        print(message, flush=True)
    return say


def cmd_gui(_args) -> int:
    from cajuru_a1.gui import launch
    launch()
    return 0


def cmd_run(args) -> int:
    say = _say_factory()
    cfg = load_config(args.config)
    if args.pasta:
        cfg["pasta_certificados"] = args.pasta
    if args.planilha:
        cfg["planilhas_senha"] = args.planilha
    if args.saida:
        cfg["saida"] = args.saida

    errors = validate_config(cfg)
    if errors:
        for err in errors:
            print(f"ERRO: {err}", file=sys.stderr)
        return 2

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
    say(f"\nRelatório HTML: {html_path}")
    say(f"Relatório Excel: {xlsx_path}")

    if result.selected and not args.no_export:
        bundle = build_bundle(
            result.selected,
            output_dir,
            senha_manual=bool(opcoes.get("senha_manual_planilha", True)),
            rejeitados=result.rejected + [(c, "SUBSTITUIDO", c.extra.get("motivo_substituicao", "")) for c in result.duplicates],
        )
        say(f"\nLOTE GERADO em: {bundle['dir']}")
        say(f"  ZIP:       {bundle['zip'].name}")
        say(f"  Planilha:  {bundle['planilha'].name}")
        say(f"  CSV senhas:{bundle['csv_senhas'].name}")
        say(f"  Quantidade:{bundle['quantidade']}")
    elif not result.selected:
        say("\nNenhum certificado válido para exportar. Confira o relatório de rejeitados.")
    return 0


def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists() and not args.force:
        print(f"{target} já existe. Use --force para sobrescrever.")
        return 1
    save_config(load_config(None), target)  # type: ignore[arg-type]
    print(f"Configuração de exemplo criada em {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cajuru_a1", description="Prepara certificados A1 para importação no Jettax 360.")
    parser.add_argument("--config", default="config.yaml", help="Caminho do config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p_gui = sub.add_parser("gui", help="Abre o painel desktop")
    p_gui.set_defaults(func=cmd_gui)

    p_run = sub.add_parser("run", help="Processa e gera o lote de importação")
    p_run.add_argument("--pasta", help="Pasta com os certificados PFX/P12")
    p_run.add_argument("--planilha", action="append", help="Planilha de senhas (repetir para várias)")
    p_run.add_argument("--saida", help="Pasta de saída")
    p_run.add_argument("--no-export", action="store_true", help="Só gera relatórios, não cria o lote")
    p_run.set_defaults(func=cmd_run)

    p_init = sub.add_parser("init", help="Cria um config.yaml de exemplo")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    if not getattr(args, "command", None):
        # Sem subcomando: abre a GUI (comportamento padrão no Windows).
        return cmd_gui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
