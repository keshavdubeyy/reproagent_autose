# ADR 0007: Knowledge package in SQLite (BM25 + static graph); no vector database yet

Status: accepted (22 Nov 2026)

## Context
The architecture describes a knowledge package with SQLite symbols, a NetworkX graph, Qdrant vectors, BM25,
runtime traces and SLM summaries per function. On one 16 GB laptop with 8 days left, summarising every
function of Django with a local model would take hours per commit, and Qdrant adds a second service.

## Decision
- One SQLite file per (repo_id, commit) holds symbols, CONTAINS/CALLS/INHERITS/IMPORTS edges and an FTS5
  table for BM25 (`autose/intel/index.py`). Calls with more than 3 possible targets stay unresolved.
- Retrieval modes `bm25`, `hybrid` (+ exact names, paths, traceback frames) and `graph` (+ one hop over
  the call graph) are the retrieval side of configurations C and D (`autose/intel/retrieve.py`).
- Vectors (config B), dynamic analysis and SLM summaries are deferred; vectors are added only if
  localisation numbers show lexical + graph retrieval is the bottleneck.

## Consequences
- Index build takes seconds, needs no extra service, and is reused by every bug on the same commit.
- Localisation is measured against the gold patch (`autose/evaluation/localization.py`), so the choice can be
  revisited with data.
