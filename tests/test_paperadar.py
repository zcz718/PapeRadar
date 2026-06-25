from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
SOURCES_DIR = SCRIPTS_DIR / "sources"
for _p in (SCRIPTS_DIR, SOURCES_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import scan_existing_notes  # noqa: E402
import search_papers  # noqa: E402
import search_biorxiv  # noqa: E402
import search_pubmed  # noqa: E402


def _base_config() -> dict:
    return {
        "research_domains": {
            "Genomics & Genome Biology": {
                "keywords": ["single-cell"],
                "arxiv_categories": ["q-bio.GN"],
                "priority": 5,
            }
        },
        "excluded_keywords": [],
    }


def test_source_windows_share_target_date_anchor():
    target_date = datetime(2025, 1, 8)

    bio_start, bio_end = search_biorxiv._resolve_window(days=7, target_date=target_date)
    pubmed_start, pubmed_end = search_pubmed._resolve_search_window(days=7, target_date=target_date)

    assert (bio_start, bio_end) == ("2025-01-01", "2025-01-08")
    assert (pubmed_start, pubmed_end) == ("2025/01/01", "2025/01/08")


def test_filter_and_score_uses_target_date_for_recency_and_popularity():
    papers = [
        {
            "id": "10.1101/example",
            "title": "A single-cell atlas",
            "authors": ["Alice Example"],
            "abstract": "This single-cell study benchmarks a new method.",
            "url": "https://example.com/paper",
            "published_date": datetime(2025, 1, 4),
            "source": "bioRxiv",
        }
    ]

    scored = search_papers.filter_and_score_papers(
        papers,
        _base_config(),
        target_date=datetime(2025, 1, 5),
        is_hot_paper_batch=False,
    )

    assert len(scored) == 1
    assert scored[0]["scores"]["recency"] == 3.0
    assert scored[0]["scores"]["popularity"] == 2.0


def test_main_passes_target_date_to_bio_sources(monkeypatch, tmp_path):
    captured = []

    def fake_search_biorxiv(config, days=7, server="biorxiv", target_date=None):
        captured.append((server, target_date))
        return [
            {
                "id": f"{server}:1",
                "title": f"{server} single-cell paper",
                "authors": ["Alice Example"],
                "abstract": "A single-cell paper with benchmark results.",
                "url": "https://example.com/preprint",
                "published_date": "2025-01-04",
                "source": "bioRxiv" if server == "biorxiv" else "medRxiv",
            }
        ]

    def fake_search_pubmed(config, days=7, target_date=None):
        captured.append(("pubmed", target_date))
        return [
            {
                "id": "PMID:1",
                "title": "PubMed single-cell paper",
                "authors": ["Bob Example"],
                "abstract": "A single-cell paper with a strong benchmark.",
                "url": "https://example.com/pubmed",
                "published_date": "2025-01-04",
                "source": "PubMed",
                "journal": "Nature",
            }
        ]

    monkeypatch.setattr(search_papers, "HAS_BIO_SOURCES", True)
    monkeypatch.setattr(search_papers, "search_biorxiv", fake_search_biorxiv)
    monkeypatch.setattr(search_papers, "search_pubmed", fake_search_pubmed)
    monkeypatch.setattr(search_papers, "search_arxiv_by_date_range", lambda **kwargs: [])
    monkeypatch.setattr(search_papers, "search_hot_papers_from_categories", lambda **kwargs: [])
    monkeypatch.setattr(search_papers, "load_research_config", lambda _path: _base_config())

    output_path = tmp_path / "arxiv_filtered.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search_papers.py",
            "--config",
            str(tmp_path / "config.yaml"),
            "--output",
            str(output_path),
            "--top-n",
            "1",
            "--days",
            "7",
            "--target-date",
            "2025-01-08",
            "--skip-hot-papers",
        ],
    )

    rc = search_papers.main()

    assert rc == 0
    assert captured == [
        ("biorxiv", datetime(2025, 1, 8)),
        ("medrxiv", datetime(2025, 1, 8)),
        ("pubmed", datetime(2025, 1, 8)),
    ]
    assert output_path.exists()


def test_scan_notes_directory_uses_vault_root_for_relative_paths(tmp_path):
    vault = tmp_path / "vault"
    papers_dir = vault / "custom" / "papers"
    note_path = papers_dir / "domain-a" / "Example_Note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        "---\n"
        "title: Example Note\n"
        "tags: [UniqueTag]\n"
        "---\n"
        "\n"
        "Body.\n",
        encoding="utf-8",
    )

    notes = scan_existing_notes.scan_notes_directory(papers_dir, vault)

    assert len(notes) == 1
    assert notes[0]["path"] == "custom/papers/domain-a/Example_Note.md"


# ---------------------------------------------------------------------------
# paperadar additions: research_brief passthrough + bio_sources gating
# ---------------------------------------------------------------------------

def test_config_with_research_brief_loads_unchanged(tmp_path):
    """A new top-level research_brief key survives load_research_config()."""
    import yaml
    cfg = {
        "research_brief": "I study ML for protein structure prediction.",
        "research_domains": {
            "ML": {"keywords": ["deep learning"], "arxiv_categories": ["cs.LG"], "priority": 3}
        },
    }
    path = tmp_path / "research_interests.yaml"
    path.write_text(yaml.dump(cfg), encoding="utf-8")
    loaded = search_papers.load_research_config(str(path))
    assert loaded.get("research_brief") == "I study ML for protein structure prediction."
    assert "ML" in loaded.get("research_domains", {})


def test_bio_sources_off_for_non_bio_config_under_auto():
    # QC P4 fix: under "auto", a clearly non-biomedical config no longer pulls
    # PubMed/bioRxiv (it injected clinical noise into non-bio personas).
    config = {"research_domains": {"ML": {"keywords": ["deep learning"], "arxiv_categories": ["cs.LG"]}}}
    assert search_papers._bio_sources_enabled(config) is False


def test_bio_sources_off_when_no_signal():
    assert search_papers._bio_sources_enabled({}) is False


def test_bio_sources_auto_needs_biomed_signal():
    # bare "auto" with no biomedical domain -> off; a q-bio domain -> on.
    assert search_papers._bio_sources_enabled({"bio_sources": "auto"}) is False
    assert search_papers._bio_sources_enabled(
        {"bio_sources": "auto",
         "research_domains": {"D": {"arxiv_categories": ["q-bio.GN"], "keywords": []}}}) is True


def test_bio_sources_explicit_false_disables():
    assert search_papers._bio_sources_enabled({"bio_sources": False}) is False
    assert search_papers._bio_sources_enabled({"bio_sources": "false"}) is False


def test_bio_sources_explicit_true_enables():
    assert search_papers._bio_sources_enabled({"bio_sources": True}) is True


# ---------------------------------------------------------------------------
# Per-domain priority weights ranking (the documented `priority: 1–5`)
# ---------------------------------------------------------------------------

def test_priority_weight_is_neutral_at_three():
    domains = {"D": {"priority": 3}}
    assert search_papers._priority_weight(domains, "D") == 1.0


def test_priority_weight_scales_with_priority():
    domains = {"hi": {"priority": 5}, "lo": {"priority": 1}}
    assert search_papers._priority_weight(domains, "hi") > 1.0
    assert search_papers._priority_weight(domains, "lo") < 1.0
    # Higher priority always weights more than lower.
    assert search_papers._priority_weight(domains, "hi") > search_papers._priority_weight(domains, "lo")


def test_priority_weight_defaults_and_guards():
    assert search_papers._priority_weight({}, None) == 1.0          # no domain
    assert search_papers._priority_weight({"D": {}}, "D") == 1.0    # missing priority → neutral 3
    assert search_papers._priority_weight({"D": {"priority": "x"}}, "D") == 1.0  # garbage → neutral
    assert search_papers._priority_weight({"D": {"priority": 99}}, "D") == 5 / 3  # clamped to 5


def test_priority_actually_changes_ranking():
    """A higher-priority domain ranks the same paper higher (the documented behavior)."""
    paper = {"title": "deep learning advances", "summary": "deep learning study",
             "published_date": datetime(2026, 6, 20)}
    base = {"D": {"keywords": ["deep learning"], "arxiv_categories": [], "priority": 3}}
    hi = {"D": {"keywords": ["deep learning"], "arxiv_categories": [], "priority": 5}}
    target = datetime(2026, 6, 24)
    s_base = search_papers.filter_and_score_papers([dict(paper)], {"research_domains": base}, target_date=target)
    s_hi = search_papers.filter_and_score_papers([dict(paper)], {"research_domains": hi}, target_date=target)
    assert s_hi[0]["scores"]["recommendation"] > s_base[0]["scores"]["recommendation"]


# ---------------------------------------------------------------------------
# Extra-source registry gating (OpenAlex / Crossref / CORE)
# ---------------------------------------------------------------------------

def test_extra_source_keyless_enabled_under_auto():
    spec = {"name": "Crossref", "cfg_key": "crossref", "key_env": None}
    assert search_papers._extra_source_enabled(spec, {}) is True  # keyless → on


def test_extra_source_keyed_auto_disabled_without_key(monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    spec = {"name": "CORE", "cfg_key": "core", "key_env": "CORE_API_KEY"}
    import unittest.mock as m
    with m.patch("search_papers.os.environ.get", return_value=""):
        assert search_papers._extra_source_enabled(spec, {}) is False


def test_extra_source_explicit_false_dict_form():
    spec = {"name": "Crossref", "cfg_key": "crossref", "key_env": None}
    assert search_papers._extra_source_enabled(spec, {"crossref": {"enabled": False}}) is False


def test_extra_source_explicit_true_scalar_form():
    spec = {"name": "CORE", "cfg_key": "core", "key_env": "CORE_API_KEY"}
    assert search_papers._extra_source_enabled(spec, {"core": True}) is True


def test_registry_has_three_sources():
    names = [s["name"] for s in search_papers._EXTRA_SOURCES]
    assert names == ["OpenAlex", "Crossref", "CORE"]


# ---------------------------------------------------------------------------
# Configurable scoring block (`scoring:`) — overrides the _scoring defaults
# ---------------------------------------------------------------------------

def test_resolve_scoring_defaults_when_absent():
    sc = search_papers._resolve_scoring({})
    assert sc["min_relevance"] == 0.5
    assert sc["title_match"] == 0.5
    assert sc["abstract_match"] == 0.3
    assert sc["category_match"] == 1.0
    assert sc["recency_thresholds"] is None
    assert round(sum(sc["weights"].values()), 6) == 1.0


def test_resolve_scoring_numeric_overrides():
    sc = search_papers._resolve_scoring({"scoring": {
        "min_relevance": 0.3, "title_match": 0.8, "abstract_match": 0.4, "category_match": 2.0}})
    assert (sc["min_relevance"], sc["title_match"], sc["abstract_match"], sc["category_match"]) == (0.3, 0.8, 0.4, 2.0)


def test_resolve_scoring_weights_merge_and_normalize():
    sc = search_papers._resolve_scoring({"scoring": {"weights": {"recency": 0.5}}})
    # partial override merges onto defaults, then normalizes to sum 1.0
    assert round(sum(sc["weights"].values()), 6) == 1.0
    assert sc["weights"]["recency"] > 0.20  # bumped above the default share


def test_resolve_scoring_recency_thresholds_parsed_and_sorted():
    sc = search_papers._resolve_scoring({"scoring": {"recency_thresholds": [[90, 2.0], [30, 3.0]]}})
    assert sc["recency_thresholds"] == [(30, 3.0), (90, 2.0)]


def test_resolve_scoring_garbage_falls_back():
    sc = search_papers._resolve_scoring({"scoring": {"min_relevance": "x", "weights": "nope",
                                                    "recency_thresholds": "bad"}})
    assert sc["min_relevance"] == 0.5
    assert round(sum(sc["weights"].values()), 6) == 1.0
    assert sc["recency_thresholds"] is None


def test_scoring_min_relevance_changes_inclusion():
    from datetime import datetime
    # Matches only in the abstract (0.3) — below the default 0.5 gate.
    paper = {"title": "unrelated", "summary": "a deep learning study",
             "published_date": datetime(2026, 6, 20)}
    dom = {"research_domains": {"D": {"keywords": ["deep learning"], "arxiv_categories": [], "priority": 3}}}
    target = datetime(2026, 6, 24)
    assert search_papers.filter_and_score_papers([dict(paper)], dom, target_date=target) == []
    loose = {**dom, "scoring": {"min_relevance": 0.3}}
    assert len(search_papers.filter_and_score_papers([dict(paper)], loose, target_date=target)) == 1
