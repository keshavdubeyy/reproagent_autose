"""Run Layer 2 retrieval for SWE-bench Lite instances.

  python -m autose.retrieval --instance pallets__flask-4045 [...]
  python -m autose.retrieval --repo sympy/sympy pallets/flask [--skip-existing]
      -> reads public/bug_reports.json, public/code/<id> and workspaces/<id>/repository_index.json
      -> writes workspaces/<id>/retrieval.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autose.retrieval import Retriever
from autose.retrieval.semantic import Embedder

PUBLIC = Path("benchmarks/swe_bench_lite/public")
WORKSPACES = Path("workspaces")
EMBED_CACHE = WORKSPACES / "_embeddings" / "nomic-embed-text.npz"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", nargs="*")
    ap.add_argument("--repo", nargs="*")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--embed", action="store_true", help="add the embedding ranker (Ollama nomic-embed-text)")
    a = ap.parse_args()
    embedder = Embedder(EMBED_CACHE) if a.embed else None
    reports = {b["instance_id"]: b for b in json.loads((PUBLIC / "bug_reports.json").read_text(encoding="utf-8"))}
    ids = list(a.instance or []) + sorted(i for i, b in reports.items() if a.repo and b["repo"] in a.repo)
    if not ids:
        ap.error("give --instance or --repo")
    for iid in ids:
        out = WORKSPACES / iid / "retrieval.json"
        src = WORKSPACES / iid / "repository_index.json"
        if a.skip_existing and out.exists():
            continue
        if not src.exists():
            print(f"{iid}: no repository_index.json, skipped", flush=True)
            continue
        t = time.time()
        index = json.loads(src.read_text(encoding="utf-8"))
        before = embedder.embedded if embedder else 0
        result = Retriever(index, PUBLIC / "code" / iid, embedder).retrieve(reports[iid]["bug_report"])
        del index
        if embedder:
            embedder.save()
        result = {"instance_id": iid, "seconds": round(time.time() - t, 1),
                  "embedded_new": (embedder.embedded - before) if embedder else 0} | result
        out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        top = [e["id"] for e in result["entities"][:3]]
        print(f"{iid}: {result['seconds']}s new-embeddings {result['embedded_new']} top3 {top}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
