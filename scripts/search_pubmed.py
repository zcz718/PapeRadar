#!/usr/bin/env python3
"""
search_pubmed.py — Query PubMed via NCBI E-utilities for recent biology papers.

Returns results in the same dict format as search_arxiv.py:
  {id, title, authors, abstract, url, published_date, source}

Rate limits:
  - Without NCBI_API_KEY: max 3 req/s  → sleep 0.34s between requests
  - With NCBI_API_KEY:    max 10 req/s → sleep 0.1s between requests
"""

import os
import sys
import time
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from difflib import SequenceMatcher

try:
    import requests
    _USE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.parse
    _USE_REQUESTS = False

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


# Resolve NCBI_API_KEY from ~/.zshrc / launchctl on import (one-shot,
# cheap), so the 0.1s vs 0.34s rate-limit choice below picks the faster
# path when the user has a key exported in their interactive shell but
# the script is invoked from a non-interactive subprocess (Claude Code's
# default). Without this, `_get_sleep_time()` would always pick the
# slower 0.34s path even when a key is available.
def _resolve_ncbi_env():
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _env_resolve import load_env_from_user_shell
        load_env_from_user_shell(("NCBI_API_KEY",))
    except ImportError:
        pass


_resolve_ncbi_env()


def _get_sleep_time():
    return 0.1 if os.environ.get("NCBI_API_KEY") else 0.34


def _build_url(endpoint, params):
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    if _USE_REQUESTS:
        import requests as req
        return req.Request("GET", f"{EUTILS_BASE}/{endpoint}", params=params).prepare().url
    else:
        import urllib.parse
        return f"{EUTILS_BASE}/{endpoint}?" + urllib.parse.urlencode(params)


def _fetch(url, retries=3):
    """Fetch URL, return response text or None on failure."""
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
                return resp.text
            else:
                import urllib.request
                with urllib.request.urlopen(url, timeout=20) as r:
                    return r.read().decode("utf-8")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"[PubMed] fetch error: {e}", file=sys.stderr)
    # Fail loud on a sustained 429 (set NCBI_API_KEY to raise the limit
    # from 3 to 10 req/s); otherwise this returns None and the search
    # quietly drops PubMed coverage for the run.
    if throttled:
        print(f"[PubMed] HTTP 429 rate limit never cleared after {retries} "
              f"attempts; PubMed results are incomplete. Consider setting "
              f"NCBI_API_KEY.", file=sys.stderr)
    return None


def _coerce_target_date(target_date=None):
    """Return a datetime anchor for PubMed search/scoring windows."""
    if target_date is None:
        return datetime.now()
    if isinstance(target_date, datetime):
        return target_date
    return datetime.strptime(str(target_date), "%Y-%m-%d")


def _resolve_search_window(days=90, target_date=None):
    """Return YYYY/MM/DD start/end strings for PubMed E-utilities."""
    end_dt = _coerce_target_date(target_date)
    end_date = end_dt.strftime("%Y/%m/%d")
    start_date = (end_dt - timedelta(days=days)).strftime("%Y/%m/%d")
    return start_date, end_date


def _esearch(query, days=90, retmax=200, target_date=None):
    """Search PubMed; return list of PMIDs."""
    start_date, end_date = _resolve_search_window(days=days, target_date=target_date)
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": str(retmax),
        "mindate": start_date,
        "maxdate": end_date,
        "datetype": "pdat",
        "retmode": "json",
    }
    url = _build_url("esearch.fcgi", params)
    time.sleep(_get_sleep_time())
    text = _fetch(url)
    if not text:
        return []
    try:
        data = json.loads(text)
        return data.get("esearchresult", {}).get("idlist", [])
    except json.JSONDecodeError:
        return []


def _efetch_batch(pmids, fallback_date=None):
    """Fetch article metadata for a list of PMIDs; return list of parsed dicts."""
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
    }
    url = _build_url("efetch.fcgi", params)
    time.sleep(_get_sleep_time())
    xml_text = _fetch(url)
    if not xml_text:
        return []
    return _parse_pubmed_xml(xml_text, fallback_date=fallback_date)


def _parse_pubmed_xml(xml_text, fallback_date=None):
    """Parse PubMed XML efetch response into list of paper dicts."""
    papers = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[PubMed] XML parse error: {e}", file=sys.stderr)
        return []

    for article in root.findall(".//PubmedArticle"):
        try:
            # PMID
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text.strip() if pmid_el is not None else None
            if not pmid:
                continue

            # Title
            title_el = article.find(".//ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""

            # Abstract
            abstract_parts = []
            for ab in article.findall(".//AbstractText"):
                label = ab.get("Label")
                text = "".join(ab.itertext()).strip()
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            # Authors
            authors = []
            for author in article.findall(".//Author"):
                last = author.findtext("LastName", "")
                fore = author.findtext("ForeName", "")
                name = f"{fore} {last}".strip()
                if name:
                    authors.append(name)

            # Published date
            pub_date = _extract_pub_date(article, fallback_date=fallback_date)

            # Journal name (full title preferred, fall back to ISO abbreviation)
            journal = ""
            j_el = article.find(".//Journal/Title")
            if j_el is not None and j_el.text:
                journal = j_el.text.strip()
            else:
                j_el = article.find(".//Journal/ISOAbbreviation")
                if j_el is not None and j_el.text:
                    journal = j_el.text.strip()

            # DOI — preferred from <ELocationID EIdType="doi">, fallback to
            # <ArticleId IdType="doi"> in PubmedData. Carried through so that
            # downstream OA-fulltext lookups (Unpaywall, EuropePMC) and
            # publisher-PDF filename matchers have a stable key.
            doi = ""
            for el in article.findall(".//ELocationID[@EIdType='doi']"):
                if el.text:
                    doi = el.text.strip()
                    break
            if not doi:
                # PubmedData/ArticleIdList path (alternative location)
                for el in article.findall(".//ArticleId[@IdType='doi']"):
                    if el.text:
                        doi = el.text.strip()
                        break

            # Use the centralized formatter so any future change to the
            # PMID string format (e.g. "PMID:" → "ncbi:pubmed:") happens
            # in one place. See `scripts/_id_parser.py`.
            try:
                from _id_parser import format_pmid
            except ImportError:
                _here = os.path.dirname(os.path.abspath(__file__))
                if _here not in sys.path:
                    sys.path.insert(0, _here)
                from _id_parser import format_pmid
            papers.append({
                "id": format_pmid(pmid),
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "published_date": pub_date,
                "source": "PubMed",
                "journal": journal,
                "doi": doi,
            })
        except Exception as e:
            print(f"[PubMed] parse article error: {e}", file=sys.stderr)
            continue

    return papers


def _extract_pub_date(article, fallback_date=None):
    """Extract publication date from a PubmedArticle element."""
    # Try PubDate in Journal
    for pub_date in article.findall(".//PubDate"):
        year = pub_date.findtext("Year")
        month = pub_date.findtext("Month", "01")
        day = pub_date.findtext("Day", "01")
        if year:
            try:
                # Month may be abbreviated name like "Jan"
                month_str = str(month).zfill(2)
                try:
                    dt = datetime.strptime(f"{year}-{month_str}-{day.zfill(2)}", "%Y-%m-%d")
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    dt = datetime.strptime(f"{year}-{month}", "%Y-%b")
                    return dt.strftime("%Y-%m-01")
            except Exception:
                return f"{year}-01-01"
    return _coerce_target_date(fallback_date).strftime("%Y-%m-%d")


def _title_similarity(a, b):
    """Fuzzy title similarity 0-1."""
    a_norm = re.sub(r"[^\w\s]", "", a.lower())
    b_norm = re.sub(r"[^\w\s]", "", b.lower())
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _deduplicate(papers, existing_titles, threshold=0.85):
    """Remove papers whose titles are too similar to existing_titles."""
    unique = []
    seen_titles = list(existing_titles)
    for paper in papers:
        title = paper.get("title", "")
        duplicate = any(_title_similarity(title, t) >= threshold for t in seen_titles)
        if not duplicate:
            unique.append(paper)
            seen_titles.append(title)
    return unique


def load_config(config_path=None):
    """Load research_interests.yaml; return config dict."""
    if config_path is None:
        # Use shared resolver: --config (None here) → $OBSIDIAN_VAULT_PATH/... → ~/.config/start-my-week/
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _config_paths import resolve_config_path
            config_path = resolve_config_path(None)
        except ImportError:
            vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", "")
            config_path = os.path.join(vault_path, "99_System", "Config", "research_interests.yaml") if vault_path else None

    if not config_path or not os.path.exists(config_path):
        print(f"[PubMed] Config not found: {config_path}", file=sys.stderr)
        return {}

    if _HAS_YAML:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read()
        # Expand ${OBSIDIAN_VAULT_PATH} in the YAML
        raw = raw.replace("${OBSIDIAN_VAULT_PATH}", os.environ.get("OBSIDIAN_VAULT_PATH", ""))
        return yaml.safe_load(raw) or {}
    else:
        print("[PubMed] PyYAML not available; cannot load config", file=sys.stderr)
        return {}


def search_pubmed(config, days=7, target_date=None):
    """
    Search PubMed for recent papers matching research domains in config.

    Args:
        config: dict loaded from research_interests.yaml
        days:   how many days back to search (default 7)
        target_date: end date anchor (datetime or YYYY-MM-DD string)

    Returns:
        list of dicts: {id, title, authors, abstract, url, published_date, source}
    """
    domains = config.get("research_domains", {})
    if not domains:
        print("[PubMed] No research_domains in config", file=sys.stderr)
        return []

    # Optional journal filter — restrict results to top-tier journals
    journals = config.get("prioritize_journals", []) or []
    journal_clause = ""
    if journals:
        journal_clause = " AND (" + " OR ".join(f'"{j}"[Journal]' for j in journals) + ")"
        print(f"[PubMed] Restricting to {len(journals)} prioritized journals", file=sys.stderr)

    all_pmids = set()
    domain_pmids = {}  # pmid -> domain name
    fallback_date = _coerce_target_date(target_date)

    for domain_name, domain_cfg in domains.items():
        pubmed_terms = domain_cfg.get("pubmed_terms", [])
        keywords = domain_cfg.get("keywords", [])

        # Build queries: use pubmed_terms if available, otherwise fall back to keywords
        queries = pubmed_terms if pubmed_terms else keywords[:5]

        domain_found = set()
        for query in queries:
            # Wrap in parens and append the journal filter (if any)
            full_query = f"({query}){journal_clause}" if journal_clause else query
            pmids = _esearch(full_query, days=days, target_date=target_date)
            new_pmids = [p for p in pmids if p not in all_pmids]
            domain_found.update(new_pmids)
            all_pmids.update(new_pmids)

        domain_pmids[domain_name] = list(domain_found)

    if not all_pmids:
        print("[PubMed] No PMIDs found", file=sys.stderr)
        return []

    # Fetch in batches of 200
    all_papers = []
    pmid_list = list(all_pmids)
    batch_size = 200
    for i in range(0, len(pmid_list), batch_size):
        batch = pmid_list[i:i + batch_size]
        papers = _efetch_batch(batch, fallback_date=fallback_date)
        all_papers.extend(papers)

    # Deduplicate by PMID
    seen_ids = set()
    unique_papers = []
    for p in all_papers:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            unique_papers.append(p)

    # Cross-deduplicate by title similarity
    unique_papers = _deduplicate(unique_papers, [], threshold=0.85)

    return unique_papers


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search PubMed for recent biology papers")
    parser.add_argument("--config", default=None, help="Path to research_interests.yaml")
    parser.add_argument("--days", type=int, default=7, help="Days back to search (default: 7)")
    parser.add_argument("--target-date", default=None, help="End date anchor (YYYY-MM-DD)")
    parser.add_argument("--top-n", type=int, default=10, help="Print top N results")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print("ERROR: Could not load config. Set OBSIDIAN_VAULT_PATH or pass --config")
        sys.exit(1)

    print(f"Searching PubMed (last {args.days} days)...\n")
    papers = search_pubmed(config, days=args.days, target_date=args.target_date)
    print(f"Found {len(papers)} papers\n")

    for i, p in enumerate(papers[:args.top_n], 1):
        print(f"{i}. [{p['source']}] {p['title']}")
        print(f"   ID: {p['id']}")
        print(f"   Authors: {', '.join(p['authors'][:3])}{'...' if len(p['authors']) > 3 else ''}")
        print(f"   Date: {p['published_date']}")
        print(f"   URL: {p['url']}")
        print()
