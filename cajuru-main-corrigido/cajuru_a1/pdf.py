"""Inspeção limitada de PDFs de apoio; PDFs nunca são elegíveis para upload A1."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cajuru_a1.cnpjutil import extract_docs_from_text
from cajuru_a1.names import normalize_name
from cajuru_a1.pfx import file_sha256

OcrCallback = Callable[[int, bytes], str]


@dataclass
class PdfInfo:
    source_path: str
    temp_path: str
    filename: str
    sha256: str
    size: int
    opened: bool = False
    protected: bool = False
    password_source: str = ""
    page_count: int = 0
    pages_read: int = 0
    has_text: bool = False
    scanned: bool = False
    ocr_used: bool = False
    duplicate_sha256: bool = False
    documents: list[str] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)
    text_sha256: str = ""
    error_code: str = ""
    error: str = ""
    review_reason: str = ""


def _tesseract_callback(command: str, language: str = "por") -> OcrCallback:
    def run(_page_index: int, image: bytes) -> str:
        process = subprocess.run(
            [command, "stdin", "stdout", "-l", language],
            input=image,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError("OCR indisponível ou falhou")
        return process.stdout.decode("utf-8", errors="replace")
    return run


def inspect_pdf(
    source: Path,
    temp: Path,
    password_candidates: list[tuple[str, str]],
    *,
    max_bytes: int = 60 * 1024 * 1024,
    max_pages: int = 30,
    ocr: bool = False,
    ocr_max_pages: int = 3,
    tesseract_command: str = "tesseract",
    ocr_callback: OcrCallback | None = None,
    known_companies: list[str] | None = None,
) -> PdfInfo:
    source, temp = Path(source), Path(temp)
    info = PdfInfo(str(source), str(temp), source.name, file_sha256(temp), temp.stat().st_size)
    if info.size > max_bytes:
        info.error_code = "pdf_grande"
        info.error = "PDF excede o limite de segurança configurado"
        info.review_reason = "REVISÃO MANUAL"
        return info
    try:
        import pymupdf
        document = pymupdf.open(str(temp))
    except Exception as exc:
        info.error_code = "pdf_corrompido"
        info.error = f"PDF ilegível ({type(exc).__name__})"
        info.review_reason = "REVISÃO MANUAL"
        return info

    try:
        info.protected = bool(document.needs_pass)
        if document.needs_pass:
            authenticated = False
            for password, source_name in password_candidates:
                try:
                    if document.authenticate(password):
                        info.password_source = source_name
                        authenticated = True
                        break
                except Exception:
                    continue
            if not authenticated:
                info.error_code = "pdf_senha_incorreta"
                info.error = "PDF protegido: nenhuma candidata plausível funcionou"
                info.review_reason = "REVISÃO MANUAL"
                return info
        info.opened = True
        info.page_count = int(document.page_count)
        page_limit = min(info.page_count, max(1, int(max_pages)))
        texts: list[str] = []
        blank_pages: list[int] = []
        for page_index in range(page_limit):
            try:
                text = document.load_page(page_index).get_text("text") or ""
            except Exception:
                text = ""
            texts.append(text)
            info.pages_read += 1
            if len("".join(text.split())) < 20:
                blank_pages.append(page_index)
        joined = "\n".join(texts)
        info.has_text = len("".join(joined.split())) >= 20
        info.scanned = bool(blank_pages) and not info.has_text

        if info.scanned and ocr:
            callback = ocr_callback or _tesseract_callback(tesseract_command)
            for page_index in blank_pages[: max(0, int(ocr_max_pages))]:
                try:
                    page = document.load_page(page_index)
                    # 150 dpi limita memória sem destruir legibilidade de CNPJ.
                    pixmap = page.get_pixmap(dpi=150, alpha=False)
                    ocr_text = callback(page_index, pixmap.tobytes("png"))
                    if ocr_text:
                        texts[page_index] = ocr_text
                        info.ocr_used = True
                except Exception:
                    continue
            joined = "\n".join(texts)
            info.has_text = len("".join(joined.split())) >= 20
        info.documents = extract_docs_from_text(joined, allow_missing_leading_zero=False)
        # Nome no PDF é apenas evidência de triagem. Aceitamos correspondência
        # exata de linha normalizada com uma empresa já conhecida; nunca fuzzy.
        known_by_normalized = {normalize_name(name): name for name in (known_companies or []) if normalize_name(name)}
        found_names: list[str] = []
        for raw_line in joined.splitlines():
            normalized_line = normalize_name(raw_line)
            for prefix in ("RAZAO SOCIAL ", "NOME EMPRESARIAL "):
                if normalized_line.startswith(prefix):
                    normalized_line = normalized_line[len(prefix):].strip()
                    break
            if normalized_line in known_by_normalized:
                name = known_by_normalized[normalized_line]
                if name not in found_names:
                    found_names.append(name)
        info.company_names = found_names
        if joined:
            info.text_sha256 = hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()
        if info.page_count > page_limit:
            info.review_reason = f"REVISÃO MANUAL: somente {page_limit} de {info.page_count} páginas foram lidas"
        elif not info.has_text:
            info.review_reason = "REVISÃO MANUAL: PDF sem texto; OCR ausente ou inconclusivo"
        elif len(info.documents) > 1:
            info.review_reason = "REVISÃO MANUAL: múltiplos documentos encontrados no PDF"
        elif len(info.company_names) > 1:
            info.review_reason = "REVISÃO MANUAL: nomes de múltiplas empresas encontrados no PDF"
    finally:
        document.close()
    return info
