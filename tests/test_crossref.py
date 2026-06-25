"""Tests for scripts/search_crossref.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "sources"))

import search_crossref  # noqa: E402


def _cfg():
    return {"research_domains": {"ML": {"keywords": ["deep learning", "transformer", "ab"]}}}


def test_build_query_drops_short_and_dedups():
    cfg = {"research_domains": {
        "A": {"keywords": ["deep learning", "transformer"]},
        "B": {"keywords": ["deep learning", "ab"]},
    }}
    q = search_crossref._build_query(cfg)
    assert q.count("deep learning") == 1
    assert "transformer" in q
    assert "ab" not in q.split()


def test_strip_jats():
    raw = "<jats:p>Hello <jats:italic>world</jats:italic></jats:p>"
    assert search_crossref._strip_jats(raw) == "Hello world"
    assert search_crossref._strip_jats("") == ""


def test_date_from_parts_prefers_published():
    item = {"published": {"date-parts": [[2026, 6, 20]]},
            "created": {"date-parts": [[2020, 1, 1]]}}
    assert search_crossref._date_from_parts(item) == "2026-06-20"


def test_date_from_parts_partial():
    assert search_crossref._date_from_parts({"issued": {"date-parts": [[2026]]}}) == "2026-01-01"
    assert search_crossref._date_from_parts({}) == ""


def test_map_item_schema():
    item = {
        "DOI": "10.1/x", "title": ["A Paper"],
        "abstract": "<jats:p>Abs text</jats:p>",
        "author": [{"given": "Ada", "family": "Lovelace"}, {"family": "Babbage"}],
        "container-title": ["J. Things"],
        "published": {"date-parts": [[2026, 6, 1]]},
        "URL": "https://doi.org/10.1/x", "subject": ["CS"],
    }
    m = search_crossref._map_item(item)
    for f in ("id", "title", "abstract", "summary", "authors", "published_date", "source", "url"):
        assert f in m
    assert m["title"] == "A Paper"
    assert m["abstract"] == "Abs text"
    assert m["authors"] == ["Ada Lovelace", "Babbage"]
    assert m["source"] == "Crossref"
    assert m["doi"] == "10.1/x"
    assert m["journal"] == "J. Things"
    assert m["arxiv_id"] is None


def test_map_item_none_without_title():
    assert search_crossref._map_item({"DOI": "10.1/x", "title": []}) is None


def test_search_no_keywords_returns_empty():
    assert search_crossref.search_crossref({"research_domains": {}}, days=7) == []


def test_search_maps_results():
    page = {"message": {"items": [
        {"DOI": "10.1/a", "title": ["One"], "abstract": "<jats:p>x</jats:p>",
         "published": {"date-parts": [[2026, 6, 20]]}},
        {"DOI": "10.1/b", "title": []},  # dropped
    ]}}
    with mock.patch.object(search_crossref, "load_env_from_user_shell"), \
         mock.patch.object(search_crossref, "_fetch_json", return_value=page) as fetch:
        out = search_crossref.search_crossref(_cfg(), days=7)
    assert len(out) == 1 and out[0]["title"] == "One"
    # created-date window must be in the request, not index-date.
    url = fetch.call_args[0][0]
    assert "from-created-date" in url and "from-index-date" not in url
