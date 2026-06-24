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
