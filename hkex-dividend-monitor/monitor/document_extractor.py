"""Targeted, single-filing document text extraction.

The upstream `hkex-filing-scraper`'s normal pipeline downloads and
extracts text for *every* filing in a scraped date range, sequentially
(measured throughput: roughly one filing per minute, since only the
download step is thread-pooled -- PDF/table extraction runs one at a
time). For a watchlist of a handful of tickers, that means waiting many
minutes to hours for the one filing that actually matters.

This module does the extraction for exactly one filing on demand,
reusing the upstream package's own battle-tested extraction logic
(`hkex_scraper.extractor.extract_content_with_tables` -- PyMuPDF /
pymupdf4llm for PDF text, camelot for tables, BeautifulSoup for HTML,
openpyxl for Excel) rather than reimplementing document parsing.
Pairs with `scraper_runner.run_scrape(..., metadata_only=True)`, which
populates `documentUrl` etc. quickly without ever running the slow
Phase 2 backfill.
"""
from __future__ import annotations

import requests

from monitor.db import update_filing_document

# Mirrors the upstream scraper's own supported-extension list
# (hkex_scraper.pipeline.SUPPORTED_EXTENSIONS) and its 25 MB size cap.
_SUPPORTED_EXTENSIONS = (".pdf", ".htm", ".html", ".xlsx", ".xls")
_MAX_DOWNLOAD_SIZE = 25 * 1024 * 1024
_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": (
        "application/pdf,text/html,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8"
    ),
}


class DocumentExtractionError(RuntimeError):
    pass


def _url_extension(document_url: str) -> str:
    return document_url.lower().split("?")[0].split("#")[0]


def _doc_type_for_url(document_url: str) -> str:
    u = _url_extension(document_url)
    if u.endswith(".pdf"):
        return "pdf"
    if u.endswith(".htm") or u.endswith(".html"):
        return "html"
    if u.endswith(".xlsx") or u.endswith(".xls"):
        return "xlsx"
    return "unknown"


def _download(document_url: str, timeout: float = 60.0) -> bytes:
    try:
        resp = requests.get(
            document_url, headers=_DOWNLOAD_HEADERS, timeout=timeout, stream=True
        )
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_DOWNLOAD_SIZE:
            raise DocumentExtractionError(
                f"Document too large ({content_length} bytes, max {_MAX_DOWNLOAD_SIZE})"
            )
        content = resp.content
        if len(content) > _MAX_DOWNLOAD_SIZE:
            raise DocumentExtractionError(
                f"Document too large ({len(content)} bytes, max {_MAX_DOWNLOAD_SIZE})"
            )
        return content
    except requests.RequestException as exc:
        raise DocumentExtractionError(f"Failed to download {document_url}: {exc}") from exc


def extract_and_save_filing(filing_id: str, document_url: str) -> str:
    """Download `document_url`, extract its text, and write it back onto
    exchange_filing:{filing_id}. Returns the extracted text.

    Raises DocumentExtractionError on any failure (unsupported type,
    download error, extraction error, or a DB write failure) -- callers
    should catch this, log it, and move on rather than letting one bad
    document block the whole matching/notify cycle.
    """
    if not document_url:
        raise DocumentExtractionError("Filing has no documentUrl")

    if not _url_extension(document_url).endswith(_SUPPORTED_EXTENSIONS):
        raise DocumentExtractionError(f"Unsupported document type: {document_url}")

    try:
        # Imported lazily: this is the upstream hkex-filing-scraper package
        # (already installed in the image alongside the `hkex-scraper` CLI),
        # not a module we own.
        from hkex_scraper import extractor as _upstream_extractor
        from hkex_scraper.extractor import extract_content_with_tables
    except ImportError as exc:
        raise DocumentExtractionError(
            f"hkex_scraper package not importable: {exc}"
        ) from exc

    # Skip camelot table extraction: it can add many *minutes* per large PDF
    # (an annual report on a results day), while the monitor and chat only
    # consume the text -- pymupdf4llm's inline markdown tables remain. This is
    # exactly the degraded path the native no-Docker mode always runs (camelot
    # isn't installed there), documented as having no effect on detection
    # quality; the upstream flag makes its extractor take that path here too.
    _upstream_extractor.CAMELOT_AVAILABLE = False

    raw_bytes = _download(document_url)

    try:
        text, _tables = extract_content_with_tables(raw_bytes, document_url)
    except Exception as exc:  # noqa: BLE001 - extraction internals are third-party
        raise DocumentExtractionError(f"Text extraction failed: {exc}") from exc

    if not text:
        raise DocumentExtractionError("Extraction produced no text")

    try:
        update_filing_document(
            filing_id,
            document_text=text,
            document_type=_doc_type_for_url(document_url),
            status="processed",
        )
    except Exception as exc:  # noqa: BLE001 - SurrealDBError or ValueError from validation
        raise DocumentExtractionError(f"Failed to save extracted text: {exc}") from exc

    return text
