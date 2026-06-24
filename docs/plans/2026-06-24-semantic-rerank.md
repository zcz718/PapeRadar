# Semantic Rerank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recall-first retrieve + agent-precision rerank so off-target papers are pruned against the user's `research_brief` and backfilled to a full top-N.

**Architecture:** `search_arxiv.py` emits a deeper candidate pool (default 25). A new SKILL.md Step 2.7 has the agent label each candidate ON/BORDERLINE/OFF vs the `research_brief`; `rerank_apply.py` applies those verdicts deterministically (ON first, BORDERLINE backfill to N, OFF dropped) and rewrites `top_papers`. The agent does only semantic judgment; all mechanics live in tested code. No agent ⇒ pipeline falls back to the keyword top-N.

**Tech Stack:** Python 3 (stdlib only — argparse/json), pytest. Interpreter: `/Users/chuzhi_zhao/miniforge3/bin/python3` (has deps). Builds on the `fix/scoring-precision` branch.

**Spec:** `docs/specs/2026-06-24-semantic-rerank-design.md`

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `scripts/rerank_apply.py` | Apply verdicts → final list (pure mechanics) | **create** |
| `scripts/search_arxiv.py` | Emit candidate pool; relax gate default | modify |
| `SKILL.md` | New Step 2.7 (agent rerank) | modify |
| `config.example.yaml` | Document the rerank step | modify |
| `tests/test_rerank.py` | Unit tests for `rerank_apply` | **create** |
| `tests/test_scoring.py` | Pool-split test; gate-default updates | modify |

---

## Task 1: `rerank_apply.select()` — the selection rule

**Files:**
- Create: `scripts/rerank_apply.py`
- Test: `tests/test_rerank.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rerank.py`:

```python
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rerank_apply  # noqa: E402


def _pool(*ids):
    return [{"id": i, "title": i} for i in ids]


def test_drops_off_keeps_on_in_order():
    cands = _pool("a", "b", "c")
    v = {"a": "ON", "b": "OFF", "c": "ON"}
    assert [p["id"] for p in rerank_apply.select(cands, v, 10)] == ["a", "c"]


def test_on_before_borderline():
    cands = _pool("a", "b", "c", "d")
    v = {"a": "BORDERLINE", "b": "ON", "c": "OFF", "d": "BORDERLINE"}
    assert [p["id"] for p in rerank_apply.select(cands, v, 10)] == ["b", "a", "d"]


def test_respects_top_n_takes_on_first():
    cands = _pool("a", "b", "c")
    v = {"a": "ON", "b": "ON", "c": "ON"}
    assert [p["id"] for p in rerank_apply.select(cands, v, 2)] == ["a", "b"]


def test_backfill_from_borderline_up_to_n():
    cands = _pool("a", "b", "c")
    v = {"a": "ON", "b": "BORDERLINE", "c": "BORDERLINE"}
    assert [p["id"] for p in rerank_apply.select(cands, v, 2)] == ["a", "b"]


def test_pool_exhaustion_returns_fewer():
    cands = _pool("a", "b")
    v = {"a": "ON", "b": "OFF"}
    assert [p["id"] for p in rerank_apply.select(cands, v, 10)] == ["a"]


def test_missing_verdict_treated_as_borderline():
    cands = _pool("a", "b")
    v = {"a": "ON"}  # b unlabeled
    assert [p["id"] for p in rerank_apply.select(cands, v, 10)] == ["a", "b"]


def test_all_off_returns_empty():
    cands = _pool("a", "b")
    v = {"a": "OFF", "b": "OFF"}
    assert rerank_apply.select(cands, v, 10) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_rerank.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rerank_apply'`

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/rerank_apply.py`:

```python
#!/usr/bin/env python3
"""Apply agent relevance verdicts to a paperadar candidate pool.

Mechanics only — the semantic judgment is the agent's (SKILL.md Step 2.7). This
module applies it deterministically (ON first in rank order, BORDERLINE backfill
to top-n, OFF dropped) so the selection is testable and cannot miscount.
"""
from __future__ import annotations

VALID_VERDICTS = {"ON", "BORDERLINE", "OFF"}


def select(candidates, verdicts, top_n):
    """Final paper list from a ranked candidate pool + verdicts.

    candidates: paper dicts, ranked best-first, each with an 'id'.
    verdicts:   {paper_id: "ON"|"BORDERLINE"|"OFF"} (case-insensitive). A
                candidate with no verdict is BORDERLINE (eligible for backfill,
                never prioritized); unknown ids are ignored. OFF is dropped.
    top_n:      max papers to return (returns fewer if the pool is thin).
    """
    def verdict_of(paper):
        return str(verdicts.get(paper.get("id"), "BORDERLINE")).strip().upper()
    on = [c for c in candidates if verdict_of(c) == "ON"]
    borderline = [c for c in candidates if verdict_of(c) == "BORDERLINE"]
    return (on + borderline)[:top_n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_rerank.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/rerank_apply.py tests/test_rerank.py
git commit -m "feat(rerank): add verdict selection rule (ON-first, BORDERLINE backfill)"
```

---

## Task 2: `rerank_apply` CLI — validation + I/O

**Files:**
- Modify: `scripts/rerank_apply.py`
- Test: `tests/test_rerank.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_rerank.py`)

```python
import json
import pytest


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_normalize_rejects_bad_verdict():
    with pytest.raises(ValueError):
        rerank_apply._normalize_verdicts({"a": "MAYBE"})


def test_normalize_accepts_dict_and_scalar_forms():
    out = rerank_apply._normalize_verdicts({"a": "on", "b": {"verdict": "off", "reason": "x"}})
    assert out == {"a": "ON", "b": "OFF"}


def test_main_rewrites_top_papers_and_drops_candidates(tmp_path):
    inp = _write(tmp_path, "in.json", {
        "candidates": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "top_papers": [{"id": "a", "title": "A"}],
    })
    ver = _write(tmp_path, "v.json", {"a": "OFF", "b": "ON"})
    rc = rerank_apply.main(["--input", inp, "--verdicts", ver, "--top-n", "10"])
    assert rc == 0
    data = json.loads(open(inp, encoding="utf-8").read())
    assert [p["id"] for p in data["top_papers"]] == ["b"]
    assert "candidates" not in data  # discarded means discarded


def test_main_errors_when_no_candidates(tmp_path):
    inp = _write(tmp_path, "in.json", {"top_papers": []})
    ver = _write(tmp_path, "v.json", {"a": "ON"})
    assert rerank_apply.main(["--input", inp, "--verdicts", ver]) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_rerank.py -q`
Expected: FAIL — `AttributeError: module 'rerank_apply' has no attribute '_normalize_verdicts'`

- [ ] **Step 3: Implement** (append to `scripts/rerank_apply.py`)

```python
import argparse
import json
import os
import sys


def _normalize_verdicts(raw):
    """Coerce a verdict file into {id: VERDICT}; raise ValueError on bad values."""
    out = {}
    for pid, val in (raw or {}).items():
        v = val.get("verdict") if isinstance(val, dict) else val
        v = str(v).strip().upper()
        if v not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict {v!r} for id {pid!r}; "
                             f"expected one of {sorted(VALID_VERDICTS)}")
        out[pid] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply rerank verdicts to a candidate pool.")
    ap.add_argument("--input", required=True, help="arxiv_filtered.json with a 'candidates' array")
    ap.add_argument("--verdicts", required=True, help="JSON {id: ON|BORDERLINE|OFF}")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--output", default=None, help="defaults to --input (in place)")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        print("ERROR: input has no 'candidates' array; re-run search_arxiv.py.", file=sys.stderr)
        return 1
    try:
        with open(args.verdicts, encoding="utf-8") as f:
            verdicts = _normalize_verdicts(json.load(f))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: bad verdicts file: {e}", file=sys.stderr)
        return 1

    final = select(candidates, verdicts, args.top_n)
    data["top_papers"] = final
    data.pop("candidates", None)  # discarded means discarded — don't persist the pool

    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from _atomic import atomic_write_json
    atomic_write_json(data, args.output or args.input)

    print(f"rerank: reviewed {len(candidates)}, kept {len(final)}", file=sys.stderr)
    if not final:
        print("rerank: WARNING all candidates judged OFF — empty list", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_rerank.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/rerank_apply.py tests/test_rerank.py
git commit -m "feat(rerank): add CLI — validate verdicts, rewrite top_papers, drop pool"
```

---

## Task 3: `search_arxiv.py` — emit the candidate pool

**Files:**
- Modify: `scripts/search_arxiv.py` (arg at `:985`, assembly at `:1311-1345`)
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_scoring.py`)

```python
def test_split_pool_candidates_and_topn_fallback():
    papers = [{"id": str(i)} for i in range(30)]
    cands, top = search_arxiv._split_pool(papers, pool_size=25, top_n=10)
    assert len(cands) == 25
    assert [p["id"] for p in top] == [str(i) for i in range(10)]
    assert top == cands[:10]  # fallback is the head of the pool


def test_split_pool_pool_size_floored_to_top_n():
    papers = [{"id": str(i)} for i in range(30)]
    cands, top = search_arxiv._split_pool(papers, pool_size=5, top_n=10)
    assert len(cands) == 10  # pool can't be smaller than top_n
    assert len(top) == 10
```

- [ ] **Step 2: Run to verify failure**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_scoring.py::test_split_pool_candidates_and_topn_fallback -q`
Expected: FAIL — `AttributeError: module 'search_arxiv' has no attribute '_split_pool'`

- [ ] **Step 3a: Add the helper.** In `scripts/search_arxiv.py`, immediately after the `_derive_arxiv_categories_from_config` function (added in `fix/scoring-precision`), insert:

```python
def _split_pool(unique_papers, pool_size, top_n):
    """Candidate pool for reranking + the keyword-only fallback list.

    `candidates` = top `pool_size` (>= top_n) papers, ranked. `top_papers` =
    first `top_n` of the pool — used directly when no rerank step runs.
    """
    size = max(pool_size, top_n)
    candidates = unique_papers[:size]
    return candidates, candidates[:top_n]
```

- [ ] **Step 3b: Add the CLI flag.** In `main()`, after the `--top-n` argument (`scripts/search_arxiv.py:981-982`), add:

```python
    parser.add_argument('--pool-size', type=int, default=25,
                        help='Candidate pool size emitted for the rerank step '
                             '(>= top-n). Default 25.')
```

- [ ] **Step 3c: Use the helper.** Replace the block at `scripts/search_arxiv.py:1311-1317`:

```python
    # Take the top-N papers
    top_papers = unique_papers[:args.top_n]

    # Add note_filename to each paper, matching generate_note.py's naming rules,
    # so paperadar wikilinks can use this field directly without re-deriving it.
    for paper in top_papers:
        paper['note_filename'] = title_to_note_filename(paper.get('title', ''))
```

with:

```python
    # Candidate pool (for the rerank step) + keyword-only fallback top-N.
    candidates, top_papers = _split_pool(unique_papers, args.pool_size, args.top_n)

    # Add note_filename to every candidate (the rerank step may promote any of
    # them into top_papers), matching generate_note.py's naming rules.
    for paper in candidates:
        paper['note_filename'] = title_to_note_filename(paper.get('title', ''))
```

- [ ] **Step 3d: Add `candidates` to the output dict.** In the `output = {...}` dict (`scripts/search_arxiv.py:1343-1345`), change:

```python
        'total_unique': len(unique_papers),
        'top_papers': top_papers
    }
```

to:

```python
        'total_unique': len(unique_papers),
        'candidates': candidates,
        'top_papers': top_papers
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_scoring.py -q`
Expected: PASS (all, including the 2 new split-pool tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_arxiv.py tests/test_scoring.py
git commit -m "feat(search): emit candidate pool (--pool-size) for the rerank step"
```

---

## Task 4: Relax the gate default to recall-first

**Files:**
- Modify: `scripts/search_arxiv.py` (`_resolve_scoring`, the `min_keyword_matches` default)
- Test: `tests/test_scoring.py`

**Why:** With the agent rerank as the precision layer, the keyword gate should
maximize recall. The strict `>=2`/compound rule from `fix/scoring-precision`
becomes opt-in (`scoring.min_keyword_matches: 2`); the default reverts to 1.

- [ ] **Step 1: Update the strict-gate tests to pin the strict config, and add a default-admits test.** In `tests/test_scoring.py`, the strict-gate tests must now pass `scoring.min_keyword_matches: 2` explicitly (the default no longer rejects). Replace `test_gate_rejects_single_ambiguous_word`, `test_gate_rejects_single_bare_token`, `test_regression_sensor_localization_math_rejected` so each builds its config with `"scoring": {"min_keyword_matches": 2}`, e.g.:

```python
def _cfg_strict(domains, excluded=None):
    return {"research_domains": domains, "excluded_keywords": excluded or [],
            "scoring": {"min_keyword_matches": 2}}


def test_gate_rejects_single_ambiguous_word():
    cfg = _cfg_strict({"AI": {"keywords": ["alignment", "RLHF", "reward model"], "arxiv_categories": []}})
    assert _score([_paper("InterAligner: ASR encoder alignment objectives",
                          "We study CTC alignment for speech recognition.")], cfg) == []


def test_gate_rejects_single_bare_token():
    cfg = _cfg_strict({"Bio": {"keywords": ["transposable element", "Nanopore"], "arxiv_categories": []}})
    assert _score([_paper("Nanopore current signal denoising",
                          "A deep model for ionic current.")], cfg) == []


def test_regression_sensor_localization_math_rejected():
    cfg = _cfg_strict({"Hum": {"keywords": ["localization", "humanitarian", "refugee", "forced displacement"],
                              "arxiv_categories": []}})
    assert _score([_paper("Sensor network localization has a benign landscape",
                          "Low-dimensional relaxation for sensor localization optimization.")], cfg) == []


def test_default_gate_admits_single_keyword():
    # Recall-first default (min_keyword_matches=1): a single bare-token match is kept.
    cfg = _cfg({"Bio": {"keywords": ["Nanopore"], "arxiv_categories": []}})
    assert len(_score([_paper("Nanopore current signal denoising", "ionic current model.")], cfg)) == 1


def test_resolve_scoring_default_min_keyword_matches_is_one():
    assert search_arxiv._resolve_scoring({})["min_keyword_matches"] == 1
```

- [ ] **Step 2: Run to verify the new/updated tests fail**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_scoring.py -q`
Expected: FAIL — `test_resolve_scoring_default_min_keyword_matches_is_one` and `test_default_gate_admits_single_keyword` fail (default is still 2).

- [ ] **Step 3: Flip the default.** In `scripts/search_arxiv.py`, in `_resolve_scoring`'s return dict, change:

```python
        'min_keyword_matches': max(1, int(_num('min_keyword_matches', 2))),
```

to:

```python
        'min_keyword_matches': max(1, int(_num('min_keyword_matches', 1))),
```

- [ ] **Step 4: Run to verify pass**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/test_scoring.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_arxiv.py tests/test_scoring.py
git commit -m "feat(scoring): default to recall-first gate (min_keyword_matches=1); strict is opt-in"
```

---

## Task 5: SKILL.md Step 2.7 + config docs

**Files:**
- Modify: `SKILL.md` (insert Step 2.7 between the Step 2 sanity guard and Step 3)
- Modify: `config.example.yaml` (document the rerank step)

- [ ] **Step 1: Insert Step 2.7 into `SKILL.md`** immediately before `# Step 3 — Materialize weekly knowledge notes`:

````markdown
# Step 2.7 — Relevance rerank (semantic precision pass)

Run this AFTER the Step 2 sanity guard and BEFORE Step 3. `search_arxiv.py`
emits a deeper `candidates` pool (default 25) in `arxiv_filtered.json`; the
keyword score provides recall, and you (the agent) provide precision by judging
each candidate against the user's `research_brief`.

1. Read `research_brief` from the config and the `candidates` array from
   `arxiv_filtered.json` (each candidate has `id`, `title`, `abstract`,
   `matched_domain`).
2. For EVERY candidate, decide its relevance to the brief:
   - `ON` — directly about the brief's topics, methods, or questions.
   - `BORDERLINE` — adjacent or plausibly useful.
   - `OFF` — only superficially keyword-matched; wrong domain/topic. Be strict:
     a single generic-phrase hit ("early warning system", "simulation") in an
     unrelated field is `OFF`.
3. Write the verdicts to `$OUTDIR/verdicts.json` as
   `{ "<paper id>": "ON" | "BORDERLINE" | "OFF", ... }`.
4. Apply them:

```bash
cd "$SKILL_DIR"
"$PY" scripts/rerank_apply.py \
  --input arxiv_filtered.json \
  --verdicts "$OUTDIR/verdicts.json" \
  --top-n 10
```

This rewrites `top_papers` to the cleaned, backfilled list (ON first, then
BORDERLINE up to 10; OFF dropped). Dropped papers are gone — they are NOT listed
in the weekly note.

**No agent available (headless run):** skip this step. `top_papers` already
holds the keyword top-N, so Step 3 still works (at the keyword layer's
precision). Note: agent judgment is not bit-reproducible week to week — expected
for a human-reviewed recommender.
````

- [ ] **Step 2: Document in `config.example.yaml`.** After the `# scoring:` block, add:

```yaml
# ─────────────────────────────────────────────────────────────────────────
# Relevance rerank (agent precision pass)
# ─────────────────────────────────────────────────────────────────────────
# search_arxiv.py emits a candidate pool (CLI: --pool-size, default 25). When an
# agent runs the skill, SKILL.md Step 2.7 has it judge each candidate against
# `research_brief` and keep the on-target ones (off-target are dropped). The
# keyword gate is recall-first by default (scoring.min_keyword_matches: 1); set
# it to 2 for a stricter deterministic filter on headless/no-agent runs.
```

- [ ] **Step 3: Manual verification**

Run a dry render against a hand-made pool to confirm wiring (no network):

```bash
cd /Users/chuzhi_zhao/research/08_code/github_repos/paperadar
TMP=$(mktemp -d)
printf '%s' '{"candidates":[{"id":"x1","title":"On topic","abstract":"a"},{"id":"x2","title":"Off topic","abstract":"b"}],"top_papers":[]}' > "$TMP/af.json"
printf '%s' '{"x1":"ON","x2":"OFF"}' > "$TMP/v.json"
/Users/chuzhi_zhao/miniforge3/bin/python3 scripts/rerank_apply.py --input "$TMP/af.json" --verdicts "$TMP/v.json" --top-n 10
python3 -c "import json;d=json.load(open('$TMP/af.json'));print('top:',[p['id'] for p in d['top_papers']]);print('candidates_dropped:', 'candidates' not in d)"
```
Expected: `rerank: reviewed 2, kept 1` on stderr; `top: ['x1']`; `candidates_dropped: True`.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md config.example.yaml
git commit -m "docs(skill): add Step 2.7 agent rerank + config notes"
```

---

## Task 6: End-to-end validation

**Files:** none (validation only)

- [ ] **Step 1: Full unit suite green**

Run: `/Users/chuzhi_zhao/miniforge3/bin/python3 -m pytest tests/ -q`
Expected: PASS (all — baseline + scoring + rerank).

- [ ] **Step 2: Re-run the QC harness with the rerank wired in**

The harness (`~/paperadar-qc-sandbox/_run_all.sh`) runs `search_arxiv.py` (now emitting `candidates`) but NOT the agent rerank (it's a script). To validate the rerank end-to-end, run the agent rerank manually on one residual case: take `~/paperadar-qc-sandbox/humanitarian-realistic-full/arxiv_filtered.json`, judge its `candidates` against the humanitarian-forecasting brief, write verdicts, run `rerank_apply.py`, and confirm the "early warning system" PFAS/volcano papers are now absent from `top_papers` while genuine forecasting papers remain.

Expected: the PFAS and volcano papers are dropped; `top_papers` is on-target and full (up to 10) via backfill.

- [ ] **Step 3: Confirm bio is preserved**

Manually rerank `~/paperadar-qc-sandbox/biology/arxiv_filtered.json` candidates against the L1HS/Nanopore brief; confirm the genuinely on-topic TE/long-read/m6A papers survive and generic spatial-transcriptomics items are dropped, with the list backfilled to N.

- [ ] **Step 4: Commit any harness/notes updates (optional)**

```bash
git add -A && git commit -m "test: validate semantic rerank end-to-end on QC residuals"
```

---

## Self-review notes
- **Spec coverage:** retrieve pool (Task 3), relaxed gate (Task 4), agent rerank + rerank_apply (Tasks 1–2, 5), discarded-means-discarded (Task 2 drops `candidates`, no note appendix), fallback (Task 3 `top_papers` head + Task 5 headless note), testing (Tasks 1–4, 6). All covered.
- **Type consistency:** `select(candidates, verdicts, top_n)`, `_normalize_verdicts(raw)`, `_split_pool(unique_papers, pool_size, top_n)`, verdict strings `ON|BORDERLINE|OFF`, JSON keys `candidates`/`top_papers` — used identically across tasks.
- **Commits:** the user's standing rule is "commit only when asked"; the commit steps above run during execution, which the user authorizes separately.
