from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_note  # noqa: E402
import save_to_zotero  # noqa: E402


def test_pubmed_payload_preserves_citation_fields():
    paper = {
        "id": "PMID:42133812",
        "source": "PubMed",
        "title": "NAT10/ac4C drives intrahepatic cholangiocarcinoma",
        "authors": ["Alice Example", "Bob Example"],
        "abstract": "A PubMed abstract.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/42133812/",
        "published_date": "2026-05-18",
        "doi": "10.1073/pnas.2532263123",
        "journal": "Proc Natl Acad Sci U S A",
        "matched_domain": "Genomics & Genome Biology",
        "scores": {"recommendation": 4.33},
    }

    item = save_to_zotero._paper_to_zotero_item(paper, "COLL123")

    assert item["itemType"] == "journalArticle"
    assert item["DOI"] == "10.1073/pnas.2532263123"
    assert item["publicationTitle"] == "Proc Natl Acad Sci U S A"
    assert "PMID: 42133812" in item["extra"]
    assert item["collections"] == ["COLL123"]


def test_find_local_pdf_prefers_vault_archive(tmp_path):
    vault = tmp_path / "vault"
    note_dir = vault / "20_Research" / "Papers" / "Genomics_&_Genome_Biology"
    note_dir.mkdir(parents=True)
    pdf = note_dir / "10.1073_pnas.2532263123__Example.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF")

    paper = {
        "id": "PMID:42133812",
        "source": "PubMed",
        "title": "NAT10/ac4C drives intrahepatic cholangiocarcinoma",
        "doi": "10.1073/pnas.2532263123",
    }

    assert save_to_zotero._find_local_pdf(paper, vault_path=str(vault)) == pdf


def test_paper_pdf_url_normalizes_arxiv_url_id():
    paper = {
        "id": "http://arxiv.org/abs/2606.18179v1",
        "source": "arxiv",
        "title": "Example arXiv paper",
    }

    assert (
        save_to_zotero._paper_pdf_url(paper)
        == "https://arxiv.org/pdf/2606.18179v1"
    )


def test_archive_pdf_for_paper_copies_to_fetched_pdf_folder(tmp_path):
    source = tmp_path / "downloaded.pdf"
    source.write_bytes(b"%PDF-1.4\nfake\n%%EOF")
    drop_dir = tmp_path / "vault" / "20_Research" / "Papers"
    paper = {
        "id": "PMID:42133812",
        "title": "Example Paper",
    }

    archived = save_to_zotero._archive_pdf_for_paper(
        source, paper, pdf_drop_dir=str(drop_dir))

    archived_path = Path(archived)
    assert archived_path.exists()
    assert archived_path.parent == drop_dir / "_Fetched_PDFs"
    assert paper["local_pdf_path"] == str(archived_path)


def test_find_existing_item_in_list_matches_collection_parent():
    paper = {
        "id": "PMID:42133812",
        "source": "PubMed",
        "title": "Example Paper",
        "doi": "10.1073/pnas.2532263123",
    }
    items = [
        {
            "key": "ATTACH01",
            "data": {
                "itemType": "attachment",
                "title": "Example Paper PDF",
            },
        },
        {
            "key": "ITEM0001",
            "data": {
                "itemType": "journalArticle",
                "title": "Example Paper",
                "DOI": "10.1073/pnas.2532263123",
            },
        },
    ]

    match = save_to_zotero._find_existing_item_in_list(items, paper)

    assert match["key"] == "ITEM0001"


def test_attach_pdf_quota_failure_creates_linked_url(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF")
    paper = {
        "title": "Quota Paper",
        "pdf_url": "https://example.org/paper.pdf",
    }

    class FakeZotero:
        def __init__(self):
            self.deleted = []
            self.created = []

        def children(self, _item_key):
            return []

        def attachment_both(self, _attachments, parentid):
            assert parentid == "PARENT"
            raise Exception(
                "URL: https://api.zotero.org/users/1/items/ABC12345/file\n"
                "Response: File would exceed quota"
            )

        def item(self, key):
            return {"key": key, "version": 1, "data": {"key": key}}

        def delete_item(self, payload, last_modified=None):
            self.deleted.append(payload["key"])

        def create_items(self, payload):
            self.created.extend(payload)
            return {"successful": {"0": {"key": "LINK1234",
                                         "data": payload[0]}}}

    zot = FakeZotero()

    assert save_to_zotero._attach_pdf(zot, "PARENT", pdf, paper)
    assert zot.deleted == ["ABC12345"]
    assert zot.created[0]["linkMode"] == "linked_url"
    assert zot.created[0]["url"] == "https://example.org/paper.pdf"


def test_generate_note_archives_fulltext_pdf(tmp_path):
    pdf = tmp_path / "downloaded.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF")
    fulltext = tmp_path / "fulltext.json"
    fulltext.write_text(
        json.dumps({
            "schema_version": 1,
            "pdf_path": str(pdf),
            "text": "Abstract\n\nMETHODS\nCells were sequenced.",
            "abstract": "A real extracted abstract.",
            "source": "user-pdf",
            "sources_tried": ["user-pdf"],
        }),
        encoding="utf-8",
    )
    note_dir = tmp_path / "vault" / "20_Research" / "Papers" / "Domain"

    archived = generate_note.archive_fulltext_pdf(
        str(fulltext), note_dir, "PMID:42133812", "Example Paper")

    archived_path = Path(archived)
    assert archived_path.exists()
    assert archived_path.parent == note_dir
    assert archived_path.name.startswith("PMID_42133812__Example_Paper")
