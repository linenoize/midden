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
NEAR_IMAGE_THRESHOLD = 10    # max dHash Hamming distance to link two images
TEXT_READ_BYTES = 256 * 1024

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


def compute_image_signatures(store: Store) -> int:
    """dHash every active image lacking a signature. Returns # computed (0 if no Pillow)."""
    if not phash.PIL_AVAILABLE:
        return 0
    have = store.hashes_with_signature(ALGO_IMAGE)
    n = 0
    for f in store.active_files():
        if f["hash"] in have or not _is_image(f["rel"], f["mime"]):
            continue
        try:
            sig = phash.dhash(Path(f["abspath"]))
        except Exception:
            continue
        store.upsert_signature(f["hash"], ALGO_IMAGE, phash.to_hex(sig))
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


def materialize_near_images(store: Store, threshold: int = NEAR_IMAGE_THRESHOLD) -> int:
    """Build near_image clusters from stored perceptual signatures. Idempotent."""
    sigs_hex = store.get_signatures(ALGO_IMAGE)
    if len(sigs_hex) < 2:
        return 0
    sigs = {h: phash.from_hex(v) for h, v in sigs_hex.items()}
    hashes = list(sigs)
    edges: list[tuple[str, str]] = []
    # image sets are small and there's no reliable structural prior; all-pairs is fine
    for a, b in itertools.combinations(hashes, 2):
        if phash.hamming(sigs[a], sigs[b]) <= threshold:
            edges.append((a, b))
    comps = _components(hashes, edges)
    already = store.clustered_hashes("near_image")
    created = 0
    for comp in comps:
        if comp & already:
            continue
        store.create_cluster("near_image", sorted(comp), label="similar images")
        created += 1
    return created


def recluster(store: Store) -> dict:
    """Full near-dup pass: compute signatures, then materialize near clusters."""
    n_text_sig = compute_text_signatures(store)
    n_img_sig = compute_image_signatures(store)
    n_doc = materialize_doc_versions(store)
    n_img = materialize_near_images(store)
    return {
        "text_signatures": n_text_sig,
        "image_signatures": n_img_sig,
        "doc_version_clusters": n_doc,
        "near_image_clusters": n_img,
    }
