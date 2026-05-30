"""64-bit SimHash over text tokens. Pure stdlib, no dependencies.

SimHash maps similar token sets to similar 64-bit fingerprints; the Hamming
distance between two fingerprints approximates document dissimilarity. Cheap to
compute and compare — good for catching near-duplicate / version-chain documents
without embeddings.

We hash word *shingles* (k-grams) rather than bare words: with a small
vocabulary, shingles carry far more positional signal and resist spurious
collisions between unrelated short texts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterator

_TOKEN = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _shingles(tokens: list[str], k: int) -> Iterator[str]:
    if len(tokens) < k:
        if tokens:
            yield " ".join(tokens)
        return
    for i in range(len(tokens) - k + 1):
        yield " ".join(tokens[i : i + k])


def _feat_hash(feature: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big"
    )


def simhash(text: str, k: int = 3) -> int:
    """Return the 64-bit SimHash of `text` over k-word shingles."""
    feats = list(_shingles(tokenize(text), k))
    if not feats:
        return 0
    v = [0] * 64
    for f in feats:
        h = _feat_hash(f)
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if v[b] > 0:
            out |= 1 << b
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_hex(sig: int) -> str:
    return f"{sig:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
