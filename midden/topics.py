"""Phase-4 topic inference: tag documents by subject, cluster by topic.

Unlike the dedup cluster kinds (exact / doc_version / near_image), which mean
"these are redundant — keep one, purge the rest", a topic cluster means "these
belong to the same project/subject". It is a NON-destructive grouping and lives
in the Projects view, never the keep/purge review queue.

Pipeline (mirrors near.py): read the first chunk of each active text file, infer
a {topic, slug, kind, confidence}, store as tags, then group active hashes by
`topic_slug` into kind='topic' clusters.

Inference backend is pluggable:
- "stub"      deterministic, offline. Uses a document's markdown H1 title as its
              topic (falls back to no-tag). For tests and as a no-model fallback.
              Tags are stored with source='rule'.
- "ollama"    local model at http://localhost:11434, schema-constrained JSON. The
              privacy-preserving default — file contents never leave the machine.
- "anthropic" Claude via the API (opt-in). Sends first-chunk text to the cloud;
              requires ANTHROPIC_API_KEY and the `anthropic` package.

Signature/clustering split matches the rest of the codebase: this module reads
files and decides clusters; store.py owns schema/access.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

from . import near
from .store import Store

TOPIC_READ_BYTES = 8 * 1024            # first chunk is plenty for a subject guess
DEFAULT_OLLAMA_BASE = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:latest"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


def ollama_base(base_url: Optional[str] = None) -> str:
    """Resolve the Ollama base URL: explicit arg > $OLLAMA_HOST > localhost.

    Lets Midden run on one host and reach Ollama on another (the user's
    separate-server setup). $OLLAMA_HOST is Ollama's own convention; we accept a
    bare host[:port] and add the scheme/default port so `export OLLAMA_HOST=
    192.168.1.5` works as well as a full URL.
    """
    raw = (base_url or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_BASE).strip()
    if "://" not in raw:
        raw = "http://" + raw
    raw = raw.rstrip("/")
    # bare host with no port -> Ollama's default 11434
    tail = raw.split("://", 1)[1]
    if ":" not in tail:
        raw = raw + ":11434"
    return raw


def _ollama_chat(base_url: Optional[str] = None) -> str:
    return ollama_base(base_url) + "/api/chat"


def _ollama_tags(base_url: Optional[str] = None) -> str:
    return ollama_base(base_url) + "/api/tags"

# Schema the local/cloud models are constrained to (Ollama `format`, Anthropic
# tool input). Matches tools/bench_ollama.py so the bench stays representative.
TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},     # 1-3 word human subject
        "slug": {"type": "string"},      # kebab-case tag
        "kind": {"type": "string", "enum": ["document", "note", "receipt", "code", "other"]},
        "confidence": {"type": "number"},
    },
    "required": ["topic", "slug", "kind", "confidence"],
}

SYSTEM = (
    "You categorize a file by its content for a personal-archive organizer. "
    "Return ONLY the JSON object. 'topic' is a 1-3 word human-readable subject. "
    "'slug' is a kebab-case tag (lowercase, hyphens). Be concise and literal — "
    "do not invent details not present in the text."
)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "untitled"


# ---------- backends ----------
def _infer_stub(text: str, rel: str) -> Optional[dict]:
    """Deterministic, offline. A document's markdown H1 is its topic.

    Headerless files (e.g. scattered notes) get no tag — that deliberately keeps
    noise out of topic clusters, the same precision concern near.py documents for
    SimHash over a shared vocabulary.
    """
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    m = re.match(r"#+\s+(.{2,60})", first.strip())
    if not m:
        return None
    title = m.group(1).strip().rstrip("#").strip()
    return {"topic": title, "slug": _slugify(title), "kind": "document", "confidence": 0.9}


def _infer_ollama(text: str, model: str, base_url: Optional[str] = None) -> Optional[dict]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Categorize this file:\n\n{text}"},
        ],
        "stream": False,
        "format": TOPIC_SCHEMA,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        _ollama_chat(base_url), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read())
    content = out.get("message", {}).get("content", "")
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return None
    return _coerce(obj)


def _infer_anthropic(text: str, model: str) -> Optional[dict]:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic package not installed — `pip install anthropic` or use "
            "--backend ollama/stub"
        ) from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()
    # Force structured output via a single tool the model must call.
    tool = {
        "name": "categorize",
        "description": "Record the file's topic categorization.",
        "input_schema": TOPIC_SCHEMA,
    }
    msg = client.messages.create(
        model=model,
        max_tokens=256,
        system=SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "categorize"},
        messages=[{"role": "user", "content": f"Categorize this file:\n\n{text}"}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            return _coerce(block.input)
    return None


def _coerce(obj: dict) -> Optional[dict]:
    """Validate/normalize a model's JSON into our tag shape, or None if unusable."""
    topic = str(obj.get("topic", "")).strip()
    if not topic:
        return None
    slug = _slugify(str(obj.get("slug") or topic))
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    kind = obj.get("kind") if obj.get("kind") in {"document", "note", "receipt", "code", "other"} else "other"
    return {"topic": topic, "slug": slug, "kind": kind, "confidence": conf}


def infer(text: str, rel: str, *, backend: str, model: Optional[str] = None,
          ollama_url: Optional[str] = None) -> Optional[dict]:
    if backend == "stub":
        return _infer_stub(text, rel)
    if backend == "ollama":
        return _infer_ollama(text, model or DEFAULT_OLLAMA_MODEL, ollama_url)
    if backend == "anthropic":
        return _infer_anthropic(text, model or DEFAULT_ANTHROPIC_MODEL)
    raise ValueError(f"unknown backend: {backend}")


# ---------- availability (for backend auto-select + UI hints) ----------
def ollama_available(timeout: float = 2.0,
                     base_url: Optional[str] = None) -> tuple[bool, list[str]]:
    try:
        with urllib.request.urlopen(_ollama_tags(base_url), timeout=timeout) as r:
            data = json.loads(r.read())
        return True, [m.get("name", "") for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False, []


def anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def resolve_backend(requested: str = "auto",
                    ollama_url: Optional[str] = None) -> str:
    """Map 'auto' to the best available backend (ollama > stub). Explicit wins."""
    if requested != "auto":
        return requested
    if ollama_available(base_url=ollama_url)[0]:
        return "ollama"
    return "stub"


# ---------- inference pass (reads files) ----------
def compute_topics(store: Store, *, backend: str, model: Optional[str] = None,
                   limit: Optional[int] = None,
                   ollama_url: Optional[str] = None) -> Iterator[dict]:
    """Tag active, untagged text files with an inferred topic. Yields events.

    Idempotent: files that already carry a `topic_slug` tag are skipped, so a
    re-run only tags newly-ingested documents.
    """
    already = store.hashes_with_tag("topic_slug")
    targets = [
        f for f in store.active_files()
        if f["hash"] not in already and near._is_text(f["rel"], f["mime"])
    ]
    if limit is not None:
        targets = targets[:limit]
    source = "rule" if backend == "stub" else "llm"
    yield {"kind": "started", "total": len(targets), "backend": backend}

    tagged = skipped = errors = 0
    for i, f in enumerate(targets, 1):
        try:
            text = Path(f["abspath"]).read_text(encoding="utf-8", errors="ignore")[:TOPIC_READ_BYTES]
        except OSError as e:
            errors += 1
            yield {"kind": "error", "path": f["rel"], "error": str(e)}
            continue
        try:
            result = infer(text, f["rel"], backend=backend, model=model,
                           ollama_url=ollama_url)
        except Exception as e:  # noqa: BLE001 — model/transport failure, surface it
            errors += 1
            yield {"kind": "error", "path": f["rel"], "error": str(e)}
            continue
        if not result:
            skipped += 1
        else:
            conf = result["confidence"]
            store.upsert_tag(f["hash"], "topic", result["topic"], source, conf)
            store.upsert_tag(f["hash"], "topic_slug", result["slug"], source, conf)
            store.upsert_tag(f["hash"], "topic_file_kind", result["kind"], source, conf)
            tagged += 1
        yield {
            "kind": "progress", "i": i, "total": len(targets),
            "tagged": tagged, "skipped": skipped, "errors": errors,
            "path": f["rel"], "topic": result["topic"] if result else None,
        }
    yield {"kind": "tagged_done", "tagged": tagged, "skipped": skipped, "errors": errors}


def materialize_topics(store: Store, min_size: int = 2) -> int:
    """Build kind='topic' clusters from topic_slug tags. Idempotent.

    A topic needs >= min_size active files to be a cluster (a lone tagged file is
    not a project). Groups overlapping an existing topic cluster are skipped.
    """
    already = store.clustered_hashes("topic")
    created = 0
    for g in store.topic_groups(min_size=min_size):
        if set(g["hashes"]) & already:
            continue
        store.create_cluster("topic", g["hashes"], label=g["label"])
        created += 1
    return created


def run_topics(store: Store, *, backend: str = "stub", model: Optional[str] = None,
               limit: Optional[int] = None, ollama_url: Optional[str] = None) -> dict:
    """Drain compute_topics + materialize. Convenience for the CLI/tests."""
    tagged = skipped = errors = 0
    for ev in compute_topics(store, backend=backend, model=model, limit=limit,
                             ollama_url=ollama_url):
        if ev["kind"] == "tagged_done":
            tagged, skipped, errors = ev["tagged"], ev["skipped"], ev["errors"]
    n_clusters = materialize_topics(store)
    return {"tagged": tagged, "skipped": skipped, "errors": errors,
            "topic_clusters": n_clusters}
