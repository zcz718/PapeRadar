#!/usr/bin/env python3
"""
search_biorxiv.py — Query bioRxiv/medRxiv REST API for recent preprints.

Returns results in the same dict format as search_papers.py:
  {id, title, authors, abstract, url, published_date, source}

API docs: https://api.biorxiv.org/
  Endpoint: https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}/json
"""

import os
import sys
import time
import json
import re
from datetime import datetime, timedelta

try:
    import requests
    _USE_REQUESTS = True
except ImportError:
    import urllib.request
    _USE_REQUESTS = False

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# This adapter lives in scripts/sources/; its shared helper (_config_paths,
# used by the standalone CLI's load_config) lives one level up in scripts/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HERE)
for _p in (_SCRIPTS_DIR, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BIORXIV_API = "https://api.biorxiv.org/details"


def _fetch_json(url, retries=3):
    """Fetch URL and parse JSON. Returns dict or None."""
    throttled = False
    for attempt in range(retries):
        try:
            if _USE_REQUESTS:
                import requests as req
                resp = req.get(url, timeout=20)
                if resp.status_code == 429:
                    throttled = True
                    time.sleep(30)
                    continue
                resp.raise_for_status()
                return resp.json()
            else:
                import urllib.request
                with urllib.request.urlopen(url, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"[bioRxiv] fetch error ({url}): {e}", file=sys.stderr)
    # Fail loud on a sustained 429: without this the function returns None
    # silently and the caller reports an empty (but "ok") result set.
    if throttled:
        print(f"[bioRxiv] HTTP 429 rate limit never cleared after {retries} "
              f"attempts ({url}); results for this page are missing",
              file=sys.stderr)
    return None


def _coerce_target_date(target_date=None):
    """Return a datetime anchor for a search window."""
    if target_date is None:
        return datetime.now()
    if isinstance(target_date, datetime):
        return target_date
    return datetime.strptime(str(target_date), "%Y-%m-%d")


def _resolve_window(days=7, target_date=None):
    """Return YYYY-MM-DD start/end strings for the requested backfill window."""
    end_dt = _coerce_target_date(target_date)
    end_date = end_dt.strftime("%Y-%m-%d")
    start_date = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    return start_date, end_date


def _keyword_matches(text, keywords):
    """Return list of keywords found in text (case-insensitive, whole-word for short terms)."""
    matches = []
    for kw in keywords:
        kw_lower = kw.lower()
        # For short keywords (≤5 chars) or all-uppercase abbreviations, require word boundaries
        # to avoid matching substrings (e.g. so "ONT" does not match inside "control")
        if len(kw) <= 5 or kw.isupper():
            pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw_lower) + r'(?![a-zA-Z0-9])'
            if re.search(pattern, text.lower()):
                matches.append(kw)
        else:
            if kw_lower in text.lower():
                matches.append(kw)
    return matches


def _all_domain_keywords(config):
    """Flatten all keywords from all domains into one list."""
    keywords = []
    for domain_cfg in config.get("research_domains", {}).values():
        keywords.extend(domain_cfg.get("keywords", []))
    return list(set(keywords))


def search_biorxiv(config, days=7, server="biorxiv", target_date=None):
    """
    Search bioRxiv or medRxiv for recent preprints matching config keywords.

    Args:
        config: dict loaded from research_interests.yaml
        days:   how many days of preprints to retrieve (default 7)
        server: "biorxiv" or "medrxiv"
        target_date: end date anchor (datetime or YYYY-MM-DD string)

    Returns:
        list of dicts: {id, title, authors, abstract, url, published_date, source}
    """
    start_date, end_date = _resolve_window(days=days, target_date=target_date)

    all_keywords = _all_domain_keywords(config)
    if not all_keywords:
        print(f"[{server}] No keywords in config", file=sys.stderr)
        return []

    source_label = "bioRxiv" if server == "biorxiv" else "medRxiv"
    papers = []
    seen_dois = set()
    cursor = 0

    while True:
        url = f"{BIORXIV_API}/{server}/{start_date}/{end_date}/{cursor}/json"
        data = _fetch_json(url)
        time.sleep(0.5)  # polite rate limiting

        if not data:
            break

        messages = data.get("messages", [])
        if messages and messages[0].get("status") == "ok":
            total = int(messages[0].get("total", 0))
        else:
            break

        collection = data.get("collection", [])
        if not collection:
            break

        for item in collection:
            doi = item.get("doi", "")
            title = item.get("title", "").strip()
            abstract = item.get("abstract", "").strip()

            if not doi or doi in seen_dois:
                continue

            # Filter by keyword relevance
            search_text = f"{title} {abstract}"
            matched = _keyword_matches(search_text, all_keywords)
            if not matched:
                continue

            seen_dois.add(doi)

            # Authors: biorxiv API gives "authors" as a single string
            authors_raw = item.get("authors", "")
            if isinstance(authors_raw, str):
                # Format: "LastName, F.; LastName2, F2.;"
                authors = [a.strip().rstrip(";") for a in authors_raw.split(";") if a.strip()]
            elif isinstance(authors_raw, list):
                authors = authors_raw
            else:
                authors = []

            # A missing/blank date must NOT default to today — that would
            # hand an undated record the maximum recency score. Leave it
            # None; the scoring pipeline treats None as recency 0.
            pub_date = item.get("date") or None

            # Construct URL
            if server == "biorxiv":
                url_paper = f"https://www.biorxiv.org/content/{doi}"
            else:
                url_paper = f"https://www.medrxiv.org/content/{doi}"

            papers.append({
                "id": doi,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "url": url_paper,
                "published_date": pub_date,
                "source": source_label,
                "matched_keywords": matched,
            })

        # Paginate: bioRxiv returns max 100 per request
        cursor += len(collection)
        if cursor >= total or len(collection) < 100:
            break

    return papers


def load_config(config_path=None):
    """Load research_interests.yaml; return config dict."""
    if config_path is None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _config_paths import resolve_config_path
            config_path = resolve_config_path(None)
        except ImportError:
            vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", "")
            config_path = os.path.join(vault_path, "99_System", "Config", "research_interests.yaml") if vault_path else None

    if not config_path or not os.path.exists(config_path):
        print(f"[bioRxiv] Config not found: {config_path}", file=sys.stderr)
        return {}

    if _HAS_YAML:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read()
        raw = raw.replace("${OBSIDIAN_VAULT_PATH}", os.environ.get("OBSIDIAN_VAULT_PATH", ""))
        return yaml.safe_load(raw) or {}
    else:
        print("[bioRxiv] PyYAML not available", file=sys.stderr)
        return {}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search bioRxiv/medRxiv for recent preprints")
    parser.add_argument("--config", default=None, help="Path to research_interests.yaml")
    parser.add_argument("--days", type=int, default=7, help="Days back to search (default: 7)")
    parser.add_argument("--target-date", default=None, help="End date anchor (YYYY-MM-DD)")
    parser.add_argument("--server", default="biorxiv", choices=["biorxiv", "medrxiv"],
                        help="Which server to search (default: biorxiv)")
    parser.add_argument("--top-n", type=int, default=10, help="Print top N results")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print("ERROR: Could not load config. Set OBSIDIAN_VAULT_PATH or pass --config")
        sys.exit(1)

    source_label = "bioRxiv" if args.server == "biorxiv" else "medRxiv"
    print(f"Searching {source_label} (last {args.days} days)...\n")
    papers = search_biorxiv(
        config,
        days=args.days,
        server=args.server,
        target_date=args.target_date,
    )
    print(f"Found {len(papers)} matching preprints\n")

    for i, p in enumerate(papers[:args.top_n], 1):
        print(f"{i}. [{p['source']}] {p['title']}")
        print(f"   DOI: {p['id']}")
        print(f"   Authors: {', '.join(p['authors'][:3])}{'...' if len(p['authors']) > 3 else ''}")
        print(f"   Date: {p['published_date']}")
        print(f"   Matched: {', '.join(p['matched_keywords'][:5])}")
        print(f"   URL: {p['url']}")
        print()
