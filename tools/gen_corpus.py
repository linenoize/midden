"""
gen_corpus.py — generate a synthetic messy "inherited drive" for testing Midden.

Produces a directory tree with:
- exact duplicates (same content in multiple locations)
- near-duplicate document versions (manuscript_v1, v2, final, FINAL_revised)
- image "duplicates" at multiple sizes/formats
- scattered project files (a kitchen reno, a wedding, a manuscript)
- junk (zero-byte files, installer remnants, browser-cache-like garbage)
- mixed dates via mtime so date triangulation has something to chew on

Also writes a ground_truth.json so tests can verify clustering.

Usage:
    python gen_corpus.py /path/to/output_root [--seed 42] [--scale 1.0]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PIL import Image, ImageDraw
    _PIL = True
except ImportError:  # adversarial image cases need Pillow; degrade gracefully
    _PIL = False

# ---------- content generators ----------

LOREM = (
    "The kitchen renovation began in earnest after the second leak. "
    "We pulled the cabinets, found the rot, and the contractor said it "
    "would be another three weeks. Joseph kept a running log of receipts. "
).split()

def paragraph(rng: random.Random, n_sentences=5) -> str:
    out = []
    for _ in range(n_sentences):
        length = rng.randint(8, 18)
        words = rng.choices(LOREM, k=length)
        words[0] = words[0].capitalize()
        out.append(" ".join(words) + ".")
    return " ".join(out)

def document(rng: random.Random, topic: str, n_paragraphs=6, seed_extra="") -> str:
    header = f"# {topic}\n\nDraft notes. {seed_extra}\n\n"
    body = "\n\n".join(paragraph(rng) for _ in range(n_paragraphs))
    return header + body + "\n"

def fake_image_bytes(rng: random.Random, w: int, h: int) -> bytes:
    # not a real image — just deterministic-ish bytes of a chosen size.
    # good enough for hashing/dedup tests; not for perceptual hash tests.
    size = w * h * 3
    return bytes(rng.getrandbits(8) for _ in range(size))

# ---------- helpers ----------

def write(path: Path, data: bytes, mtime: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))

def write_text(path: Path, text: str, mtime: datetime | None = None) -> None:
    write(path, text.encode("utf-8"), mtime)

def jitter(text: str, rng: random.Random, edits: int) -> str:
    """Make `edits` small changes to text — for near-dup documents."""
    chars = list(text)
    for _ in range(edits):
        i = rng.randint(0, len(chars) - 1)
        op = rng.choice(["insert", "swap", "delete"])
        if op == "insert":
            chars.insert(i, rng.choice("abcdefg "))
        elif op == "swap" and i + 1 < len(chars):
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
        elif op == "delete":
            chars.pop(i)
    return "".join(chars)

# ---------- adversarial cases (Phase 6 regression: the things that broke on
# real data — blank images, near-dup chains, renamed-identical archives) ----------

def _gen_adversarial(root: Path, rng: random.Random, scale: float,
                     truth: dict, rel) -> None:
    # Renamed-identical exact dup: ~1.5 MiB identical bytes, two DIFFERENT
    # names/dirs. Content-addressing must catch it; rationale must flag "names
    # differ". This is the real-world '75059ffe' four-RAR case in miniature.
    big = bytes(rng.getrandbits(8) for _ in range(1_500_000))
    a = root / "archives" / "pack_alpha.bin"
    b = root / "backups" / "renamed" / "pack_beta.bin"
    write(a, big)
    write(b, big)
    truth["renamed_identical"] = [rel(a), rel(b)]
    truth["exact_duplicate_groups"].append([rel(a), rel(b)])

    if not _PIL:
        truth["pil_available"] = False
        return
    truth["pil_available"] = True

    # Blank / uniform images -> degenerate (popcount 0) dHash. Under the OLD
    # single-linkage clustering these all collapsed into the mega-blob; they MUST
    # now be excluded entirely.
    for i in range(int(24 * scale)):
        shade = (i * 9) % 256
        p = root / "images" / "blanks" / f"blank_{i:02d}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), (shade, shade, shade)).save(p)
        truth["blank_images"].append(rel(p))

    # A genuine near-dup chain: ONE structured square image re-saved at several
    # JPEG qualities — byte-distinct files, near-identical dHash. MUST cluster.
    base = Image.new("RGB", (256, 256), (18, 22, 30))
    d = ImageDraw.Draw(base)
    d.rectangle([40, 40, 210, 200], fill=(210, 170, 60))
    d.ellipse([70, 90, 180, 180], fill=(40, 90, 200))
    d.line([0, 0, 256, 256], fill=(250, 250, 250), width=6)
    members = []
    for q in (95, 88, 80, 70):
        p = root / "images" / "near" / f"poster_q{q}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        base.save(p, quality=q)
        members.append(rel(p))
    truth["near_image_chain"]["members"] = members

    # An unrelated WIDE image (different aspect) — must NOT join the square chain.
    wide = Image.new("RGB", (640, 200), (8, 8, 8))
    dw = ImageDraw.Draw(wide)
    dw.rectangle([20, 20, 300, 180], fill=(200, 60, 60))
    dw.ellipse([350, 30, 600, 170], fill=(60, 200, 120))
    wp = root / "images" / "near" / "banner_unrelated.jpg"
    wide.save(wp)
    truth["near_image_chain"]["unrelated"] = rel(wp)


# ---------- the messy tree ----------

def build(root: Path, seed: int, scale: float, adversarial: bool = False) -> dict:
    rng = random.Random(seed)
    truth = {
        "exact_duplicate_groups": [],   # list of lists of relative paths
        "doc_version_chains": [],       # list of {canonical, others[]}
        "projects": {},                 # project_name -> list of paths
        "junk_paths": [],
        "near_image_groups": [],
        # adversarial (Phase 6) — populated only when adversarial=True
        "blank_images": [],
        "near_image_chain": {"members": [], "unrelated": None},
        "renamed_identical": [],
    }

    def rel(p: Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    # --- Project 1: Kitchen renovation, scattered across two folders ---
    proj_paths = []
    base_doc = document(rng, "Kitchen Renovation Notes", 8)
    # canonical in Projects/kitchen
    p1 = root / "Documents" / "Projects" / "kitchen_reno" / "notes.md"
    write_text(p1, base_doc, datetime(2018, 4, 12))
    proj_paths.append(rel(p1))

    # version chain — v1, v2, final, FINAL_revised
    v1 = jitter(base_doc, rng, 5)
    v2 = jitter(v1, rng, 15)
    v_final = jitter(v2, rng, 8)
    v_final_rev = jitter(v_final, rng, 3)
    chain = []
    for name, content, mt in [
        ("notes_v1.md",          v1,         datetime(2018, 4, 14)),
        ("notes_v2.md",          v2,         datetime(2018, 5, 2)),
        ("notes_final.md",       v_final,    datetime(2018, 5, 20)),
        ("notes_FINAL_revised.md", v_final_rev, datetime(2018, 6, 1)),
    ]:
        pp = root / "Desktop" / "kitchen stuff" / name
        write_text(pp, content, mt)
        chain.append(rel(pp))
        proj_paths.append(rel(pp))
    truth["doc_version_chains"].append({"canonical_guess": chain[-1], "members": chain})

    # receipt PDFs (fake — just bytes labeled .pdf)
    for i in range(int(6 * scale)):
        pp = root / "Documents" / "Projects" / "kitchen_reno" / "receipts" / f"receipt_{i:03d}.pdf"
        write(pp, fake_image_bytes(rng, 10, 10), datetime(2018, 4, 20) + timedelta(days=i))
        proj_paths.append(rel(pp))
    truth["projects"]["kitchen_reno_2018"] = proj_paths

    # --- Project 2: Manuscript, with version chain and stray copy ---
    book_paths = []
    book_v1 = document(rng, "The Long Migration", 12)
    book_v2 = jitter(book_v1, rng, 60)
    book_v3 = jitter(book_v2, rng, 40)

    p_book_root = root / "Documents" / "Writing" / "long_migration"
    write_text(p_book_root / "manuscript_v1.txt", book_v1, datetime(2019, 2, 1))
    write_text(p_book_root / "manuscript_v2.txt", book_v2, datetime(2019, 6, 1))
    write_text(p_book_root / "manuscript_v3.txt", book_v3, datetime(2019, 11, 1))
    chain_book = [
        rel(p_book_root / "manuscript_v1.txt"),
        rel(p_book_root / "manuscript_v2.txt"),
        rel(p_book_root / "manuscript_v3.txt"),
    ]
    truth["doc_version_chains"].append({"canonical_guess": chain_book[-1], "members": chain_book})
    book_paths.extend(chain_book)

    # stray copy of v2 on the desktop (exact dup)
    stray = root / "Desktop" / "manuscript_v2_BACKUP.txt"
    write_text(stray, book_v2, datetime(2019, 7, 15))
    book_paths.append(rel(stray))
    truth["exact_duplicate_groups"].append([
        rel(p_book_root / "manuscript_v2.txt"),
        rel(stray),
    ])
    truth["projects"]["long_migration_book"] = book_paths

    # --- Photo dumps (exact duplicates across folders) ---
    photo_groups = []
    for i in range(int(4 * scale)):
        img_bytes = fake_image_bytes(rng, 40, 30)
        a = root / "Pictures" / "Camera Roll" / "2020" / f"IMG_{1000+i}.jpg"
        b = root / "Pictures" / "2020 photos backup" / f"IMG_{1000+i}.jpg"
        # extra stray copy in Downloads sometimes
        write(a, img_bytes, datetime(2020, 6, 1) + timedelta(days=i))
        write(b, img_bytes, datetime(2020, 7, 1) + timedelta(days=i))
        group = [rel(a), rel(b)]
        if rng.random() < 0.4:
            c = root / "Downloads" / f"IMG_{1000+i}.jpg"
            write(c, img_bytes, datetime(2020, 8, 1))
            group.append(rel(c))
        truth["exact_duplicate_groups"].append(group)
        photo_groups.append(group)

    # --- Junk ---
    for i in range(int(20 * scale)):
        pp = root / "Downloads" / "_installers" / f"setup_{i}.exe"
        write(pp, fake_image_bytes(rng, 1, 1), datetime(2017, 1, 1) + timedelta(days=i))
        truth["junk_paths"].append(rel(pp))
    # zero-byte files
    for i in range(int(8 * scale)):
        pp = root / "AppData" / "Temp" / f"tmp{i}.tmp"
        write(pp, b"", datetime(2015, 1, 1))
        truth["junk_paths"].append(rel(pp))

    # --- Random scattered files (noise) ---
    for i in range(int(30 * scale)):
        depth = rng.randint(1, 4)
        parts = [rng.choice(["stuff", "misc", "old", "tmp", "backup", "new folder"]) for _ in range(depth)]
        pp = root / "Misc" / Path(*parts) / f"file_{i:03d}.txt"
        write_text(pp, paragraph(rng, 3), datetime(2016, 1, 1) + timedelta(days=rng.randint(0, 2000)))

    if adversarial:
        _gen_adversarial(root, rng, scale, truth, rel)

    return truth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="Output directory (will be created/overwritten).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scale", type=float, default=1.0, help="Scale factor for counts.")
    ap.add_argument("--clean", action="store_true", help="Wipe root before generating.")
    ap.add_argument("--adversarial", action="store_true",
                    help="also emit blank images, a near-dup image chain, and a "
                         "renamed-identical archive (Phase 6 regression cases).")
    args = ap.parse_args()

    if args.clean and args.root.exists():
        shutil.rmtree(args.root)
    args.root.mkdir(parents=True, exist_ok=True)

    truth = build(args.root, args.seed, args.scale, adversarial=args.adversarial)
    (args.root / "_ground_truth.json").write_text(json.dumps(truth, indent=2))

    n_files = sum(1 for _ in args.root.rglob("*") if _.is_file())
    print(f"Generated {n_files} files under {args.root}")
    print(f"  exact-dup groups: {len(truth['exact_duplicate_groups'])}")
    print(f"  doc-version chains: {len(truth['doc_version_chains'])}")
    print(f"  projects: {len(truth['projects'])}")
    print(f"  junk paths: {len(truth['junk_paths'])}")
    print(f"  ground truth: {args.root / '_ground_truth.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
