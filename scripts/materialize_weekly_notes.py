#!/usr/bin/env python3
"""Materialize weekly recommendation notes for Obsidian or standalone output.

`search_arxiv.py` creates the ranked `arxiv_filtered.json`; this script turns
that JSON into durable Markdown knowledge artifacts:

* a weekly index note
* one per-paper literature-note scaffold per recommended paper

The paper notes are deliberately idempotent. Existing notes are not overwritten
unless `--overwrite-paper-notes` is passed, so later human review or agent
annotations stay intact.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency preflight handles this
    raise SystemExit("PyYAML is required: pip install -r requirements.txt") from exc

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _config_paths import resolve_config_path  # noqa: E402
from _id_parser import parse_arxiv_id, parse_paper_id  # noqa: E402
from search_arxiv import title_to_note_filename  # noqa: E402

try:
    from save_to_zotero import (  # noqa: E402
        _find_local_pdf,
        _paper_arxiv_id,
        _paper_doi,
        _paper_journal,
        _paper_pdf_url,
        _paper_pmid,
    )
except Exception:  # pragma: no cover - import should be stable, fallback below
    _find_local_pdf = None

    def _paper_doi(paper: dict) -> str:
        doi = paper.get("doi") or (paper.get("externalIds") or {}).get("DOI") or ""
        if not doi and str(paper.get("id", "")).startswith("10."):
            doi = paper.get("id", "")
        return str(doi).strip()

    def _paper_pmid(paper: dict) -> str:
        pmid, _doi = parse_paper_id(str(paper.get("id", "")))
        return pmid or str(paper.get("pmid") or "").strip()

    def _paper_arxiv_id(paper: dict) -> str:
        return (
            paper.get("arxiv_id")
            or paper.get("arxivId")
            or parse_arxiv_id(str(paper.get("id", "")))
            or ""
        )

    def _paper_journal(paper: dict) -> str:
        return str(paper.get("journal") or paper.get("venue") or "").strip()

    def _paper_pdf_url(paper: dict) -> str:
        if paper.get("pdf_url"):
            return str(paper["pdf_url"]).strip()
        arxiv_id = _paper_arxiv_id(paper)
        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"
        doi = _paper_doi(paper)
        source = str(paper.get("source") or "").lower()
        if doi and source in {"biorxiv", "medrxiv"}:
            return f"https://www.{source}.org/content/{doi}.full.pdf"
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _read_config(explicit: str | None) -> dict[str, Any]:
    path = resolve_config_path(explicit)
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _config_output(config: dict[str, Any]) -> dict[str, Any]:
    output = config.get("output")
    return output if isinstance(output, dict) else {}


def _resolve_mode(config: dict[str, Any], explicit_mode: str | None) -> str:
    if explicit_mode:
        return explicit_mode
    return str(_config_output(config).get("mode") or "standalone").lower()


def _resolve_vault(config: dict[str, Any], explicit_vault: str | None) -> Path | None:
    raw = explicit_vault or os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if not raw:
        raw = str(
            (_config_output(config).get("obsidian") or {}).get("vault_path") or ""
        )
    return Path(raw).expanduser() if raw else None


def _resolve_daily_dir(config: dict[str, Any]) -> str:
    return str((_config_output(config).get("obsidian") or {}).get("daily_dir") or "10_Daily")


def _resolve_papers_dir(config: dict[str, Any]) -> str:
    return str(
        (_config_output(config).get("obsidian") or {}).get("papers_dir")
        or "20_Research/Papers"
    )


def _resolve_standalone_dir(
    config: dict[str, Any], explicit_output_dir: str | None
) -> Path:
    raw = explicit_output_dir or str(
        (_config_output(config).get("standalone") or {}).get("output_dir")
        or "~/start-my-week-output"
    )
    return Path(raw).expanduser()


def _safe_domain_dir(domain: str) -> str:
    domain = (domain or "Other").strip("/\\").replace("..", "")
    if not domain:
        domain = "Other"
    return re.sub(r'[ /\\:*?"<>|]+', "_", domain).strip("_") or "Other"


def _note_filename(paper: dict[str, Any]) -> str:
    raw = paper.get("note_filename") or title_to_note_filename(paper.get("title", ""))
    raw = re.sub(r'[ /\\:*?"<>|]+', "_", str(raw)).strip("_. ")
    return raw or "Untitled_Paper"


def _paper_note_path(base_dir: Path, paper: dict[str, Any]) -> Path:
    domain_dir = _safe_domain_dir(str(paper.get("matched_domain") or "Other"))
    return base_dir / domain_dir / f"{_note_filename(paper)}.md"


def _legacy_paper_note_path(base_dir: Path, paper: dict[str, Any]) -> Path | None:
    raw = paper.get("note_filename")
    if raw:
        raw = str(raw).strip()
    else:
        # Legacy search/generate behavior stripped underscores only, leaving a
        # title-ending period to produce paths like `Title..md`.
        raw = re.sub(r'[ /\\:*?"<>|]+', "_", str(paper.get("title") or "")).strip("_")
    if not raw or raw == _note_filename(paper):
        return None
    domain_dir = _safe_domain_dir(str(paper.get("matched_domain") or "Other"))
    return base_dir / domain_dir / f"{raw}.md"


def _yaml_string(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def _yaml_list(values: list[str]) -> str:
    return json.dumps([str(v) for v in values if str(v).strip()], ensure_ascii=False)


def _authors(paper: dict[str, Any]) -> list[str]:
    authors = paper.get("authors") or []
    out = []
    for author in authors:
        if isinstance(author, dict):
            name = str(author.get("name") or "").strip()
        else:
            name = str(author).strip()
        if name:
            out.append(name)
    return out


def _authors_line(paper: dict[str, Any], limit: int = 4) -> str:
    authors = _authors(paper)
    if not authors:
        return "Unknown"
    if len(authors) > limit:
        return ", ".join(authors[:limit]) + ", et al."
    return ", ".join(authors)


def _score(paper: dict[str, Any]) -> str:
    scores = paper.get("scores") if isinstance(paper.get("scores"), dict) else {}
    score = scores.get("recommendation")
    if score in (None, ""):
        return "n/a"
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return str(score)


def _summary_text(paper: dict[str, Any]) -> str:
    text = str(paper.get("summary") or paper.get("abstract") or "").strip()
    return re.sub(r"\s+", " ", text)


def _one_sentence(text: str, max_chars: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return "No abstract text was available in the recommendation JSON."
    match = re.search(r"(?<=[.!?])\s+", text)
    first = text[: match.start() + 1] if match else text
    if len(first) <= max_chars:
        return first
    return first[: max_chars - 3].rstrip() + "..."


def _paper_url(paper: dict[str, Any]) -> str:
    raw = paper.get("url") or paper.get("canonical_url") or ""
    if raw:
        return str(raw).strip()
    pmid = _paper_pmid(paper)
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    arxiv_id = _paper_arxiv_id(paper)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    doi = _paper_doi(paper)
    return f"https://doi.org/{doi}" if doi else ""


def _paper_id(paper: dict[str, Any]) -> str:
    return str(paper.get("id") or _paper_doi(paper) or _paper_pmid(paper) or _paper_arxiv_id(paper) or "").strip()


def _published_date(paper: dict[str, Any]) -> str:
    raw = str(paper.get("published_date") or paper.get("publicationDate") or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return match.group(1) if match else raw


def _matched_keywords(paper: dict[str, Any]) -> list[str]:
    raw = paper.get("matched_keywords") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def _local_pdf_path(paper: dict[str, Any], vault_path: Path | None) -> str:
    for key in ("local_pdf_path", "local_pdf", "pdf_path", "fulltext_pdf_path"):
        raw = paper.get(key)
        if raw and Path(str(raw)).expanduser().exists():
            return str(Path(str(raw)).expanduser())
    if vault_path is not None and _find_local_pdf is not None:
        found = _find_local_pdf(paper, vault_path=str(vault_path))
        if found:
            return str(found)
    return ""


def _obsidian_link(vault: Path, path: Path, title: str) -> str:
    rel = path.relative_to(vault).with_suffix("").as_posix()
    return f"[[{rel}|{title}]]"


def _plain_link(path: Path, title: str) -> str:
    return f"[{title}]({path.as_posix()})"


def _paper_note_content(
    paper: dict[str, Any],
    *,
    date_str: str,
    local_pdf: str,
    zotero_collection: str,
) -> str:
    title = str(paper.get("title") or "Untitled paper").strip()
    domain = str(paper.get("matched_domain") or "Other").strip()
    source = str(paper.get("source") or "").strip()
    abstract = _summary_text(paper)
    url = _paper_url(paper)
    pdf_url = _paper_pdf_url(paper)
    doi = _paper_doi(paper)
    pmid = _paper_pmid(paper)
    arxiv_id = _paper_arxiv_id(paper)
    journal = _paper_journal(paper)
    keywords = _matched_keywords(paper)
    aliases = [title]
    if doi:
        aliases.append(doi)
    if pmid:
        aliases.append(f"PMID:{pmid}")
    if arxiv_id:
        aliases.append(f"arXiv:{arxiv_id}")

    links = []
    if url:
        links.append(f"- Paper: {url}")
    if pdf_url:
        links.append(f"- External PDF: {pdf_url}")
    if local_pdf:
        links.append(f"- Local PDF: {local_pdf}")
    if zotero_collection:
        links.append(f"- Zotero collection: {zotero_collection}")
    links_block = "\n".join(links) if links else "- No external links recorded."

    keyword_line = ", ".join(keywords) if keywords else "No matched keywords recorded."
    return f"""---
title: {_yaml_string(title)}
aliases: {_yaml_list(aliases)}
paper_id: {_yaml_string(_paper_id(paper))}
doi: {_yaml_string(doi)}
pmid: {_yaml_string(pmid)}
arxiv_id: {_yaml_string(arxiv_id)}
source: {_yaml_string(source)}
journal: {_yaml_string(journal)}
published_date: {_yaml_string(_published_date(paper))}
weekly_recommendation_date: {_yaml_string(date_str)}
domain: {_yaml_string(domain)}
recommendation_score: {_yaml_string(_score(paper))}
matched_keywords: {_yaml_list(keywords)}
local_pdf: {_yaml_string(local_pdf)}
pdf_url: {_yaml_string(pdf_url)}
paper_url: {_yaml_string(url)}
tags: ["weekly-paper-recommend", "paper-review", "agent-readable-reference"]
status: "metadata_scaffold"
---

# {title}

## Agent Access

This note is the project-knowledge landing page for this paper. Use it to
connect the weekly recommendation, Zotero record, local PDF, and future review
notes.

{links_block}

## Why It Was Recommended

- Domain: {domain}
- Recommendation score: {_score(paper)}
- Matched keywords: {keyword_line}

## Citation Metadata

- Authors: {_authors_line(paper)}
- Source: {source or "Unknown"}
- Journal/venue: {journal or "preprint/unknown"}
- Published: {_published_date(paper) or "unknown"}
- DOI: {doi or "--"}
- PMID: {pmid or "--"}
- arXiv: {arxiv_id or "--"}

## Abstract

{abstract or "No abstract text was available in the recommendation JSON."}

## Review Notes

- Core contribution: {_one_sentence(abstract)}
- Relevance to current projects:
- Methods/data to inspect:
- Caveats:
- Follow-up questions for future agents:
"""


def _weekly_overview(papers: list[dict[str, Any]]) -> str:
    domains: dict[str, int] = {}
    scores: list[float] = []
    for paper in papers:
        domain = str(paper.get("matched_domain") or "Other")
        domains[domain] = domains.get(domain, 0) + 1
        try:
            scores.append(float((paper.get("scores") or {}).get("recommendation")))
        except (TypeError, ValueError):
            pass
    domain_text = ", ".join(
        f"{name} ({count})"
        for name, count in sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    )
    score_text = (
        f"{min(scores):.2f}-{max(scores):.2f}" if scores else "n/a"
    )
    return (
        f"This week's {len(papers)} papers cover {domain_text or 'unclassified topics'}. "
        f"Recommendation scores span {score_text}."
    )


def _paper_entry(
    paper: dict[str, Any],
    *,
    index: int,
    note_path: Path,
    vault: Path | None,
    local_pdf: str,
) -> str:
    title = str(paper.get("title") or "Untitled paper").strip()
    if vault is not None:
        note_link = _obsidian_link(vault, note_path, title)
        local_pdf_link = f"[[{Path(local_pdf).relative_to(vault).as_posix()}|local PDF]]" if local_pdf and Path(local_pdf).is_relative_to(vault) else local_pdf
    else:
        note_link = _plain_link(note_path, title)
        local_pdf_link = local_pdf
    url = _paper_url(paper)
    pdf_url = _paper_pdf_url(paper)
    source_bits = [str(paper.get("source") or "Unknown")]
    if url:
        source_bits.append(f"[record]({url})")
    if pdf_url:
        source_bits.append(f"[external PDF]({pdf_url})")
    if local_pdf_link:
        source_bits.append(str(local_pdf_link))
    return f"""### {index}. {note_link}
- **Authors**: {_authors_line(paper, limit=3)}
- **Source**: {" | ".join(source_bits)}
- **Journal**: {_paper_journal(paper) or "preprint/unknown"}
- **Domain**: {paper.get("matched_domain") or "Other"} | **Score**: {_score(paper)}
- **Identifiers**: DOI `{_paper_doi(paper) or "--"}`; PMID `{_paper_pmid(paper) or "--"}`; arXiv `{_paper_arxiv_id(paper) or "--"}`
- **Why now**: {_one_sentence(_summary_text(paper), max_chars=260)}
"""


def _weekly_note_content(
    data: dict[str, Any],
    *,
    date_str: str,
    papers: list[dict[str, Any]],
    paper_paths: dict[int, Path],
    local_pdfs: dict[int, str],
    vault: Path | None,
) -> str:
    keywords = sorted({
        keyword
        for paper in papers
        for keyword in _matched_keywords(paper)
    })
    entries = "\n".join(
        _paper_entry(
            paper,
            index=i,
            note_path=paper_paths[i - 1],
            vault=vault,
            local_pdf=local_pdfs.get(i - 1, ""),
        )
        for i, paper in enumerate(papers, start=1)
    )
    source_status = data.get("bio_status") or "unknown"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""---
date: {_yaml_string(date_str)}
keywords: {_yaml_list(keywords)}
tags: ["llm-generated", "weekly-paper-recommend", "agent-readable-reference"]
status: "generated"
---

# {date_str} Paper Recommendations

## This Week's Overview

{_weekly_overview(papers)}

- Total unique candidates: {data.get("total_unique", "unknown")}
- Recent-source candidates: {data.get("total_recent", "unknown")}
- Bio-source candidates: {data.get("total_bio", "unknown")}
- Bio-source status: {source_status}
- Generated: {generated_at}

## Reading Order

{entries}
"""


def materialize(
    input_path: Path,
    *,
    config_path: str | None = None,
    mode: str | None = None,
    vault_path: str | None = None,
    output_dir: str | None = None,
    date_str: str | None = None,
    overwrite_paper_notes: bool = False,
) -> dict[str, Any]:
    data = _read_json(input_path)
    papers = data.get("top_papers") or []
    if not isinstance(papers, list) or not papers:
        raise ValueError("input JSON has no top_papers; refusing to write empty notes")

    config = _read_config(config_path)
    mode = _resolve_mode(config, mode)
    date_str = date_str or str(data.get("target_date") or datetime.now().strftime("%Y-%m-%d"))
    zotero_collection = f"{date_str} Paper Recommendations"

    if mode == "obsidian":
        vault = _resolve_vault(config, vault_path)
        if vault is None:
            raise ValueError("Obsidian mode requires --vault, OBSIDIAN_VAULT_PATH, or config output.obsidian.vault_path")
        daily_dir = vault / _resolve_daily_dir(config)
        papers_base = vault / _resolve_papers_dir(config)
        weekly_path = daily_dir / f"{date_str}-paper-recommendations.md"
    elif mode == "standalone":
        vault = None
        out = _resolve_standalone_dir(config, output_dir)
        weekly_path = out / f"{date_str}-paper-recommendations.md"
        papers_base = out / "papers"
    else:
        raise ValueError(f"unknown output mode: {mode!r}")

    weekly_path.parent.mkdir(parents=True, exist_ok=True)
    papers_base.mkdir(parents=True, exist_ok=True)

    paper_paths: dict[int, Path] = {}
    local_pdfs: dict[int, str] = {}
    created = 0
    reused = 0
    overwritten = 0
    migrated = 0

    for idx, paper in enumerate(papers):
        note_path = _paper_note_path(papers_base, paper)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path = _legacy_paper_note_path(papers_base, paper)
        if (
            legacy_path is not None
            and legacy_path.exists()
            and not note_path.exists()
        ):
            legacy_path.rename(note_path)
            migrated += 1
        local_pdf = _local_pdf_path(paper, vault)
        paper_paths[idx] = note_path
        local_pdfs[idx] = local_pdf

        existed = note_path.exists()
        should_write = overwrite_paper_notes or not note_path.exists()
        if should_write:
            note_path.write_text(
                _paper_note_content(
                    paper,
                    date_str=date_str,
                    local_pdf=local_pdf,
                    zotero_collection=zotero_collection,
                ),
                encoding="utf-8",
            )
            if existed:
                overwritten += 1
            else:
                created += 1
        else:
            reused += 1

    weekly_path.write_text(
        _weekly_note_content(
            data,
            date_str=date_str,
            papers=papers,
            paper_paths=paper_paths,
            local_pdfs=local_pdfs,
            vault=vault,
        ),
        encoding="utf-8",
    )

    return {
        "mode": mode,
        "weekly_note": str(weekly_path),
        "paper_notes_created": created,
        "paper_notes_reused": reused,
        "paper_notes_overwritten": overwritten,
        "paper_notes_migrated": migrated,
        "paper_notes_total": len(papers),
        "local_pdfs_linked": sum(1 for value in local_pdfs.values() if value),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize weekly start-my-week Markdown notes."
    )
    parser.add_argument("--input", default="arxiv_filtered.json")
    parser.add_argument("--config", default=None)
    parser.add_argument("--mode", choices=["obsidian", "standalone"], default=None)
    parser.add_argument("--vault", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--overwrite-paper-notes", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = materialize(
            Path(args.input),
            config_path=args.config,
            mode=args.mode,
            vault_path=args.vault,
            output_dir=args.output_dir,
            date_str=args.date,
            overwrite_paper_notes=args.overwrite_paper_notes,
        )
    except Exception as exc:
        wrapped = "\n".join(textwrap.wrap(str(exc), width=88))
        print(f"ERROR: {wrapped}", file=sys.stderr)
        return 1

    print(f"WEEKLY_NOTE={result['weekly_note']}")
    print(f"PAPER_NOTES_TOTAL={result['paper_notes_total']}")
    print(f"PAPER_NOTES_CREATED={result['paper_notes_created']}")
    print(f"PAPER_NOTES_REUSED={result['paper_notes_reused']}")
    print(f"PAPER_NOTES_OVERWRITTEN={result['paper_notes_overwritten']}")
    print(f"PAPER_NOTES_MIGRATED={result['paper_notes_migrated']}")
    print(f"LOCAL_PDFS_LINKED={result['local_pdfs_linked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
