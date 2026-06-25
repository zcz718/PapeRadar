#!/usr/bin/env python3
"""
search_core.py — Query CORE for recent open-access works (any field).

CORE (https://core.ac.uk) aggregates ~400M+ open-access works harvested from
thousands of institutional and subject repositories worldwide. Its value to
paperadar is OA-repository and gray-literature coverage — theses, working
papers, and repository deposits that the publisher-centric sources miss. CORE
returns plain-text abstracts and direct PDF links.

Auth: CORE requires a free API key. Set CORE_API_KEY in your environment (or
~/.zshrc); register at https://core.ac.uk/services/api. Without a key this
module logs once and returns [] — the rest of the pipeline is unaffected.

Returns the standard paperadar paper dict (see search_papers.filter_and_score_papers).
API docs: https://api.core.ac.uk/docs/v3
"""

from __future__ import annotations

import json
import logging
import os
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

CORE_API = "https://api.core.ac.uk/v3/search/works"
_MAX_QUERY_TERMS = 12


def _build_query(config: dict, from_date: str, until_date: str) -> str:
    """CORE Elasticsearch-style query: keyword OR group AND a createdDate window."""
    terms = collect_keyword_terms(config, max_terms=_MAX_QUERY_TERMS)
    if not terms:
        return ""
    kw_group = "(" + " OR ".join(f'"{t}"' if " " in t else t for t in terms) + ")"
    # `createdDate` is when CORE INDEXED the record — a precise window, but on
    # its own it surfaces old papers freshly re-deposited (e.g. a 2009 paper
    # added this week). CORE 500s on a precise `publishedDate` range, so we
    # additionally constrain the publication YEAR (the window's year, or both
    # years across a Jan boundary) to keep only genuinely recent work.
    # Note: CORE's parser wants a bracketed range; `createdDate>=x` 500s.
    years = sorted({from_date[:4], until_date[:4]})
    year_clause = " OR ".join(f"yearPublished:{y}" for y in years)
    return (f"{kw_group} AND createdDate:[{from_date} TO {until_date}] "
            f"AND ({year_clause})")


def _date_of(result: dict) -> str:
    """Best-effort YYYY-MM-DD from CORE date fields."""
    pub = result.get("publishedDate") or result.get("createdDate") or ""
    if isinstance(pub, str) and len(pub) >= 10:
        return pub[:10]
    year = result.get("yearPublished")
    if year:
        try:
            return f"{int(year)}-01-01"
        except (TypeError, ValueError):
            pass
    return ""


def _map_result(result: dict) -> Optional[dict]:
    """Map one CORE work to the paperadar paper schema."""
    title = (result.get("title") or "").strip()
    if not title:
        return None
    abstract = (result.get("abstract") or "").strip()
    authors = [a.get("name", "") for a in (result.get("authors") or []) if a.get("name")]
    journals = result.get("journals") or []
    journal = ""
    if journals and isinstance(journals[0], dict):
        journal = journals[0].get("title", "") or ""
    doi = (result.get("doi") or "").replace("https://doi.org/", "")
    url = result.get("downloadUrl") or ""
    if not url:
        fulltext_urls = result.get("sourceFulltextUrls") or []
        if fulltext_urls:
            url = fulltext_urls[0]
    if not url and doi:
        url = f"https://doi.org/{doi}"
    return {
        "id": f"core:{result.get('id') or doi or title[:48]}",
        "title": title,
        "abstract": abstract,
        "summary": abstract,
        "authors": authors,
        "published_date": _date_of(result),
        "source": "CORE",
        "url": url or "",
        "journal": journal,
        "doi": doi,
        "arxiv_id": None,
        "categories": [],
    }


def _fetch_json(url, api_key, retries=3):
    """GET a URL (with CORE Bearer auth) and parse JSON via the shared helper.

    A thin wrapper so the retry/back-off/429 logic is shared while tests can
    still patch ``search_core._fetch_json`` and assert the key is forwarded.
    """
    return fetch_json(url, headers={"Authorization": f"Bearer {api_key}"},
                      retries=retries, label="CORE")


def search_core(
    config: dict,
    days: int = 7,
    target_date: Optional[datetime] = None,
) -> list[dict]:
    """Search CORE for OA works created in the last `days` days.

    Returns [] (after one INFO log) when CORE_API_KEY is unset, when the config
    has no keywords, or on a sustained API failure.
    """
    load_env_from_user_shell(("CORE_API_KEY",))
    api_key = os.environ.get("CORE_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "[CORE] CORE_API_KEY not set — skipping. "
            "Get a free key at https://core.ac.uk/services/api to enable this source."
        )
        return []

    if target_date is None:
        target_date = datetime.now()
    from_date = (target_date - timedelta(days=days)).strftime("%Y-%m-%d")
    until_date = target_date.strftime("%Y-%m-%d")

    query = _build_query(config, from_date, until_date)
    if not query:
        logger.warning("[CORE] no keywords in config — skipping")
        return []

    core_cfg = config.get("core") or {}
    try:
        max_results = int(core_cfg.get("max_results", 100))
    except (TypeError, ValueError):
        max_results = 100

    params = {"q": query, "limit": str(min(max_results, 100))}
    url = CORE_API + "?" + urllib.parse.urlencode(params)

    data = _fetch_json(url, api_key)
    if not data:
        return []
    results = data.get("results") or []
    papers = [m for m in (_map_result(r) for r in results) if m]
    logger.info("[CORE] Found %d works (%s → %s)", len(papers), from_date, until_date)
    return papers[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    import argparse, yaml
    p = argparse.ArgumentParser(description="Search CORE (paperadar source)")
    p.add_argument("--config", required=True)
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args()
    cfg = yaml.safe_load(open(a.config)) or {}
    print(json.dumps(search_core(cfg, days=a.days), indent=2, ensure_ascii=False, default=str))
