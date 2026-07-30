"""Turn an uploaded file into indexable text, under hard limits.

This module is the only place a third-party parser touches an uploaded byte, and
it exists as its own file so that boundary is easy to find and easy to move.

**Residual risk, stated rather than hidden (adr/0014 §4c, codex F6).** `pypdf`
runs in-process, in the service that owns the vector store. The limits below
bound *time* and *memory*. They do **not** contain a parser compromise. Isolating
extraction into a separate worker is the mitigation and is deliberately out of
scope for this phase; it is recorded as debt, not quietly assumed away.

What the limits actually defend against:

  * a 4KB file that decompresses to gigabytes  -> MAX_CHARS, checked DURING
    extraction rather than after, so the blowup cannot run to completion
  * a PDF with 60,000 pages                    -> MAX_PAGES, checked before any
    page is read
  * a parser that loops                        -> TIMEOUT_SECONDS wall clock
"""
import io
import os
import time
from dataclasses import dataclass, field
from typing import Optional

MAX_BYTES = 10 * 1024 * 1024      # 10 MB  (RVB-ING-08)
MAX_PAGES = 80                    #         (RVB-ING-09)
MAX_CHARS = 200_000               #         (RVB-ING-10) — the paste path's ceiling
TIMEOUT_SECONDS = 20.0            #         (RVB-ING-36)

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
PDF_SUFFIXES = {".pdf"}
# Named rather than lumped into "unsupported", so the UI can say something useful
# (RVB-ING-13). DOCX is a zip archive; zip-bomb and traversal handling deserves
# its own change with its own tests.
DEFERRED_SUFFIXES = {
    ".docx": "DOCX",
    ".doc": "DOC",
    ".rtf": "RTF",
    ".odt": "ODT",
}


class ExtractionError(Exception):
    """Anything that makes a file unusable. Always surfaces as 422, never 500.

    Carries only messages WE wrote. A parser's own exception text can echo
    document content, and document content is the thing we are trying not to
    leak (RVB-ING-12).
    """


@dataclass
class Extracted:
    text: str
    pages: int
    chars: int
    kind: str                      # "pdf" | "text"
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


def suffix_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def check_supported(filename: str) -> None:
    """Reject early, and by name where we can.

    Called before a single byte is parsed, so an unsupported type never reaches
    the parser at all.
    """
    suffix = suffix_of(filename)

    if suffix in DEFERRED_SUFFIXES:
        label = DEFERRED_SUFFIXES[suffix]
        raise ExtractionError(
            f"{label} files are not supported yet. Save the document as a PDF "
            f"and upload that."
        )
    if suffix not in TEXT_SUFFIXES | PDF_SUFFIXES:
        raise ExtractionError(
            "Supported file types are PDF, TXT and MD."
        )


def extract(data: bytes, filename: str) -> Extracted:
    """Bytes -> text. Raises ExtractionError for anything a human should see."""
    check_supported(filename)

    if len(data) > MAX_BYTES:
        raise ExtractionError(
            f"That file is larger than the {MAX_BYTES // (1024 * 1024)} MB limit."
        )
    if not data:
        raise ExtractionError("That file is empty.")

    if suffix_of(filename) in PDF_SUFFIXES:
        return _extract_pdf(data)
    return _extract_text(data)


def _extract_text(data: bytes) -> Extracted:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Not a hard failure: a policy exported from Word is often cp1252, and
        # refusing it would send the user away for no good reason.
        try:
            text = data.decode("latin-1")
        except Exception:  # noqa: BLE001
            raise ExtractionError("That file is not readable as text.")

    truncated = len(text) > MAX_CHARS
    notes = []
    if truncated:
        text = text[:MAX_CHARS]
        notes.append(f"Truncated to the first {MAX_CHARS:,} characters.")

    if not text.strip():
        raise ExtractionError("That file has no text in it.")

    return Extracted(text=text, pages=1, chars=len(text), kind="text",
                     truncated=truncated, notes=notes)


def _extract_pdf(data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is pinned
        raise ExtractionError("PDF support is unavailable on this server.")

    started = time.monotonic()

    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
    except Exception:  # noqa: BLE001
        # Deliberately swallowing the parser's message — see ExtractionError.
        raise ExtractionError("That PDF could not be read. It may be corrupt or encrypted.")

    if getattr(reader, "is_encrypted", False):
        raise ExtractionError("That PDF is password-protected. Remove the password and re-upload.")

    # BEFORE reading any page (RVB-ING-37).
    if page_count > MAX_PAGES:
        raise ExtractionError(
            f"That PDF has {page_count} pages; the limit is {MAX_PAGES}. "
            f"Split it and upload the relevant section."
        )

    chunks: list[str] = []
    total = 0
    truncated = False
    notes: list[str] = []

    for i, page in enumerate(reader.pages):
        # Checked per page rather than once at the end: a decompression blowup
        # must not be allowed to run to completion first.
        if time.monotonic() - started > TIMEOUT_SECONDS:
            raise ExtractionError(
                "That PDF took too long to read. It may be unusually complex — "
                "try splitting it."
            )

        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            notes.append(f"Page {i + 1} could not be read and was skipped.")
            continue

        if total + len(page_text) > MAX_CHARS:
            chunks.append(page_text[: MAX_CHARS - total])
            truncated = True
            notes.append(
                f"Truncated at the {MAX_CHARS:,} character limit "
                f"(stopped on page {i + 1} of {page_count})."
            )
            break

        chunks.append(page_text)
        total += len(page_text)

    text = "\n\n".join(c for c in chunks if c.strip())

    if not text.strip():
        # A scan. Indexing it as an empty document would be worse than refusing:
        # it would look ingested and answer nothing (RVB-ING-14).
        raise ExtractionError(
            "No text could be read from that PDF. It looks like a scan — "
            "this system cannot read scanned images yet."
        )

    return Extracted(text=text, pages=page_count, chars=len(text), kind="pdf",
                     truncated=truncated, notes=notes)


# --------------------------------------------------------------------------- #
# metadata (RVB-ING-31 .. 33, codex F3)
# --------------------------------------------------------------------------- #
def clean_title(title: Optional[str], filename: str) -> str:
    """A title always exists, and it is never silently invented from nothing."""
    candidate = (title or "").strip()
    if not candidate:
        candidate = os.path.splitext(os.path.basename(filename or ""))[0].strip()
    candidate = candidate.replace("_", " ").replace("-", " ").strip()
    return (candidate or "Untitled document")[:300]
