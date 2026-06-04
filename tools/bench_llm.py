"""Bench the LAN LLM models for phase-4 topic inference.

Task: given the first chunk of a document, return a short topic tag + project
guess as JSON. We score on: does it return valid JSON matching the schema, is
the tag sane, and how fast.

Talks to the OpenAI-compatible server (llama.cpp + llama-swap on FIEF) via the
same helpers the app uses — base URL / key resolve from $MIDDEN_LLM_BASE /
$MIDDEN_LLM_API_KEY / the documented key file (see windows-sysadmin/CONNECT.md).
Replaces the former Ollama bench. Read-only, no deps beyond stdlib + midden.

Run:  python tools/bench_llm.py
"""
from __future__ import annotations

import json
import time
import urllib.request

from midden import topics

# Model IDs on the new server (see CONNECT.md). Old Ollama names also resolve.
CANDIDATES = ["llama3.2-3b", "llama3.1-8b", "qwen3.5", "gemma4-e4b", "qwen3-1.7b"]

SYSTEM = topics.SYSTEM  # reuse the app's exact prompt so the bench stays representative

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
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    key = topics.llm_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        topics._llm_chat(), data=json.dumps(body).encode(), headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read())
    except Exception as e:
        return None, time.time() - t0, f"ERROR: {e}"
    dt = time.time() - t0
    choices = out.get("choices") or []
    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
    try:
        return json.loads(content), dt, ""
    except json.JSONDecodeError:
        return None, dt, f"BAD JSON: {content[:120]}"


def main() -> int:
    print(f"endpoint: {topics.llm_base()}")
    ok, models = topics.llm_available()
    if not ok:
        print("  LLM server not reachable — set MIDDEN_LLM_BASE / MIDDEN_LLM_API_KEY")
        return 1
    print(f"  available models: {', '.join(models) or 'none'}")
    print(f"\nbenchmarking {len(CANDIDATES)} models on {len(SAMPLES)} samples\n")
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
