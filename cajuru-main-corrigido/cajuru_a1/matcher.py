"""Conciliação conservadora, baseada em evidências e confiança."""

from __future__ import annotations

from collections import defaultdict

from cajuru_a1.cnpjutil import is_valid_doc, only_digits, pad_cnpj
from cajuru_a1.models import JetaxClient, MatchResult
from cajuru_a1.names import exact_normalized, normalize_name, similarity
from cajuru_a1.pfx import PfxInfo, pick_newest


def _doc(value: str | None) -> str:
    digits = pad_cnpj(only_digits(value))
    return digits if is_valid_doc(digits) else ""


def _index(clients: list[JetaxClient]) -> dict[str, list[JetaxClient]]:
    result: dict[str, list[JetaxClient]] = defaultdict(list)
    for client in clients or []:
        if client:
            document = _doc(client.cnpj)
            if document:
                result[document].append(client)
    return result


def _pick_by_name(cert, candidates: list[JetaxClient]) -> tuple[JetaxClient, float] | None:
    """Desempata automaticamente quando um CNPJ interno aponta para mais de um
    cliente Jettax, comparando o nome extraído do X.509 com a razão social de
    cada candidato.

    Só resolve quando existe um único candidato com alta similaridade e
    margem clara para o segundo colocado (mesmos limiares já usados em
    ``PasswordVault.lookup_by_name``). Caso contrário devolve ``None`` e o
    chamador mantém o caso como AMBÍGUO para revisão manual — a evidência de
    nome nunca é usada sozinha para autorizar envio; ela só desempata entre
    candidatos que já compartilham o mesmo CNPJ verificado no X.509.
    """
    subject_name = str(getattr(cert, "company_from_cert", "") or "")
    if not subject_name or len(candidates) < 2:
        return None
    ranked = sorted(
        ((similarity(subject_name, candidate.razao_social), candidate) for candidate in candidates),
        key=lambda item: item[0], reverse=True,
    )
    best_score, best_client = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score >= 94.0 and best_score - second_score >= 8.0:
        return best_client, best_score
    return None


def _classify(client: JetaxClient, cert, method: str, confidence: float, reason: str, evidence: list[str]) -> MatchResult:
    if getattr(cert, "identity_conflict", False):
        return MatchResult("conflito", client, cert, method, confidence, getattr(cert, "error", "Identidade em conflito"), evidence)
    if not getattr(cert, "opened", False):
        return MatchResult("sem_senha", client, cert, method, confidence, getattr(cert, "error", None) or "Não foi possível abrir o certificado", evidence)
    if not getattr(cert, "has_private_key", False):
        return MatchResult("invalido", client, cert, method, confidence, "O PKCS#12 não contém chave privada utilizável", evidence)
    if getattr(cert, "not_yet_valid", False):
        return MatchResult("nao_valido", client, cert, method, confidence, f"Certificado ainda não válido até {getattr(cert, 'not_before', '')}", evidence)
    if getattr(cert, "expired", False):
        return MatchResult("vencido", client, cert, method, confidence, f"Certificado vencido em {getattr(cert, 'not_after', '')}", evidence)
    if method != "cnpj_certificado":
        return MatchResult("revisao_manual", client, cert, method, confidence, reason, evidence)
    return MatchResult("pronto", client, cert, method, confidence, reason, evidence)


def match_all(
    certs,
    sem_certificado: list[JetaxClient],
    com_certificado: list[JetaxClient] | None = None,
    *,
    atualizar_todos: bool = False,
    escolher_mais_novo: bool = True,
) -> list[MatchResult]:
    """Concilia PFX com clientes Jettax.

    Parâmetros novos:
    - ``atualizar_todos``: quando True, certificados de clientes que JÁ possuem
      A1 também ficam elegíveis (renovação do certificado existente) em vez de
      ``extra_pfx``. A lista ``com_certificado`` precisa ter sido fornecida
      (normalmente via listagem "todos os clientes" no Jettax).
    - ``escolher_mais_novo``: quando True (padrão), entre vários PFX diferentes
      com o mesmo CNPJ interno, escolhe o mais atualizado (validade mais longa,
      início mais recente, mtime) e marca os outros como ``substituido``.
      Quando False, preserva o comportamento antigo (ambos ``ambiguo``).
    """
    certs: list[PfxInfo] = [cert for cert in (certs or []) if cert is not None]
    without = [client for client in (sem_certificado or []) if client is not None]
    with_certificate = [client for client in (com_certificado or []) if client is not None]
    # No modo "atualizar todos", todos os clientes (com e sem A1) são alvos
    # elegíveis. O conjunto "sem certificado" continua existindo para o relatório
    # (saber quem realmente não tem nada), mas a decisão de PRONTO pode recair
    # sobre um cliente que já tenha A1 vencido/antigo.
    if atualizar_todos:
        elegivel = list(without)
        seen_docs = {_doc(c.cnpj) for c in elegivel}
        for client in with_certificate:
            if _doc(client.cnpj) not in seen_docs:
                elegivel.append(client)
                seen_docs.add(_doc(client.cnpj))
    else:
        elegivel = list(without)

    idx_sem = _index(without)
    idx_com = _index(with_certificate)
    idx_elegivel = _index(elegivel)
    used_clients: set[str] = set()
    used_certs: set[int] = set()
    results: list[MatchResult] = []

    # Arquivos byte a byte iguais: todos ficam bloqueados.
    by_hash: dict[str, list[int]] = defaultdict(list)
    for index, cert in enumerate(certs):
        digest = str(getattr(cert, "sha256", "") or "")
        if digest:
            by_hash[digest].append(index)
    for digest, indexes in by_hash.items():
        if len(indexes) < 2:
            continue
        for index in indexes:
            certs[index].duplicate_sha256 = True
            used_certs.add(index)
            results.append(MatchResult(
                "duplicado", None, certs[index], "sha256", 100.0,
                "Conteúdo idêntico a outro arquivo; nenhum dos duplicados pode ser enviado.",
                ["SHA-256 repetido"],
            ))

    # Dois PFX DIFERENTES com o mesmo CNPJ interno:
    # - com escolher_mais_novo=True (padrão): seleciona o mais atualizado e
    #   marca os demais como 'substituido' (ficam para revisão/histórico, mas
    #   não bloqueiam o envio do escolhido).
    # - com False: comportamento legado (todos 'ambiguo').
    by_internal_doc: dict[str, list[int]] = defaultdict(list)
    for index, cert in enumerate(certs):
        if index not in used_certs:
            document = _doc(getattr(cert, "cnpj_cert", None))
            if document:
                by_internal_doc[document].append(index)
    for document, indexes in by_internal_doc.items():
        if len(indexes) <= 1:
            continue
        group = [certs[i] for i in indexes]
        if escolher_mais_novo:
            winner, losers = pick_newest(group)
            winner_index = None
            if winner is not None:
                for i in indexes:
                    if certs[i] is winner:
                        winner_index = i
                        break
            if winner_index is None or winner is None:
                # Nenhum abre ou todos são idênticos semanticamente -> ambíguo.
                client = idx_elegivel.get(document, [None])[0]
                for index in indexes:
                    used_certs.add(index)
                    results.append(MatchResult(
                        "ambiguo", client, certs[index], "cnpj_certificado", 100.0,
                        "Mais de um arquivo contém o mesmo CNPJ interno e não foi possível escolher o mais novo automaticamente.",
                        ["CNPJ interno repetido em múltiplos PFX"],
                    ))
                if client:
                    used_clients.add(document)
                continue
            used_certs.add(winner_index)
            for loser in losers:
                for i in indexes:
                    if certs[i] is loser and i not in used_certs:
                        used_certs.add(i)
                        motivo = loser.extra.get("motivo_substituicao", "Substituído por certificado mais atualizado.")
                        results.append(MatchResult(
                            "substituido", None, loser, "cnpj_certificado", 90.0,
                            motivo, ["CNPJ interno repetido", "certificado mais novo selecionado"],
                        ))
            # Classifica o vencedor como um certificado único: PRONTO se o
            # cliente elegível existir; VENCIDO/SEM_SENHA etc. caso contrário.
            if document in idx_elegivel:
                candidates = idx_elegivel[document]
                resolved = _pick_by_name(winner, candidates) if len(candidates) != 1 else None
                if len(candidates) != 1 and resolved is None:
                    results.append(MatchResult(
                        "ambiguo", candidates[0], winner, "cnpj_certificado", 100.0,
                        "Mais de um cliente Jettax possui o mesmo CNPJ e o nome do certificado não permitiu desempate seguro.",
                        ["CNPJ interno exato", "CNPJ duplicado no Jettax", "nome insuficiente para desempate automático"],
                    ))
                elif len(candidates) != 1:
                    client, score = resolved
                    reason = (
                        f"CNPJ interno bate com {len(candidates)} clientes Jettax; o nome do certificado "
                        f"confirmou \"{client.razao_social}\" com {score:.1f}% de similaridade (mais novo selecionado)."
                    )
                    evidence = [
                        "CNPJ extraído do X.509", "CNPJ duplicado no Jettax",
                        f"nome do certificado desempata ({score:.1f}%)", "certificado mais novo escolhido",
                    ]
                    results.append(_classify(client, winner, "cnpj_certificado", 100.0, reason, evidence))
                    used_clients.add(document)
                else:
                    client = candidates[0]
                    reason = "CNPJ interno do certificado é idêntico ao CNPJ único do cliente Jettax (mais novo selecionado)."
                    evidence = ["CNPJ extraído do X.509", "CNPJ Jettax exato", "certificado mais novo escolhido"]
                    if atualizar_todos and document in idx_com:
                        reason = "Renovação: cliente já possui A1; o certificado mais novo foi conciliado para substituição."
                        evidence.append("modo atualizar_todos")
                    results.append(_classify(client, winner, "cnpj_certificado", 100.0, reason, evidence))
                used_clients.add(document)
        else:
            client = idx_elegivel.get(document, [None])[0]
            for index in indexes:
                used_certs.add(index)
                results.append(MatchResult(
                    "ambiguo", client, certs[index], "cnpj_certificado", 100.0,
                    "Mais de um arquivo diferente contém o mesmo CNPJ interno; seleção manual obrigatória.",
                    ["CNPJ interno repetido em múltiplos PFX"],
                ))
            if client:
                used_clients.add(document)

    # CNPJ extraído criptograficamente do certificado: única via automática.
    for index, cert in enumerate(certs):
        if index in used_certs:
            continue
        internal_doc = _doc(getattr(cert, "cnpj_cert", None))
        if not internal_doc:
            continue
        if internal_doc in idx_sem and internal_doc in idx_com and not atualizar_todos:
            results.append(MatchResult(
                "conflito", idx_sem[internal_doc][0], cert, "cnpj_certificado", 100.0,
                "O mesmo CNPJ apareceu simultaneamente nas listas com e sem certificado do Jettax.",
                ["listas Jettax conflitantes"],
            ))
        elif internal_doc in idx_elegivel:
            candidates = idx_elegivel[internal_doc]
            resolved = _pick_by_name(cert, candidates) if len(candidates) != 1 else None
            if len(candidates) != 1 and resolved is None:
                results.append(MatchResult(
                    "ambiguo", candidates[0], cert, "cnpj_certificado", 100.0,
                    "Mais de um cliente Jettax possui o mesmo CNPJ e o nome do certificado não permitiu desempate seguro.",
                    ["CNPJ interno exato", "CNPJ duplicado no Jettax", "nome insuficiente para desempate automático"],
                ))
            elif len(candidates) != 1:
                client, score = resolved
                reason = (
                    f"CNPJ interno bate com {len(candidates)} clientes Jettax; o nome do certificado "
                    f"confirmou \"{client.razao_social}\" com {score:.1f}% de similaridade."
                )
                evidence = [
                    "CNPJ extraído do X.509", "CNPJ duplicado no Jettax",
                    f"nome do certificado desempata ({score:.1f}%)",
                ]
                results.append(_classify(client, cert, "cnpj_certificado", 100.0, reason, evidence))
            else:
                client = candidates[0]
                reason = "CNPJ interno do certificado é idêntico ao CNPJ único do cliente Jettax."
                evidence = ["CNPJ extraído do X.509", "CNPJ Jettax exato"]
                if atualizar_todos and internal_doc in idx_com:
                    reason = (
                        "Renovação: cliente já possui A1 no Jettax; o certificado mais novo foi "
                        "conciliado para substituição via importação em lote."
                    )
                    evidence = ["CNPJ extraído do X.509", "cliente já possui A1", "modo atualizar_todos"]
                results.append(_classify(client, cert, "cnpj_certificado", 100.0, reason, evidence))
            used_clients.add(internal_doc)
        else:
            continue
        used_certs.add(index)

    # CNPJ apenas no nome é pista média, nunca autorização de envio.
    for index, cert in enumerate(certs):
        if index in used_certs or _doc(getattr(cert, "cnpj_cert", None)):
            continue
        filename_doc = _doc(getattr(cert, "cnpj_filename", None))
        if not filename_doc:
            continue
        if filename_doc in idx_elegivel:
            candidates = idx_elegivel[filename_doc]
            client = candidates[0]
            if len(candidates) > 1:
                # CNPJ só no nome do arquivo nunca autoriza envio, mesmo com
                # desempate por nome — mas o desempate ainda ajuda a apontar
                # um único candidato mais provável para a revisão manual, em
                # vez de deixar totalmente em aberto entre vários.
                resolved = _pick_by_name(cert, candidates)
                if resolved is not None:
                    named_client, score = resolved
                    results.append(MatchResult(
                        "revisao_manual", named_client, cert, "cnpj_nome_arquivo", min(score, 85.0),
                        (
                            f"CNPJ do nome aponta para {len(candidates)} clientes Jettax; o nome do "
                            f"certificado sugere \"{named_client.razao_social}\" ({score:.1f}%), mas confirme "
                            "manualmente antes de enviar."
                        ),
                        ["CNPJ somente no nome do arquivo", f"nome do certificado sugere candidato ({score:.1f}%)"],
                    ))
                else:
                    results.append(MatchResult(
                        "ambiguo", client, cert, "cnpj_nome_arquivo", 70.0,
                        "CNPJ do nome aponta para mais de um cliente Jettax e o nome do certificado não ajudou a decidir.",
                        ["CNPJ somente no nome do arquivo"],
                    ))
            else:
                results.append(_classify(
                    client, cert, "cnpj_nome_arquivo", 75.0,
                    "CNPJ encontrado somente no nome do arquivo; confirme manualmente a identidade interna.",
                    ["CNPJ válido no nome", "CNPJ ausente no X.509"],
                ))
            used_clients.add(filename_doc)
            used_certs.add(index)

    # Nome é somente triagem manual. Exige unicidade e margem, mas nunca envia.
    remaining_clients = [client for client in elegivel if _doc(client.cnpj) not in used_clients]
    for index, cert in enumerate(certs):
        if index in used_certs or _doc(getattr(cert, "cnpj_cert", None)) or _doc(getattr(cert, "cnpj_filename", None)):
            continue
        subject_name = str(getattr(cert, "company_from_cert", "") or "")
        filename_name = str(getattr(cert, "filename", "") or "")
        ranked: list[tuple[float, str, JetaxClient]] = []
        for client in remaining_clients:
            subject_score = similarity(subject_name, client.razao_social) if subject_name else 0.0
            filename_score = min(80.0, similarity(filename_name, client.razao_social) * 0.80) if filename_name else 0.0
            if exact_normalized(subject_name, client.razao_social):
                subject_score = 98.0
            if subject_score >= filename_score:
                ranked.append((subject_score, "nome_certificado", client))
            else:
                ranked.append((filename_score, "nome_arquivo", client))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            continue
        best, method, client = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if best >= 94.0 and best - second >= 8.0:
            results.append(_classify(
                client, cert, method, min(best, 98.0),
                "Nome único com alta similaridade, porém sem CNPJ interno; revisão manual obrigatória.",
                ["nome normalizado", f"margem para segundo candidato: {best - second:.1f}"],
            ))
            document = _doc(client.cnpj)
            if document:
                used_clients.add(document)
                remaining_clients = [item for item in remaining_clients if _doc(item.cnpj) != document]
        else:
            results.append(MatchResult(
                "revisao_manual", None, cert, method, best,
                "Nome insuficiente ou ambíguo; nenhuma empresa foi associada automaticamente.",
                [f"melhor pontuação: {best:.1f}", f"margem: {best - second:.1f}"],
            ))
        used_certs.add(index)

    for client in elegivel:
        document = _doc(client.cnpj)
        if not document:
            results.append(MatchResult(
                "revisao_manual", client, None, "cnpj_jettax_invalido", 0.0,
                "Cliente Jettax sem CNPJ/CPF válido; associação automática proibida.",
                ["documento Jettax inválido"],
            ))
        elif document not in used_clients:
            if client in with_certificate and atualizar_todos:
                results.append(MatchResult(
                    "sem_cert_novo", client, None, motivo="Cliente já possui A1 no Jettax e não há PFX novo correspondente na pasta para renová-lo.", evidencias=[],
                ))
            else:
                results.append(MatchResult(
                    "sem_cert", client, None, motivo="Nenhum certificado com identidade suficientemente segura foi encontrado.", evidencias=["nenhum CNPJ interno correspondente"],
                ))

    for index, cert in enumerate(certs):
        if index not in used_certs:
            results.append(MatchResult(
                "extra_pfx", None, cert,
                motivo="PFX sem cliente elegível no Jettax para o modo selecionado.",
                evidencias=["nenhum CNPJ Jettax correspondente"],
            ))

    order = {
        "pronto": 0, "vencido": 1, "nao_valido": 2, "sem_senha": 3,
        "invalido": 4, "conflito": 5, "ambiguo": 6, "revisao_manual": 7,
        "substituido": 8, "sem_cert": 9, "sem_cert_novo": 10,
        "duplicado": 11, "extra_pfx": 12,
    }
    results.sort(key=lambda result: (
        order.get(result.status, 99),
        normalize_name(result.cliente.razao_social if result.cliente else getattr(result.cert, "filename", "")),
    ))
    return results
