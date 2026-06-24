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

Returns the standard paperadar paper dict (see search_arxiv.filter_and_score_papers).
API docs: https://api.core.ac.uk/docs/v3
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

try:
    import requests
    _USE_REQUESTS = True
except ImportError:  # pragma: no cover
    import urllib.request
    _USE_REQUESTS = False

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from _env_resolve import load_env_from_user_shell

logger = logging.getLogger(__name__)

CORE_API = "https://api.core.ac.uk/v3/search/works"
_MAX_QUERY_TERMS = 12


def _build_query(config: dict, from_date: str, until_date: str) -> str:
    """CORE Elasticsearch-style query: keyword OR group AND a createdDate window."""
    seen, terms = set(), []
    for domain in (config.get("research_domains") or {}).values():
        for kw in (domain.get("keywords") or []):
            kw = (kw or "").strip()
            if len(kw) <= 2 or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            terms.append(f'"{kw}"' if " " in kw else kw)
            if len(terms) >= _MAX_QUERY_TERMS:
                break
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    if not terms:
        return ""
    kw_group = "(" + " OR ".join(terms) + ")"
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


def _fetch_json(url: str, api_key: str, retries: int = 3) -> Optional[dict]:
    headers = {"User-Agent": "paperadar/1.0", "Authorization": f"Bearer {api_key}"}
    for attempt in range(retries):
        try:
            if _USE_REQUESTS:
                import requests as req
                resp = req.get(url, timeout=30, headers=headers)
                if resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            else:  # pragma: no cover
                rq = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(rq, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error("[CORE] fetch error: %s", e)
    return None


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
