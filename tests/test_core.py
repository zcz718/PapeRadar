"""Tests for scripts/search_core.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import search_core  # noqa: E402


def _cfg():
    return {"research_domains": {"ML": {"keywords": ["deep learning", "transformer"]}}}


def test_graceful_skip_without_key(monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    with mock.patch.object(search_core, "load_env_from_user_shell"), \
         mock.patch.object(search_core, "_fetch_json") as fetch:
        out = search_core.search_core(_cfg(), days=7)
    assert out == []
    fetch.assert_not_called()


def test_build_query_has_keywords_and_date_window():
    q = search_core._build_query(_cfg(), "2026-06-17", "2026-06-24")
    assert "deep learning" in q
    # CORE wants a bracketed range, not >=/<= operators (which 500 the parser),
    # plus a publication-year constraint so old re-indexed papers are excluded.
    assert "createdDate:[2026-06-17 TO 2026-06-24]" in q
    assert "yearPublished:2026" in q
    assert " OR " in q


def test_build_query_spans_two_years_across_boundary():
    q = search_core._build_query(_cfg(), "2025-12-29", "2026-01-04")
    assert "yearPublished:2025" in q and "yearPublished:2026" in q


def test_build_query_empty_when_no_keywords():
    assert search_core._build_query({"research_domains": {}}, "a", "b") == ""


def test_date_of_variants():
    assert search_core._date_of({"publishedDate": "2026-06-20T00:00:00"}) == "2026-06-20"
    assert search_core._date_of({"yearPublished": 2025}) == "2025-01-01"
    assert search_core._date_of({}) == ""


def test_map_result_schema():
    r = {
        "id": 42, "title": "A CORE Paper", "abstract": "abstract text",
        "authors": [{"name": "Jane Doe"}], "publishedDate": "2026-06-20T00:00:00",
        "doi": "https://doi.org/10.9/y", "downloadUrl": "https://core.ac.uk/x.pdf",
        "journals": [{"title": "Repo J"}],
    }
    m = search_core._map_result(r)
    for f in ("id", "title", "abstract", "summary", "authors", "published_date", "source", "url"):
        assert f in m
    assert m["title"] == "A CORE Paper"
    assert m["source"] == "CORE"
    assert m["doi"] == "10.9/y"
    assert m["url"] == "https://core.ac.uk/x.pdf"
    assert m["journal"] == "Repo J"
    assert m["published_date"] == "2026-06-20"


def test_map_result_none_without_title():
    assert search_core._map_result({"id": 1, "title": ""}) is None


def test_search_maps_results_with_key(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "k")
    page = {"results": [
        {"id": 1, "title": "One", "abstract": "a", "publishedDate": "2026-06-20"},
        {"id": 2, "title": ""},  # dropped
    ]}
    with mock.patch.object(search_core, "load_env_from_user_shell"), \
         mock.patch.object(search_core, "_fetch_json", return_value=page) as fetch:
        out = search_core.search_core(_cfg(), days=7)
    assert len(out) == 1 and out[0]["title"] == "One"
    # The Bearer key must be passed to _fetch_json.
    assert fetch.call_args[0][1] == "k"
