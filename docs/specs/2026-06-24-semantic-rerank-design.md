# Design: Semantic rerank for paperadar weekly recommendations

**Date:** 2026-06-24
**Status:** approved (brainstorming) — pending implementation plan

## Problem

paperadar's keyword scoring builds a recall-oriented candidate list but cannot judge
*semantic* relevance, so the weekly top-N still contains off-target papers. The cross-field
QC and the `fix/scoring-precision` validation found two residual false-positive modes that
keyword logic cannot fix:

1. **Generic multi-word phrases.** A single compound keyword like "early warning system"
   clears the gate but matches cross-domain papers (PFAS contamination, volcano monitoring)
   that are irrelevant to a humanitarian-forecasting reader.
2. **On-config-but-off-core-interest.** Papers that legitimately match a lower-priority
   domain (e.g. generic spatial-transcriptomics for an L1HS/Nanopore biologist) are "on
   config" yet not what the reader actually wants.

The stricter keyword gate from `fix/scoring-precision` reduced mode 1 but also cut volume
(top-6 some weeks) and still leaks generic phrases — keyword math has a hard ceiling here.

## Decision

Adopt **retrieve-then-rerank**: keep a cheap, recall-first keyword retrieve, then add a
semantic rerank performed by the agent that already runs the skill, anchored on the
`research_brief`. The keyword layer maximizes recall; the agent provides precision.

- Run context: paperadar always runs with an agent in the loop (confirmed) — no headless
  judge needed; a no-agent fallback to keyword ranking keeps the pipeline robust.
- Prune behavior: **prune + backfill to N** from a deeper candidate pool.
- Discarded papers are simply dropped — not shown, listed, or counted in the weekly note.
- Division of labor: **the agent makes only semantic judgments; all mechanics
  (apply verdicts, backfill, ordering, count) live in tested code.**

## Architecture

### Pipeline (SKILL.md)
```
Step 2    search_arxiv.py  → CANDIDATE POOL (top-K, default 25) as `candidates`
Step 2.5  sanity guard     → pool non-empty (unchanged)
Step 2.7  RERANK (new)     → agent labels each candidate vs research_brief;
                             rerank_apply.py applies the verdicts → final top_papers
Step 3    materialize      → consumes the agent-selected top_papers (unchanged)
```

### 1. Retrieve layer — `scripts/search_arxiv.py`
- **Relax the gate** to recall-first: `min_keyword_matches` default `2 → 1` (the
  compound/≥N logic stays, just configurable; the agent is now the precision layer).
  The reweight and P3–P6 fixes from `fix/scoring-precision` are unchanged.
- **Deeper pool**: add `--pool-size` (default 25). `arxiv_filtered.json` gains a
  `candidates` array (top-K by keyword score). `top_papers` remains the first `top_n` of
  `candidates`, so a no-agent run is byte-for-byte today's behavior (back-compat +
  fallback).

### 2. Rerank step — new SKILL.md Step 2.7 + `scripts/rerank_apply.py`
- **Agent (semantic judgment only).** Reads `candidates` (id, title, abstract,
  matched_domain) and the config's `research_brief`. Labels each candidate
  **ON / BORDERLINE / OFF** with a one-line reason. Rubric:
  - ON — directly about the brief's topics, methods, or questions.
  - BORDERLINE — adjacent or plausibly useful.
  - OFF — only superficially keyword-matched; wrong domain/topic. Be strict on OFF.
  Writes verdicts to a temp JSON (`{id: {verdict, reason}}`).
- **`rerank_apply.py` (mechanics, deterministic, tested).** Inputs: the candidate pool +
  the verdict JSON + `--top-n`. Produces the final list: ON in keyword-rank order, then
  BORDERLINE to fill up to `top_n`, OFF dropped. If the pool is exhausted it returns fewer
  than `top_n`. Rewrites `top_papers` in `arxiv_filtered.json`. Validates the verdict
  schema and fails loud on malformed input (so the agent can't silently miscount). Drops
  are not persisted or surfaced — discarded means discarded.

### 3. Auditability (minimal)
- One stderr log line: `rerank: reviewed K, kept N` (operational feedback for the run log
  / tests). No "pruned" list, no JSON audit block, nothing about dropped papers in the
  weekly note.
- Reproducibility caveat (documented in SKILL.md): agent judgment is not bit-reproducible
  week to week; acceptable for a human-reviewed recommender.

### 4. Fallback (no-agent / headless)
If Step 2.7 is skipped, `materialize_weekly_notes.py` consumes `top_papers` (= keyword
top-N) exactly as today. The pipeline never breaks; it degrades to current precision.

## Components & interfaces

| Unit | Purpose | Input | Output |
|---|---|---|---|
| `search_arxiv.py` (changed) | recall-first retrieve + pool emission | config, `--pool-size`, `--top-n` | `arxiv_filtered.json` with `candidates` + fallback `top_papers` |
| Agent (SKILL.md Step 2.7) | semantic judgment | `candidates`, `research_brief` | verdict JSON `{id: {verdict, reason}}` |
| `rerank_apply.py` (new) | apply verdicts, backfill, write final list | candidates + verdicts + `--top-n` | rewritten `top_papers`; stderr summary |
| `materialize_weekly_notes.py` (unchanged) | render note | `top_papers` | weekly note |

## Testing
- **`rerank_apply.py` unit tests:** drop-OFF; fill-from-BORDERLINE; ON order preserved;
  respects `top_n`; pool-exhaustion → fewer-than-N; malformed verdicts rejected; a verdict
  for an unknown id ignored; empty/all-OFF pool handled.
- **`search_arxiv.py` unit test:** `candidates` length == `pool_size`; `top_papers` ==
  first `top_n` of `candidates` (fallback intact); `--pool-size` default applied.
- **End-to-end:** re-run the QC harness (`~/paperadar-qc-sandbox/_run_all.sh`) with the
  rerank step; confirm the residual cases (humanitarian "early warning system" PFAS/volcano
  papers) are pruned and the bio top-N stays clean and full.

## Scope / non-goals
- No headless LLM-API or embedding judge (agent-in-loop confirmed).
- No keyword IDF/specificity weighting or generic-phrase stoplist (the rerank supersedes
  that need).
- No change to sources, fetching, Zotero, or rendering beyond consuming the reranked list.
- `min_keyword_matches` stays configurable so a user who wants a pure-deterministic run can
  set it back to 2.

## Files
- `scripts/search_arxiv.py` — pool emission, `--pool-size`, relaxed gate default.
- `scripts/rerank_apply.py` — new.
- `SKILL.md` — new Step 2.7 + reproducibility note.
- `config.example.yaml` — document `pool_size` / rerank behavior.
- `tests/test_rerank.py` — new; `tests/test_scoring.py` — pool-emission test.
