"""Perceptual image hashing (dHash) via Pillow. Optional dependency.

dHash: downscale to 9x8 grayscale, compare adjacent pixels row-wise to produce
64 bits. Robust to resize/recompression/minor edits; Hamming distance between
hashes approximates visual dissimilarity.

Pillow is an optional dep — import errors are surfaced via PIL_AVAILABLE so the
rest of Midden keeps working without it.
"""
from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image  # type: ignore
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without Pillow
    PIL_AVAILABLE = False


def dhash(path: Path, size: int = 8) -> int:
    """64-bit difference hash of the image at `path`. Raises if Pillow absent."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not installed; perceptual hashing unavailable")
    img = Image.open(path).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(img.getdata())
    bits = 0
    bit = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            if left > right:
                bits |= 1 << bit
            bit += 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_hex(sig: int) -> str:
    return f"{sig:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
