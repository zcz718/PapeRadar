#!/usr/bin/env python3
"""
search_crossref.py — Query Crossref for recently-registered works (any field).

Crossref is the DOI registry of record: ~180M works across every discipline,
deposited by 20k+ publishers. Its value to paperadar is FRESHNESS — the
`from-index-date` filter catches papers the moment their metadata is registered,
often before aggregators like OpenAlex have ingested them. No API key is needed;
including a contact email (CROSSREF_EMAIL, or the existing UNPAYWALL_EMAIL) joins
the faster "polite pool".

Caveat: only a minority of Crossref records carry an abstract, and when present
it is JATS-XML (stripped to plain text here). paperadar's scorer still matches
on the title, and drops anything that fails the relevance threshold, so sparse
abstracts cost recall, not correctness.

Returns the standard paperadar paper dict (see search_papers.filter_and_score_papers).
API docs: https://api.crossref.org/swagger-ui/index.html
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

# This adapter lives in scripts/sources/; shared helpers (_env_resolve, _http,
# _query) live one level up in scripts/. Put that parent dir on the import path
# first, then pull in the shell-env resolver, JSON-fetch helper, and collector.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HERE)
for _p in (_SCRIPTS_DIR, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from _env_resolve import load_env_from_user_shell
from _http import fetch_json
from _query import collect_keyword_terms

logger = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works"
_MAX_QUERY_TERMS = 12
_JATS_TAG = re.compile(r"<[^>]+>")


def _build_query(config: dict) -> str:
    """Space-joined keyword query (Crossref `query` is relevance, not boolean)."""
    return " ".join(collect_keyword_terms(config, max_terms=_MAX_QUERY_TERMS))


def _strip_jats(abstract: str) -> str:
    """Crossref abstracts are JATS-XML; strip tags to plain text."""
    if not abstract:
        return ""
    text = _JATS_TAG.sub(" ", abstract)
    return re.sub(r"\s+", " ", text).strip()


def _date_from_parts(item: dict) -> str:
    """Best-effort YYYY-MM-DD from Crossref date-part fields."""
    for key in ("published", "issued", "published-online", "published-print", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])
        if parts and parts[0]:
            y = parts[0][0]
            m = parts[0][1] if len(parts[0]) > 1 else 1
            d = parts[0][2] if len(parts[0]) > 2 else 1
            try:
                return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue
    return ""


def _map_item(item: dict) -> Optional[dict]:
    """Map one Crossref work item to the paperadar paper schema."""
    title = " ".join(item.get("title") or []).strip()
    if not title:
        return None
    abstract = _strip_jats(item.get("abstract", ""))
    authors = []
    for a in (item.get("author") or []):
        name = " ".join(p for p in (a.get("given", ""), a.get("family", "")) if p).strip()
        if name:
            authors.append(name)
    doi = item.get("DOI", "") or ""
    journal = " ".join(item.get("container-title") or []).strip()
    return {
        "id": f"crossref:{doi}" if doi else f"crossref:{title[:48]}",
        "title": title,
        "abstract": abstract,
        "summary": abstract,
        "authors": authors,
        "published_date": _date_from_parts(item),
        "source": "Crossref",
        "url": item.get("URL", "") or (f"https://doi.org/{doi}" if doi else ""),
        "journal": journal,
        "doi": doi,
        "arxiv_id": None,
        "categories": item.get("subject") or [],
    }


def _fetch_json(url, retries=3):
    """GET a URL and parse JSON via the shared helper (scripts/_http.py).

    A thin wrapper so the retry/back-off/429 logic is shared while tests can
    still patch ``search_crossref._fetch_json``.
    """
    return fetch_json(url, retries=retries, label="Crossref")


def search_crossref(
    config: dict,
    days: int = 7,
    target_date: Optional[datetime] = None,
) -> list[dict]:
    """Search Crossref for works indexed in the last `days` days.

    No API key required. Returns [] when the config has no keywords or on a
    sustained API failure.
    """
    query = _build_query(config)
    if not query:
        logger.warning("[Crossref] no keywords in config — skipping")
        return []

    load_env_from_user_shell(("CROSSREF_EMAIL", "UNPAYWALL_EMAIL"))
    email = (os.environ.get("CROSSREF_EMAIL")
             or os.environ.get("UNPAYWALL_EMAIL") or "").strip()

    if target_date is None:
        target_date = datetime.now()
    from_date = (target_date - timedelta(days=days)).strftime("%Y-%m-%d")
    until_date = target_date.strftime("%Y-%m-%d")

    cr_cfg = config.get("crossref") or {}
    try:
        max_results = int(cr_cfg.get("max_results", 100))
    except (TypeError, ValueError):
        max_results = 100

    # Filter on `created` (when the DOI first appeared in Crossref), NOT
    # `indexed` — the latter also fires on backfile re-indexing of old papers,
    # which would surface decade-old works in a "this week" run.
    params = {
        "query": query,
        "filter": f"from-created-date:{from_date},until-created-date:{until_date},type:journal-article",
        "rows": str(min(max_results, 100)),
        "sort": "created",
        "order": "desc",
        "select": "DOI,title,abstract,author,container-title,published,issued,created,URL,subject",
    }
    if email:
        params["mailto"] = email
    url = CROSSREF_API + "?" + urllib.parse.urlencode(params)

    data = _fetch_json(url)
    if not data:
        return []
    items = ((data.get("message") or {}).get("items") or [])
    papers = [m for m in (_map_item(it) for it in items) if m]
    logger.info("[Crossref] Found %d works (%s → %s)", len(papers), from_date, until_date)
    return papers[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    import argparse, yaml
    p = argparse.ArgumentParser(description="Search Crossref (paperadar source)")
    p.add_argument("--config", required=True)
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args()
    cfg = yaml.safe_load(open(a.config)) or {}
    print(json.dumps(search_crossref(cfg, days=a.days), indent=2, ensure_ascii=False, default=str))
