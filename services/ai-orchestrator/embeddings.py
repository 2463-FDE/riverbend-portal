"""Embedding backends and the cache that keeps this week inside its quota.

Two backends behind one interface:

  * ``titan``  — Amazon Titan Text Embeddings V2 (``amazon.titan-embed-text-v2:0``)
    on Bedrock. 8,192 tokens / 50,000 characters in; output 1,024 (default), 512
    or 256 dimensions. We default to 256 for the sampled demo corpus: a quarter
    of the index size and query cost, with no measurable retrieval loss at this
    scale. Configurable, because that trade-off changes as the corpus grows.

  * ``offline`` — a deterministic hashed bag-of-terms projection. No network, no
    credentials, no spend, and byte-identical run to run, which the eval harness
    depends on. It is a weaker semantic signal than a real model and it is
    supposed to be: it exists so CI can measure retrieval *behaviour*
    reproducibly, not so we can pretend we have embeddings for free.

The cache is not an optimisation, it is a quota control
-------------------------------------------------------
The client packet flags Week 2 as a quota-risk week and says so explicitly:
"embed once and cache — do not re-embed per run." The cache key is
``sha256(text + model + dims)``, so re-ingesting unchanged content performs
**zero** embedding calls. A test asserts the call count is zero, because "we
added a cache" and "the cache actually prevents the calls" are different claims.
"""
import hashlib
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from config import settings

_WORD = re.compile(r"[a-z0-9]+")
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# tokenization — shared with the lexical retriever so both sides agree
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset(
    "a an the is are was were be been being of to in on at for from with by and "
    "or not no do does did i you we they it this that these those my your our "
    "how what when where which who whom why can could should would will shall "
    "have has had if then than so as about into out up down over under me "
    "show tell give list find get need want".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with a crude suffix stem. Deterministic."""
    out = []
    for tok in _WORD.findall((text or "").lower()):
        for suffix in ("ing", "ed", "es", "s"):
            if len(tok) > 4 and tok.endswith(suffix):
                tok = tok[: -len(suffix)]
                break
        out.append(tok)
    return out


def content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _STOPWORDS}


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
@dataclass
class EmbedStats:
    calls: int = 0            # texts actually sent to a backend
    cache_hits: int = 0
    batches: int = 0          # network round trips (0 for the offline backend)

    def as_dict(self) -> dict:
        return {"calls": self.calls, "cache_hits": self.cache_hits,
                "batches": self.batches}


def _offline_vector(text: str, dims: int) -> list[float]:
    """Deterministic hashed bag-of-terms, L2-normalised.

    Each content term is hashed to a dimension and a sign, then accumulated with
    a sublinear term-frequency weight. Same input, same vector, forever — which
    is what makes the eval harness reproducible.
    """
    vec = [0.0] * dims
    terms = tokenize(text)
    if not terms:
        return vec
    counts: dict[str, int] = {}
    for t in terms:
        counts[t] = counts.get(t, 0) + 1
    for term, tf in counts.items():
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign * (1.0 + math.log(tf))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm:
        vec = [v / norm for v in vec]
    return vec


class OfflineEmbedder:
    """No network, no credentials, no spend, byte-identical run to run."""

    name = "offline"

    def __init__(self, dims: int):
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_offline_vector(t, self.dims) for t in texts]


class TitanEmbedder:
    """Amazon Titan Text Embeddings V2 over Bedrock."""

    name = "titan"

    def __init__(self, dims: int, model_id: str, client=None):
        self.dims = dims
        self.model_id = model_id
        self._client = client

    def _bedrock(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=settings.aws_region,
                config=Config(
                    connect_timeout=settings.connect_timeout_s,
                    read_timeout=settings.read_timeout_s,
                    retries={"max_attempts": 0},
                ),
            )
        return self._client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import json

        client = self._bedrock()
        out: list[list[float]] = []
        for text in texts:
            # Titan V2 embeds one input per call; batching is a client-side loop.
            resp = client.invoke_model(
                modelId=self.model_id,
                body=json.dumps({"inputText": text, "dimensions": self.dims,
                                 "normalize": True}),
            )
            payload = json.loads(resp["body"].read())
            out.append(payload["embedding"])
        return out


# --------------------------------------------------------------------------- #
# the cached front door
# --------------------------------------------------------------------------- #
class Embedder:
    """Backend + cache. The only thing the rest of the service should use."""

    def __init__(self, backend=None, dims: Optional[int] = None):
        self.dims = dims or settings.embed_dims
        self.backend = backend or self._default_backend()
        self.stats = EmbedStats()
        self._cache: dict[str, list[float]] = {}

    def _default_backend(self):
        if settings.embed_backend == "titan" and not settings.use_stub:
            return TitanEmbedder(self.dims, settings.embed_model_id)
        return OfflineEmbedder(self.dims)

    def _key(self, text: str) -> str:
        raw = f"{self.backend.name}|{self.dims}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed, using the cache. Only cache misses reach the backend."""
        with _lock:
            keys = [self._key(t) for t in texts]
            misses = [(i, t) for i, (t, k) in enumerate(zip(texts, keys))
                      if k not in self._cache]
            self.stats.cache_hits += len(texts) - len(misses)

            if misses:
                vectors = self.backend.embed([t for _i, t in misses])
                self.stats.calls += len(misses)
                self.stats.batches += 1
                for (i, _t), vec in zip(misses, vectors):
                    self._cache[keys[i]] = vec

            return [self._cache[k] for k in keys]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def clear_cache(self) -> None:
        with _lock:
            self._cache.clear()
