#!/usr/bin/env python3
"""Apply agent relevance verdicts to a paperadar candidate pool.

Mechanics only — the semantic judgment is the agent's (SKILL.md Step 2.7). This
module applies it deterministically (ON first in rank order, BORDERLINE backfill
to top-n, OFF dropped) so the selection is testable and cannot miscount.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

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
