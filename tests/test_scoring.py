"""Unit tests for the scoring core (`_scoring.py`) and the inclusion gate
(`search_arxiv.filter_and_score_papers`), plus the precision fixes from the
cross-field QC: a stronger gate (>=2 keyword matches OR >=1 compound keyword),
re-weighted ranking, conservative bio-source auto-detection, in-script arXiv
category derivation, and CORE demoted to opt-in.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import search_arxiv  # noqa: E402
import _scoring  # noqa: E402

TARGET = datetime(2026, 6, 24)


def _paper(title, abstract="", date=datetime(2026, 6, 22), source="arxiv"):
    return {
        "id": "x", "title": title, "abstract": abstract, "authors": ["A"],
        "url": "http://x", "published_date": date, "source": source,
    }


def _cfg(domains, excluded=None):
    return {"research_domains": domains, "excluded_keywords": excluded or []}


def _score(papers, cfg):
    return search_arxiv.filter_and_score_papers(
        [dict(p) for p in papers], cfg, target_date=TARGET)


# --------------------------------------------------------------------------
# relevance primitives
# --------------------------------------------------------------------------
def test_relevance_title_beats_abstract():
    dom = {"D": {"keywords": ["transposon"], "arxiv_categories": []}}
    t = _scoring.calculate_relevance_score({"title": "transposon biology", "summary": ""}, dom, [])
    a = _scoring.calculate_relevance_score({"title": "x", "summary": "a transposon here"}, dom, [])
    assert t[0] > a[0]


def test_relevance_excluded_keyword_zeroes():
    dom = {"D": {"keywords": ["transposon"], "arxiv_categories": []}}
    assert _scoring.calculate_relevance_score(
        {"title": "transposon review", "summary": ""}, dom, ["review"]) == (0, None, [])


def test_short_keyword_uses_word_boundary():
    dom = {"D": {"keywords": ["L1"], "arxiv_categories": []}}
    assert _scoring.calculate_relevance_score({"title": "HTML1 parsing", "summary": ""}, dom, [])[0] == 0
    assert _scoring.calculate_relevance_score({"title": "L1 element", "summary": ""}, dom, [])[0] > 0


# --------------------------------------------------------------------------
# the inclusion gate: >=2 keyword matches OR >=1 compound keyword
# --------------------------------------------------------------------------
def test_gate_rejects_single_ambiguous_word():
    # ASR paper: only the ambiguous single word "alignment" matches -> reject
    cfg = _cfg({"AI": {"keywords": ["alignment", "RLHF", "reward model"], "arxiv_categories": []}})
    assert _score([_paper("InterAligner: ASR encoder alignment objectives",
                          "We study CTC alignment for speech recognition.")], cfg) == []


def test_gate_accepts_two_keyword_matches():
    cfg = _cfg({"AI": {"keywords": ["alignment", "RLHF", "reward model"], "arxiv_categories": []}})
    assert len(_score([_paper("RLHF reward model alignment for safety",
                              "We align a reward model with RLHF.")], cfg)) == 1


def test_gate_accepts_single_space_phrase():
    cfg = _cfg({"Bio": {"keywords": ["transposable element", "Nanopore"], "arxiv_categories": []}})
    assert len(_score([_paper("A transposable element atlas",
                              "We map transposable element loci.")], cfg)) == 1


def test_gate_accepts_single_hyphenated_keyword():
    # compound (hyphen) keyword qualifies alone -> preserves bio recall & existing behavior
    cfg = _cfg({"Bio": {"keywords": ["single-cell"], "arxiv_categories": []}})
    assert len(_score([_paper("A single-cell atlas", "single-cell profiling.")], cfg)) == 1


def test_gate_rejects_single_bare_token():
    cfg = _cfg({"Bio": {"keywords": ["transposable element", "Nanopore"], "arxiv_categories": []}})
    assert _score([_paper("Nanopore current signal denoising",
                          "A deep model for ionic current.")], cfg) == []


# --------------------------------------------------------------------------
# regression fixtures: the exact QC false positives must now be rejected
# --------------------------------------------------------------------------
def test_regression_sensor_localization_math_rejected():
    cfg = _cfg({"Hum": {"keywords": ["localization", "humanitarian", "refugee", "forced displacement"],
                        "arxiv_categories": []}})
    assert _score([_paper("Sensor network localization has a benign landscape",
                          "Low-dimensional relaxation for sensor localization optimization.")], cfg) == []


def test_regression_hpc_simulation_rejected():
    cfg = _cfg({"AIsci": {"keywords": ["simulation", "neural operator", "physics-informed", "surrogate model"],
                          "arxiv_categories": []}})
    assert _score([_paper("GPU-accelerated electrostatic boundary element solver",
                          "A fast simulation of the boundary element method.")], cfg) == []


# --------------------------------------------------------------------------
# re-weighted ranking (default-on)
# --------------------------------------------------------------------------
def test_weights_normal_reweighted():
    w = _scoring.WEIGHTS_NORMAL
    assert (w["relevance"], w["recency"], w["popularity"], w["quality"]) == (0.50, 0.15, 0.25, 0.10)
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_weights_hot_reweighted():
    w = _scoring.WEIGHTS_HOT
    assert w["relevance"] == 0.45
    assert abs(sum(w.values()) - 1.0) < 1e-9


# --------------------------------------------------------------------------
# P4: conservative bio_sources auto
# --------------------------------------------------------------------------
def test_bio_unknown_value_is_off():
    assert search_arxiv._bio_sources_enabled({"bio_sources": "garbage"}) is False
    assert search_arxiv._bio_sources_enabled({"bio_sources": None}) is False
    assert search_arxiv._bio_sources_enabled({}) is False


def test_bio_explicit_true_overrides():
    assert search_arxiv._bio_sources_enabled({"bio_sources": True}) is True


def test_bio_qbio_category_enables():
    assert search_arxiv._bio_sources_enabled(
        {"bio_sources": "auto", "research_domains": {"D": {"arxiv_categories": ["q-bio.GN"], "keywords": []}}}) is True


def test_bio_biomed_keyword_enables():
    assert search_arxiv._bio_sources_enabled(
        {"research_domains": {"D": {"arxiv_categories": [], "keywords": ["DNA methylation"]}}}) is True


def test_bio_nonbio_config_off():
    assert search_arxiv._bio_sources_enabled(
        {"research_domains": {"D": {"arxiv_categories": ["econ.GN"], "keywords": ["cash transfer", "famine"]}}}) is False


# --------------------------------------------------------------------------
# P3: arXiv categories derived from config
# --------------------------------------------------------------------------
def test_derive_categories_union_order_preserving():
    cfg = {"research_domains": {
        "A": {"arxiv_categories": ["cs.LG", "cs.AI"]},
        "B": {"arxiv_categories": ["cs.AI", "stat.ML"]}}}
    assert search_arxiv._derive_arxiv_categories_from_config(cfg) == ["cs.LG", "cs.AI", "stat.ML"]


def test_derive_categories_empty():
    assert search_arxiv._derive_arxiv_categories_from_config({}) == []


# --------------------------------------------------------------------------
# P5: CORE demoted to opt-in
# --------------------------------------------------------------------------
def _core_spec():
    return next(s for s in search_arxiv._EXTRA_SOURCES if s["name"] == "CORE")


def test_core_default_off():
    assert search_arxiv._extra_source_enabled(_core_spec(), {}) is False


def test_core_explicit_true_enables():
    assert search_arxiv._extra_source_enabled(_core_spec(), {"core": {"enabled": True}}) is True


def test_openalex_default_remains_auto():
    spec = next(s for s in search_arxiv._EXTRA_SOURCES if s["name"] == "OpenAlex")
    assert spec.get("default", "auto") == "auto"


def test_split_pool_candidates_and_topn_fallback():
    papers = [{"id": str(i)} for i in range(30)]
    cands, top = search_arxiv._split_pool(papers, pool_size=25, top_n=10)
    assert len(cands) == 25
    assert [p["id"] for p in top] == [str(i) for i in range(10)]
    assert top == cands[:10]  # fallback is the head of the pool


def test_split_pool_pool_size_floored_to_top_n():
    papers = [{"id": str(i)} for i in range(30)]
    cands, top = search_arxiv._split_pool(papers, pool_size=5, top_n=10)
    assert len(cands) == 10  # pool can't be smaller than top_n
    assert len(top) == 10
