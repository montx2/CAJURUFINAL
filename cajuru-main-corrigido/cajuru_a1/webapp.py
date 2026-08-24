"""Painel web do Cajuru A1 — controle elegante no navegador.

Roda localmente (127.0.0.1) e oferece:
- Dashboard com estatísticas e últimos relatórios;
- Configuração (pasta Dropbox, planilhas, opções);
- Análise do Dropbox + conciliação sem depender do Jettax;
- Geração do lote MANUAL (ZIP + planilha com senha em branco + CSV de senhas);
- Download e visualização dos relatórios (HTML, Excel, diagnóstico);
- Log em tempo real via polling.

É 100% opcional: a GUI Tkinter continua existindo. No ambiente de
desenvolvimento sem configuração, o painel sobe em modo demonstração.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cajuru_a1.config import (
    _blank,
    effective_config,
    get_output_dir,
    load_config,
    save_config,
    validate_config,
)
from cajuru_a1.matcher import match_all
from cajuru_a1.models import PipelineResult
from cajuru_a1.pipeline import analyze, refresh_stats
from cajuru_a1.report import write_excel_report, write_html_report
from cajuru_a1.diagnostico import (
    STATUS_COLOR, STATUS_LABEL, build_diagnostico, write_diagnostico_excel, write_diagnostico_html,
)

# Ordem de exibição da barra de saúde: o que está pronto para envio primeiro,
# depois o que precisa de atenção, depois o que está fora do escopo de envio.
_HEALTH_ORDER = [
    "pronto", "substituido", "revisao_manual", "ambiguo", "sem_senha",
    "nao_valido", "vencido", "invalido", "conflito", "duplicado",
    "extra_pfx", "sem_cert", "sem_cert_novo",
]


def _health_segments(stats: dict | None) -> list[dict]:
    """Resume o `stats` da última execução em segmentos para a barra de saúde
    do dashboard, reaproveitando os mesmos rótulos/cores do diagnóstico."""
    stats = stats or {}
    total = sum(int(stats.get(key, 0) or 0) for key in _HEALTH_ORDER)
    if total <= 0:
        return []
    segments = []
    for key in _HEALTH_ORDER:
        count = int(stats.get(key, 0) or 0)
        if count <= 0:
            continue
        segments.append({
            "key": key,
            "label": STATUS_LABEL.get(key, key),
            "color": STATUS_COLOR.get(key, "7F8C8D"),
            "count": count,
            "pct": round(count / total * 100, 1),
        })
    return segments

log = logging.getLogger("cajuru_a1.web")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error
    message: str = ""
    logs: list[str] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    result: Any = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "logs": list(self.logs[-300:]),
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        jid = datetime.now().strftime("%Y%m%d%H%M%S%f")
        job = Job(id=jid, kind=kind, started=datetime.now().strftime("%d/%m/%Y %H:%M"))
        with self._lock:
            self._jobs[jid] = job
        return job

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def latest(self, kind: str | None = None) -> Job | None:
        with self._lock:
            items = list(self._jobs.values())
        if kind:
            items = [j for j in items if j.kind == kind]
        return items[-1] if items else None

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())[-limit:]


JOBS = JobManager()


def _now_stamp() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def create_app(config_path: str | Path | None = None):
    """Cria o app Flask. ``config_path`` aponta para o config.yaml do projeto."""
    try:
        from flask import (
            Flask, Response, abort, jsonify, render_template, request, send_file,
        )
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Flask não está instalado. Execute: pip install flask"
        ) from exc

    base_dir = Path(__file__).resolve().parent
    templates = base_dir / "web" / "templates"
    static = base_dir / "web" / "static"
    app = Flask(
        __name__,
        template_folder=str(templates),
        static_folder=str(static),
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["CONFIG_PATH"] = str(Path(config_path or "config.yaml").resolve())
    app.jinja_env.globals["STATUS_LABEL"] = STATUS_LABEL

    def load_cfg() -> dict:
        try:
            return load_config(app.config["CONFIG_PATH"])
        except Exception:
            return _blank()

    def cfg_path() -> Path:
        return Path(app.config["CONFIG_PATH"])

    # ------------------------------------------------------------------ Pages
    @app.route("/")
    def page_dashboard():
        cfg = load_cfg()
        output = get_output_dir(cfg)
        bundles = _list_bundles(output)
        latest_analyze = JOBS.latest("analyze") or JOBS.latest("rodar_tudo")
        # Os KPIs e a barra de saúde sobrevivem a um reinício do painel: lemos
        # o resumo persistido da última execução, não só o job em memória.
        last_audit = output / "auditoria_ultima_execucao.json"
        persisted = _read_json(last_audit) if last_audit.exists() else {}
        stats = persisted.get("stats") or {}
        if not stats and latest_analyze and latest_analyze.result:
            stats = latest_analyze.result.get("stats") or {}
        return render_template(
            "dashboard.html",
            cfg=cfg,
            output_dir=str(output),
            bundles=bundles,
            latest_job=latest_analyze.to_dict() if latest_analyze else None,
            stats=stats,
            health=_health_segments(stats),
            reports=_list_reports(output),
            errors=validate_config(effective_config(cfg)),
            now=_now_stamp(),
        )

    @app.route("/configuracao", methods=["GET"])
    def page_config():
        cfg = load_cfg()
        errors = validate_config(effective_config(cfg))
        return render_template(
            "config.html",
            cfg=effective_config(cfg),
            errors=errors,
            config_path=str(cfg_path()),
            out_dir=str(get_output_dir(cfg)),
        )

    @app.route("/certificados")
    def page_certificates():
        """Mostra o que foi lido na última análise."""
        cfg = load_cfg()
        output = get_output_dir(cfg)
        last_audit = output / "auditoria_ultima_execucao.json"
        data = _read_json(last_audit) if last_audit.exists() else {}
        decisions = data.get("decisions", [])
        stats = data.get("stats", {})
        return render_template(
            "certificates.html",
            decisions=decisions,
            stats=stats,
            health=_health_segments(stats),
            has_data=bool(decisions),
            output_dir=str(output),
        )

    @app.route("/relatorios")
    def page_reports():
        cfg = load_cfg()
        output = get_output_dir(cfg)
        return render_template(
            "reports.html",
            reports=_list_reports(output),
            output_dir=str(output),
        )

    @app.route("/lotes")
    def page_bundles():
        cfg = load_cfg()
        output = get_output_dir(cfg)
        return render_template(
            "bundles.html",
            bundles=_list_bundles(output),
            output_dir=str(output),
        )

    # ----------------------------------------------------------------- API/JSON
    @app.get("/api/status")
    def api_status():
        cfg = load_cfg()
        output = get_output_dir(cfg)
        reports = _list_reports(output)
        return jsonify({
            "ok": True,
            "output_dir": str(output),
            "config_path": str(cfg_path()),
            "config_valid": not validate_config(effective_config(cfg)),
            "reports": [r["name"] for r in reports],
            "bundles": len(_list_bundles(output)),
            "latest_job": JOBS.latest().to_dict() if JOBS.latest() else None,
        })

    @app.post("/api/config")
    def api_save_config():
        cfg = load_cfg()
        data = request.get_json(force=True, silent=True) or request.form.to_dict()
        cfg.setdefault("dropbox", {})["pasta"] = (data.get("dropbox_pasta") or "").strip()
        arquivos = [p for p in (data.get("excel_1", ""), data.get("excel_2", "")) if p and p.strip()]
        cfg.setdefault("excel", {})["arquivos"] = arquivos
        cfg.setdefault("jettax", {})["url"] = data.get("jettax_url") or "https://admin.jettax360.com.br"
        opcoes = cfg.setdefault("opcoes", {})
        opcoes["dry_run"] = _as_bool(data.get("dry_run", True))
        opcoes["atualizar_todas_empresas"] = _as_bool(data.get("atualizar_todas_empresas", False))
        opcoes["escolher_certificado_mais_novo"] = _as_bool(data.get("escolher_certificado_mais_novo", True))
        opcoes["lote_senha_manual"] = _as_bool(data.get("lote_senha_manual", True))
        opcoes["salvar_senhas_csv"] = _as_bool(data.get("salvar_senhas_csv", True))
        opcoes["tentar_senhas_comuns"] = _as_bool(data.get("tentar_senhas_comuns", False))
        # "Varredura global" tem duas chaves porque exige intenção explícita em
        # dois lugares: a opção do usuário e a permissão de segurança. O painel
        # trata como um único controle para não deixar a opção "ligada, mas sem
        # efeito" de forma silenciosa.
        varredura_global = _as_bool(data.get("tentar_todas_senhas_da_planilha", False))
        opcoes["tentar_todas_senhas_da_planilha"] = varredura_global
        cfg.setdefault("seguranca", {})["permitir_varredura_global"] = varredura_global
        cfg.setdefault("dropbox", {})["somente_leitura"] = True
        errors = validate_config(effective_config(cfg))
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        save_config(cfg, cfg_path())
        return jsonify({"ok": True})

    def _full_analysis(cfg: dict, log_fn) -> PipelineResult:
        """Replica o "Fluxo completo" da GUI: abre o Chrome do Jettax, espera o
        login (assistido — a senha nunca é gravada), lista os clientes reais
        e só então lê o Dropbox. Sem essa etapa o matcher não tem com quem
        conciliar e todo certificado cairia em EXTRA_PFX; com ela, o CNPJ do
        X.509 é comparado ao CNPJ real do cliente Jettax e o nome do
        certificado desempata automaticamente CNPJs duplicados no cadastro.
        """
        from cajuru_a1.jettax import JettaxBot
        from cajuru_a1.reattempt import reattempt_locked

        atualizar_todas = bool(cfg.get("opcoes", {}).get("atualizar_todas_empresas", False))
        mais_novo = bool(cfg.get("opcoes", {}).get("escolher_certificado_mais_novo", True))

        log_fn("Etapa 1/5 — abrindo o Chrome do Jettax…")
        bot = JettaxBot(cfg, log_fn=log_fn)
        clientes_sem: list = []
        clientes_com: list = []
        try:
            bot.start()
            bot.login()
            log_fn("Etapa 2/5 — listando os clientes reais do Jettax…")
            if atualizar_todas:
                clientes_sem, clientes_com = bot.list_all_clients()
                log_fn(f"Jettax: {len(clientes_sem)} sem A1, {len(clientes_com)} já com A1 (renovação total).")
            else:
                clientes_sem = bot.list_without_certificate()
                log_fn(f"Jettax: {len(clientes_sem)} empresa(s) sem certificado.")
        finally:
            bot.close()

        log_fn("Etapa 3/5 — lendo e auditando a pasta do Dropbox…")
        result = analyze(cfg, log_fn=log_fn, clientes_sem=clientes_sem, clientes_com=clientes_com)

        log_fn("Etapa 4/5 — segunda tentativa de senha usando os nomes reais do Jettax…")
        reattempt_locked(result, cfg, clientes_sem)
        result.clientes_sem = clientes_sem
        result.clientes_com = clientes_com
        result.matches = match_all(
            result.certificados, clientes_sem, clientes_com,
            atualizar_todos=atualizar_todas, escolher_mais_novo=mais_novo,
        )
        refresh_stats(result)

        output = get_output_dir(cfg)
        write_excel_report(result, output / "relatorio.xlsx")
        write_html_report(result, output / "relatorio.html")
        # Reaproveita o checkpoint da própria análise para trazer o histórico
        # de execuções anteriores ("o que tinha antes") no diagnóstico —
        # exatamente como o diagnóstico interno do pipeline faz.
        state_for_diag = None
        try:
            if result.checkpoint_path:
                from cajuru_a1.state import StateStore
                state_for_diag = StateStore(Path(result.checkpoint_path))
            diag = build_diagnostico(result, state=state_for_diag)
            write_diagnostico_excel(diag, output / "diagnostico.xlsx")
            write_diagnostico_html(diag, output / "diagnostico.html", stats=result.stats)
        finally:
            if state_for_diag is not None:
                state_for_diag.close()
        return result

    @app.post("/api/analisar")
    def api_analyze():
        """Pré-checagem rápida, só do Dropbox: abre os PFX com as senhas da
        planilha e mostra validade/erros, sem falar com o Jettax. Como não há
        cliente para conciliar, nenhum certificado fica PRONTO aqui — use
        "Rodar tudo" para isso."""
        cfg = effective_config(load_cfg())
        errors = validate_config(cfg)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        job = JOBS.create("analyze")

        def run():
            def log_fn(msg):
                job.logs.append(f"[{_now_stamp()}] {msg}")

            try:
                log_fn("Iniciando leitura e auditoria do Dropbox (sem conectar ao Jettax)…")
                result = analyze(cfg, log_fn=log_fn)
                output = get_output_dir(cfg)
                write_excel_report(result, output / "relatorio.xlsx")
                write_html_report(result, output / "relatorio.html")
                # analyze() já grava diagnostico.html/xlsx com histórico de
                # execuções anteriores; não é preciso regenerar aqui.
                job.result = {
                    "stats": result.stats,
                    "output_dir": str(output),
                    "prontos": result.stats.get("pronto", 0),
                    "total": len(result.matches),
                }
                job.status = "done"
                job.message = f"Auditoria do Dropbox concluída: {len(result.matches)} certificado(s) inspecionado(s)."
                job.finished = _now_stamp()
                log_fn(job.message)
            except Exception as exc:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = job.error
                job.finished = _now_stamp()
                job.logs.append(traceback.format_exc())

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job.id})

    @app.get("/api/job/<jid>")
    def api_job(jid):
        job = JOBS.get(jid)
        if not job:
            abort(404)
        return jsonify(job.to_dict())

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify({"jobs": [j.to_dict() for j in JOBS.recent(30)]})

    @app.post("/api/gerar-lote-manual")
    def api_manual_bundle():
        """Conecta ao Jettax (só para listar clientes, leitura), concilia e
        gera o ZIP + planilha (senha em branco) + CSV de senhas — para você
        importar/enviar manualmente no Jettax. Nada é escrito no Jettax por
        aqui; o painel web nunca envia automaticamente."""
        cfg = effective_config(load_cfg())
        errors = validate_config(cfg)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        job = JOBS.create("lote_manual")

        def run():
            def log_fn(msg):
                job.logs.append(f"[{_now_stamp()}] {msg}")
            try:
                result = _full_analysis(cfg, log_fn)
                output = get_output_dir(cfg)
                from cajuru_a1.lote import build_persistent_bundle
                ready = [m for m in result.matches if m.status == "pronto"]
                if not ready:
                    job.result = {"stats": result.stats, "prontos": 0, "total": len(result.matches)}
                    job.status = "done"
                    job.message = "Nenhum certificado PRONTO para o lote. Veja o diagnóstico."
                    job.finished = _now_stamp()
                    return
                log_fn(f"Etapa 5/5 — montando ZIP + planilha com {len(ready)} certificado(s)…")
                bundle = build_persistent_bundle(
                    ready, output,
                    senha_manual=bool(cfg.get("opcoes", {}).get("lote_senha_manual", True)),
                    salvar_senhas_csv=bool(cfg.get("opcoes", {}).get("salvar_senhas_csv", True)),
                )
                job.result = {
                    "dir": str(bundle["dir"]),
                    "zip": str(bundle["zip"]),
                    "planilha": str(bundle["planilha"]),
                    "csv_senhas": str(bundle["csv_senhas"]) if bundle.get("csv_senhas") else None,
                    "quantidade": len(ready),
                    "stats": result.stats,
                }
                job.status = "done"
                job.message = f"Lote manual com {len(ready)} certificado(s) em: {bundle['dir']}"
                job.finished = _now_stamp()
                log_fn(job.message)
            except Exception as exc:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = job.error
                job.finished = _now_stamp()
                job.logs.append(traceback.format_exc())

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job.id})

    @app.post("/api/rodar-tudo")
    def api_run_all():
        """Fluxo automático em um clique — até o ponto de envio: conecta ao
        Jettax (só leitura, para listar os clientes reais), lê o Dropbox,
        concilia (com desempate por nome quando o CNPJ é duplicado no
        Jettax), gera relatório + diagnóstico completo e, se houver
        certificados PRONTO, já monta o ZIP + planilha + CSV de senhas do
        lote manual. O envio ao Jettax em si é sempre feito por você — o
        painel web nunca escreve no Jettax automaticamente."""
        cfg = effective_config(load_cfg())
        errors = validate_config(cfg)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        job = JOBS.create("rodar_tudo")

        def run():
            def log_fn(msg):
                job.logs.append(f"[{_now_stamp()}] {msg}")
            try:
                result = _full_analysis(cfg, log_fn)
                output = get_output_dir(cfg)
                ready = [m for m in result.matches if m.status == "pronto"]
                bundle = None
                if ready:
                    log_fn(f"Etapa 5/5 — montando ZIP + planilha + CSV de senhas com {len(ready)} certificado(s)…")
                    from cajuru_a1.lote import build_persistent_bundle
                    bundle = build_persistent_bundle(
                        ready, output,
                        senha_manual=bool(cfg.get("opcoes", {}).get("lote_senha_manual", True)),
                        salvar_senhas_csv=bool(cfg.get("opcoes", {}).get("salvar_senhas_csv", True)),
                    )
                else:
                    log_fn("Etapa 5/5 — nenhum certificado ficou PRONTO; nada para empacotar. Veja o diagnóstico.")

                job.result = {
                    "stats": result.stats,
                    "output_dir": str(output),
                    "prontos": len(ready),
                    "total": len(result.matches),
                    "bundle": {
                        "dir": str(bundle["dir"]),
                        "zip": str(bundle["zip"]),
                        "planilha": str(bundle["planilha"]),
                        "csv_senhas": str(bundle["csv_senhas"]) if bundle.get("csv_senhas") else None,
                    } if bundle else None,
                }
                job.status = "done"
                job.message = (
                    f"Concluído: {len(ready)} certificado(s) PRONTO de {len(result.matches)} decisões — "
                    + ("ZIP, planilha e CSV de senhas prontos em output/lotes/. Importe/envie você mesmo no Jettax." if bundle else "nenhum lote gerado (veja o diagnóstico).")
                )
                job.finished = _now_stamp()
                log_fn(job.message)
            except Exception as exc:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = job.error
                job.finished = _now_stamp()
                job.logs.append(traceback.format_exc())

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job.id})

    # ---------------------------------------------------------------- Downloads
    @app.get("/download/<path:fname>")
    def download(fname):
        cfg = load_cfg()
        output = get_output_dir(cfg)
        path = (output / fname).resolve()
        try:
            path.relative_to(output.resolve())
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        return send_file(str(path), as_attachment=True)

    @app.get("/view/<path:fname>")
    def view(fname):
        cfg = load_cfg()
        output = get_output_dir(cfg)
        path = (output / fname).resolve()
        try:
            path.relative_to(output.resolve())
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        if path.suffix.lower() == ".html":
            return Response(path.read_text(encoding="utf-8"), mimetype="text/html")
        return send_file(str(path))

    @app.get("/bundles/<bundle_dir>/<path:fname>")
    def download_bundle(bundle_dir, fname):
        cfg = load_cfg()
        output = get_output_dir(cfg)
        base = output / "lotes"
        path = (base / bundle_dir / fname).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        return send_file(str(path), as_attachment=True)

    return app


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "sim", "yes", "on", "ligado")


def _read_json(path: Path) -> dict:
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        p = output / name
        if p.is_file():
            st = p.stat()
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
    result = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        files = {}
        for name in ("certificados_jettax.zip", "planilha_importacao_jettax.xlsx",
                     "senhas_para_preenchimento_manual.csv", "LEIA-ME.txt"):
            p = d / name
            if p.is_file():
                files[name] = round(p.stat().st_size / 1024, 1)
        if files:
            result.append({
                "name": d.name,
                "path": str(d),
                "files": files,
                "mtime": datetime.fromtimestamp(d.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
            })
    return result


def run_server(host: str = "127.0.0.1", port: int = 8765, debug: bool = False,
               config_path: str | Path | None = None, open_browser: bool = False) -> None:
    """Sobe o painel web. Por padrão escuta somente em 127.0.0.1 (sem rede)."""
    app = create_app(config_path)
    if open_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"\n  Cajuru A1 — painel web em http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


if __name__ == "__main__":  # pragma: no cover
    run_server(open_browser=True)
