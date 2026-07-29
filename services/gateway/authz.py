"""Per-action authorization for the knowledge base (W2).

The inherited system has exactly one role — `staff` — and no per-action
authorization beyond "is logged in" (see ARCHITECTURE §7, adr/0003). Adding a
document to the AI's knowledge base is a privileged action: one bad or malicious
document silently changes every future answer the assistant grounds on, for every
user, indefinitely, and the poisoned answers still carry confident citations — to
the poisoned document.

Low volume, unbounded blast radius, no cheap undo. That is the test the design
debate set for where a human gate belongs, and knowledge ingest passes it.

Rather than block on the full role-hierarchy migration (D7, Week 9), this
introduces one narrow, explicit capability the gateway enforces:

  * `KNOWLEDGE_INGEST_USERS` — a username allowlist, and
  * `KNOWLEDGE_INGEST_ROLES` — a role allowlist, so it keeps working unchanged
    once real roles land.

A session may ingest if its username OR its role is on the respective list.
Read, query and eval stay open to any authenticated user; only writes are gated.

**This is an interim control, and it should be described that way to the client.**
It is not least-privilege and it does not resolve D7. It is a capability bolted
beside a role model that cannot express capabilities yet.
"""
from fastapi import HTTPException

from config import settings


def can_ingest(session: dict) -> bool:
    username = (session or {}).get("username", "")
    role = (session or {}).get("role", "")
    return (
        (username and username in settings.knowledge_ingest_users)
        or (role and role in settings.knowledge_ingest_roles)
    )


def require_ingest(session: dict) -> dict:
    """Raise 403 unless the session holds the knowledge-admin capability."""
    if not can_ingest(session):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to add documents to the knowledge base.",
        )
    return session
