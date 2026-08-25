from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID, ObjectIdentifier

from cajuru_a1.cnpjutil import (
    best_doc_from_filename,
    extract_docs_from_text,
    is_valid_doc,
    only_digits,
    pad_cnpj,
)

OID_CNPJ = ObjectIdentifier("2.16.76.1.3.3")
OID_CPF = ObjectIdentifier("2.16.76.1.3.1")


@dataclass
class PfxInfo:
    source_path: str
    temp_path: str
    filename: str
    sha256: str
    size: int
    cnpj_filename: str | None = None
    cnpj_cert: str | None = None
    company_from_cert: str | None = None
    password: str | None = None
    password_verified: bool = False
    has_private_key: bool = False
    password_source: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    expired: bool = False
    not_yet_valid: bool = False
    opened: bool = False
    error: str | None = None
    error_code: str = ""
    attempts: int = 0
    identity_conflict: bool = False
    duplicate_sha256: bool = False
    # Data de modificação do arquivo de origem (Dropbox), usada para escolher
    # o certificado mais atualizado quando existem dois PFX do mesmo CNPJ.
    source_mtime: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def cnpj(self) -> str | None:
        # Identidade interna sempre prevalece sobre o nome do arquivo.
        return self.cnpj_cert or self.cnpj_filename

    @property
    def dias_para_vencer(self) -> int | None:
        if self.not_after is None:
            return None
        delta = self.not_after - datetime.now(timezone.utc)
        return delta.days


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _docs_from_other_name(raw: bytes) -> list[str]:
    # cryptography entrega DER. Procurar uma sequência ASCII dentro do DER é
    # mais seguro que remover números do hexadecimal (a implementação antiga
    # podia criar um documento inexistente a partir dos bytes de tag/tamanho).
    text = raw.decode("latin-1", errors="ignore")
    return extract_docs_from_text(text, allow_missing_leading_zero=False)


def extract_identity(cert: x509.Certificate) -> tuple[str | None, str | None, list[str]]:
    """Retorna empresa, documento único e evidências sem dados secretos."""
    evidence: list[str] = []
    companies: list[str] = []
    cnpjs: list[str] = []
    cpfs: list[str] = []

    def add_document(value: str, evidence_label: str) -> None:
        candidate = pad_cnpj(only_digits(value))
        target = cnpjs if len(candidate) == 14 else cpfs
        if is_valid_doc(candidate) and candidate not in target:
            target.append(candidate)
            evidence.append(evidence_label)

    for oid, label in ((NameOID.ORGANIZATION_NAME, "organizationName"), (NameOID.COMMON_NAME, "commonName")):
        try:
            for attribute in cert.subject.get_attributes_for_oid(oid):
                value = str(attribute.value).strip()
                if value and value not in companies:
                    companies.append(value)
                    evidence.append(f"nome:{label}")
        except Exception:
            continue

    try:
        for attribute in cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER):
            add_document(str(attribute.value), "documento:serialNumber")
    except Exception:
        pass

    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        for general_name in san.value:
            if not isinstance(general_name, x509.OtherName) or general_name.type_id not in {OID_CNPJ, OID_CPF}:
                continue
            for document in _docs_from_other_name(getattr(general_name, "value", b"")):
                expected = 14 if general_name.type_id == OID_CNPJ else 11
                if len(document) == expected:
                    add_document(document, "documento:subjectAltName ICP-Brasil")
    except x509.ExtensionNotFound:
        pass
    except Exception:
        evidence.append("aviso:subjectAltName ilegível")

    # Último fallback: somente sequências delimitadas no DN, sem concatenar.
    if not cnpjs and not cpfs:
        for candidate in extract_docs_from_text(cert.subject.rfc4514_string(), allow_missing_leading_zero=False):
            add_document(candidate, "documento:subject-DN")

    # e-CNPJ normalmente contém o CPF do responsável e o CNPJ da empresa.
    # Isso não é conflito: CNPJ tem precedência. Conflito existe somente entre
    # múltiplos documentos do mesmo tipo prioritário.
    if len(cnpjs) == 1:
        document = cnpjs[0]
    elif not cnpjs and len(cpfs) == 1:
        document = cpfs[0]
    else:
        document = None
    if len(cnpjs) > 1 or (not cnpjs and len(cpfs) > 1):
        evidence.append("conflito:múltiplos documentos prioritários no certificado")
    company = companies[0] if companies else None
    if company and document:
        # CN ICP-Brasil frequentemente termina em :CNPJ.
        company = re.sub(rf"\s*[:\-]\s*{re.escape(document)}\s*$", "", company).strip()
    return company, document, evidence


def try_open_pfx(data: bytes, password: str | None) -> tuple[object | None, x509.Certificate | None, Exception | None]:
    try:
        encoded = None if password is None else str(password).encode("utf-8")
        key, certificate, _chain = pkcs12.load_key_and_certificates(data, encoded)
        if certificate is None:
            return key, None, ValueError("PFX sem certificado X.509")
        if key is None:
            return None, certificate, ValueError("PFX sem chave privada")
        return key, certificate, None
    except Exception as exc:
        return None, None, exc


def unlock_pfx(
    path: Path,
    candidates: list[tuple[str, str]],
    *,
    max_bytes: int = 30 * 1024 * 1024,
    max_attempts: int = 250,
    progress=None,
) -> tuple[str | None, str | None, object | None, x509.Certificate | None, int, str]:
    size = Path(path).stat().st_size
    if size > max_bytes:
        return None, None, None, None, 0, "arquivo_grande"
    data = Path(path).read_bytes()
    if not data or data[:1] != b"\x30":
        return None, None, None, None, 0, "pfx_corrompido"
    attempts = 0
    # Para PKCS#12 sem proteção, OpenSSL pode aceitar None ou b"" dependendo
    # de como o arquivo foi criado. Testamos ambos sem registrar valor algum.
    expanded = list(candidates)
    if any(password == "" for password, _ in expanded):
        expanded.insert(0, (None, "certificado:sem-senha"))  # type: ignore[arg-type]
    for password, source in expanded[:max_attempts]:
        if password is not None and len(str(password)) > 1024:
            continue
        attempts += 1
        # Alguns PFX inválidos são caros para o OpenSSL testar repetidamente.
        # O progresso evita que o painel pareça travado e também dá ao job uma
        # oportunidade de observar um pedido de cancelamento entre tentativas.
        if progress is not None and (attempts == 1 or attempts % 10 == 0):
            progress(attempts, min(len(expanded), max_attempts))
        key, certificate, _error = try_open_pfx(data, password)
        if certificate is not None and key is not None:
            return ("" if password is None else str(password)), source, key, certificate, attempts, ""
    return None, None, None, None, attempts, "senha_nao_encontrada_ou_pfx_invalido"


def inspect_file(
    source: Path,
    temp: Path,
    candidates: list[tuple[str, str]],
    *,
    max_bytes: int = 30 * 1024 * 1024,
    max_attempts: int = 250,
    progress=None,
) -> PfxInfo:
    source, temp = Path(source), Path(temp)
    try:
        source_mtime = source.stat().st_mtime
    except OSError:
        source_mtime = 0.0
    info = PfxInfo(
        source_path=str(source),
        temp_path=str(temp),
        filename=source.name,
        sha256=file_sha256(temp),
        size=temp.stat().st_size,
        cnpj_filename=best_doc_from_filename(source.name),
        source_mtime=source_mtime,
    )
    password, password_source, key, certificate, attempts, error_code = unlock_pfx(
        temp, candidates, max_bytes=max_bytes, max_attempts=max_attempts, progress=progress
    )
    info.attempts = attempts
    if certificate is None:
        info.error_code = error_code
        info.error = {
            "arquivo_grande": "Arquivo excede o limite de segurança configurado",
            "pfx_corrompido": "Arquivo vazio ou estrutura PKCS#12 inválida",
            "senha_nao_encontrada_ou_pfx_invalido": "Senha não encontrada ou PKCS#12 ilegível",
        }.get(error_code, "Não foi possível abrir o certificado")
        return info

    info.opened = True
    info.has_private_key = key is not None
    info.password = password
    info.password_verified = True
    info.password_source = password_source
    company, document, evidence = extract_identity(certificate)
    info.company_from_cert = company
    info.cnpj_cert = document
    info.extra["identity_evidence"] = evidence
    if any(item.startswith("conflito:") for item in evidence):
        info.identity_conflict = True
        info.error_code = "identidade_interna_ambigua"
        info.error = "O certificado contém mais de um documento válido"
    if info.cnpj_filename and info.cnpj_cert and info.cnpj_filename != info.cnpj_cert:
        info.identity_conflict = True
        info.error_code = "cnpj_nome_diferente_certificado"
        info.error = "CNPJ do nome do arquivo difere do CNPJ interno do certificado"

    not_before = getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before.replace(tzinfo=timezone.utc)
    not_after = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after.replace(tzinfo=timezone.utc)
    info.not_before = not_before
    info.not_after = not_after
    now = datetime.now(timezone.utc)
    info.expired = not_after < now
    info.not_yet_valid = not_before > now
    return info


def _sort_key(info: PfxInfo):
    """Chave de ordenação para escolher o certificado 'mais atual'.

    Prioriza (1) a validade final mais distante (certificado novo vence mais
    tarde), depois (2) o início de validade mais recente, depois (3) o mtime
    do arquivo de origem e por fim (4) o tamanho/sha como desempate determinístico.
    Valores nulos (PFX que não abriu) vão para o fim.
    """
    not_after = info.not_after.timestamp() if info.not_after else -1.0
    not_before = info.not_before.timestamp() if info.not_before else -1.0
    return (
        1 if info.opened else 0,
        not_after,
        not_before,
        float(info.source_mtime or 0.0),
        int(info.size or 0),
        info.sha256 or "",
    )


def pick_newest(certs: list[PfxInfo]) -> tuple[PfxInfo | None, list[PfxInfo]]:
    """Recebe vários PFX do mesmo CNPJ e devolve (escolhido, substituídos).

    O certificado mais novo e válido é escolhido; os demais são marcados como
    substitutos. Se nenhum abrir ou todos estiverem empatados de forma ambígua,
    devolve (None, todos) para que o matcher trate como revisão manual.
    """
    candidates = [c for c in certs if c is not None]
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], []
    opened = [c for c in candidates if c.opened and not getattr(c, "identity_conflict", False)]
    if not opened:
        return None, candidates
    opened.sort(key=_sort_key, reverse=True)
    winner = opened[0]
    losers = [c for c in candidates if c is not winner]
    for loser in losers:
        loser.extra["substituido_por"] = winner.filename
        loser.extra["motivo_substituicao"] = (
            f"Substituído por {winner.filename} (validade "
            f"{winner.not_after.strftime('%d/%m/%Y') if winner.not_after else 'desconhecida'})"
        )
    winner.extra["selecionado_entre"] = len(opened)
    return winner, losers
