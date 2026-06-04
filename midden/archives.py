"""Read-only listing of archive contents (zip / 7z / rar / tar-family).

Lists what's INSIDE an archive without extracting it — for confirming a
duplicate archive's contents when the outer filename is misleading (e.g. the
same pack saved as `nsfw.rar` and `1000 mulheres super stls.rar`).

Design:
- `.zip` uses the Python standard library (`zipfile`) — no dependency, and it
  reads only the central directory, so it's cheap even for multi-GB archives.
- `.7z` / `.rar` (and anything else) shell out to an available CLI lister
  (`7z` / `7za` / `7zr`, else `bsdtar` / `tar`). NO new Python dependency — if no
  tool is installed the result carries a clear message instead of failing.
- Listing never decompresses payload data, so there is no zip-bomb risk, and it
  runs with stdin closed + a timeout so an encrypted/header-locked archive can't
  hang waiting for a password prompt.
"""
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

# Extensions we offer an "inspect" button for. Compound suffixes (.tar.gz) are
# matched by `is_archive` on the full lowercased name.
ARCHIVE_EXTS = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".tbz",
    ".xz", ".txz", ".zst", ".cbz", ".cbr", ".cb7", ".jar", ".war", ".whl",
    ".epub", ".apk", ".iso",
}
_COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

MAX_ENTRIES = 2000   # cap entries returned to the UI; entry_count carries the true total
_LIST_TIMEOUT = 60   # seconds; listing a directory/header is fast even over a NAS


def is_archive(name: str) -> bool:
    low = str(name).lower()
    if low.endswith(_COMPOUND):
        return True
    return Path(low).suffix in ARCHIVE_EXTS


def _result(ok=False, fmt=None, backend=None, entries=None, entry_count=0,
            total_size=0, truncated=False, encrypted=False, error=None) -> dict:
    return {
        "ok": ok, "format": fmt, "backend": backend,
        "entries": entries or [], "entry_count": entry_count,
        "total_size": total_size, "truncated": truncated,
        "encrypted": encrypted, "error": error,
    }


def _fmt_of(name: str) -> str:
    low = str(name).lower()
    for c in _COMPOUND:
        if low.endswith(c):
            return "tar"
    return Path(low).suffix.lstrip(".") or "?"


def _find_cli() -> tuple[str | None, str | None]:
    """(executable_path, kind) for the best available lister, else (None, None)."""
    for name in ("7z", "7za", "7zr"):
        exe = shutil.which(name)
        if exe:
            return exe, "7z"
    for name in ("bsdtar", "tar"):
        exe = shutil.which(name)
        if exe:
            return exe, "tar"
    return None, None


def _cap(entries: list) -> tuple[list, int, int, bool]:
    """Return (capped_entries, true_count, total_uncompressed, truncated)."""
    total = sum(e["size"] for e in entries)
    count = len(entries)
    if count > MAX_ENTRIES:
        return entries[:MAX_ENTRIES], count, total, True
    return entries, count, total, False


def _list_zip_stdlib(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            encrypted = any(i.flag_bits & 0x1 for i in zf.infolist())
            entries = [
                {"name": i.filename, "size": i.file_size, "is_dir": i.is_dir()}
                for i in zf.infolist()
            ]
    except (zipfile.BadZipFile, OSError) as e:
        return _result(error=f"not a readable zip: {e}", fmt="zip")
    capped, count, total, trunc = _cap(entries)
    return _result(ok=True, fmt="zip", backend="zipfile", entries=capped,
                   entry_count=count, total_size=total, truncated=trunc,
                   encrypted=encrypted)


def _parse_7z_slt(text: str) -> list:
    """Parse `7z l -slt` output. Per-entry key=value blocks follow a line of
    dashes; each block with a Path is an entry (Folder=+ or D attribute = dir)."""
    entries: list = []
    in_entries = False
    cur: dict = {}

    def flush():
        if "Path" in cur:
            entries.append({
                "name": cur["Path"],
                "size": int(cur.get("Size") or 0),
                "is_dir": cur.get("Folder") == "+"
                or cur.get("Attributes", "").startswith("D"),
            })

    for line in text.splitlines():
        if not in_entries:
            if line.strip().startswith("----------"):
                in_entries = True
            continue
        if not line.strip():
            flush()
            cur = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            cur[k.strip()] = v.strip()
    flush()
    return entries


def _list_with_7z(exe: str, path: Path, fmt: str) -> dict:
    try:
        proc = subprocess.run(
            [exe, "l", "-slt", "-y", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_LIST_TIMEOUT, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return _result(error="listing timed out", fmt=fmt, backend="7z")
    out = proc.stdout or ""
    blob = (out + "\n" + (proc.stderr or "")).lower()
    if proc.returncode != 0:
        enc = "wrong password" in blob or "encrypted" in blob or "cannot open encrypted" in blob
        return _result(error=(proc.stderr or "7z could not read this archive").strip()[:300],
                       fmt=fmt, backend="7z", encrypted=enc)
    entries = _parse_7z_slt(out)
    capped, count, total, trunc = _cap(entries)
    return _result(ok=True, fmt=fmt, backend="7z", entries=capped,
                   entry_count=count, total_size=total, truncated=trunc,
                   encrypted="encrypted = +" in out.lower())


def _list_with_tar(exe: str, path: Path, fmt: str) -> dict:
    """libarchive (bsdtar/tar) fallback. Names-only (-tf) parses cleanly across
    formats; sizes aren't reported in this mode (shown as unknown in the UI)."""
    try:
        proc = subprocess.run(
            [exe, "-tf", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_LIST_TIMEOUT, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return _result(error="listing timed out", fmt=fmt, backend="tar")
    if proc.returncode != 0:
        return _result(error=(proc.stderr or "could not read this archive").strip()[:300],
                       fmt=fmt, backend="tar")
    names = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    entries = [{"name": n, "size": 0, "is_dir": n.endswith("/")} for n in names]
    capped, count, total, trunc = _cap(entries)
    return _result(ok=True, fmt=fmt, backend="tar", entries=capped,
                   entry_count=count, total_size=0, truncated=trunc)


def list_archive(path: Path) -> dict:
    """List entries of the archive at `path`, read-only. Never extracts."""
    path = Path(path)
    if not path.exists():
        return _result(error="file not found at its recorded location")
    fmt = _fmt_of(path.name)
    # .zip: stdlib first (fast, no subprocess). Fall through to a CLI only if the
    # stdlib reader rejects it (some self-extracting / odd zips parse better in 7z).
    if path.suffix.lower() == ".zip":
        res = _list_zip_stdlib(path)
        if res["ok"]:
            return res
    exe, kind = _find_cli()
    if not exe:
        return _result(
            error="no archive tool available — install 7-Zip (7z) to inspect "
                  ".7z/.rar archives, or this build can read .zip natively",
            fmt=fmt,
        )
    if kind == "7z":
        return _list_with_7z(exe, path, fmt)
    return _list_with_tar(exe, path, fmt)
