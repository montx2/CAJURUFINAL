"""Normalização, validação e extração conservadora de CNPJ/CPF."""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")


def only_digits(value: str | None) -> str:
    return _DIGITS.sub("", str(value)) if value else ""


def pad_cnpj(digits: str) -> str:
    """Restaura zeros à esquerda apenas nos formatos legados conhecidos."""
    d = only_digits(digits)
    if 12 <= len(d) <= 13:
        d = d.zfill(14)
    elif len(d) == 10:
        d = d.zfill(11)
    return d


def format_cnpj(digits: str) -> str:
    d = pad_cnpj(digits)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _mod11(nums: list[int], weights: list[int]) -> int:
    rest = sum(n * w for n, w in zip(nums, weights)) % 11
    return 0 if rest < 2 else 11 - rest


def is_valid_cnpj(value: str | None) -> bool:
    d = pad_cnpj(value or "")
    if len(d) != 14 or not d.isdigit() or d == d[0] * 14:
        return False
    nums = [int(c) for c in d]
    if _mod11(nums[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) != nums[12]:
        return False
    return _mod11(nums[:13], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]) == nums[13]


def is_valid_cpf(value: str | None) -> bool:
    d = only_digits(value)
    if len(d) != 11 or not d.isdigit() or d == d[0] * 11:
        return False
    nums = [int(c) for c in d]
    s1 = sum(n * w for n, w in zip(nums[:9], range(10, 1, -1)))
    r1 = (s1 * 10) % 11
    r1 = 0 if r1 == 10 else r1
    if r1 != nums[9]:
        return False
    s2 = sum(n * w for n, w in zip(nums[:10], range(11, 1, -1)))
    r2 = (s2 * 10) % 11
    return (0 if r2 == 10 else r2) == nums[10]


def is_valid_doc(value: str | None) -> bool:
    d = only_digits(value)
    return is_valid_cnpj(d) if len(d) in (12, 13, 14) else is_valid_cpf(d)


def extract_docs_from_text(text: str, *, allow_missing_leading_zero: bool = True) -> list[str]:
    """Extrai documentos sem concatenar grupos numéricos independentes.

    A versão anterior removia toda pontuação do texto e deslizava uma janela
    sobre todos os dígitos. Isso podia fabricar um CNPJ válido juntando data,
    número de nota e ano. Aqui somente sequências contíguas ou a máscara oficial
    são aceitas.
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str, kind: str) -> None:
        digits = only_digits(raw)
        if kind == "cnpj":
            if len(digits) in (12, 13) and allow_missing_leading_zero:
                digits = digits.zfill(14)
            if not is_valid_cnpj(digits):
                return
        elif not is_valid_cpf(digits):
            return
        if digits not in seen:
            seen.add(digits)
            found.append(digits)

    cnpj_patterns = (
        r"(?<!\d)\d{2}[.\s]?\d{3}[.\s]?\d{3}\s*[/\\]\s*\d{4}\s*[-–]\s*\d{2}(?!\d)",
        r"(?<!\d)\d{14}(?!\d)",
    )
    if allow_missing_leading_zero:
        cnpj_patterns += (r"(?<!\d)\d{12,13}(?!\d)",)
    for pattern in cnpj_patterns:
        for match in re.finditer(pattern, str(text)):
            add(match.group(0), "cnpj")

    for pattern in (
        r"(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}\s*[-–]\s*\d{2}(?!\d)",
        r"(?<!\d)\d{11}(?!\d)",
    ):
        for match in re.finditer(pattern, str(text)):
            add(match.group(0), "cpf")
    return found


def best_doc_from_filename(filename: str) -> str | None:
    """Retorna documento somente se a evidência do nome não for ambígua."""
    docs = extract_docs_from_text(filename)
    cnpjs = [d for d in docs if len(d) == 14]
    preferred = cnpjs or docs
    return preferred[0] if len(preferred) == 1 else None
