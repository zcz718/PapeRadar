"""Tests for scripts/search_openalex.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import search_openalex  # noqa: E402


def _ml_config():
    return {
        "research_domains": {
            "ML": {
                "keywords": ["deep learning", "transformer", "ab"],  # "ab" is too short → dropped
                "arxiv_categories": ["cs.LG"],
                "priority": 5,
            }
        }
    }


def test_graceful_skip_when_no_api_key(monkeypatch):
    """Returns [] (no HTTP) when OPENALEX_API_KEY is absent."""
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    # Stop the shell-rc resolver from supplying a key from the real environment.
    with mock.patch.object(search_openalex, "load_env_from_user_shell"), \
         mock.patch.object(search_openalex, "_fetch_json") as fetch:
        result = search_openalex.search_openalex(_ml_config(), days=7)
    assert result == []
    fetch.assert_not_called()


def test_abstract_reconstruction_orders_by_position():
    inverted = {"The": [0], "quick": [1], "brown": [2], "fox": [3]}
    assert search_openalex._reconstruct_abstract(inverted) == "The quick brown fox"


def test_abstract_reconstruction_handles_repeats_and_gaps():
    inverted = {"a": [0, 2], "b": [1]}
    assert search_openalex._reconstruct_abstract(inverted) == "a b a"


def test_abstract_reconstruction_empty():
    assert search_openalex._reconstruct_abstract(None) == ""
    assert search_openalex._reconstruct_abstract({}) == ""


def test_build_search_query_dedups_and_drops_short_terms():
    config = {
        "research_domains": {
            "A": {"keywords": ["deep learning", "transformer", "ab"]},
            "B": {"keywords": ["deep learning", "diffusion model"]},
        }
    }
    q = search_openalex._build_search_query(config)
    assert q.count('"deep learning"') == 1          # de-duplicated, phrase quoted
    assert "transformer" in q                        # single word, unquoted
    assert '"diffusion model"' in q
    assert "ab" not in q.split(" OR ")               # too short, dropped
    assert " OR " in q


def test_map_work_full_schema():
    work = {
        "id": "https://openalex.org/W12345",
        "display_name": "A Test Paper",
        "abstract_inverted_index": {"hello": [0], "world": [1]},
        "authorships": [
            {"author": {"display_name": "Alice Smith"}},
            {"author": {"display_name": "Bob Jones"}},
        ],
        "publication_date": "2026-06-20",
        "primary_location": {"source": {"display_name": "Nature"}},
        "doi": "https://doi.org/10.1234/test",
        "ids": {"arxiv": "https://arxiv.org/abs/2606.00001"},
        "open_access": {"oa_url": "https://example.org/paper.pdf"},
        "topics": [{"display_name": "Machine Learning"}],
        "cited_by_count": 7,
    }
    mapped = search_openalex._map_work(work)
    assert mapped is not None
    for field in ("id", "title", "abstract", "summary", "authors",
                  "published_date", "source", "url"):
        assert field in mapped, f"missing required field {field}"
    assert mapped["title"] == "A Test Paper"
    assert mapped["abstract"] == "hello world"
    assert mapped["summary"] == "hello world"
    assert mapped["authors"] == ["Alice Smith", "Bob Jones"]
    assert mapped["published_date"] == "2026-06-20"
    assert mapped["source"] == "OpenAlex"
    assert mapped["url"] == "https://example.org/paper.pdf"
    assert mapped["journal"] == "Nature"
    assert mapped["doi"] == "10.1234/test"
    assert mapped["arxiv_id"] == "2606.00001"
    assert mapped["categories"] == ["Machine Learning"]


def test_map_work_returns_none_without_title():
    assert search_openalex._map_work({"id": "W1", "display_name": ""}) is None
    assert search_openalex._map_work({"id": "W1"}) is None


def test_map_work_url_falls_back_to_doi_then_id():
    work = {
        "id": "https://openalex.org/W9",
        "display_name": "No OA URL",
        "doi": "https://doi.org/10.5/x",
        "open_access": {},
    }
    mapped = search_openalex._map_work(work)
    assert mapped["url"] == "https://doi.org/10.5/x"
    assert mapped["arxiv_id"] is None


def test_search_openalex_maps_results_with_key(monkeypatch):
    """With a key set, a single page of results is mapped and returned."""
    monkeypatch.setenv("OPENALEX_API_KEY", "test-key")
    fake_page = {
        "results": [
            {"id": "https://openalex.org/W1", "display_name": "Paper One",
             "abstract_inverted_index": {"x": [0]}, "publication_date": "2026-06-20"},
            {"id": "https://openalex.org/W2", "display_name": ""},  # dropped (no title)
        ],
        "meta": {"next_cursor": None},
    }
    with mock.patch.object(search_openalex, "load_env_from_user_shell"), \
         mock.patch.object(search_openalex, "_fetch_json", return_value=fake_page) as fetch:
        result = search_openalex.search_openalex(_ml_config(), days=7)
    assert len(result) == 1
    assert result[0]["title"] == "Paper One"
    assert result[0]["source"] == "OpenAlex"
    # The key must be carried into the request URL.
    called_url = fetch.call_args[0][0]
    assert "api_key=test-key" in called_url
