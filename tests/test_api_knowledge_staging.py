"""Staging holds PHI, so the rules about who can read it and how it ends matter.

The staged document is the *un-published* one, and the whole reason it exists is
that `scrub_document` is lenient and may have missed something. So it is PHI
sitting in Redis for thirty minutes, and these tests pin the three properties
`adr/0014` claims for it:

  * encrypted at rest (codex F11)
  * exactly one commit can win (codex F7)
  * it belongs to the person who staged it
"""
import sys
import time

import pytest

from conftest import load_module


class FakeRedis:
    """Enough Redis to exercise the semantics we depend on.

    Hand-rolled rather than adding `fakeredis`: the surface used is five commands,
    and `getdel` atomicity is the one behaviour under test -- which a stub models
    honestly, because the test is about *calling* getdel rather than about
    Redis's own concurrency.
    """

    def __init__(self):
        self.kv: dict[str, tuple[str, float]] = {}
        self.sets: dict[str, set] = {}
        self.getdel_calls = 0

    def _live(self, key):
        item = self.kv.get(key)
        if item is None:
            return None
        value, expires = item
        if expires and expires < time.time():
            del self.kv[key]
            return None
        return value

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self.kv[key] = (value, time.time() + ttl)

    def get(self, key):
        return self._live(key)

    def getdel(self, key):
        self.getdel_calls += 1
        value = self._live(key)
        self.kv.pop(key, None)
        return value

    def delete(self, key):
        self.kv.pop(key, None)

    def ttl(self, key):
        item = self.kv.get(key)
        return int(item[1] - time.time()) if item else -2

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        return True


@pytest.fixture
def staging(monkeypatch):
    mod = load_module("services/ai-orchestrator/staging.py", "st_staging")
    fake = FakeRedis()
    monkeypatch.setattr(mod, "_redis", lambda: fake)
    mod._fake = fake
    return mod


def _doc(staging, username="kbadmin", text="Fasting: eight hours."):
    return staging.StagedDoc(
        staging_id="", title="Fasting policy", text=text, source="uploaded file",
        filename="fasting.pdf", staged_by=username, kind="pdf", pages=1,
        chars=len(text), chunk_count=1,
    )


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #
def test_a_staged_document_belongs_to_its_uploader(staging):
    doc = staging.stage(_doc(staging))
    with pytest.raises(staging.StagingForbidden):
        staging.peek(doc.staging_id, "someone.else")


def test_owner_mismatch_is_403_not_404(staging):
    """RVB-ING-17. The legitimate owner is entitled to know their id is valid.

    Returning 404 would only hide that from the person who actually owns it,
    while telling an attacker nothing they could not already guess.
    """
    doc = staging.stage(_doc(staging))
    with pytest.raises(staging.StagingForbidden):
        staging.claim(doc.staging_id, "someone.else")


def test_a_failed_claim_does_not_destroy_the_document(staging):
    """The ownership check runs BEFORE the delete.

    Otherwise anyone who learned a staging id could destroy someone else's work
    by attempting to commit it -- a denial-of-service with a 403 receipt.
    """
    doc = staging.stage(_doc(staging))
    with pytest.raises(staging.StagingForbidden):
        staging.claim(doc.staging_id, "someone.else")
    assert staging.peek(doc.staging_id, "kbadmin").title == "Fasting policy"


# --------------------------------------------------------------------------- #
# single-use (codex F7)
# --------------------------------------------------------------------------- #
def test_commit_is_single_use(staging):
    doc = staging.stage(_doc(staging))
    assert staging.claim(doc.staging_id, "kbadmin").text
    with pytest.raises(staging.StagingNotFound):
        staging.claim(doc.staging_id, "kbadmin")


def test_the_claim_uses_an_atomic_getdel(staging):
    """Not read-then-delete. Two concurrent commits must not both win."""
    doc = staging.stage(_doc(staging))
    staging.claim(doc.staging_id, "kbadmin")
    assert staging._fake.getdel_calls == 1


def test_an_unknown_id_is_not_found(staging):
    with pytest.raises(staging.StagingNotFound):
        staging.peek("nope", "kbadmin")


def test_an_expired_preview_is_not_found(staging, monkeypatch):
    doc = staging.stage(_doc(staging))
    key = f"{staging.KEY_PREFIX}{doc.staging_id}"
    value, _ = staging._fake.kv[key]
    staging._fake.kv[key] = (value, time.time() - 1)

    with pytest.raises(staging.StagingNotFound):
        staging.peek(doc.staging_id, "kbadmin")


# --------------------------------------------------------------------------- #
# encryption at rest (codex F11)
# --------------------------------------------------------------------------- #
def test_staged_text_is_encrypted_when_a_key_is_configured(staging, monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "unit-test-key-not-a-real-secret")

    secret = "Patient Maria Gonzalez, penicillin allergy."
    doc = staging.stage(_doc(staging, text=secret))

    stored = staging._fake.kv[f"{staging.KEY_PREFIX}{doc.staging_id}"][0]
    assert secret not in stored, "staged PHI is sitting in Redis in plaintext"
    assert "aes-gcm" in stored

    assert staging.peek(doc.staging_id, "kbadmin").text == secret


def test_without_a_key_the_envelope_says_so(staging, monkeypatch):
    """Dev and CI run without a key. It must be obvious on inspection.

    An unmarked plaintext blob is indistinguishable from a failed encryption, so
    the envelope names which one it is.
    """
    monkeypatch.delenv("LANGGRAPH_AES_KEY", raising=False)
    doc = staging.stage(_doc(staging))
    stored = staging._fake.kv[f"{staging.KEY_PREFIX}{doc.staging_id}"][0]
    assert '"v": "plain"' in stored or '"v":"plain"' in stored


def test_a_tampered_ciphertext_does_not_decrypt(staging, monkeypatch):
    """AES-GCM is authenticated. A modified staged document must not commit."""
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "unit-test-key-not-a-real-secret")
    doc = staging.stage(_doc(staging))

    key = f"{staging.KEY_PREFIX}{doc.staging_id}"
    blob, expires = staging._fake.kv[key]
    staging._fake.kv[key] = (blob.replace('"d": "', '"d": "A'), expires)

    with pytest.raises(Exception):
        staging.peek(doc.staging_id, "kbadmin")


# --------------------------------------------------------------------------- #
# capacity + lifecycle
# --------------------------------------------------------------------------- #
def test_a_user_cannot_hoard_previews(staging):
    """RVB-ING-41. Abandoned uploads are PHI accumulating in Redis."""
    for _ in range(staging.MAX_STAGED_PER_USER):
        staging.stage(_doc(staging))
    with pytest.raises(staging.TooManyStaged):
        staging.stage(_doc(staging))


def test_discarding_frees_a_slot(staging):
    docs = [staging.stage(_doc(staging)) for _ in range(staging.MAX_STAGED_PER_USER)]
    staging.discard(docs[0].staging_id, "kbadmin")
    staging.stage(_doc(staging))  # must not raise


def test_committing_frees_a_slot(staging):
    docs = [staging.stage(_doc(staging)) for _ in range(staging.MAX_STAGED_PER_USER)]
    staging.claim(docs[0].staging_id, "kbadmin")
    staging.stage(_doc(staging))


def test_one_users_quota_does_not_affect_another(staging):
    for _ in range(staging.MAX_STAGED_PER_USER):
        staging.stage(_doc(staging, username="kbadmin"))
    staging.stage(_doc(staging, username="other.admin"))


def test_the_preview_reports_what_will_be_indexed(staging):
    doc = staging.stage(_doc(staging))
    preview = doc.preview(expires_in=1800)
    assert preview["text"] == doc.text
    assert preview["chunk_count"] == 1
    assert preview["expires_in_seconds"] == 1800
    assert preview["staged_by"] == "kbadmin"


def test_staging_fails_closed_when_redis_is_down(monkeypatch):
    """No staging store means no ingest. It must never fall through to a write."""
    mod = load_module("services/ai-orchestrator/staging.py", "st_staging_down")

    def boom():
        raise mod.StagingUnavailable("redis down")

    monkeypatch.setattr(mod, "_redis", boom)
    with pytest.raises(mod.StagingUnavailable):
        mod.stage(_doc(mod))
