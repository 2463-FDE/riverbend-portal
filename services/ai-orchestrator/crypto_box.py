"""Authenticated encryption for short-lived PHI in Redis.

Two things store pre-decision PHI outside Postgres and outside the checkpointer:
`staging.py` (a document waiting on a human read) and `approvals.py` (an assembled
cross-chart view waiting on a release decision). Both hold material that has
deliberately *not* been cleared for disclosure yet, which is the whole point of
the gate in front of them.

They share this module rather than each carrying a copy, because two
implementations of the same envelope is how one of them quietly stops being
encrypted.

**The key path is `LANGGRAPH_AES_KEY`** — the same variable the checkpointer's
`EncryptedSerializer` uses. Deliberate: one key to provision, one to rotate, and
no way to end up with an encrypted checkpoint beside a plaintext staging blob.

**What this is not.** AES-GCM on the value. It does not cover Redis persistence
files, snapshots, operator access, or key rotation — see `docs/debt-register.md`
D-04 and D-05. Calling this "encryption at rest" without that sentence is the
overstatement this codebase keeps correcting.
"""
import base64
import hashlib
import json
import os
from typing import Optional

PLAIN = "plain"
AES_GCM = "aes-gcm"


class SealedBoxError(RuntimeError):
    """The envelope could not be opened. Never carries plaintext."""


def _key() -> Optional[bytes]:
    raw = os.getenv("LANGGRAPH_AES_KEY", "")
    if not raw:
        return None
    key = raw.encode()
    # AES accepts 16/24/32. Deriving rather than erroring keeps dev usable
    # without making the production path depend on a lucky key length.
    if len(key) not in (16, 24, 32):
        key = hashlib.sha256(key).digest()
    return key


def configured() -> bool:
    """Whether a key is present. Surfaced on /healthz so the posture is visible."""
    return _key() is not None


def seal(payload: dict) -> str:
    raw = json.dumps(payload).encode()
    key = _key()

    if key is None:
        # Dev and CI only. Tagged so a plaintext blob can never be mistaken for
        # an encrypted one on inspection — an untagged fallback is how a missing
        # key in production looks exactly like a working one.
        return json.dumps({"v": PLAIN, "d": raw.decode()})

    from Crypto.Cipher import AES

    cipher = AES.new(key, AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(raw)
    return json.dumps({
        "v": AES_GCM,
        "n": base64.b64encode(cipher.nonce).decode(),
        "t": base64.b64encode(tag).decode(),
        "d": base64.b64encode(ct).decode(),
    })


def unseal(blob: str) -> dict:
    try:
        envelope = json.loads(blob)
    except ValueError:
        raise SealedBoxError("stored value is not a sealed envelope")

    if envelope.get("v") == PLAIN:
        return json.loads(envelope["d"])

    key = _key()
    if key is None:
        # Refusing beats guessing: a key that vanished between write and read is
        # an operational fault, and silently failing open would hand back PHI
        # the operator believes is protected.
        raise SealedBoxError("value is encrypted but no key is configured")

    from Crypto.Cipher import AES

    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(envelope["n"]))
        raw = cipher.decrypt_and_verify(base64.b64decode(envelope["d"]),
                                        base64.b64decode(envelope["t"]))
    except Exception:  # noqa: BLE001
        # GCM is authenticated, so this covers tampering as well as a wrong key.
        # The message says nothing about the payload.
        raise SealedBoxError("sealed value failed authentication")

    return json.loads(raw.decode())
