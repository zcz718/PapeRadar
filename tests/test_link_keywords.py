"""Regression tests for `link_keywords.py`.

Pins four behaviours that today's misfiring run made obvious:

1. The overview block (everything before the first `### ` heading) is not
   auto-linked. Without this guard, a keyword that happens to be tagged on
   exactly one paper (e.g. `methylation` on a pituitary-tumor TE note) gets
   wikilinked into unrelated narrative text.
2. Self-link suppression: inside a paper-entry block, a keyword whose unique
   target is that same paper does not link. Without this guard, the FOXP2
   paper entry self-linked "FOXP2" four times.
3. First-occurrence-only per entry: a keyword that targets some *other* paper
   links at most once per entry block, not at every textual occurrence.
4. Biology-generic stopwords (`methylation`, `expression`, `cell`, …) are
   filtered globally, even when uniquely owned by one note. Today's
   `Methylation → pituitary-tumor` misfire is the canonical example.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import link_keywords  # noqa: E402
from common_words import COMMON_WORDS  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_linker(content: str, keyword_index: dict) -> str:
    """Run link_keywords_in_file end-to-end and return the rewritten content."""
    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "in.md"
        out_path = Path(tmp) / "out.md"
        in_path.write_text(content, encoding="utf-8")
        link_keywords.link_keywords_in_file(
            str(in_path), str(out_path), keyword_index
        )
        return out_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# behaviour: overview suppression
# ---------------------------------------------------------------------------

def test_overview_block_is_not_auto_linked():
    """Anything before the first `### ` heading must not gain wikilinks."""
    content = dedent(
        """\
        ## This Week's Overview

        - Trends: papers on FOXP2 and TP53 this week.

        ### [[Some_Paper.md|Some Paper]]
        - **Domain**: Genomics & Genome Biology

        Body mentions FOXP2 in passing.
        """
    )
    keyword_index = {"FOXP2": ["20_Research/Papers/Other/FOXP2_Note.md"]}

    out = _run_linker(content, keyword_index)

    overview = out.split("### ")[0]
    assert "[[" not in overview, (
        f"Overview must not gain wikilinks but got:\n{overview}"
    )


# ---------------------------------------------------------------------------
# behaviour: self-link suppression
# ---------------------------------------------------------------------------

def test_self_link_suppressed_when_keyword_targets_same_paper():
    """Inside a paper entry, a keyword pointing at that paper must not link."""
    content = dedent(
        """\
        ## Overview

        ### [[A_Paper_About_FOXP2.md|FOXP2 paper]]
        - **Report**: [[20_Research/Papers/Other/A_Paper_About_FOXP2|FOXP2 study]]

        FOXP2 is the focus of this entry.
        FOXP2 again, and a third FOXP2 mention.
        """
    )
    keyword_index = {
        # Note: the index lists the canonical full vault path; the entry's
        # heading uses a bare filename. The linker must match by basename.
        "FOXP2": ["20_Research/Papers/Other/A_Paper_About_FOXP2.md"]
    }

    out = _run_linker(content, keyword_index)

    # No wikilink may target the same paper from inside its own block.
    body = out.split("### ", 1)[1]
    assert "[[20_Research/Papers/Other/A_Paper_About_FOXP2" not in body or (
        body.count("[[20_Research/Papers/Other/A_Paper_About_FOXP2") <= 1
    ), (
        "Self-link to the entry's own paper should be suppressed; "
        f"found multiple. Body:\n{body}"
    )


def test_self_link_suppressed_via_basename_match():
    """The heading often uses bare filename while the index has a full path.

    `_is_same_paper` must compare by basename so the suppression still fires.
    """
    content = dedent(
        """\
        ## Overview

        ### [[GENE1_Note.|GENE1 paper]]
        Body mentions GENE1 twice. GENE1 again.
        """
    )
    keyword_index = {
        "GENE1": ["20_Research/Papers/Single-cell_Biology/GENE1_Note.md"]
    }

    out = _run_linker(content, keyword_index)
    body = out.split("### ", 1)[1]
    assert "[[20_Research/Papers/Single-cell_Biology/GENE1_Note" not in body, (
        f"Self-link via basename-only heading should still be suppressed. "
        f"Body:\n{body}"
    )


# ---------------------------------------------------------------------------
# behaviour: first-occurrence-only
# ---------------------------------------------------------------------------

def test_first_occurrence_only_per_block():
    """A keyword that targets a *different* paper links at most once per block."""
    content = dedent(
        """\
        ## Overview

        ### [[Current_Paper.md|Current paper]]

        FOXP2 first occurrence.
        FOXP2 second occurrence.
        FOXP2 third occurrence.
        """
    )
    keyword_index = {
        "FOXP2": ["20_Research/Papers/Other/FOXP2_Note.md"]
    }

    out = _run_linker(content, keyword_index)
    foxp2_links = out.count("[[20_Research/Papers/Other/FOXP2_Note")
    assert foxp2_links == 1, (
        f"Expected exactly 1 wikilink to FOXP2_Note in the block, got "
        f"{foxp2_links}. Output:\n{out}"
    )


# ---------------------------------------------------------------------------
# behaviour: biology-generic stopwords
# ---------------------------------------------------------------------------

def test_biology_generic_words_in_common_words():
    """The common_words layer covers single-owner biology generics."""
    expected = {
        'methylation', 'expression', 'transcription',
        'cell', 'gene', 'mutation', 'pathway', 'regulation',
        'mechanism', 'sequencing', 'protein',
    }
    missing = expected - {w.lower() for w in COMMON_WORDS}
    assert not missing, f"Missing biology generics from COMMON_WORDS: {missing}"
    # Must NOT filter biology-relevant terms that are also common in CS.
    must_not_filter = {'network', 'model'}
    leaked = must_not_filter & {w.lower() for w in COMMON_WORDS}
    assert not leaked, (
        f"Biology-relevant terms must not be filtered: {leaked}"
    )


def test_methylation_pituitary_misfire_does_not_recur():
    """The exact failure mode from 2026-05-05: 'Methylation' in narrative
    text must NOT link to a paper that uniquely owns a 'methylation' tag."""
    content = dedent(
        """\
        ## Overview

        This week's papers cover Genomics & Genome Biology and Gene Regulation & Epigenetics.

        ### [[GENE1_Note.|GENE1]]

        Methylation patterns at LINE-1 elements differ between cell states.
        """
    )
    # The example paper is the sole owner of the 'methylation' tag —
    # this is the exact shape of today's index.
    keyword_index = {
        "methylation": [
            "20_Research/Papers/Genomics_&_Genome_Biology/Example_Method_Paper.md"
        ]
    }

    out = _run_linker(content, keyword_index)
    assert "Example_Method_Paper" not in out, (
        f"'methylation' must not link to the example paper. Output:\n{out}"
    )


# ---------------------------------------------------------------------------
# behaviour: legitimate cross-paper links still happen
# ---------------------------------------------------------------------------

def test_legitimate_cross_paper_link_still_fires():
    """A specific keyword (acronym) that uniquely identifies another paper
    should still link from a different paper's entry — exactly once."""
    content = dedent(
        """\
        ## Overview

        ### [[Foo_Paper.md|Foo paper]]

        This work compares against METH1, a recent benchmark.
        """
    )
    keyword_index = {
        "METH1": [
            "20_Research/Papers/Single-cell_Biology/Example_Method_Paper.md"
        ]
    }

    out = _run_linker(content, keyword_index)
    # The linker preserves the index's path verbatim (including the .md
    # extension), so match that.
    assert "[[20_Research/Papers/Single-cell_Biology/Example_Method_Paper.md|METH1]]" in out, (
        f"Legitimate cross-paper METH1 link should fire. Output:\n{out}"
    )
