"""Near-duplicate detection: signature computation + clustering.

Two cluster kinds:
- doc_version: text files with similar SimHash that ALSO share structural signal
  (same parent directory or normalized filename stem). The structural restriction
  is ESSENTIAL, not an optimization: pure content thresholds over a small shared
  vocabulary produce false positives — measured on the synthetic corpus, unrelated
  short docs can collide at SimHash distance 0 (see tools/probe_simhash.py). The
  directory/stem prior is what makes clustering precise. Tuned: k=4 shingles,
  Hamming threshold 12, single-linkage over candidate pairs only.
- near_image: images with similar perceptual (dHash) signatures. Requires Pillow.

Signature *computation* reads files (read-only) and lives here, not in store.py,
which only handles schema/access. Clustering logic also lives here; store.py
provides the cluster-creation primitive.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Iterable, Optional

from . import phash, simhash
from .store import Store

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".rst", ".tex", ".html", ".xml", ".ini", ".py"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"}

ALGO_TEXT = "simhash_text"
ALGO_IMAGE = "phash_image"

# Tuned on the synthetic corpus (tools/e2e_near.py). Measured intra-chain
# consecutive SimHash distances at k=4 reach 15 (heavily-edited manuscript
# chain); random cross-document pairs sit at median ~32, p5 ~25. 16 clears the
# real chains with margin while staying well under the noise floor.
DOC_VERSION_THRESHOLD = 16   # max SimHash Hamming distance to link two docs
TEXT_READ_BYTES = 256 * 1024

# Near-image clustering. The old single-linkage all-pairs at Hamming ≤10 chained
# ~9,581 unrelated images (driven by 771 all-zero "blank" dHashes) into one blob.
# The fix has four parts:
#   1. exclude degenerate dHashes (phash.POPCOUNT_MIN/MAX) — blanks aren't "similar".
#   2. aspect-ratio prior: only images of the same shape are candidates.
#   3. tight threshold (6, was 10) — precision over recall; a missed near-dup is
#      cheap, a mega-cluster is not.
#   4. diameter cap: any component exceeding MAX_NEAR_CLUSTER is dropped, not
#      surfaced — a clean near-dup set is small.
# Candidate generation uses LSH banding (not O(n²) all-pairs over 42k images).
NEAR_IMAGE_THRESHOLD = 6     # max dHash Hamming distance to link two images
MAX_NEAR_CLUSTER = 200       # components larger than this are dropped + counted
LSH_BANDS = 8                # 8 bands × 8 bits; threshold < bands ⇒ no missed pairs
LSH_BUCKET_CAP = 4000        # skip pair-gen for pathologically large LSH buckets

# Version markers stripped to derive a shared stem. NOTE: the digit form REQUIRES
# a leading 'v' (_v1, _v2). A bare _<number> is NOT treated as a version — doing
# so collapsed enumerated junk like file_000/file_001 to one stem ("file"),
# making all scattered noise files mutual clustering candidates.
_VER = re.compile(r"(_v\d+|_final|_revised|_backup|_copy|\bfinal\b)", re.I)


def _norm_stem(rel: str) -> str:
    stem = Path(rel).stem.lower()
    return _VER.sub("", stem).strip("_ ") or stem


def _is_text(rel: str, mime: Optional[str]) -> bool:
    if Path(rel).suffix.lower() in TEXT_EXT:
        return True
    return bool(mime and mime.startswith("text/"))


def _is_image(rel: str, mime: Optional[str]) -> bool:
    if Path(rel).suffix.lower() in IMAGE_EXT:
        return True
    return bool(mime and mime.startswith("image/"))


# ---------- signature computation (reads files) ----------
def compute_text_signatures(store: Store) -> int:
    """SimHash every active text file lacking a signature. Returns # computed."""
    have = store.hashes_with_signature(ALGO_TEXT)
    n = 0
    for f in store.active_files():
        if f["hash"] in have or not _is_text(f["rel"], f["mime"]):
            continue
        p = Path(f["abspath"])
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:TEXT_READ_BYTES]
        except OSError:
            continue
        store.upsert_signature(f["hash"], ALGO_TEXT, simhash.to_hex(simhash.simhash(text, k=4)))
        n += 1
    return n


def compute_image_signatures(store: Store, force_dims_only: bool = False) -> int:
    """dHash every active image lacking a signature; store the dHash + (w,h).

    Returns # computed (0 if no Pillow). force_dims_only also re-reads images
    whose signature exists but has no stored dimensions (backfill for DBs signed
    before dimensions were tracked). Idempotent + resumable: each image commits
    independently, so an interrupted run resumes by re-invocation.
    """
    if not phash.PIL_AVAILABLE:
        return 0
    have = store.hashes_with_signature(ALGO_IMAGE)
    need_dims = store.image_hashes_missing_dims() if force_dims_only else set()
    n = 0
    for f in store.active_files():
        h = f["hash"]
        if not _is_image(f["rel"], f["mime"]):
            continue
        if h in have and not (force_dims_only and h in need_dims):
            continue
        try:
            sig, w, ht = phash.dhash_with_dims(Path(f["abspath"]))
        except Exception:
            continue
        store.upsert_signature(h, ALGO_IMAGE, phash.to_hex(sig))
        store.upsert_image_dims(h, w, ht)
        n += 1
    return n


# ---------- clustering ----------
def _components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[set[str]]:
    par = {n: n for n in nodes}

    def find(x: str) -> str:
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for a, b in edges:
        par[find(a)] = find(b)
    groups: dict[str, set] = {}
    for n in par:
        groups.setdefault(find(n), set()).add(n)
    return [g for g in groups.values() if len(g) >= 2]


def materialize_doc_versions(store: Store, threshold: int = DOC_VERSION_THRESHOLD) -> int:
    """Build doc_version clusters from stored text signatures. Idempotent.

    Candidate pairs are restricted to files sharing a parent directory OR a
    normalized filename stem; linked if SimHash Hamming <= threshold;
    connected components of size >= 2 become clusters.
    """
    sigs_hex = store.get_signatures(ALGO_TEXT)
    if len(sigs_hex) < 2:
        return 0
    sigs = {h: simhash.from_hex(v) for h, v in sigs_hex.items()}

    # Bucket by structural key over ALL active paths (a hash with copies in
    # several folders becomes a candidate in each). Sets dedup repeats.
    by_dir: dict[str, set[str]] = {}
    by_stem: dict[str, set[str]] = {}
    for ap in store.active_paths():
        h = ap["hash"]
        if h not in sigs:
            continue
        rel = ap["rel"]
        by_dir.setdefault(str(Path(rel).parent), set()).add(h)
        by_stem.setdefault(_norm_stem(rel), set()).add(h)

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in itertools.chain(by_dir.values(), by_stem.values()):
        for a, b in itertools.combinations(sorted(group), 2):
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            if simhash.hamming(sigs[a], sigs[b]) <= threshold:
                edges.append((a, b))

    comps = _components(list(sigs), edges)
    already = store.clustered_hashes("doc_version")
    created = 0
    for comp in comps:
        if comp & already:  # overlaps existing doc_version cluster — skip (idempotent)
            continue
        store.create_cluster("doc_version", sorted(comp), label="document versions")
        created += 1
    return created


def _lsh_candidates(items: dict, bands: int = LSH_BANDS) -> set:
    """Candidate near pairs: those sharing ≥1 identical band. With B bands and a
    distance threshold T < B, any true pair (Hamming ≤ T) shares ≥ B−T identical
    bands, so this is a superset of all true pairs (no misses). Avoids O(n²)."""
    bw = 64 // bands
    mask = (1 << bw) - 1
    buckets: dict = {}
    for h, sig in items.items():
        for bi in range(bands):
            bv = (sig >> (bi * bw)) & mask
            buckets.setdefault((bi, bv), []).append(h)
    cand: set = set()
    for hs in buckets.values():
        if len(hs) < 2 or len(hs) > LSH_BUCKET_CAP:
            continue  # singleton band, or a pathological low-entropy band — skip
        for a, b in itertools.combinations(sorted(hs), 2):
            cand.add((a, b))
    return cand


def materialize_near_images(store: Store, threshold: int = NEAR_IMAGE_THRESHOLD) -> dict:
    """Build near_image clusters from stored perceptual signatures. Idempotent.

    Degenerate dHashes are excluded; candidates are restricted to the same
    aspect-ratio bucket and linked only at Hamming ≤ threshold; components larger
    than MAX_NEAR_CLUSTER are dropped (a clean near-dup set is small). Returns
    {created, dropped_degenerate, dropped_oversize}.
    """
    result = {"created": 0, "dropped_degenerate": 0, "dropped_oversize": 0}
    sigs_hex = store.get_signatures(ALGO_IMAGE)
    if len(sigs_hex) < 2:
        return result
    dims = store.get_image_dims()

    # Parse signatures, dropping degenerate (blank / inverted-uniform) hashes.
    sigs: dict = {}
    for h, v in sigs_hex.items():
        s = phash.from_hex(v)
        pc = bin(s).count("1")
        if pc < phash.POPCOUNT_MIN or pc > phash.POPCOUNT_MAX:
            result["dropped_degenerate"] += 1
            continue
        sigs[h] = s
    if len(sigs) < 2:
        return result

    # Aspect-ratio prior: only same-shape images are clustering candidates.
    by_aspect: dict = {}
    for h, s in sigs.items():
        w, ht = dims.get(h, (None, None))
        by_aspect.setdefault(phash.aspect_bucket(w, ht), {})[h] = s

    already = store.clustered_hashes("near_image")
    for bucket_items in by_aspect.values():
        if len(bucket_items) < 2:
            continue
        edges = [
            (a, b) for a, b in _lsh_candidates(bucket_items)
            if phash.hamming(bucket_items[a], bucket_items[b]) <= threshold
        ]
        for comp in _components(list(bucket_items), edges):
            if len(comp) > MAX_NEAR_CLUSTER:
                result["dropped_oversize"] += 1
                continue
            if comp & already:
                continue
            store.create_cluster("near_image", sorted(comp), label="similar images")
            result["created"] += 1
    return result


def recluster(store: Store, reset_near: bool = False,
              recompute_images: bool = False) -> dict:
    """Full near-dup pass: compute signatures, then materialize near clusters.

    reset_near: delete existing near_image clusters before rebuilding (repair).
    recompute_images: re-read images to backfill dimensions on old signatures.
    """
    removed = store.reset_clusters("near_image") if reset_near else 0
    n_text_sig = compute_text_signatures(store)
    n_img_sig = compute_image_signatures(store, force_dims_only=recompute_images)
    n_doc = materialize_doc_versions(store)
    img = materialize_near_images(store)
    return {
        "text_signatures": n_text_sig,
        "image_signatures": n_img_sig,
        "doc_version_clusters": n_doc,
        "near_image_clusters": img["created"],
        "near_image_removed": removed,
        "dropped_degenerate": img["dropped_degenerate"],
        "dropped_oversize": img["dropped_oversize"],
    }
