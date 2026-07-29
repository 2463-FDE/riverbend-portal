"""Staged documents: extracted, scrubbed, and waiting on a human.

Phase one of ingest writes here. Phase two reads here and writes to Chroma.
Nothing else may write to the knowledge collection (adr/0014 §4a).

Three properties this module exists to guarantee:

**It holds PHI.** That is the premise of the preview -- we show the uploader the
text precisely because the scrub is lenient and may have missed something. So the
payload is encrypted at rest with the same key path as the checkpointer
(`LANGGRAPH_AES_KEY`), and every transition is audited (codex F11).

**Exactly one commit can win.** `GETDEL` is atomic, so a replayed or concurrent
commit finds nothing rather than double-indexing (codex F7).

**A staged document belongs to its uploader.** `staged_by` is compared on commit.
One employee must not be able to commit another's document, and provenance must
stay server-stamped across the two-request handoff.
"""
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

TTL_SECONDS = 30 * 60             # RVB-ING-15
MAX_STAGED_PER_USER = 5           # RVB-ING-41
KEY_PREFIX = "kb:staging:"
OWNER_INDEX = "kb:staging:by_user:"


class StagingUnavailable(RuntimeError):
    """Redis is not reachable. Fails CLOSED — no staging, therefore no ingest."""


class StagingNotFound(LookupError):
    """Unknown or expired id. 404."""


class StagingForbidden(PermissionError):
    """Someone else's staged document. 403, not 404 (RVB-ING-17)."""


class TooManyStaged(RuntimeError):
    """The uploader has too many previews open. 429."""


@dataclass
class StagedDoc:
    staging_id: str
    title: str
    text: str
    source: str
    filename: str
    staged_by: str
    kind: str
    pages: int
    chars: int
    chunk_count: int
    redactions: list = field(default_factory=list)
    metadata_redactions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    truncated: bool = False
    created_at: float = 0.0

    def preview(self, expires_in: int) -> dict:
        """What the confirm screen renders. The text is post-scrub (RVB-ING-20)."""
        return {
            "staging_id": self.staging_id,
            "title": self.title,
            "filename": self.filename,
            "kind": self.kind,
            "pages": self.pages,
            "chars": self.chars,
            "chunk_count": self.chunk_count,
            "text": self.text,
            "redactions": self.redactions,
            "metadata_redactions": self.metadata_redactions,
            "notes": self.notes,
            "truncated": self.truncated,
            "staged_by": self.staged_by,
            "expires_in_seconds": expires_in,
        }


# --------------------------------------------------------------------------- #
# encryption — same key path as the checkpointer (codex F11)
# --------------------------------------------------------------------------- #
def _key() -> Optional[bytes]:
    raw = os.getenv("LANGGRAPH_AES_KEY", "")
    if not raw:
        return None
    key = raw.encode()
    # AES wants 16/24/32. Deriving rather than erroring keeps dev usable without
    # making the production path depend on a lucky key length.
    if len(key) not in (16, 24, 32):
        import hashlib
        key = hashlib.sha256(key).digest()
    return key


def _seal(payload: dict) -> str:
    raw = json.dumps(payload).encode()
    key = _key()
    if key is None:
        # Dev/CI only. Marked in the envelope so a plaintext blob can never be
        # mistaken for an encrypted one on inspection.
        return json.dumps({"v": "plain", "d": raw.decode()})

    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(raw)
    import base64
    return json.dumps({
        "v": "aes-gcm",
        "n": base64.b64encode(cipher.nonce).decode(),
        "t": base64.b64encode(tag).decode(),
        "d": base64.b64encode(ct).decode(),
    })


def _open(blob: str) -> dict:
    envelope = json.loads(blob)
    if envelope.get("v") == "plain":
        return json.loads(envelope["d"])

    key = _key()
    if key is None:
        raise StagingUnavailable("staged document is encrypted but no key is configured")

    import base64
    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(envelope["n"]))
    raw = cipher.decrypt_and_verify(base64.b64decode(envelope["d"]),
                                    base64.b64decode(envelope["t"]))
    return json.loads(raw.decode())


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
_client = None


def _redis():
    global _client
    if _client is None:
        try:
            import redis as redis_lib
            from config import settings

            url = getattr(settings, "redis_url", None) or os.getenv(
                "REDIS_URL", "redis://redis:6379/0")
            _client = redis_lib.from_url(url, decode_responses=True)
            _client.ping()
        except Exception as e:  # noqa: BLE001
            _client = None
            raise StagingUnavailable(f"staging store unavailable: {type(e).__name__}")
    return _client


def reset_client() -> None:
    """Tests swap the backing store; without this they share a connection."""
    global _client
    _client = None


def stage(doc: StagedDoc) -> StagedDoc:
    r = _redis()

    owner_key = f"{OWNER_INDEX}{doc.staged_by}"
    open_count = r.scard(owner_key)
    if open_count >= MAX_STAGED_PER_USER:
        raise TooManyStaged(
            f"You have {open_count} previews waiting. Commit or discard one "
            f"before uploading another."
        )

    doc.staging_id = doc.staging_id or uuid.uuid4().hex
    doc.created_at = doc.created_at or time.time()

    r.setex(f"{KEY_PREFIX}{doc.staging_id}", TTL_SECONDS, _seal(asdict(doc)))
    r.sadd(owner_key, doc.staging_id)
    r.expire(owner_key, TTL_SECONDS)
    return doc


def peek(staging_id: str, username: str) -> StagedDoc:
    """Read WITHOUT consuming. Used to re-render a preview."""
    r = _redis()
    blob = r.get(f"{KEY_PREFIX}{staging_id}")
    if blob is None:
        # Deliberately covers BOTH cases. `peek` cannot distinguish an expired
        # key from one a successful commit already consumed -- in Redis they are
        # the same absence -- and claiming "expired" for a document that was in
        # fact just added would send someone to re-upload a duplicate.
        raise StagingNotFound(
            "that preview is no longer available — it was already added, or it "
            "expired. Check the knowledge base before uploading again.")

    doc = StagedDoc(**_open(blob))
    if doc.staged_by != username:
        raise StagingForbidden("that document was staged by someone else")
    return doc


def claim(staging_id: str, username: str) -> StagedDoc:
    """Take the document, atomically, so exactly one commit can win (codex F7).

    The ownership check happens BEFORE the delete: a caller who is not the owner
    must not be able to destroy someone else's staged document by trying to
    commit it. So this is peek-then-GETDEL rather than GETDEL-then-check.

    The residual race — owner commits twice, concurrently — is what GETDEL
    settles: one gets the document, the other gets a 404.
    """
    doc = peek(staging_id, username)          # raises 403 for a non-owner

    r = _redis()
    key = f"{KEY_PREFIX}{staging_id}"
    try:
        blob = r.getdel(key)
    except AttributeError:  # redis-py < 4.0 / older server
        pipe = r.pipeline()
        pipe.get(key)
        pipe.delete(key)
        blob = pipe.execute()[0]

    if blob is None:
        raise StagingNotFound("that preview has already been used")

    r.srem(f"{OWNER_INDEX}{username}", staging_id)
    return doc


def discard(staging_id: str, username: str) -> None:
    doc = peek(staging_id, username)
    r = _redis()
    r.delete(f"{KEY_PREFIX}{staging_id}")
    r.srem(f"{OWNER_INDEX}{doc.staged_by}", staging_id)


def ttl(staging_id: str) -> int:
    try:
        remaining = _redis().ttl(f"{KEY_PREFIX}{staging_id}")
    except StagingUnavailable:
        return 0
    return max(0, int(remaining or 0))
