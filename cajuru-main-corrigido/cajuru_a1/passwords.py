"""Cofre em memória e geração restrita de candidatas de senha.

Senhas nunca são serializadas por este módulo. Fontes e diagnósticos não contêm
o valor da senha.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cajuru_a1.cnpjutil import is_valid_doc, only_digits, pad_cnpj
from cajuru_a1.names import name_key, normalize_name, significant_tokens, similarity

COMMON_DEFAULTS = ["123456", "12345678", "123456789", "1234", "12345"]
GENERIC_STEMS = {
    "CONTABILIDADE", "CONTABIL", "ASSESSORIA", "CONSULTORIA", "SERVICOS", "SERVICO",
    "COMERCIO", "INDUSTRIA", "EMPREENDIMENTOS", "EMPRESA", "GRUPO", "BRASIL",
}


def norm(text: str) -> str:
    return normalize_name(text)


def first_company_stem(text: str) -> str:
    tokens = significant_tokens(text)
    for token in tokens:
        if token not in GENERIC_STEMS and len(token) >= 3:
            return token
    return tokens[0] if tokens else ""


def first_name(text: str) -> str:
    return first_company_stem(text)


def year_suffixes(extra: Iterable[str] | None = None) -> list[str]:
    now = datetime.now(timezone.utc).year
    raw = list(extra) if extra is not None else [str(now)[-2:], str(now), str(now - 1)[-2:], str(now - 1)]
    result: list[str] = []
    for item in raw:
        value = str(item).strip()
        if re.fullmatch(r"(?:\d{2}|\d{4})", value) and value not in result:
            result.append(value)
    return result


def pattern_passwords(empresa: str, years: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """Gera apenas ``marca principal + ano``.

    Não gera combinações com toda palavra da razão social, pois padrões como
    ``SERVICOS26`` ou ``COMERCIO2026`` são comuns a empresas diferentes.
    """
    stem = first_company_stem(empresa)
    if len(stem) < 3:
        return []
    return [(f"{stem}{year}".upper(), f"padrão:{stem}+ano") for year in year_suffixes(years)]


@dataclass(frozen=True)
class PasswordEntry:
    empresa: str
    senha: str
    cnpj: str = ""
    validade: str = ""
    origem: str = ""
    aba: str = ""
    linha: int = 0


@dataclass
class PasswordVault:
    entries: list[PasswordEntry] = field(default_factory=list)
    by_cnpj: dict[str, list[PasswordEntry]] = field(default_factory=dict)
    by_key: dict[str, list[PasswordEntry]] = field(default_factory=dict)
    all_unique: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    _fingerprints: set[tuple[str, str, str]] = field(default_factory=set, repr=False)

    def finding(self, severity: str, code: str, message: str, **context) -> None:
        self.findings.append({"severity": severity, "code": code, "message": message, **context})

    def add(self, entry: PasswordEntry) -> None:
        pwd = str(entry.senha).strip()
        company = normalize_name(entry.empresa)
        doc = pad_cnpj(only_digits(entry.cnpj)) if entry.cnpj else ""
        if doc and not is_valid_doc(doc):
            self.finding("warning", "documento_invalido", "Documento inválido ignorado", origem=entry.origem, aba=entry.aba, linha=entry.linha)
            doc = ""
        if not pwd or not company:
            return
        fingerprint = (name_key(company), doc, pwd)
        if fingerprint in self._fingerprints:
            self.finding("info", "linha_duplicada", "Linha duplicada ignorada", origem=entry.origem, aba=entry.aba, linha=entry.linha)
            return
        self._fingerprints.add(fingerprint)
        normalized_entry = PasswordEntry(entry.empresa, pwd, doc, entry.validade, entry.origem, entry.aba, entry.linha)

        key = name_key(company)
        existing = self.by_key.get(key, [])
        if existing and any(item.senha != pwd for item in existing):
            self.finding("warning", "senhas_conflitantes_nome", "A mesma empresa possui senhas diferentes; ambas serão testadas, sem exposição dos valores", empresa=entry.empresa)
        if doc and self.by_cnpj.get(doc) and any(item.senha != pwd for item in self.by_cnpj[doc]):
            self.finding("high", "senhas_conflitantes_documento", "O mesmo documento possui senhas diferentes", cnpj=doc)

        self.entries.append(normalized_entry)
        self.by_key.setdefault(key, []).append(normalized_entry)
        if doc:
            self.by_cnpj.setdefault(doc, []).append(normalized_entry)
        if pwd not in self.all_unique:
            self.all_unique.append(pwd)

    def lookup_by_name(self, empresa: str, min_score: float = 94.0, min_margin: float = 8.0) -> list[tuple[str, str, float]]:
        query_key = name_key(empresa)
        if not query_key:
            return []
        exact = self.by_key.get(query_key, [])
        if exact:
            return _entries_as_candidates(exact, "planilha:nome-exato", 100.0)

        query_tokens = set(significant_tokens(empresa))
        scored: dict[str, tuple[float, list[PasswordEntry]]] = {}
        for key, entries in self.by_key.items():
            label = entries[0].empresa
            tokens = set(significant_tokens(label))
            score = similarity(empresa, label)
            # Abreviação explícita de uma única marca, ex.: BIO3 -> BIO3 SERVICOS.
            if len(tokens) == 1 and next(iter(tokens), "") in query_tokens and len(next(iter(tokens), "")) >= 4:
                score = max(score, 96.0)
            if key not in scored or score > scored[key][0]:
                scored[key] = (score, entries)
        ranked = sorted(scored.values(), key=lambda item: item[0], reverse=True)
        if not ranked:
            return []
        best_score, best_entries = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score < min_score or best_score - second_score < min_margin:
            return []
        return _entries_as_candidates(best_entries, "planilha:nome-único", best_score)


def _entries_as_candidates(entries: list[PasswordEntry], source: str, score: float) -> list[tuple[str, str, float]]:
    result: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.senha not in seen:
            seen.add(entry.senha)
            result.append((entry.senha, source, score))
    return result


def candidate_passwords(
    *,
    vault: PasswordVault,
    empresa: str = "",
    cnpj: str = "",
    extra_names: list[str] | None = None,
    years: Iterable[str] | None = None,
    include_all_sheet: bool = False,
    include_common: bool = False,
    include_empty: bool = True,
    max_candidates: int = 250,
) -> list[tuple[str, str]]:
    """Ordena candidatas plausíveis, com limite rígido.

    ``include_all_sheet`` existe só para migração/uso explícito. O pipeline de
    produção não o ativa por padrão.
    """
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(password: str, source: str, *, empty_ok: bool = False) -> None:
        value = str(password) if password is not None else ""
        if not empty_ok:
            value = value.strip()
        if (not value and not empty_ok) or value in seen or len(result) >= max(1, max_candidates):
            return
        seen.add(value)
        result.append((value, source))

    doc = pad_cnpj(only_digits(cnpj)) if cnpj else ""
    if doc and is_valid_doc(doc):
        for entry in vault.by_cnpj.get(doc, []):
            add(entry.senha, "planilha:documento-exato")

    names: list[str] = []
    for name in [empresa, *(extra_names or [])]:
        if name and normalize_name(name) and normalize_name(name) not in {normalize_name(x) for x in names}:
            names.append(name)
    for name in names:
        for password, source, _score in vault.lookup_by_name(name):
            add(password, source)
    for name in names:
        for password, source in pattern_passwords(name, years):
            add(password, source)
    if include_common:
        for password in COMMON_DEFAULTS:
            add(password, "padrão:comum-autorizado")
    if include_all_sheet:
        for password in vault.all_unique:
            add(password, "planilha:varredura-global-autorizada")
    if include_empty:
        add("", "certificado:sem-senha", empty_ok=True)
    return result
