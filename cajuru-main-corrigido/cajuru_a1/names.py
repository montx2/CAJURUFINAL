"""Normalização Unicode e comparação conservadora de razões sociais."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

LEGAL_SUFFIXES = {
    "LTDA", "ME", "EPP", "EIRELI", "MEI", "SA", "S A", "SS", "SLU",
    "SOCIEDADE UNIPESSOAL", "ASSOCIACAO", "FUNDACAO", "INSTITUTO",
}
STOP = {"E", "DE", "DA", "DO", "DAS", "DOS", "EM", "NA", "NO", "THE", "A", "O"}


def strip_accents(text: str) -> str:
    # NFKC primeiro trata formas de compatibilidade; NFKD separa diacríticos.
    normalized = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(text or "")))
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_name(text: str) -> str:
    value = strip_accents(text).casefold().upper().replace("&", " E ").replace("+", " E ")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    words = re.sub(r"\s+", " ", value).strip().split()
    # Sufixos são removidos somente no fim. Removê-los no meio poderia apagar
    # parte legítima de uma marca (por exemplo, Instituto ME ...).
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) >= 2 and " ".join(words[-2:]) in LEGAL_SUFFIXES:
        words = words[:-2]
    return " ".join(words)


def significant_tokens(text: str) -> list[str]:
    return [token for token in normalize_name(text).split() if token and token not in STOP]


def first_name(text: str) -> str:
    tokens = significant_tokens(text)
    return tokens[0] if tokens else ""


def name_key(text: str) -> str:
    return "".join(significant_tokens(text))


def similarity(a: str, b: str) -> float:
    left, right = normalize_name(a), normalize_name(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    # Não use partial_ratio sozinho: "CAJURU" daria nota alta tanto para
    # CAJURU ALIMENTOS quanto para CAJURU CONTABILIDADE.
    token_set = float(fuzz.token_set_ratio(left, right))
    token_sort = float(fuzz.token_sort_ratio(left, right))
    ratio = float(fuzz.ratio(left, right))
    score = max(ratio, token_sort, token_set * 0.96)
    shorter, longer = sorted((left, right), key=len)
    if longer.startswith(shorter + " "):
        coverage = len(shorter) / len(longer)
        score = max(score, 88.0 + 10.0 * coverage)
    return min(100.0, score)


def exact_normalized(a: str, b: str) -> bool:
    return bool(normalize_name(a)) and normalize_name(a) == normalize_name(b)


def names_match(a: str, b: str, threshold: float = 94.0) -> bool:
    """Compatibilidade; associação automática ainda exige margem no matcher."""
    return exact_normalized(a, b) or similarity(a, b) >= threshold
