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
