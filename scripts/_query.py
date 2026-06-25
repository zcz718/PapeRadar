#!/usr/bin/env python3
"""Shared keyword-collection helper for paperadar source adapters.

OpenAlex, Crossref, and CORE each build their query string from the same raw
material: every keyword across all of the config's ``research_domains``, in
order, case-insensitively de-duplicated, with too-short noise terms dropped.
They differ only in how they *format* those terms (OR-joined and quoted /
space-joined / wrapped in an Elasticsearch group), so the collection step lives
here and each adapter formats the returned list itself.
"""
from __future__ import annotations


def collect_keyword_terms(config, max_terms=None, min_len=3):
    """Ordered, de-duplicated keyword terms from every ``research_domains`` entry.

    Terms shorter than ``min_len`` characters are dropped (filters noise such as
    "ab" or "3D"); de-duplication is case-insensitive but the first-seen casing
    is preserved. Collection stops once ``max_terms`` unique terms are gathered
    (``None`` = no cap). Returns ``list[str]``.
    """
    seen, terms = set(), []
    for domain in (config.get("research_domains") or {}).values():
        for kw in (domain.get("keywords") or []):
            kw = (kw or "").strip()
            if len(kw) < min_len:
                continue
            key = kw.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(kw)
            if max_terms is not None and len(terms) >= max_terms:
                return terms
    return terms
