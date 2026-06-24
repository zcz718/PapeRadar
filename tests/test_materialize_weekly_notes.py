from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import materialize_weekly_notes as weekly  # noqa: E402


def _sample_input(path: Path) -> Path:
    payload = {
        "target_date": "2026-06-22",
        "total_recent": 3,
        "total_bio": 4,
        "total_unique": 5,
        "bio_status": "ok",
        "top_papers": [
            {
                "id": "http://arxiv.org/abs/2606.18179v1",
                "title": (
                    "PyPeakRankR: Reproducible Peak-Level Feature Extraction "
                    "for Regulatory Element Ranking"
                ),
                "authors": ["Alice Example", "Bob Example"],
                "summary": (
                    "A reproducible peak-ranking feature extractor is "
                    "introduced for regulatory element prioritization."
                ),
                "source": "arxiv",
                "matched_domain": "Epigenomics",
                "matched_keywords": ["ATAC-seq", "regulatory element"],
                "scores": {"recommendation": 8.25},
                "note_filename": "PyPeakRankR_Reproducible_Peak-Level",
                "published_date": "2026-06-18",
            },
            {
                "id": "PMID:42321491",
                "title": "Efficient site-specific gene addition using R2.",
                "authors": ["Carol Example"],
                "abstract": "A method for precise, targeted gene insertion in plants.",
                "source": "PubMed",
                "matched_domain": "Genomics & Genome Biology",
                "doi": "10.1038/example",
                "journal": "Nature Biotechnology",
                "scores": {"recommendation": 7.5},
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_materialize_obsidian_weekly_and_paper_notes(tmp_path: Path) -> None:
    input_path = _sample_input(tmp_path / "arxiv_filtered.json")
    vault = tmp_path / "vault"
    pdf_dir = vault / "20_Research" / "Papers" / "_Fetched_PDFs"
    pdf_dir.mkdir(parents=True)
    pdf = pdf_dir / "2606.18179v1__PyPeakRankR.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF")

    result = weekly.materialize(
        input_path,
        mode="obsidian",
        vault_path=str(vault),
    )

    weekly_note = Path(result["weekly_note"])
    assert weekly_note == vault / "10_Daily" / "2026-06-22-paper-recommendations.md"
    assert weekly_note.exists()
    weekly_text = weekly_note.read_text(encoding="utf-8")
    assert "[[20_Research/Papers/Epigenomics/PyPeakRankR_Reproducible_Peak-Level|" in weekly_text
    assert "local PDF" in weekly_text

    paper_note = (
        vault
        / "20_Research"
        / "Papers"
        / "Epigenomics"
        / "PyPeakRankR_Reproducible_Peak-Level.md"
    )
    assert paper_note.exists()
    note_text = paper_note.read_text(encoding="utf-8")
    assert "## Agent Access" in note_text
    assert "agent-readable-reference" in note_text
    assert f"local_pdf: {json.dumps(str(pdf))}" in note_text
    assert result["paper_notes_created"] == 2
    assert result["local_pdfs_linked"] == 1


def test_materialize_preserves_existing_paper_note(tmp_path: Path) -> None:
    input_path = _sample_input(tmp_path / "arxiv_filtered.json")
    vault = tmp_path / "vault"
    existing = (
        vault
        / "20_Research"
        / "Papers"
        / "Epigenomics"
        / "PyPeakRankR_Reproducible_Peak-Level.md"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("human edits stay here\n", encoding="utf-8")

    result = weekly.materialize(
        input_path,
        mode="obsidian",
        vault_path=str(vault),
    )

    assert existing.read_text(encoding="utf-8") == "human edits stay here\n"
    assert result["paper_notes_reused"] == 1
    assert result["paper_notes_created"] == 1


def test_materialize_migrates_legacy_trailing_dot_note_name(tmp_path: Path) -> None:
    input_path = _sample_input(tmp_path / "arxiv_filtered.json")
    vault = tmp_path / "vault"
    legacy = (
        vault
        / "20_Research"
        / "Papers"
        / "Genomics_&_Genome_Biology"
        / "Efficient_site-specific_gene_addition_using_R2..md"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy generated content\n", encoding="utf-8")

    result = weekly.materialize(
        input_path,
        mode="obsidian",
        vault_path=str(vault),
    )

    migrated = legacy.with_name("Efficient_site-specific_gene_addition_using_R2.md")
    assert not legacy.exists()
    assert migrated.exists()
    assert migrated.read_text(encoding="utf-8") == "legacy generated content\n"
    assert result["paper_notes_migrated"] == 1
    assert "..md" not in Path(result["weekly_note"]).read_text(encoding="utf-8")


def test_materialize_standalone_writes_plain_markdown(tmp_path: Path) -> None:
    input_path = _sample_input(tmp_path / "arxiv_filtered.json")
    out_dir = tmp_path / "out"

    result = weekly.materialize(
        input_path,
        mode="standalone",
        output_dir=str(out_dir),
    )

    weekly_note = Path(result["weekly_note"])
    text = weekly_note.read_text(encoding="utf-8")
    assert weekly_note == out_dir / "2026-06-22-paper-recommendations.md"
    assert "[PyPeakRankR:" in text
    assert "[[" not in text
    assert (out_dir / "papers" / "Epigenomics" / "PyPeakRankR_Reproducible_Peak-Level.md").exists()
