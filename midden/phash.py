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


# dHash bit-population gates. A dHash with very few set bits (≈ uniform / blank
# image) or very many (inverted-uniform) carries no discriminative structure:
# such hashes sit within a tiny Hamming radius of each other and, under
# single-linkage clustering, chain thousands of unrelated blank/icon images into
# one blob. They are EXCLUDED from near-image clustering rather than clustered.
POPCOUNT_MIN = 8   # below -> degenerate (blank / near-uniform)
POPCOUNT_MAX = 56  # above -> degenerate (inverted-uniform)


def _dhash_bits(small, size: int) -> int:
    """dHash bits from an already-(L, (size+1)xsize)-resized image."""
    px = list(small.getdata())
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


def dhash_with_dims(path: Path, size: int = 8) -> tuple[int, int, int]:
    """Return (dhash, width, height). Reads the image once; original dimensions
    are captured BEFORE the downscale so callers can use aspect ratio as a
    structural prior (dHash itself discards aspect)."""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not installed; perceptual hashing unavailable")
    with Image.open(path) as im:
        w, h = im.size
        small = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    return _dhash_bits(small, size), w, h


def dhash(path: Path, size: int = 8) -> int:
    """64-bit difference hash of the image at `path`. Raises if Pillow absent."""
    return dhash_with_dims(path, size)[0]


def aspect_bucket(w, h) -> str:
    """Coarse aspect-ratio bucket — a structural prior so images of very
    different shape are never near-dup candidates. None/0-safe."""
    if not w or not h:
        return "unknown"
    r = w / h
    if r >= 2.2:
        return "pano_wide"
    if r >= 1.45:
        return "wide"        # ~3:2, 16:9
    if r >= 1.15:
        return "landscape"   # ~4:3
    if r >= 0.87:
        return "square"      # ~1:1
    if r >= 0.69:
        return "portrait"    # ~3:4
    if r >= 0.45:
        return "tall"        # ~9:16
    return "pano_tall"


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_hex(sig: int) -> str:
    return f"{sig:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
