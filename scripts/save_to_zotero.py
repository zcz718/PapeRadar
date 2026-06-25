#!/usr/bin/env python3
"""
save_to_zotero.py — Save weekly recommended papers to a Zotero collection.

Reads arxiv_filtered.json and creates a date-named collection in the user's
Zotero library, then adds all top papers as items.

Env vars required:
  ZOTERO_API_KEY  — from https://www.zotero.org/settings/keys
  ZOTERO_USER_ID  — numeric ID from Zotero settings
"""

import os
import sys
import json
import logging
import re
import argparse
import shutil
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Make sibling scripts/ importable when this file is invoked directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _env_resolve import load_env_from_user_shell  # noqa: E402
from _id_parser import strip_pmid_prefix  # noqa: E402

logger = logging.getLogger(__name__)

_ZOTERO_VARS = ("ZOTERO_API_KEY", "ZOTERO_USER_ID")


def _load_zotero_env_from_user_shell():
    """Populate os.environ with ZOTERO_API_KEY / ZOTERO_USER_ID when missing.

    Thin wrapper kept for source compatibility with older callers; new
    code should call `_env_resolve.load_env_from_user_shell` directly.
    The original three-helper implementation lives in `_env_resolve.py`
    so other scripts (fetch_fulltext, search_pubmed) can reuse it.
    """
    load_env_from_user_shell(_ZOTERO_VARS)


def _resolve_numeric_user_id(api_key):
    """Look up the numeric userID for an API key via Zotero's /keys/current endpoint.
    Returns (numeric_id_str, username) or (None, None) on failure."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            "https://api.zotero.org/keys/current",
            headers={"Zotero-API-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("userID", "")), data.get("username", "")
    except Exception:
        return None, None


def _get_zotero_client():
    """Initialize pyzotero client from env vars. Returns (client, None) or (None, error_msg)."""
    _load_zotero_env_from_user_shell()
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()

    if not api_key or not user_id:
        return None, "ZOTERO_API_KEY or ZOTERO_USER_ID not set — skipping Zotero sync"

    # Validate userID is numeric — Zotero's Web API requires the numeric userID
    # (visible at https://www.zotero.org/settings/keys), not the username/handle.
    if not user_id.isdigit():
        numeric_id, username = _resolve_numeric_user_id(api_key)
        hint = ""
        if numeric_id:
            match_note = " (matches your username)" if username and username == user_id else ""
            hint = (
                f"\n  Resolved your numeric userID via the API: {numeric_id}{match_note}.\n"
                f"  Fix: export ZOTERO_USER_ID=\"{numeric_id}\" in ~/.zshrc, then re-source."
            )
        else:
            hint = (
                "\n  Could not auto-resolve. Find your numeric userID at "
                "https://www.zotero.org/settings/keys (\"Your userID for use in API calls\")."
            )
        return None, (
            f"ZOTERO_USER_ID=\"{user_id}\" is not numeric — Zotero's Web API needs the "
            f"numeric userID, not your username.{hint}"
        )

    try:
        from pyzotero import zotero
        return zotero.Zotero(user_id, "user", api_key), None
    except Exception as e:
        return None, f"Failed to initialize Zotero client: {e}"


def _find_or_create_collection(zot, name):
    """Find existing collection by name, or create it. Returns collection key."""
    collections = zot.collections()
    for c in collections:
        if c["data"]["name"] == name:
            logger.info("Collection '%s' already exists — reusing", name)
            return c["key"]

    resp = zot.create_collections([{"name": name}])
    if resp and "successful" in resp:
        key = list(resp["successful"].values())[0]["data"]["key"]
        logger.info("Created collection '%s' (key: %s)", name, key)
        return key

    raise RuntimeError(f"Failed to create collection: {resp}")


def _extract_date(paper):
    """Extract a YYYY-MM-DD date string from paper's published_date."""
    raw = paper.get("published_date", "")
    if not raw:
        return datetime.now().strftime("%Y-%m-%d")
    # Handle datetime-like strings: "2026-04-03 00:00:00+00:00" or "2026-04-03"
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw))
    return match.group(1) if match else datetime.now().strftime("%Y-%m-%d")


def _extract_authors(paper):
    """Convert paper authors to Zotero creators format.

    As of 2026-05, `search_papers._normalize_authors` ensures
    `paper["authors"]` is always `list[str]` regardless of source.
    The `isinstance(author, dict)` branch below remains as a
    backward-compatibility shim for any consumer feeding an older
    `arxiv_filtered.json` that pre-dates the normalization. Safe to
    delete in a future release cycle.
    """
    creators = []
    for author in paper.get("authors", []):
        if isinstance(author, dict):
            name = author.get("name", "")
        else:
            name = str(author)
        name = name.strip()
        if not name:
            continue
        # Try to split "First Last" or "Last, First"
        if ", " in name:
            parts = name.split(", ", 1)
            creators.append({
                "creatorType": "author",
                "lastName": parts[0].strip(),
                "firstName": parts[1].strip() if len(parts) > 1 else "",
            })
        else:
            # Single name or "First Last" — use 'name' field for unsplittable names
            creators.append({"creatorType": "author", "name": name})
    return creators


def _external_ids(paper):
    ext = paper.get("externalIds") or paper.get("external_ids") or {}
    return ext if isinstance(ext, dict) else {}


def _paper_doi(paper):
    ext = _external_ids(paper)
    doi = paper.get("doi") or ext.get("DOI") or ext.get("Doi") or ""
    if not doi and str(paper.get("id", "")).startswith("10."):
        doi = paper.get("id", "")
    return str(doi).strip()


def _paper_pmid(paper):
    ext = _external_ids(paper)
    raw = (
        paper.get("pmid")
        or ext.get("PubMed")
        or ext.get("Pubmed")
        or ext.get("PMID")
        or ""
    )
    if not raw and paper.get("source", "").lower() == "pubmed":
        raw = paper.get("id", "")
    raw = str(raw).strip()
    if not raw:
        return ""
    stripped = strip_pmid_prefix(raw)
    return stripped if stripped != raw or stripped.isdigit() else raw


def _paper_arxiv_id(paper):
    ext = _external_ids(paper)
    arxiv_id = (
        paper.get("arxiv_id")
        or paper.get("arxivId")
        or ext.get("ArXiv")
        or ext.get("Arxiv")
        or ""
    )
    if not arxiv_id:
        match = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)", str(paper.get("id", "")))
        arxiv_id = match.group(1) if match else ""
    return str(arxiv_id).strip()


def _paper_journal(paper):
    venue = paper.get("journal") or paper.get("venue") or ""
    pub_venue = paper.get("publicationVenue")
    if not venue and isinstance(pub_venue, dict):
        venue = pub_venue.get("name") or ""
    return str(venue).strip()


def _paper_title_key(paper):
    return re.sub(r"[^a-z0-9]+", " ", paper.get("title", "").lower()).strip()


def _drop_empty_item_values(item):
    return {
        key: value for key, value in item.items()
        if value not in ("", None, [], {})
    }


def _paper_to_zotero_item(paper, collection_key):
    """Convert a paper dict from arxiv_filtered.json to a Zotero item dict."""
    source = paper.get("source", "arxiv").lower()
    abstract = paper.get("abstract") or paper.get("summary", "")
    title = paper.get("title", "Unknown")
    url = paper.get("url", "")
    date = _extract_date(paper)
    creators = _extract_authors(paper)
    domain = paper.get("matched_domain", "")
    score = paper.get("scores", {}).get("recommendation", 0)
    doi = _paper_doi(paper)
    pmid = _paper_pmid(paper)
    arxiv_id = _paper_arxiv_id(paper)
    journal = _paper_journal(paper)

    # Tags
    tags = []
    if domain:
        tags.append({"tag": domain})
    tags.append({"tag": "weekly-paper-recommend"})

    # Extra field — store score and source-specific IDs
    extra_lines = [f"Score: {score}"]
    if pmid:
        extra_lines.append(f"PMID: {pmid}")

    if source == "arxiv":
        item_type = "preprint"
        extra_lines.append(f"arXiv: {arxiv_id}")
        item = {
            "itemType": item_type,
            "title": title,
            "creators": creators,
            "abstractNote": abstract,
            "url": url,
            "date": date,
            "repository": "arXiv",
            "archiveID": f"arXiv:{arxiv_id}" if arxiv_id else "",
            "DOI": doi,
            "tags": tags,
            "collections": [collection_key],
            "extra": "\n".join(extra_lines),
        }

    elif source in ("biorxiv", "medrxiv"):
        item_type = "preprint"
        repo = "bioRxiv" if source == "biorxiv" else "medRxiv"
        item = {
            "itemType": item_type,
            "title": title,
            "creators": creators,
            "abstractNote": abstract,
            "url": url,
            "date": date,
            "repository": repo,
            "DOI": doi,
            "tags": tags,
            "collections": [collection_key],
            "extra": "\n".join(extra_lines),
        }

    elif source == "pubmed":
        item_type = "journalArticle"
        item = {
            "itemType": item_type,
            "title": title,
            "creators": creators,
            "abstractNote": abstract,
            "url": url,
            "date": date,
            "DOI": doi,
            "publicationTitle": journal,
            "tags": tags,
            "collections": [collection_key],
            "extra": "\n".join(extra_lines),
        }

    else:
        # Semantic Scholar or unknown — use journalArticle as fallback
        if arxiv_id:
            extra_lines.append(f"arXiv: {arxiv_id}")
        item = {
            "itemType": "journalArticle",
            "title": title,
            "creators": creators,
            "abstractNote": abstract,
            "url": url,
            "date": date,
            "DOI": doi,
            "publicationTitle": journal,
            "archiveID": f"arXiv:{arxiv_id}" if arxiv_id else "",
            "tags": tags,
            "collections": [collection_key],
            "extra": "\n".join(extra_lines),
        }

    return _drop_empty_item_values(item)


def _zotero_item_matches(item, paper):
    data = item.get("data", item)
    title_key = _paper_title_key(paper)
    doi = _paper_doi(paper).lower()
    pmid = _paper_pmid(paper)
    arxiv_id = _paper_arxiv_id(paper).lower()

    item_doi = str(data.get("DOI", "")).lower().strip()
    if doi and item_doi == doi:
        return True

    extra = str(data.get("extra", ""))
    if pmid and re.search(rf"\bPMID:\s*{re.escape(pmid)}\b", extra, re.I):
        return True

    archive_id = str(data.get("archiveID", "")).lower()
    if arxiv_id and (
        arxiv_id == archive_id.replace("arxiv:", "").strip()
        or re.search(rf"\barxiv:\s*{re.escape(arxiv_id)}\b", extra, re.I)
    ):
        return True

    item_title_key = re.sub(
        r"[^a-z0-9]+", " ", str(data.get("title", "")).lower()).strip()
    return bool(title_key and item_title_key == title_key)


def _find_existing_item(zot, paper):
    """Best-effort existing-item lookup to avoid duplicate weekly imports."""
    queries = []
    for q in (_paper_doi(paper), _paper_pmid(paper), _paper_arxiv_id(paper),
              paper.get("title", "")):
        q = str(q or "").strip()
        if q and q not in queries:
            queries.append(q)
    for query in queries:
        try:
            hits = zot.items(q=query, qmode="everything", limit=25)
        except Exception as e:
            logger.debug("Zotero lookup failed for %r: %s", query, e)
            continue
        for item in hits or []:
            if item.get("data", {}).get("itemType") == "attachment":
                continue
            if _zotero_item_matches(item, paper):
                return item
    return None


def _find_existing_item_in_list(items, paper):
    for item in items or []:
        if item.get("data", {}).get("itemType") == "attachment":
            continue
        if item.get("data", {}).get("parentItem"):
            continue
        if _zotero_item_matches(item, paper):
            return item
    return None


def _ensure_in_collection(zot, item, collection_key):
    data = item.get("data", {})
    if collection_key in (data.get("collections") or []):
        return
    zot.addto_collection(collection_key, item)


def _created_item_from_response(resp):
    if not isinstance(resp, dict):
        return None
    successful = resp.get("successful") or {}
    if not successful:
        return None
    first = successful[sorted(successful.keys(), key=str)[0]]
    data = first.get("data", {})
    return {
        "key": first.get("key") or data.get("key"),
        "version": first.get("version") or data.get("version", 0),
        "data": data,
    }


def _normalize_token(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _safe_filename_token(value, max_len=96):
    text = re.sub(r"\s+", "_", str(value or "").strip())
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    text = text[:max_len].rstrip("._-")
    return text or "paper"


def _paper_pdf_url(paper):
    raw = paper.get("pdf_url") or paper.get("pdfUrl") or ""
    if raw:
        return str(raw).strip()

    arxiv_id = _paper_arxiv_id(paper)
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}"

    doi = _paper_doi(paper)
    source = str(paper.get("source", "")).lower()
    if doi and source in ("biorxiv", "medrxiv"):
        server = "medrxiv" if source == "medrxiv" else "biorxiv"
        return f"https://www.{server}.org/content/{doi}.full.pdf"

    return ""


def _pdf_archive_dir(pdf_drop_dir="", vault_path=""):
    if pdf_drop_dir:
        root = Path(pdf_drop_dir).expanduser()
    elif vault_path:
        root = Path(vault_path).expanduser() / "20_Research" / "Papers"
    else:
        return None
    archive_dir = root / "_Fetched_PDFs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    return archive_dir


def _pdf_archive_name(paper):
    ident = (
        _paper_doi(paper)
        or _paper_pmid(paper)
        or _paper_arxiv_id(paper)
        or paper.get("id")
        or "paper"
    )
    return (
        f"{_safe_filename_token(ident, 64)}__"
        f"{_safe_filename_token(paper.get('title', 'paper'), 120)}.pdf"
    )


def _archive_pdf_for_paper(pdf_path, paper, pdf_drop_dir="", vault_path=""):
    source = Path(pdf_path).expanduser()
    if not source.exists() or source.suffix.lower() != ".pdf":
        return None

    archive_dir = _pdf_archive_dir(pdf_drop_dir=pdf_drop_dir,
                                   vault_path=vault_path)
    if not archive_dir:
        return source

    target = archive_dir / _pdf_archive_name(paper)
    try:
        if source.resolve() == target.resolve():
            paper["local_pdf_path"] = str(target)
            return target
    except OSError:
        pass

    if not target.exists():
        shutil.copy2(source, target)
        logger.info("Archived fetched PDF for '%s' to %s",
                    paper.get("title", "?"), target)
    paper["local_pdf_path"] = str(target)
    return target


def _download_pdf_url(url, paper, pdf_drop_dir="", vault_path=""):
    archive_dir = _pdf_archive_dir(pdf_drop_dir=pdf_drop_dir,
                                   vault_path=vault_path)
    if not archive_dir or not url:
        return None

    target = archive_dir / _pdf_archive_name(paper)
    if target.exists():
        paper["local_pdf_path"] = str(target)
        paper["_zotero_pdf_url"] = url
        return target

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 paperadar/1.0",
            "Accept": "application/pdf,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_type = resp.headers.get("content-type", "")
            content = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.info("Direct PDF download failed for '%s' (%s): %s",
                    paper.get("title", "?"), url, e)
        return None

    if not content.startswith(b"%PDF") and "pdf" not in content_type.lower():
        logger.info("Direct PDF URL did not return a PDF for '%s' (%s)",
                    paper.get("title", "?"), url)
        return None

    target.write_bytes(content)
    paper["local_pdf_path"] = str(target)
    paper["_zotero_pdf_url"] = url
    logger.info("Downloaded PDF for '%s' to %s", paper.get("title", "?"),
                target)
    return target


def _explicit_pdf_paths(paper):
    keys = (
        "local_pdf_path",
        "local_pdf",
        "pdf_path",
        "fulltext_pdf_path",
        "archived_pdf",
    )
    for key in keys:
        raw = paper.get(key)
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        if path.exists() and path.suffix.lower() == ".pdf":
            yield path


def _find_pdf_in_vault(paper, vault_path):
    if not vault_path:
        return None
    root = Path(vault_path).expanduser() / "20_Research" / "Papers"
    if not root.exists():
        return None

    tokens = [
        _normalize_token(paper.get("note_filename")),
        _normalize_token(paper.get("title")),
        _normalize_token(_paper_doi(paper).split("/", 1)[-1]),
        _normalize_token(_paper_pmid(paper)),
        _normalize_token(_paper_arxiv_id(paper)),
    ]
    tokens = [t for t in tokens if len(t) >= 6]
    for pdf in root.rglob("*.pdf"):
        haystack = _normalize_token(str(pdf.relative_to(root)))
        if any(t and t in haystack for t in tokens):
            return pdf
    return None


def _resolve_vault_path(cli_vault=""):
    if cli_vault:
        return str(Path(cli_vault).expanduser())
    env_vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if env_vault:
        return str(Path(env_vault).expanduser())
    try:
        from _config_paths import resolve_config_path
        import yaml
        config_path = resolve_config_path()
        if not config_path:
            return ""
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        vault = (
            (config.get("output") or {})
            .get("obsidian", {})
            .get("vault_path", "")
        )
        return str(Path(vault).expanduser()) if vault else ""
    except Exception:
        return ""


def _fetch_missing_pdf(paper, pdf_drop_dir="", vault_path=""):
    direct_pdf_url = _paper_pdf_url(paper)
    if direct_pdf_url:
        downloaded = _download_pdf_url(
            direct_pdf_url,
            paper,
            pdf_drop_dir=pdf_drop_dir,
            vault_path=vault_path,
        )
        if downloaded:
            return downloaded

    try:
        import fetch_fulltext
    except Exception as e:
        logger.warning("Cannot fetch missing PDF; fetch_fulltext import failed: %s", e)
        return None
    result, tried = fetch_fulltext.fetch(
        str(paper.get("id", "")),
        doi=_paper_doi(paper),
        drop_dir=pdf_drop_dir or "",
    )
    if result:
        fetched_from = str(result.get("fetched_from", ""))
        if fetched_from.startswith(("http://", "https://")):
            paper["_zotero_pdf_url"] = fetched_from
        if result.get("pdf_path"):
            return _archive_pdf_for_paper(
                result["pdf_path"],
                paper,
                pdf_drop_dir=pdf_drop_dir,
                vault_path=vault_path,
            )
    logger.info("No PDF fetched for '%s'; sources tried: %s",
                paper.get("title", "?"), ", ".join(tried or []))
    return None


def _find_local_pdf(paper, vault_path="", fetch_missing=False,
                    pdf_drop_dir=""):
    for path in _explicit_pdf_paths(paper):
        return path
    found = _find_pdf_in_vault(paper, vault_path)
    if found:
        return found
    if fetch_missing:
        return _fetch_missing_pdf(
            paper,
            pdf_drop_dir=pdf_drop_dir,
            vault_path=vault_path,
        )
    return None


def _item_has_pdf_attachment(zot, item_key):
    try:
        children = zot.children(item_key)
    except Exception as e:
        logger.debug("Could not inspect Zotero attachments for %s: %s",
                     item_key, e)
        return False
    for child in children or []:
        data = child.get("data", {})
        if data.get("itemType") == "attachment" and (
            data.get("contentType") == "application/pdf"
            or str(data.get("path", "")).lower().endswith(".pdf")
            or str(data.get("title", "")).lower().endswith(".pdf")
        ):
            return True
    return False


def _delete_failed_attachment_stub(zot, error):
    match = re.search(r"/items/([A-Z0-9]{8})/file", str(error))
    if not match:
        return
    key = match.group(1)
    try:
        zot.delete_item(zot.item(key))
        logger.info("Deleted failed Zotero attachment stub %s", key)
    except Exception as e:
        logger.debug("Could not delete failed attachment stub %s: %s", key, e)


def _attach_linked_pdf_url(zot, item_key, paper):
    url = paper.get("_zotero_pdf_url") or _paper_pdf_url(paper)
    if not url:
        return False

    title = paper.get("title", "")
    payload = _drop_empty_item_values({
        "itemType": "attachment",
        "parentItem": item_key,
        "linkMode": "linked_url",
        "title": f"{title} PDF" if title else "PDF",
        "url": url,
        "contentType": "application/pdf",
    })
    resp = zot.create_items([payload])
    return bool(_created_item_from_response(resp))


def _attach_pdf(zot, item_key, pdf_path, paper):
    if _item_has_pdf_attachment(zot, item_key):
        return False
    if not pdf_path or not Path(pdf_path).exists():
        return _attach_linked_pdf_url(zot, item_key, paper)

    title = paper.get("title", "")
    attachment_title = f"{title} PDF" if title else Path(pdf_path).name
    try:
        zot.attachment_both([(attachment_title, str(pdf_path))],
                            parentid=item_key)
        return True
    except Exception as e:
        _delete_failed_attachment_stub(zot, e)
        if _attach_linked_pdf_url(zot, item_key, paper):
            logger.warning(
                "PDF upload failed for '%s'; added linked PDF URL instead: %s",
                title or item_key,
                e,
            )
            return True
        logger.warning("PDF upload failed for '%s': %s", title or item_key, e)
        return False


def save_to_zotero(input_path, date_str=None, skip_paper_ids=None,
                   attach_pdfs=False, vault_path="", fetch_missing_pdfs=False,
                   pdf_drop_dir=""):
    """Main function: load papers from JSON, create Zotero collection, add items.

    Args:
        skip_paper_ids: set of paper ID strings to exclude (e.g. top-3 papers
            handled separately by MCP add-with-PDF in Step 5b).
        attach_pdfs: attach local PDFs discovered from paper metadata or vault.
        fetch_missing_pdfs: if true, run fetch_fulltext.py for missing PDFs.
    """
    # Load papers
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    papers = data.get("top_papers", [])
    if not papers:
        logger.info("No papers to save")
        return 0

    # Filter out papers the caller wants handled separately (e.g. top-3 with PDFs)
    if skip_paper_ids:
        before = len(papers)
        papers = [p for p in papers if p.get("id") not in skip_paper_ids]
        skipped = before - len(papers)
        if skipped:
            logger.info("Skipping %d top papers "
                        "(handled separately by MCP add-with-PDF): %s",
                        skipped, sorted(skip_paper_ids))

    if not papers:
        logger.info("No papers to save (all skipped)")
        return 0

    # Initialize client
    zot, err = _get_zotero_client()
    if err:
        logger.warning("%s", err)
        return 0  # Exit cleanly — don't break the pipeline

    # Determine collection name
    if not date_str:
        date_str = data.get("target_date", datetime.now().strftime("%Y-%m-%d"))
    collection_name = f"{date_str} Paper Recommendations"

    # Create or find collection
    try:
        collection_key = _find_or_create_collection(zot, collection_name)
    except Exception as e:
        logger.error("Error creating collection: %s", e)
        return 1

    # Emit collection key/name to stdout for the orchestrator to capture
    # NOTE: These two lines MUST stay on stdout (not via logger). They are
    # part of the SKILL.md Step 5a contract — the bash caller parses them
    # with `awk -F= '/^COLLECTION_KEY=/{print $2}'`. Do not migrate to
    # logger or stderr.
    print(f"COLLECTION_KEY={collection_key}")
    print(f"COLLECTION_NAME={collection_name}")

    vault_path = _resolve_vault_path(vault_path)
    if attach_pdfs and vault_path:
        logger.info("Will search vault for PDFs: %s", vault_path)

    created = 0
    reused = 0
    pdf_attached = 0
    failed = 0
    try:
        existing_collection_items = [
            item for item in zot.collection_items(collection_key, limit=100)
            if not item.get("data", {}).get("parentItem")
        ]
    except Exception as e:
        existing_collection_items = []
        logger.debug("Could not prefetch collection items for idempotency: %s",
                     e)

    for paper in papers:
        try:
            existing = (
                _find_existing_item_in_list(existing_collection_items, paper)
                or _find_existing_item(zot, paper)
            )
            if existing:
                _ensure_in_collection(zot, existing, collection_key)
                item = existing
                reused += 1
            else:
                payload = _paper_to_zotero_item(paper, collection_key)
                resp = zot.create_items([payload])
                item = _created_item_from_response(resp)
                if not item or not item.get("key"):
                    logger.warning("Could not create Zotero item for '%s': %s",
                                   paper.get("title", "?"), resp)
                    failed += 1
                    continue
                created += 1
                existing_collection_items.append(item)

            if attach_pdfs or fetch_missing_pdfs:
                pdf_path = _find_local_pdf(
                    paper,
                    vault_path=vault_path,
                    fetch_missing=fetch_missing_pdfs,
                    pdf_drop_dir=pdf_drop_dir,
                )
                if (pdf_path or _paper_pdf_url(paper)) and _attach_pdf(
                        zot, item["key"], pdf_path, paper):
                    pdf_attached += 1
        except Exception as e:
            failed += 1
            logger.warning("Skipping paper '%s': %s", paper.get('title', '?'), e)

    logger.info(
        "Zotero sync complete for '%s': %d created, %d reused, "
        "%d PDFs attached, %d failed",
        collection_name, created, reused, pdf_attached, failed,
    )
    if failed:
        return 1

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [Zotero] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Save weekly papers to Zotero")
    parser.add_argument("--input", required=True, help="Path to arxiv_filtered.json")
    parser.add_argument("--date", default=None, help="Collection date (YYYY-MM-DD)")
    parser.add_argument(
        "--skip-paper-ids", default="",
        help="Space- or comma-separated list of paper IDs to skip "
             "(legacy; usually omit)")
    parser.add_argument(
        "--attach-pdfs", action="store_true",
        help="Attach local PDFs found in arxiv_filtered.json or the Obsidian "
             "vault's 20_Research/Papers folder")
    parser.add_argument(
        "--fetch-missing-pdfs", action="store_true",
        help="For papers without a local PDF, try fetch_fulltext.py and "
             "attach any fetched PDF")
    parser.add_argument(
        "--vault", default="",
        help="Obsidian vault path used to discover archived PDFs")
    parser.add_argument(
        "--pdf-drop-dir", default="",
        help="Directory fetch_fulltext.py should scan for manually downloaded PDFs")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        logger.error("Input file not found: %s", args.input)
        sys.exit(1)

    # Parse skip IDs from the comma/space-separated string
    skip_ids = set(re.split(r"[\s,]+", args.skip_paper_ids.strip())) - {""} \
        if args.skip_paper_ids.strip() else None

    sys.exit(save_to_zotero(
        args.input,
        args.date,
        skip_paper_ids=skip_ids,
        attach_pdfs=args.attach_pdfs or args.fetch_missing_pdfs,
        vault_path=args.vault,
        fetch_missing_pdfs=args.fetch_missing_pdfs,
        pdf_drop_dir=args.pdf_drop_dir,
    ))
