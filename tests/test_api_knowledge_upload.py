"""Ingest is a two-phase disclosure gate, and every way past it is closed.

`RVB-AG-18`. These are **API contract tests** -- named `test_api_*` deliberately,
because the naming mistake that let codex finding R7 stay open for four reviews
was calling gateway tests `test_e2e_*`. The browser journey is separate.

Why this endpoint gets this much attention: the knowledge collection has no scope
filter and patients can query it, so a committed document is readable -- through
grounded, cited answers -- by every authenticated user in the system,
indefinitely, with no cheap undo. See `adr/0014`.

Fixture PDFs are **generated**, never committed (`RVB-ING-19` / spec §10). A
binary blob in a PHI repository is a review burden with no upside.
"""
import io
import sys
import zlib

import httpx
import pytest

from conftest import load_module

pytest.importorskip("pypdf")

_sessions: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# generated fixtures
# --------------------------------------------------------------------------- #
def make_pdf(pages: list[str]) -> bytes:
    """A minimal, real PDF that pypdf can extract text from.

    Hand-built rather than pulled from a library so the test suite gains no
    dependency purely to make fixtures, and so page count is exact.
    """
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for text in pages:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + str(len(objects) + 2).encode() + b" 0 R >>"
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")

    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
              f"{xref_at}\n%%EOF\n".encode())
    return out.getvalue()


def make_zip_bomb_pdf() -> bytes:
    """A tiny PDF whose content stream inflates to ~50 MB.

    The point is that MAX_CHARS trips DURING extraction. A guard that only
    measures the finished string has already paid the memory cost.
    """
    payload = b"A" * (50 * 1024 * 1024)
    compressed = zlib.compress(payload)
    stream = b"BT /F1 12 Tf 72 720 Td (x) Tj ET"
    return (b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [4 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
            b"4 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>\nendobj\n"
            b"5 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream\nendobj\n"
            b"6 0 obj\n<< /Length " + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n" + compressed
            + b"\nendstream\nendobj\ntrailer\n<< /Size 7 /Root 1 0 R >>\n%%EOF\n")


# --------------------------------------------------------------------------- #
# extraction — unit level, no stack
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def extract():
    return load_module("services/ai-orchestrator/extract.py", "up_extract")


def test_api_extracts_text_from_a_real_pdf(extract):
    got = extract.extract(make_pdf(["Fasting instructions: eight hours."]), "policy.pdf")
    assert "Fasting instructions" in got.text
    assert got.pages == 1
    assert got.kind == "pdf"


def test_api_docx_is_named_not_lumped_into_unsupported(extract):
    """RVB-ING-13. "Unsupported file type" sends someone away with no next step."""
    with pytest.raises(extract.ExtractionError) as e:
        extract.check_supported("policy.docx")
    assert "DOCX" in str(e.value)
    assert "PDF" in str(e.value), "the message must say what to do instead"


def test_api_unknown_types_are_rejected_before_any_parsing(extract):
    for name in ("payload.exe", "archive.zip", "sheet.xlsx", "image.png"):
        with pytest.raises(extract.ExtractionError):
            extract.check_supported(name)


def test_api_page_cap_is_checked_before_reading_pages(extract):
    """RVB-ING-37. A 60,000-page PDF must not be read to discover it is too long."""
    with pytest.raises(extract.ExtractionError) as e:
        extract.extract(make_pdf([f"page {i}" for i in range(extract.MAX_PAGES + 5)]),
                        "long.pdf")
    assert "pages" in str(e.value).lower()


def test_api_oversize_file_is_rejected(extract):
    with pytest.raises(extract.ExtractionError) as e:
        extract.extract(b"x" * (extract.MAX_BYTES + 1), "big.txt")
    assert "MB" in str(e.value)


def test_api_char_cap_truncates_rather_than_exploding(extract):
    got = extract.extract(("word " * 100_000).encode(), "long.txt")
    assert got.chars <= extract.MAX_CHARS
    assert got.truncated
    assert got.notes, "truncation must be visible in the preview, not silent"


def test_api_decompression_blowup_is_bounded(extract):
    """codex F6. Bounded, and bounded DURING extraction."""
    try:
        got = extract.extract(make_zip_bomb_pdf(), "bomb.pdf")
    except extract.ExtractionError:
        return  # refusing outright is also a correct outcome
    assert got.chars <= extract.MAX_CHARS


def test_api_a_scanned_pdf_is_refused_not_staged_empty(extract):
    """RVB-ING-14. An empty document looks ingested and answers nothing."""
    with pytest.raises(extract.ExtractionError) as e:
        extract.extract(make_pdf(["   "]), "scan.pdf")
    assert "scan" in str(e.value).lower()


def test_api_parser_errors_never_echo_document_content(extract):
    """RVB-ING-12. A parser message can contain the bytes it choked on."""
    poisoned = b"%PDF-1.4\n" + b"SSN 123-45-6789 PATIENT MARIA GONZALEZ" * 20
    with pytest.raises(extract.ExtractionError) as e:
        extract.extract(poisoned, "corrupt.pdf")
    message = str(e.value)
    assert "123-45-6789" not in message
    assert "MARIA" not in message.upper()


# --------------------------------------------------------------------------- #
# gateway contract
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "up_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    cfg = load_module("services/gateway/config.py", "up_gw_config")
    cfg.settings.knowledge_ingest_users = frozenset({"kbadmin"})
    sys.modules["config"] = cfg

    app_mod = load_module("services/gateway/app.py", "up_gw_app")
    _sessions["kbadmin"] = {"username": "kbadmin", "role": "staff"}
    _sessions["frontdesk"] = {"username": "frontdesk", "role": "staff"}
    _sessions["maria"] = {"username": "maria.gonzalez", "role": "patient",
                          "patient_id": "1042"}
    return app_mod


@pytest.fixture
def client(gateway, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gateway, "_same_as_lookup", lambda pid: [pid])
    return TestClient(gateway.app)


@pytest.fixture
def upstream(gateway, monkeypatch):
    """Capture what reaches the orchestrator."""
    seen: list[dict] = []

    def fake_httpx_post(url, files=None, data=None, timeout=None, **kw):
        seen.append({"url": url, "files": files, "data": data})
        return httpx.Response(200, json={"staging_id": "abc123", "chunk_count": 2})

    def fake_post(service, path, payload):
        seen.append({"path": path, "payload": payload})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(gateway.httpx, "post", fake_httpx_post)
    monkeypatch.setattr(gateway, "_post", fake_post)
    return seen


def _upload(client, token, name="policy.pdf", data=None, title="Fasting policy"):
    return client.post(
        "/ai/knowledge/upload",
        files={"file": (name, data if data is not None else make_pdf(["hello"]),
                        "application/pdf")},
        data={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_api_upload_requires_the_ingest_capability(client, upstream):
    """RVB-ING-04 / RVB-AG-11. The UI hiding the control is never the check."""
    assert _upload(client, "frontdesk").status_code == 403
    assert _upload(client, "maria").status_code == 403
    assert upstream == [], "an unauthorized upload reached the orchestrator"


def test_api_upload_is_session_guarded(client, upstream):
    r = client.post("/ai/knowledge/upload",
                    files={"file": ("p.pdf", make_pdf(["x"]), "application/pdf")})
    assert r.status_code == 401
    assert upstream == []


def test_api_upload_rejects_oversize_before_forwarding(client, upstream):
    """RVB-ING-35. Streaming abort, not measure-after-buffering."""
    r = _upload(client, "kbadmin", name="big.pdf",
                data=b"%PDF-1.4\n" + b"x" * (11 * 1024 * 1024))
    assert r.status_code == 413
    assert upstream == [], "an oversize body was forwarded to the orchestrator"


def test_api_upload_stamps_provenance_from_the_session(client, upstream):
    """RVB-ING-05. A client must not be able to attribute a document elsewhere."""
    assert _upload(client, "kbadmin").status_code == 200
    assert upstream[0]["data"]["staged_by"] == "kbadmin"


def test_api_a_client_cannot_choose_its_own_provenance(client, upstream):
    """`staged_by` is read from the session and never from the form."""
    client.post(
        "/ai/knowledge/upload",
        files={"file": ("p.pdf", make_pdf(["x"]), "application/pdf")},
        data={"title": "t", "staged_by": "someone.else", "added_by": "someone.else"},
        headers={"Authorization": "Bearer kbadmin"},
    )
    assert upstream[0]["data"]["staged_by"] == "kbadmin"


def test_api_commit_takes_no_body_from_the_client(client, upstream):
    """There is nothing on the commit request for a caller to influence."""
    r = client.post("/ai/knowledge/staged/abc123/commit",
                    json={"username": "someone.else", "text": "injected"},
                    headers={"Authorization": "Bearer kbadmin"})
    assert r.status_code == 200
    assert upstream[0]["payload"] == {"username": "kbadmin"}


@pytest.mark.parametrize("method,path", [
    ("post", "/ai/knowledge/stage"),
    ("post", "/ai/knowledge/staged/abc/commit"),
    ("post", "/ai/knowledge/staged/abc/discard"),
])
def test_api_every_staging_route_requires_the_capability(client, upstream, method, path):
    r = getattr(client, method)(path, json={},
                                headers={"Authorization": "Bearer frontdesk"})
    assert r.status_code == 403
    assert upstream == []


def test_api_the_unpreviewed_write_path_is_gone(client, upstream):
    """codex F4. `/ai/knowledge/ingest` wrote straight to the index.

    While it existed, "every knowledge write passes a human gate" was false, and
    adr/0014 said it anyway. A 404 here is the claim becoming true.
    """
    r = client.post("/ai/knowledge/ingest",
                    json={"title": "t", "text": "body"},
                    headers={"Authorization": "Bearer kbadmin"})
    assert r.status_code == 404, (
        "the single-shot ingest endpoint is back; the preview gate is bypassable"
    )
