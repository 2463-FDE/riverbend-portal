"""The schema file actually loads into a real Postgres.

`db/schema.sql` was, until this test existed, the only file in the repository
with no test of any kind. It is also the file `docker-entrypoint` runs first on
a fresh volume, so a syntax or ordering error in it means **nothing starts**.

One did. #16 added `patient_id INTEGER REFERENCES patients(id)` to `users`, which
is declared twenty lines *above* `patients` in the same file:

    psql:/docker-entrypoint-initdb.d/01-schema.sql:28:
      ERROR:  relation "patients" does not exist

The stack had been unstartable from a clean volume ever since. 248 backend tests
stayed green throughout, because every one of them mocks the database.

This runs the file the way the entrypoint does — `psql -v ON_ERROR_STOP=1`,
against a throwaway database — rather than parsing it, because the only authority
on whether Postgres accepts this file is Postgres.

Requires the stack (`make up`). See docs/findings/w1ui-nothing-ever-ran-the-stack.md.
"""
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "db" / "schema.sql"
SCRATCH_DB = "riverbend_schema_check"


def _psql(db: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run psql inside the compose postgres container.

    Deliberately NOT over a host port: the local dev stack remaps 5432 (see
    docker-compose.override.yml) and CI does not, so a hard-coded port would make
    this test pass in one place and error in the other.
    """
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "riverbend_app", "-d", db, "-v", "ON_ERROR_STOP=1", *args],
        cwd=REPO_ROOT, input=stdin, capture_output=True, text=True, timeout=120,
    )


@pytest.fixture
def scratch_db():
    probe = _psql("postgres", "-c", "SELECT 1")
    if probe.returncode != 0:
        pytest.skip(f"compose postgres not reachable — run `make up` first: {probe.stderr.strip()[:200]}")

    _psql("postgres", "-c", f'DROP DATABASE IF EXISTS {SCRATCH_DB}')
    created = _psql("postgres", "-c", f'CREATE DATABASE {SCRATCH_DB}')
    assert created.returncode == 0, created.stderr
    try:
        yield SCRATCH_DB
    finally:
        _psql("postgres", "-c", f'DROP DATABASE IF EXISTS {SCRATCH_DB}')


def test_schema_loads_into_an_empty_database(scratch_db):
    """The whole point. A fresh volume runs exactly this."""
    result = _psql(scratch_db, "-f", "-", stdin=SCHEMA.read_text())
    assert result.returncode == 0, (
        f"db/schema.sql does not load into an empty database, so `docker compose up` "
        f"cannot initialise and NO service starts:\n{result.stderr}"
    )


def test_schema_is_idempotent(scratch_db):
    """Loading twice must not error.

    Every CREATE is `IF NOT EXISTS`, and the users->patients constraint is added
    with an explicit DROP first for exactly this reason. A future `ALTER TABLE
    ... ADD CONSTRAINT` written without that DROP would pass the test above and
    fail here.
    """
    sql = SCHEMA.read_text()
    assert _psql(scratch_db, "-f", "-", stdin=sql).returncode == 0
    second = _psql(scratch_db, "-f", "-", stdin=sql)
    assert second.returncode == 0, f"schema.sql is not idempotent:\n{second.stderr}"


def test_the_user_patient_binding_survived(scratch_db):
    """Pin the constraint itself, not just that the file parses.

    The cheapest way to make the load error go away was to delete the foreign
    key. That would have silently removed the W4 / adr/0011 binding that makes
    "which patient is this login?" answerable at all -- a green build hiding a
    lost authorization control.
    """
    assert _psql(scratch_db, "-f", "-", stdin=SCHEMA.read_text()).returncode == 0

    fk = _psql(scratch_db, "-tAc",
               "SELECT conname FROM pg_constraint "
               "WHERE conrelid = 'users'::regclass AND contype = 'f'")
    assert "users_patient_id_fkey" in fk.stdout, (
        "the users -> patients foreign key is gone; adr/0011's session identity "
        "binding is no longer enforced by the database"
    )

    uniq = _psql(scratch_db, "-tAc",
                 "SELECT indexname FROM pg_indexes WHERE tablename = 'users'")
    assert "users_patient_id_uniq" in uniq.stdout, (
        "two logins could share one patient row, which makes 'who viewed this "
        "patient?' (W10) unanswerable"
    )
