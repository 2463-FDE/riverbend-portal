"""
Shared test helpers.

There is no shared Python package across services (see adr/0001), so tests load
the specific module-under-test directly from its service directory by file path.
This avoids the module-name collisions you'd otherwise get from every service
having its own `app.py` / `config.py` / `models.py`.

Order-independence
------------------
That collision is real, not theoretical. Every service has a `config.py`, and a
service module that does `from config import settings` resolves it through
`sys.modules` — so once one service's `config` is cached, the *next* service to
load silently binds to the wrong settings object. The failure mode is an
``AttributeError`` on a setting that exists in one service and not another, and
it appears only in a particular collection order, which is the worst kind of test
failure to debug.

`load_module` therefore evicts sibling modules that were loaded from a *different*
service directory before importing. Modules already captured by an earlier test
file keep working — they hold their own references — so this is safe per-load.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# The live tier spends money. It is opt-in via an explicit flag, NOT via a
# marker expression in addopts: a `-m` on the command line overrides addopts, so
# `pytest -m "not integration"` (which the Makefile passes) would silently
# re-enable the spending tests. A collection hook cannot be overridden that way.
# --------------------------------------------------------------------------- #
def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run the live AWS Bedrock tests. These spend real money.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(
        reason="live tier spends money — pass --live to run (see tests/test_live_bedrock.py)"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)

# Module names that appear in more than one service directory. Anything here is
# evicted from sys.modules when it was loaded from a different service than the
# one we are about to import from.
_COLLIDING = {
    "app", "config", "db", "models", "schemas", "logging_config", "security",
    "authz", "check", "deidentify", "guardrails", "audit", "model_client",
    "retention", "chunking", "embeddings", "vector_store", "rag", "eval_harness",
    "knowledge_seed", "index_port", "chroma_index", "graph", "agent", "breaker",
    "eligibility_client", "mpi",
}


def _evict_foreign_siblings(service_dir: str) -> None:
    service_dir = os.path.abspath(service_dir)
    for name in list(sys.modules):
        if name not in _COLLIDING:
            continue
        module = sys.modules.get(name)
        file = getattr(module, "__file__", None)
        if not file:
            continue
        if os.path.dirname(os.path.abspath(file)) != service_dir:
            del sys.modules[name]


def load_module(relpath: str, name: str):
    """Load <REPO_ROOT>/<relpath> as a uniquely-named module.

    The module can import its own siblings (`config`, `models`, ...) and gets the
    ones from its own service directory regardless of what ran before it.
    """
    path = os.path.join(REPO_ROOT, relpath)
    service_dir = os.path.dirname(path)

    _evict_foreign_siblings(service_dir)

    # This service directory must win the sys.path race for its own siblings.
    while service_dir in sys.path:
        sys.path.remove(service_dir)
    sys.path.insert(0, service_dir)

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
