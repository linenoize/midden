"""Bench local Ollama models for phase-4 topic inference.

Task: given the first chunk of a document, return a short topic tag + project
guess as JSON (schema-constrained). We score on: does it return valid JSON
matching the schema, is the tag sane, and how fast.

Uses the synthetic corpus's known doc topics (kitchen reno, the manuscript,
generic notes) as a sanity check. Read-only, no deps beyond stdlib.

Run:  python tools/bench_ollama.py
"""
from __future__ import annotations

import json
import time
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"

CANDIDATES = ["llama3.2:latest", "llama3.1:8b", "qwen3.5:latest", "gemma4:e4b", "qwen3:1.7b"]

SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},          # 1-3 word human topic
        "slug": {"type": "string"},           # kebab-case tag
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

# Representative samples: 2 from the corpus's real topics + 1 ambiguous.
SAMPLES = {
    "kitchen_reno": (
        "# Kitchen Renovation Notes\n\nThe kitchen renovation began in earnest after "
        "the second leak. We pulled the cabinets, found the rot, and the contractor "
        "said it would be another three weeks. Joseph kept a running log of receipts."
    ),
    "manuscript": (
        "# The Long Migration\n\nDraft notes. Chapter one. The geese left the northern "
        "lake before the first frost, and she watched them go from the porch, wondering "
        "whether the book would ever be finished, whether anyone would read it."
    ),
    "tax_doc": (
        "Form 1040 — U.S. Individual Income Tax Return. Filing status: single. "
        "Wages, salaries, tips. Taxable interest. Adjusted gross income. "
        "Withholding from W-2. Refund amount."
    ),
}


def chat(model: str, text: str) -> tuple[dict | None, float, str]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Categorize this file:\n\n{text}"},
        ],
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(OLLAMA, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read())
    except Exception as e:
        return None, time.time() - t0, f"ERROR: {e}"
    dt = time.time() - t0
    content = out.get("message", {}).get("content", "")
    try:
        return json.loads(content), dt, ""
    except json.JSONDecodeError:
        return None, dt, f"BAD JSON: {content[:120]}"


def main() -> int:
    print(f"benchmarking {len(CANDIDATES)} models on {len(SAMPLES)} samples\n")
    results = {}
    for model in CANDIDATES:
        print(f"=== {model} ===")
        oks, times = 0, []
        for name, text in SAMPLES.items():
            obj, dt, err = chat(model, text)
            times.append(dt)
            if obj is None:
                print(f"  {name:<14} {dt:6.1f}s  {err}")
            else:
                oks += 1
                print(f"  {name:<14} {dt:6.1f}s  topic={obj.get('topic')!r:<22} "
                      f"slug={obj.get('slug')!r:<22} kind={obj.get('kind')!r} "
                      f"conf={obj.get('confidence')}")
        avg = sum(times) / len(times)
        results[model] = {"ok": oks, "n": len(SAMPLES), "avg_s": avg}
        print(f"  -> {oks}/{len(SAMPLES)} valid, avg {avg:.1f}s/doc\n")

    print("=== summary (valid JSON / speed) ===")
    for m, r in sorted(results.items(), key=lambda kv: (-kv[1]["ok"], kv[1]["avg_s"])):
        print(f"  {m:<22} {r['ok']}/{r['n']}  {r['avg_s']:.1f}s/doc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
