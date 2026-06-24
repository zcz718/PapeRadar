#!/usr/bin/env python3
"""
search_openalex.py — Query OpenAlex for recent cross-disciplinary works.

OpenAlex (https://openalex.org) indexes ~270M scholarly works across every
discipline — computer science, physics, mathematics, economics, chemistry,
materials, social science, and the life sciences. It aggregates arXiv,
Crossref, PubMed, DataCite, and institutional repositories, so it extends
(rather than duplicates) the arXiv + Semantic Scholar + bio sources.

Returns results in the same dict format the orchestrator expects from every
source (see search_arxiv.filter_and_score_papers):
  {id, title, abstract, summary, authors, published_date, source, url,
   journal, doi, arxiv_id, categories}

Auth: OpenAlex requires a free API key for sustained use (the anonymous pool
is testing-only since Feb 2025). Set OPENALEX_API_KEY in your environment (or
~/.zshrc — it is resolved the same way as Zotero/Unpaywall credentials).
OPENALEX_EMAIL is optional and joins the "polite pool" for faster responses.
If no key is set the module logs once and returns [] — the rest of the
pipeline proceeds unaffected.

API docs: https://docs.openalex.org/api-entities/works
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
except ImportError:  # pragma: no cover - urllib fallback
    import urllib.request
    _USE_REQUESTS = False

# Reuse the shared shell-env resolver (same pattern as save_to_zotero /
# fetch_fulltext) so a key exported in ~/.zshrc is visible even when a
# non-interactive runner invokes us.
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from _env_resolve import load_env_from_user_shell

logger = logging.getLogger(__name__)

OPENALEX_API = "https://api.openalex.org/works"
_RATE_LIMIT_PAUSE = 0.2          # polite pause between paginated requests
_MAX_QUERY_TERMS = 18            # keep the search URL a sane length
_SELECT_FIELDS = (
    "id,display_name,abstract_inverted_index,authorships,publication_date,"
    "primary_location,doi,ids,open_access,topics,cited_by_count"
)


def _reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """Rebuild plain-text abstract from an OpenAlex inverted index.

    OpenAlex stores abstracts as {word: [positions]} for legal reasons. We
    place each word at every position it occupies, then read the positions in
    order. Returns "" when the field is absent (≈40% of works) or empty.
    """
    if not inverted_index:
        return ""
    pos_word: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            pos_word[pos] = word
    if not pos_word:
        return ""
    return " ".join(pos_word[i] for i in sorted(pos_word))


def _build_search_query(config: dict) -> str:
    """Build an OpenAlex boolean `search` string from the config's keywords.

    Collects every keyword across all research_domains, de-duplicates
    (case-insensitively), caps the count, and joins with OR. Multi-word
    phrases are quoted. The local re-scorer in search_arxiv prunes anything
    irrelevant afterwards, so a broad recall here is fine.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for domain in (config.get("research_domains") or {}).values():
        for kw in (domain.get("keywords") or []):
            kw = (kw or "").strip()
            if len(kw) <= 2:
                continue
            key = kw.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(f'"{kw}"' if " " in kw else kw)
            if len(terms) >= _MAX_QUERY_TERMS:
                break
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return " OR ".join(terms)


def _map_work(work: dict) -> Optional[dict]:
    """Map one OpenAlex Work object to the paperadar paper schema.

    Returns None if the work has no usable title.
    """
    title = (work.get("display_name") or "").strip()
    if not title:
        return None

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

    authors = [
        a.get("author", {}).get("display_name", "")
        for a in (work.get("authorships") or [])
        if a.get("author", {}).get("display_name")
    ]

    primary_location = work.get("primary_location") or {}
    source_info = primary_location.get("source") or {}
    journal = source_info.get("display_name", "") or ""

    doi = (work.get("doi") or "").replace("https://doi.org/", "")

    oa_url = (work.get("open_access") or {}).get("oa_url") or ""
    url = oa_url or (f"https://doi.org/{doi}" if doi else "") or work.get("id", "")

    ids = work.get("ids") or {}
    arxiv_raw = ids.get("arxiv") or ""
    arxiv_id = arxiv_raw.replace("https://arxiv.org/abs/", "").strip() or None

    topics = [t.get("display_name", "") for t in (work.get("topics") or [])
              if t.get("display_name")]

    return {
        "id": work.get("id") or f"openalex:{title[:48]}",
        "title": title,
        "abstract": abstract,
        "summary": abstract,            # _scoring also reads "summary"
        "authors": authors,
        "published_date": work.get("publication_date") or "",  # "YYYY-MM-DD"
        "source": "OpenAlex",
        "url": url,
        "journal": journal,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "categories": topics,
    }


def _fetch_json(url: str, retries: int = 3) -> Optional[dict]:
    """GET a URL and parse JSON. Returns dict or None on sustained failure."""
    for attempt in range(retries):
        try:
            if _USE_REQUESTS:
                import requests as req
                resp = req.get(
                    url, timeout=30,
                    headers={"User-Agent": "paperadar/1.0"},
                )
                if resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            else:  # pragma: no cover - urllib fallback
                req = urllib.request.Request(
                    url, headers={"User-Agent": "paperadar/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.error("[OpenAlex] fetch error (%s): %s", url, e)
    return None


def search_openalex(
    config: dict,
    days: int = 7,
    target_date: Optional[datetime] = None,
) -> list[dict]:
    """Search OpenAlex for works published in the last `days` days.

    Returns a list of paper dicts in the orchestrator's schema. Returns []
    (after a single INFO log) when OPENALEX_API_KEY is not configured, when
    the config has no keywords, or on a sustained API failure.
    """
    load_env_from_user_shell(("OPENALEX_API_KEY", "OPENALEX_EMAIL"))
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    email = os.environ.get("OPENALEX_EMAIL", "").strip()

    if not api_key:
        logger.info(
            "[OpenAlex] OPENALEX_API_KEY not set — skipping. "
            "Get a free key at https://openalex.org to enable this source."
        )
        return []

    search_query = _build_search_query(config)
    if not search_query:
        logger.warning("[OpenAlex] no keywords in config — skipping")
        return []

    if target_date is None:
        target_date = datetime.now()
    from_date = (target_date - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = target_date.strftime("%Y-%m-%d")

    oa_cfg = config.get("openalex") or {}
    try:
        max_results = int(oa_cfg.get("max_results", 100))
    except (TypeError, ValueError):
        max_results = 100

    filter_str = (
        f"from_publication_date:{from_date},"
        f"to_publication_date:{to_date},"
        "is_paratext:false"
    )

    papers: list[dict] = []
    cursor = "*"
    per_page = min(max_results, 200)

    while len(papers) < max_results and cursor:
        params = {
            "filter": filter_str,
            "search": search_query,
            "select": _SELECT_FIELDS,
            "per-page": str(per_page),
            "cursor": cursor,
            "sort": "cited_by_count:desc",
            "api_key": api_key,
        }
        if email:
            params["mailto"] = email
        url = OPENALEX_API + "?" + urllib.parse.urlencode(params)

        data = _fetch_json(url)
        if not data:
            break
        results = data.get("results") or []
        for work in results:
            mapped = _map_work(work)
            if mapped:
                papers.append(mapped)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not results:
            break
        if len(papers) < max_results and cursor:
            time.sleep(_RATE_LIMIT_PAUSE)

    logger.info("[OpenAlex] Found %d works (%s → %s)",
                len(papers), from_date, to_date)
    return papers[:max_results]


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    import argparse
    p = argparse.ArgumentParser(description="Search OpenAlex (paperadar source)")
    p.add_argument("--config", required=True)
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    import yaml
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}
    out = search_openalex(cfg, days=args.days)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
