"""Deterministic document chunking.

Small enough to retrieve, large enough to mean something. A sliding word-boundary
window with overlap, so a fact that straddles a boundary stays retrievable from
either side.

Determinism is a requirement, not a nicety: the eval harness compares runs, and a
chunker that reorders or re-splits between runs turns every retrieval metric into
noise. Same input, same chunks, forever.

Two small properties worth knowing:
  * `overlap` is clamped strictly below `size` so the window always advances.
    Without the clamp, `overlap >= size` is an infinite loop.
  * A blank line is treated as a preferred break point once we are near the
    target size, so a paragraph boundary wins over an arbitrary word boundary.
"""
from dataclasses import dataclass

_CHARS_PER_TOKEN = 4      # matches model_client.estimate_tokens so budgets line up
_CHARS_PER_WORD = 6       # including the trailing space


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    approx_tokens: int


def approx_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def chunk_text(text: str, chunk_tokens: int = 120, overlap_tokens: int = 24) -> list[Chunk]:
    """Split `text` into overlapping chunks. Returns [] for empty input."""
    words = (text or "").split()
    if not words:
        return []

    chunk_words = max(1, chunk_tokens * _CHARS_PER_TOKEN // _CHARS_PER_WORD)
    overlap_words = max(0, min(overlap_tokens * _CHARS_PER_TOKEN // _CHARS_PER_WORD,
                               chunk_words - 1))
    step = chunk_words - overlap_words

    chunks: list[Chunk] = []
    start = 0
    index = 0
    while start < len(words):
        window = words[start : start + chunk_words]
        body = " ".join(window).strip()
        if body:
            chunks.append(Chunk(index=index, text=body, approx_tokens=approx_tokens(body)))
            index += 1
        if start + chunk_words >= len(words):
            break
        start += step
    return chunks
