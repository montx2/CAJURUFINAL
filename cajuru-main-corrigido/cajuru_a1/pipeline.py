from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from cajuru_a1.audit import serialize_changes, write_run_audit
from cajuru_a1.cnpjutil import best_doc_from_filename, format_cnpj, only_digits, pad_cnpj
from cajuru_a1.config import (
    effective_config,
    get_output_dir,
    get_state_dir,
    validate_config,
)
from cajuru_a1.dropbox_safe import ReadOnlyDropbox, cleanup_temp
from cajuru_a1.excel_passwords import load_excel_files
from cajuru_a1.matcher import match_all
from cajuru_a1.models import PipelineResult
from cajuru_a1.names import normalize_name
from cajuru_a1.passwords import candidate_passwords
from cajuru_a1.pdf import PdfInfo, inspect_pdf
from cajuru_a1.pfx import PfxInfo, file_sha256, inspect_file
from cajuru_a1.state import StateStore

log = logging.getLogger("cajuru_a1.pipeline")
Progress = Callable[[str], None]
STATUS_KEYS = {
    "pronto", "vencido", "nao_valido", "sem_senha", "invalido", "conflito",
    "ambiguo", "revisao_manual", "sem_cert", "sem_cert_novo", "duplicado",
    "extra_pfx", "substituido",
}


def refresh_stats(result: PipelineResult) -> dict[str, int]:
    for key in STATUS_KEYS:
        result.stats.pop(key, None)
    result.stats.update(Counter(match.status for match in result.matches))
    result.stats.update({
        "pfx": len(result.certificados),
        "pfx_abertos": sum(1 for cert in result.certificados if cert.opened),
        "pdf": len(result.documents),
        "pdf_abertos": sum(1 for document in result.documents if document.opened),
        "pdf_duplicados": sum(1 for document in result.documents if document.duplicate_sha256),
        "clientes_sem": len(result.clientes_sem),
        "prontos_enviaveis": sum(1 for match in result.matches if match.pode_enviar),
        "fontes_verificadas": len(result.source_inventory),
        "alterados": len(result.integrity_changes),
    })
    return result.stats


def _inventory_digest(inventory: dict) -> str:
    raw = json.dumps(inventory, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _security_limits(cfg: dict) -> tuple[int, int, int]:
    security = cfg.get("seguranca") or {}
    return (
        int(security.get("max_certificado_mb", 30)) * 1024 * 1024,
        int(security.get("max_pdf_mb", 60)) * 1024 * 1024,
        int(security.get("max_tentativas_senha", 250)),
    )


def _password_candidates(vault, cfg: dict, *, name: str, document: str = "", extra_names=None):
    options = cfg.get("opcoes") or {}
    security = cfg.get("seguranca") or {}
    global_scan_requested = bool(options.get("tentar_todas_senhas_da_planilha", False))
    global_scan_allowed = bool(security.get("permitir_varredura_global", False))
    return candidate_passwords(
        vault=vault,
        empresa=name,
        cnpj=document,
        extra_names=extra_names or [],
        years=options.get("anos_senha"),
        include_all_sheet=global_scan_requested and global_scan_allowed,
        include_common=bool(options.get("tentar_senhas_comuns", False)),
        max_candidates=int(security.get("max_tentativas_senha", 250)),
    )


def analyze(cfg: dict, log_fn: Progress | None = None, clientes_sem=None, clientes_com=None) -> PipelineResult:
    cfg = effective_config(cfg)
    say = log_fn or (lambda message: log.info(message))
    errors = validate_config(cfg)
    if errors:
        raise ValueError("Configuração inválida:\n- " + "\n- ".join(errors))

    source_path = Path(cfg["dropbox"]["pasta"]).expanduser()
    output_dir = get_output_dir(cfg)
    state_dir = get_state_dir(cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    dbx = ReadOnlyDropbox(source_path)
    max_pfx_bytes, max_pdf_bytes, max_attempts = _security_limits(cfg)
    retries = int(cfg.get("dropbox", {}).get("tentativas_copia", 2))
    inventory_limit = int(cfg.get("dropbox", {}).get("max_arquivos_inventario", 5000))

    say("Escopo: somente a pasta CERTIFICADOS/CERTIFICADOS A1 selecionada; a raiz do Dropbox não será lida.")
    say(f"Limite de segurança do inventário: {inventory_limit:,} arquivos.")
    say("1/5 — Auditando as duas planilhas (somente leitura)…")
    vault = load_excel_files(cfg.get("excel", {}).get("arquivos") or [], log_fn=say)
    if not vault.entries:
        say("ALERTA — nenhuma senha válida nos Excel; somente padrões plausíveis serão tentados.")
    if cfg.get("opcoes", {}).get("tentar_todas_senhas_da_planilha") and not cfg.get("seguranca", {}).get("permitir_varredura_global"):
        say("Barreira: varredura global de senhas solicitada, mas bloqueada pela política de segurança.")

    say("2/5 — Criando inventário integral SHA-256 da origem Dropbox…")
    inventory_before = dbx.inventory(progress=say, max_files=inventory_limit)
    cert_sources = dbx.list_certificates(progress=say)
    pdf_sources = dbx.list_pdfs(progress=say) if cfg.get("pdf", {}).get("habilitado", True) else []
    source_manifest = dbx.manifest(cert_sources)
    temp_root = dbx.make_temp_root()
    checkpoint = state_dir / "checkpoints.sqlite3"
    clients_by_doc = {
        pad_cnpj(only_digits(getattr(client, "cnpj", ""))): client
        for client in (clientes_sem or []) if client is not None and getattr(client, "cnpj", "")
    }
    known_company_names = list(dict.fromkeys(
        [entry.empresa for entry in vault.entries]
        + [getattr(client, "razao_social", "") for client in (clientes_sem or []) if client is not None]
    ))
    company_set_digest = hashlib.sha256(
        json.dumps(sorted(name.casefold() for name in known_company_names), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    pdf_state_kind = f"pdf:{company_set_digest}"
    certificates: list[PfxInfo] = []
    documents: list[PdfInfo] = []
    used_names: set[str] = set()
    state = StateStore(checkpoint)
    state.start(str(dbx.root), _inventory_digest(inventory_before))

    try:
        say("3/5 — Copiando e inspecionando PFX/P12 em área temporária isolada…")
        for index, source in enumerate(cert_sources, 1):
            relative = source.relative_to(dbx.root).as_posix()
            record = inventory_before.get(relative, {})
            expected_hash = str(record.get("sha256", ""))
            state.file(relative, expected_hash, "pfx", "iniciado")
            destination = temp_root / dbx.unique_temp_name(source, used_names)
            say(f"[{index}/{len(cert_sources)}] {source.name}")
            if int(record.get("size", 0)) > max_pfx_bytes:
                info = PfxInfo(str(source), "", source.name, expected_hash, int(record.get("size", 0)), cnpj_filename=best_doc_from_filename(source.name), error="Arquivo excede o limite de segurança configurado", error_code="arquivo_grande")
                certificates.append(info)
                state.file(relative, expected_hash, "pfx", "bloqueado", error_code=info.error_code)
                continue
            try:
                dbx.copy_one(source, destination, retries=retries)
            except Exception as exc:
                info = PfxInfo(str(source), "", source.name, expected_hash, int(record.get("size", 0)), cnpj_filename=best_doc_from_filename(source.name), error=f"Falha segura de cópia ({type(exc).__name__})", error_code="falha_copia")
                certificates.append(info)
                state.file(relative, expected_hash, "pfx", "erro", error_code=info.error_code)
                say(f"  BLOQUEADO — falha de cópia/integridade ({type(exc).__name__})")
                continue

            filename_doc = best_doc_from_filename(source.name) or ""
            names = [source.stem]
            if filename_doc and filename_doc in clients_by_doc:
                names.insert(0, clients_by_doc[filename_doc].razao_social)
            candidates = _password_candidates(vault, cfg, name=names[0], document=filename_doc, extra_names=names)
            try:
                info = inspect_file(
                    source, destination, candidates, max_bytes=max_pfx_bytes, max_attempts=max_attempts,
                    progress=lambda attempt, total, name=source.name: say(
                        f"  {name}: testando senha {attempt}/{total}…"
                    ),
                )
            except Exception as exc:
                info = PfxInfo(
                    str(source), str(destination), source.name, expected_hash,
                    int(record.get("size", 0)), cnpj_filename=filename_doc or None,
                    error=f"Falha isolada na inspeção ({type(exc).__name__})",
                    error_code="falha_inspecao",
                )
            certificates.append(info)
            state.file(relative, info.sha256, "pfx", "aberto" if info.opened else "bloqueado", attempts=info.attempts, error_code=info.error_code)
            if not info.opened:
                tag = "REVISÃO MANUAL"
            elif info.identity_conflict:
                # Abriu com a senha, mas o próprio arquivo tem um problema de
                # identidade (nome ≠ CNPJ interno, ou 2 documentos no
                # certificado) — isso vai virar CONFLITO no resultado final e
                # NUNCA autoriza envio sozinho. Não é seguro rotular como OK.
                tag = "CONFLITO"
            else:
                tag = "OK"
            say(f"  {tag} — {info.error or info.password_source or 'identidade lida'}")

        rejected_certificates = [item for item in dbx.rejected_files if item[0].suffix.casefold() in {".pfx", ".p12"}]
        for source, reason in rejected_certificates:
            relative = source.relative_to(dbx.root).as_posix()
            info = PfxInfo(
                str(source), "", source.name, "", 0,
                cnpj_filename=best_doc_from_filename(source.name),
                error=f"Origem insegura/ilegível bloqueada ({reason})",
                error_code="origem_insegura",
            )
            certificates.append(info)
            state.file(relative, "", "pfx", "bloqueado", error_code=info.error_code)

        say("4/5 — Inspecionando PDFs de apoio (nunca elegíveis para upload)…")
        pdf_options = cfg.get("pdf") or {}
        for index, source in enumerate(pdf_sources, 1):
            relative = source.relative_to(dbx.root).as_posix()
            record = inventory_before.get(relative, {})
            expected_hash = str(record.get("sha256", ""))
            cached = state.previous_file(relative, expected_hash, pdf_state_kind)
            if cached:
                safe_fields = {key: value for key, value in cached.items() if key in PdfInfo.__dataclass_fields__}
                safe_fields.update({"source_path": str(source), "temp_path": "", "filename": source.name, "sha256": expected_hash, "size": int(record.get("size", 0))})
                info = PdfInfo(**safe_fields)
                documents.append(info)
                state.file(relative, expected_hash, pdf_state_kind, "aberto" if info.opened else "bloqueado", error_code=info.error_code, metadata=safe_fields)
                say(f"  checkpoint reutilizado: {source.name}")
                continue
            state.file(relative, expected_hash, pdf_state_kind, "iniciado")
            destination = temp_root / dbx.unique_temp_name(source, used_names)
            if int(record.get("size", 0)) > max_pdf_bytes:
                info = PdfInfo(str(source), "", source.name, expected_hash, int(record.get("size", 0)), error_code="pdf_grande", error="PDF excede o limite", review_reason="REVISÃO MANUAL")
                documents.append(info)
                state.file(relative, expected_hash, pdf_state_kind, "bloqueado", error_code=info.error_code, metadata=asdict(info))
                continue
            try:
                dbx.copy_one(source, destination, retries=retries)
                candidates = _password_candidates(vault, cfg, name=source.stem, document=best_doc_from_filename(source.name) or "", extra_names=[source.stem])
                info = inspect_pdf(
                    source, destination, candidates,
                    max_bytes=max_pdf_bytes,
                    max_pages=int(pdf_options.get("max_paginas", 30)),
                    ocr=bool(pdf_options.get("ocr", False)),
                    ocr_max_pages=int(pdf_options.get("ocr_max_paginas", 3)),
                    tesseract_command=str(pdf_options.get("tesseract", "tesseract")),
                    known_companies=known_company_names,
                )
            except Exception as exc:
                info = PdfInfo(str(source), "", source.name, expected_hash, int(record.get("size", 0)), error_code="falha_pdf", error=f"Falha segura no PDF ({type(exc).__name__})", review_reason="REVISÃO MANUAL")
            documents.append(info)
            pdf_metadata = asdict(info)
            pdf_metadata["temp_path"] = ""
            state.file(relative, info.sha256, pdf_state_kind, "aberto" if info.opened else "bloqueado", error_code=info.error_code, metadata=pdf_metadata)
            if (index % 10) == 0:
                say(f"  {index}/{len(pdf_sources)} PDF(s) processados")

        for source, reason in [item for item in dbx.rejected_files if item[0].suffix.casefold() == ".pdf"]:
            documents.append(PdfInfo(
                str(source), "", source.name, "", 0,
                error_code="origem_insegura",
                error=f"Origem insegura/ilegível bloqueada ({reason})",
                review_reason="REVISÃO MANUAL",
            ))

        pdfs_by_hash: dict[str, list[PdfInfo]] = {}
        for document in documents:
            if document.sha256:
                pdfs_by_hash.setdefault(document.sha256, []).append(document)
        for same_content in pdfs_by_hash.values():
            if len(same_content) > 1:
                for document in same_content:
                    document.duplicate_sha256 = True
                    document.review_reason = "REVISÃO MANUAL: PDF duplicado byte a byte"

        # Fallback conservador: um PDF de apoio com o MESMO nome-base pode
        # fornecer nome/documento apenas para gerar novas candidatas. Mesmo se
        # abrir, a autorização continua dependendo do CNPJ interno do X.509.
        pdf_by_stem: dict[str, list[PdfInfo]] = {}
        for document in documents:
            if document.opened and not document.duplicate_sha256:
                pdf_by_stem.setdefault(normalize_name(Path(document.filename).stem), []).append(document)
        for cert in [item for item in certificates if not item.opened and item.temp_path]:
            sidecars = pdf_by_stem.get(normalize_name(Path(cert.filename).stem), [])
            if len(sidecars) != 1:
                continue
            sidecar = sidecars[0]
            if len(sidecar.company_names) != 1:
                continue
            pdf_document = sidecar.documents[0] if len(sidecar.documents) == 1 else ""
            candidates = _password_candidates(
                vault, cfg, name=sidecar.company_names[0], document=pdf_document,
                extra_names=[sidecar.company_names[0], Path(cert.filename).stem],
            )
            try:
                reopened = inspect_file(
                    Path(cert.source_path), Path(cert.temp_path), candidates,
                    max_bytes=max_pfx_bytes, max_attempts=max_attempts,
                )
            except Exception:
                continue
            cert.attempts += reopened.attempts
            if reopened.opened:
                previous_attempts = cert.attempts
                cert.__dict__.update(reopened.__dict__)
                cert.attempts = previous_attempts
                cert.password_source = (cert.password_source or "candidata") + "+pdf-sidecar-exato"
            relative = Path(cert.source_path).relative_to(dbx.root).as_posix()
            state.file(
                relative, cert.sha256, "pfx", "aberto" if cert.opened else "bloqueado",
                attempts=cert.attempts, error_code=cert.error_code,
            )

        without = [client for client in (clientes_sem or []) if client is not None]
        with_cert = [client for client in (clientes_com or []) if client is not None]
        options = cfg.get("opcoes") or {}
        matches = match_all(
            certificates, without, with_cert,
            atualizar_todos=bool(options.get("atualizar_todas_empresas", False)),
            escolher_mais_novo=bool(options.get("escolher_certificado_mais_novo", True)),
        )
        changes = dbx.verify_inventory(inventory_before, progress=say, max_files=inventory_limit)
        if changes:
            state.finish("integridade_falhou")
            details = "; ".join(f"{change.kind}:{change.path}" for change in changes[:8])
            write_run_audit(
                output_dir / "auditoria_ultima_execucao.json", action="analise", stats={}, manifest=source_manifest,
                dry_run=True, inventory_before=inventory_before, inventory_after={}, changes=changes,
                decisions=matches, outcome="integridade_falhou",
            )
            raise RuntimeError("A origem Dropbox mudou durante a análise; operação bloqueada: " + details)

        result = PipelineResult(
            certificates, without, with_cert, matches, str(temp_root), {}, True,
            "READ_ONLY_MODE ativo; inventário integral sem alterações.", source_manifest,
            inventory_before, str(dbx.root), documents, list(vault.findings), [], str(checkpoint), str(output_dir),
        )
        refresh_stats(result)
        state.finish("concluida", result.stats)
        write_run_audit(
            output_dir / "auditoria_ultima_execucao.json", action="analise", stats=result.stats,
            manifest=source_manifest, dry_run=True, inventory_before=inventory_before,
            inventory_after=inventory_before, changes=[], decisions=matches,
        )
        # Relatório de diagnóstico completo (por que falhou, validade,
        # histórico do que tinha antes). Nunca inclui valor de senha.
        try:
            from cajuru_a1.diagnostico import (
                build_diagnostico, write_diagnostico_excel, write_diagnostico_html,
            )
            diag = build_diagnostico(result, state=state)
            write_diagnostico_excel(diag, output_dir / "diagnostico.xlsx")
            write_diagnostico_html(diag, output_dir / "diagnostico.html", stats=result.stats)
            say(f"Diagnóstico completo: {output_dir / 'diagnostico.html'}")
        except Exception as diag_exc:  # diagnóstico não pode travar a análise
            say(f"AVISO: não foi possível gerar o diagnóstico ({type(diag_exc).__name__}: {diag_exc})")
        say(f"5/5 — Análise concluída com arquivos alterados=0, excluídos=0, movidos=0, criados=0. {result.stats}")
        return result
    except Exception:
        cleanup_temp(temp_root)
        raise
    finally:
        state.close()


def verify_result_integrity(result: PipelineResult, log_fn: Progress | None = None):
    if not result.source_root or not result.source_inventory:
        return []
    dbx = ReadOnlyDropbox(result.source_root)
    # A execução já foi limitada no inventário inicial; preserve essa capacidade
    # para a conferência posterior mesmo quando a configuração não acompanha o resultado.
    inventory_limit = max(5000, sum(1 for item in result.source_inventory.values() if item.get("type") == "file"))
    changes = dbx.verify_inventory(result.source_inventory, progress=log_fn, max_files=inventory_limit)
    result.integrity_changes = serialize_changes(changes)
    result.safety_ok = not changes
    result.safety_message = (
        "arquivos alterados=0, excluídos=0, movidos=0, criados=0"
        if not changes else "ALTERAÇÃO INESPERADA NA ORIGEM: " + "; ".join(f"{item.kind}:{item.path}" for item in changes[:8])
    )
    refresh_stats(result)
    return changes


def _verify_temp_copies(matches) -> None:
    for match in matches:
        cert = match.cert
        if not cert or not cert.temp_path:
            raise RuntimeError("Cópia temporária ausente; refaça a análise")
        path = Path(cert.temp_path)
        if not path.is_file() or file_sha256(path) != cert.sha256:
            raise RuntimeError(f"Integridade da cópia temporária falhou: {cert.filename}")


def enviar(cfg: dict, result: PipelineResult, log_fn: Progress | None = None, wait_login=None, wait_import=None):
    from cajuru_a1.jettax import JettaxBot
    from cajuru_a1.lote import build_importacao_jettax, cleanup_import_bundle

    cfg = effective_config(cfg)
    say = log_fn or (lambda message: log.info(message))
    options = cfg.get("opcoes") or {}
    dry_run = bool(options.get("dry_run", True))
    mode = str(options.get("modo_envio") or "lote").casefold()
    output_dir = get_output_dir(cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not result.safety_ok:
        raise RuntimeError("Barreira de segurança reprovada; envio proibido")
    if bool(options.get("enviar_vencidos", False)):
        raise RuntimeError("Envio de certificados vencidos é permanentemente bloqueado")
    ready = [match for match in result.matches if match.pode_enviar]
    if not ready:
        return []
    if len({_doc_key(match.cliente.cnpj) for match in ready}) != len(ready):
        raise RuntimeError("Mais de um envio para o mesmo documento foi bloqueado")
    changes = verify_result_integrity(result, log_fn=say)
    if changes:
        raise RuntimeError(result.safety_message)
    _verify_temp_copies(ready)

    if dry_run:
        simulados = [(match, "simulado") for match in ready]
        write_run_audit(
            output_dir / "auditoria_ultima_execucao.json", action="simulacao", stats=result.stats,
            manifest=result.source_manifest, dry_run=True, inventory_before=result.source_inventory,
            inventory_after=result.source_inventory, changes=[], decisions=result.matches,
            send_results=_serialize_send_results(simulados),
        )
        say(f"[SIMULAÇÃO] {len(ready)} certificado(s) validado(s). Nenhum ZIP com senha foi criado e nada foi enviado.")
        return simulados

    bot = JettaxBot(cfg, log_fn=say)
    zip_bundle: Path | None = None
    planilha_bundle: Path | None = None
    secure_dir: Path | None = None
    outcome = "falha"
    results = []
    try:
        bot.start()
        bot.login(wait_fn=wait_login)
        current_without = bot.list_without_certificate()
        current_docs = Counter(_doc_key(client.cnpj) for client in current_without)
        if any(current_docs[_doc_key(match.cliente.cnpj)] != 1 for match in ready):
            raise RuntimeError("A lista Jettax mudou ou contém CNPJ duplicado; lote inteiro abortado")
        if mode == "individual":
            falhas: list[tuple[str, str]] = []
            for match in ready:
                cliente = match.cliente
                rotulo = f"{cliente.razao_social} ({format_cnpj(cliente.cnpj)})"
                try:
                    status = bot.upload_individual(match, dry_run=False)
                    results.append((match, status))
                except Exception as exc:  # não trava o lote por causa de UM certificado
                    erro = f"{type(exc).__name__}: {exc}"
                    say(f"FALHOU — {rotulo}: {erro}")
                    falhas.append((rotulo, erro))
                    results.append((match, f"falha: {erro}"))
                    try:
                        out_shot = output_dir / f"erro_envio_{only_digits(cliente.cnpj)}.png"
                        bot.screenshot(out_shot)
                        say(f"Print do erro salvo em {out_shot}")
                    except Exception:
                        pass
            enviados = sum(1 for _, status in results if status == "enviado")
            say(f"Envio individual concluído: {enviados} enviado(s), {len(falhas)} falha(s) de {len(ready)}.")
            if falhas:
                say("Certificados NÃO enviados (revise manualmente): " + "; ".join(r for r, _ in falhas))
            outcome = "enviado" if not falhas else ("falha" if not enviados else "concluido_com_falhas")
        elif mode == "lote":
            manual_mode = bool(options.get("lote_senha_manual", True))
            save_csv = bool(options.get("salvar_senhas_csv", True))
            if manual_mode:
                # MODO MANUAL: salva o ZIP + a planilha oficial (senha em
                # branco) em output/lotes/lote_<timestamp>/ e opcionalmente um
                # CSV com as senhas validadas. O usuário importa no Jettax e
                # digita a senha ele mesmo. Os arquivos NÃO são apagados.
                from cajuru_a1.lote import build_persistent_bundle
                bundle = build_persistent_bundle(
                    ready, output_dir,
                    senha_manual=True, salvar_senhas_csv=save_csv,
                )
                zip_bundle = bundle["zip"]
                planilha_bundle = bundle["planilha"]
                # secure_dir vazio sinaliza ao finally para NÃO apagar.
                secure_dir = None
                say(f"LOTE MANUAL pronto em: {bundle['dir']}")
                say(f"  ZIP:        {zip_bundle.name}")
                say(f"  Planilha:   {planilha_bundle.name} (senha em branco)")
                if bundle.get("csv_senhas"):
                    say(f"  Senhas CSV: {bundle['csv_senhas'].name} (preencha manualmente)")
                status = bot.upload_lote_planilha(
                    zip_bundle, planilha_bundle, dry_run=False, wait_fn=wait_import,
                )
                results = [(match, status) for match in ready]
                outcome = "enviado_manual" if status != "simulado" else "simulado"
            else:
                secure_dir = Path(tempfile.mkdtemp(prefix="cajuru_a1_bundle_"))
                zip_bundle, planilha_bundle = build_importacao_jettax(ready, secure_dir)
                status = bot.upload_lote_planilha(zip_bundle, planilha_bundle, dry_run=False, wait_fn=wait_import)
                results = [(match, status) for match in ready]
                outcome = "enviado"
        else:
            raise ValueError("opcoes.modo_envio deve ser lote ou individual")
        return results
    finally:
        try:
            bot.close()
        finally:
            cleanup_error: Exception | None = None
            # No modo manual, o lote é persistente (output/lotes/) e NÃO deve
            # ser apagado — é o comportamento solicitado pelo usuário.
            if secure_dir:
                try:
                    cleanup_import_bundle(
                        zip_bundle or (secure_dir / "certificados_jettax.zip"),
                        planilha_bundle or (secure_dir / "planilha_importacao_jettax.xlsx"),
                    )
                except Exception as exc:  # não impedir que a auditoria final seja gravada
                    cleanup_error = exc
            final_changes = verify_result_integrity(result, log_fn=say)
            write_run_audit(
                output_dir / "auditoria_ultima_execucao.json", action="envio", stats=result.stats,
                manifest=result.source_manifest, dry_run=False, inventory_before=result.source_inventory,
                inventory_after=None, changes=final_changes, decisions=result.matches, outcome=outcome,
                send_results=_serialize_send_results(results),
            )
            if final_changes and outcome in ("enviado", "concluido_com_falhas"):
                raise RuntimeError("Envio terminou, mas a origem Dropbox mudou; alerta crítico registrado")
            if cleanup_error:
                raise RuntimeError("Falha crítica ao apagar lote transitório com senhas") from cleanup_error


def _doc_key(value: str) -> str:
    return pad_cnpj(only_digits(value))


def _serialize_send_results(results) -> list[dict]:
    """Registro auditável de quais certificados foram enviados/falharam.

    Nunca inclui senha; ``status`` é somente o resultado textual do envio.
    """
    serialized = []
    for match, status in results or []:
        client = getattr(match, "cliente", None)
        cert = getattr(match, "cert", None)
        serialized.append({
            "empresa": getattr(client, "razao_social", "") or "",
            "cnpj": format_cnpj(getattr(client, "cnpj", "")) if client and getattr(client, "cnpj", "") else "",
            "arquivo": getattr(cert, "filename", "") or "",
            "status": str(status),
        })
    return serialized


def finish(result: PipelineResult) -> None:
    integrity_error: RuntimeError | None = None
    try:
        changes = verify_result_integrity(result)
        if changes:
            integrity_error = RuntimeError(result.safety_message)
        if result.output_dir:
            write_run_audit(
                Path(result.output_dir) / "auditoria_ultima_execucao.json", action="finalizacao",
                stats=result.stats, manifest=result.source_manifest, dry_run=True,
                inventory_before=result.source_inventory, inventory_after=None,
                changes=changes, decisions=result.matches,
                outcome="ok" if not changes else "integridade_falhou",
            )
    finally:
        if result.temp_dir and not cleanup_temp(Path(result.temp_dir)):
            result.safety_ok = False
            integrity_error = integrity_error or RuntimeError("Pasta temporária não pôde ser removida com segurança")
    if integrity_error:
        raise integrity_error
