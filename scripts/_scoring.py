#!/usr/bin/env python3
"""Paper-scoring subsystem — relevance, recency, popularity, quality.

Extracted from `search_papers.py` (2026-05) per DEFERRED.md #5. The
scoring functions are the largest self-contained block in that file
(~230 LOC) and don't share state with arXiv / Semantic Scholar HTTP
client code, so they're the cleanest unit to lift first.

Public surface (re-exported from `search_papers.py` so existing test
imports `from search_papers import calculate_recency_score` still work):

  Constants:
    SCORE_MAX
    RELEVANCE_TITLE_KEYWORD_BOOST, RELEVANCE_SUMMARY_KEYWORD_BOOST,
    RELEVANCE_CATEGORY_MATCH_BOOST
    RECENCY_THRESHOLDS, RECENCY_DEFAULT
    WEIGHTS_NORMAL, WEIGHTS_HOT

  Functions:
    calculate_relevance_score(paper, domains, excluded_keywords)
    calculate_recency_score(published_date, reference_date=None)
    calculate_quality_score(summary)
    calculate_recommendation_score(rel, rec, pop, qual, is_hot=False)

Tuning happens by editing this file's constants in one place rather
than chasing them across the orchestrator.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring constants — edit weights here and nowhere else
# ---------------------------------------------------------------------------

# Maximum raw score for each dimension (normalisation baseline)
SCORE_MAX = 3.0

# Relevance score: bonus added when a keyword matches in title / abstract
RELEVANCE_TITLE_KEYWORD_BOOST = 0.5
RELEVANCE_SUMMARY_KEYWORD_BOOST = 0.3
RELEVANCE_CATEGORY_MATCH_BOOST = 1.0

# Recency thresholds (days since publication) → corresponding score
RECENCY_THRESHOLDS = [
    (30, 3.0),
    (90, 2.0),
    (180, 1.0),
]
RECENCY_DEFAULT = 0.0

# Combined recommendation score weights (typical papers).
# Relevance raised 0.40->0.50 (recency 0.20->0.15, popularity 0.30->0.25) after the
# cross-field QC found low-relevance papers floating into the top-N on freshness alone.
WEIGHTS_NORMAL = {
    'relevance': 0.50,
    'recency': 0.15,
    'popularity': 0.25,
    'quality': 0.10,
}
# High-impact papers: popularity-led, but relevance also raised 0.35->0.45 so a
# heavily-cited but off-topic paper can't dominate on citations alone.
WEIGHTS_HOT = {
    'relevance': 0.45,
    'recency': 0.10,
    'popularity': 0.35,
    'quality': 0.10,
}


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

def calculate_relevance_score(
    paper: Dict,
    domains: Dict,
    excluded_keywords: List[str],
    *,
    title_boost: Optional[float] = None,
    summary_boost: Optional[float] = None,
    category_boost: Optional[float] = None,
) -> Tuple[float, Optional[str], List[str]]:
    """Score a paper against the user's `research_domains` config.

    Returns `(score, best_domain, matched_keywords)`. A paper that
    matches any `excluded_keywords` token (substring in title/summary)
    returns `(0, None, [])` immediately — it's filtered out.

    Per-domain scoring sums:
      - +`RELEVANCE_TITLE_KEYWORD_BOOST` for each keyword found in title
        (word-boundary matched if keyword ≤5 chars or ALL CAPS, to
        avoid "ONT" matching inside "in-context").
      - +`RELEVANCE_SUMMARY_KEYWORD_BOOST` for each found in summary only.
      - +`RELEVANCE_CATEGORY_MATCH_BOOST` for each arXiv category hit.

    The highest-scoring domain becomes `best_domain`. Ties pick the
    first-encountered (dict iteration order — Python 3.7+ stable).
    """
    # Fall back to the tuned module defaults when no override is supplied.
    title_boost = RELEVANCE_TITLE_KEYWORD_BOOST if title_boost is None else title_boost
    summary_boost = RELEVANCE_SUMMARY_KEYWORD_BOOST if summary_boost is None else summary_boost
    category_boost = RELEVANCE_CATEGORY_MATCH_BOOST if category_boost is None else category_boost

    title = paper.get('title', '').lower()
    summary = paper.get('summary', '').lower() if 'summary' in paper else paper.get('abstract', '').lower()
    categories = set(paper.get('categories', []))

    # Check excluded keywords
    for keyword in excluded_keywords:
        if keyword.lower() in title or keyword.lower() in summary:
            return 0, None, []

    max_score = 0
    best_domain = None
    matched_keywords = []

    # Iterate over all configured domains
    for domain_name, domain_config in domains.items():
        score = 0
        domain_matched_keywords = []

        # Keyword matching
        keywords = domain_config.get('keywords', [])
        for keyword in keywords:
            keyword_lower = keyword.lower()
            # Use word-boundary matching for short (≤5 chars) or all-uppercase keywords
            # to prevent substring false positives (e.g. "ONT" inside "in-context").
            if len(keyword) <= 5 or keyword.isupper():
                _pat = re.compile(r'\b' + re.escape(keyword_lower) + r'\b')
                in_title = bool(_pat.search(title))
                in_summary = bool(_pat.search(summary))
            else:
                in_title = keyword_lower in title
                in_summary = keyword_lower in summary
            if in_title:
                score += title_boost
                domain_matched_keywords.append(keyword)
            elif in_summary:
                score += summary_boost
                domain_matched_keywords.append(keyword)

        # Category matching
        domain_categories = domain_config.get('arxiv_categories', [])
        for cat in domain_categories:
            if cat in categories:
                score += category_boost
                domain_matched_keywords.append(cat)

        if score > max_score:
            max_score = score
            best_domain = domain_name
            matched_keywords = domain_matched_keywords

    return max_score, best_domain, matched_keywords


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def _resolve_reference_now(
    published_date: Optional[datetime],
    reference_date: Optional[datetime] = None,
) -> datetime:
    """Align the scoring reference date with the paper timestamp if needed.

    Handles tz-aware vs tz-naive mismatches: if the paper has tzinfo and
    the reference doesn't, normalise the reference to the paper's
    timezone (and vice versa). Pure datetime arithmetic that follows
    raises TypeError on mismatched aware/naive datetimes; this helper
    smooths over that.
    """
    if reference_date is None:
        return datetime.now(published_date.tzinfo) if published_date and published_date.tzinfo else datetime.now()

    ref = reference_date
    if published_date and published_date.tzinfo and ref.tzinfo is None:
        return ref.replace(tzinfo=published_date.tzinfo)
    if published_date and not published_date.tzinfo and ref.tzinfo is not None:
        return ref.replace(tzinfo=None)
    return ref


def calculate_recency_score(
    published_date: Optional[datetime],
    reference_date: Optional[datetime] = None,
    *,
    thresholds: Optional[List[Tuple[int, float]]] = None,
) -> float:
    """Bucket the days-since-publish into a recency score 0..SCORE_MAX.

    Buckets default to `RECENCY_THRESHOLDS` (≤30 d → 3, ≤90 d → 2, ≤180 d → 1,
    else 0) but may be overridden per-run. `reference_date` lets tests pin
    "now" to a fixed timestamp.
    """
    if published_date is None:
        return 0

    buckets = RECENCY_THRESHOLDS if thresholds is None else thresholds
    now = _resolve_reference_now(published_date, reference_date)
    days_diff = (now - published_date).days

    for max_days, score in buckets:
        if days_diff <= max_days:
            return score
    return RECENCY_DEFAULT


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

def calculate_quality_score(summary: str) -> float:
    """Heuristic quality score from abstract vocabulary.

    Treats both ML and biology vocabulary as positive signals: we don't
    try to detect "what kind of paper" first. Strong innovation
    vocabulary (breakthrough / first / SOTA / discover) outweighs weak
    (novel / propose / new approach). Method names (CRISPR / single-
    cell / hi-c / framework / pipeline) and quantitative result markers
    (fold change / FDR / p<0.05 / outperforms) add further weight.

    Returns 0..SCORE_MAX. Tunable by editing the four word-list
    constants in this function — kept as locals (not module-level) so
    each function stays self-documenting.
    """
    if not summary:
        return 0.0
    score = 0.0
    summary_lower = summary.lower()

    # Quality cues. The lists co-exist for ML and biology papers — whichever
    # set fires gives the paper credit. We don't try to detect "what kind of
    # paper is this" first; treating both vocabularies as positive signals
    # works well in practice and keeps the function single-purpose.
    strong_innovation = [
        # Universal. NOTE: no bare 'first' — it substring-matches
        # "first author"/"for the first time" and also double-counts the
        # specific phrases below. The phrases stay; the bare word goes.
        'breakthrough', 'pioneering', 'first to demonstrate',
        # ML. 'surpass'/'outperform' deliberately live only in
        # quantitative_indicators below (as 'surpasses'/'outperforms') so
        # one comparison verb doesn't score in two lists at once.
        'state-of-the-art', 'sota',
        # Biology
        'discover', 'uncover', 'first evidence', 'unveil', 'establish',
    ]
    weak_innovation = [
        'novel', 'propose', 'introduce', 'new approach', 'new method',
        'innovative', 'reveal', 'identify',
    ]
    method_indicators = [
        # Universal / methodological vocabulary
        'framework', 'architecture', 'algorithm', 'pipeline', 'end-to-end',
        # Biology methods that signal a real experimental contribution
        'crispr knockout', 'crispr-cas9', 'knockin', 'knock-in',
        'rescue experiment', 'in vivo', 'in vitro',
        'single-cell', 'single cell', 'long-read', 'spatial transcriptomics',
        'chip-seq', 'atac-seq', 'cut&run', 'cut&tag', 'hi-c',
    ]
    quantitative_indicators = [
        # ML
        'outperforms', 'improves by', 'achieves', 'accuracy',
        'f1', 'bleu', 'rouge', 'beats', 'surpasses',
        # Biology — fold change / DE statistics / dose-response language
        'fold change', 'log2fc', 'log2 fold', 'fdr', 'p < 0.', 'p<0.',
        'differential expression', 'differentially expressed',
        'significantly increased', 'significantly decreased',
        'effect size',
    ]
    experiment_indicators = [
        # ML
        'experiment', 'evaluation', 'benchmark', 'ablation',
        'baseline', 'comparison',
        # Biology — orthogonal validation / replication / controls language
        'orthogonal validation', 'biological replicate', 'technical replicate',
        'control experiment', 'rescue', 'multiple cell lines',
        'multiple donors',
    ]

    strong_count = sum(1 for ind in strong_innovation if ind in summary_lower)
    if strong_count >= 2:
        score += 1.0
    elif strong_count == 1:
        score += 0.7
    else:
        weak_count = sum(1 for ind in weak_innovation if ind in summary_lower)
        if weak_count > 0:
            score += 0.3

    if any(ind in summary_lower for ind in method_indicators):
        score += 0.5

    if any(ind in summary_lower for ind in quantitative_indicators):
        score += 0.8
    elif any(ind in summary_lower for ind in experiment_indicators):
        score += 0.4

    return min(score, SCORE_MAX)


# ---------------------------------------------------------------------------
# Final recommendation score (weighted blend)
# ---------------------------------------------------------------------------

def calculate_recommendation_score(
    relevance_score: float,
    recency_score: float,
    popularity_score: float,
    quality_score: float,
    is_hot_paper: bool = False,
    *,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """Weighted blend of the four sub-scores → 0..10 final recommendation.

    `WEIGHTS_NORMAL` weights are tuned for the typical preprint
    discovery case; `WEIGHTS_HOT` is used when the paper carries a
    high Semantic Scholar citation count (the orchestrator passes
    `is_hot_paper=True` for those) — shifts weight from recency to
    popularity since high-citation papers tend not to be brand-new.

    Sub-scores are normalised from `SCORE_MAX` to 10 before weighting,
    so a 1.5 raw relevance becomes 5.0 normalised, then weight × 5.0.
    Result is rounded to 2 decimal places for display.
    """
    scores = {
        'relevance': relevance_score,
        'recency': recency_score,
        'popularity': popularity_score,
        'quality': quality_score,
    }
    # Normalise each sub-score to 0–10
    # Clamp each normalised sub-score at 10. `relevance` (and, depending on
    # the caller, `popularity`) can exceed SCORE_MAX in raw form; without
    # this clamp the weighted blend could break the documented 0–10 ceiling.
    # recency and quality are already ≤ SCORE_MAX so the clamp is a no-op
    # for them. Clamping here (rather than inside calculate_relevance_score)
    # preserves the raw relevance that search_papers.py's keyword_only_score
    # gate subtracts category boosts from.
    normalized = {k: min((v / SCORE_MAX) * 10, 10.0) for k, v in scores.items()}

    if weights is None:
        weights = WEIGHTS_HOT if is_hot_paper else WEIGHTS_NORMAL
    final_score = sum(normalized[k] * weights.get(k, 0.0) for k in scores)

    return round(final_score, 2)
