from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_existing_notes  # noqa: E402
import search_arxiv  # noqa: E402
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

    scored = search_arxiv.filter_and_score_papers(
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

    monkeypatch.setattr(search_arxiv, "HAS_BIO_SOURCES", True)
    monkeypatch.setattr(search_arxiv, "search_biorxiv", fake_search_biorxiv)
    monkeypatch.setattr(search_arxiv, "search_pubmed", fake_search_pubmed)
    monkeypatch.setattr(search_arxiv, "search_arxiv_by_date_range", lambda **kwargs: [])
    monkeypatch.setattr(search_arxiv, "search_hot_papers_from_categories", lambda **kwargs: [])
    monkeypatch.setattr(search_arxiv, "load_research_config", lambda _path: _base_config())

    output_path = tmp_path / "arxiv_filtered.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "search_arxiv.py",
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

    rc = search_arxiv.main()

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
    loaded = search_arxiv.load_research_config(str(path))
    assert loaded.get("research_brief") == "I study ML for protein structure prediction."
    assert "ML" in loaded.get("research_domains", {})


def test_bio_sources_auto_enabled_for_qbio_category():
    config = {"research_domains": {"G": {"keywords": ["x"], "arxiv_categories": ["q-bio.GN"]}}}
    assert search_arxiv._bio_sources_enabled(config) is True


def test_bio_sources_auto_enabled_for_bio_keyword():
    config = {"research_domains": {"G": {"keywords": ["chromatin accessibility"], "arxiv_categories": ["cs.LG"]}}}
    assert search_arxiv._bio_sources_enabled(config) is True


def test_bio_sources_auto_disabled_for_non_bio_config():
    config = {"research_domains": {"ML": {"keywords": ["deep learning", "transformer"], "arxiv_categories": ["cs.LG"]}}}
    assert search_arxiv._bio_sources_enabled(config) is False


def test_bio_sources_explicit_true_overrides_auto():
    config = {"bio_sources": True, "research_domains": {"ML": {"keywords": ["deep learning"], "arxiv_categories": ["cs.LG"]}}}
    assert search_arxiv._bio_sources_enabled(config) is True


def test_bio_sources_explicit_false_overrides_auto():
    config = {"bio_sources": False, "research_domains": {"G": {"keywords": ["genome"], "arxiv_categories": ["q-bio.GN"]}}}
    assert search_arxiv._bio_sources_enabled(config) is False
