#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obsidian note generation script — handles frontmatter format, multiple
languages, and multiple research domains.

Supported paper-ID formats (link format is dispatched automatically):
  - arXiv ID ("2501.12345" / "arXiv:2501.12345")
  - PubMed ID ("PMID:38291234")
  - bioRxiv / medRxiv DOI ("10.1101/2024.01.01.123456")
  - Generic DOI ("10.xxxx/...")

Supported domain categories (used to select the section template):
  - biology — biology / life-science papers (the primary use case for this
    skill, e.g. genomics, gene regulation, single-cell, computational
    biology); section template covers Background & Motivation / Methods /
    Key Results / Strengths /
    Limitations / Relevance / Quality Scores / Related Papers.
  - ml      — machine-learning / AI papers; retains the legacy template
    with Method Overview / Architecture / Experiments / Ablation /
    Baselines sections.
  - other   — unknown domain; falls back to the ml template as a generic
    catch-all.
"""

import sys
import os
import argparse
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain classification + paper-id dispatch
# ---------------------------------------------------------------------------

_BIOLOGY_DOMAINS = {
    # English
    'Genomics', 'Genome Biology',
    'Gene Regulation', 'Gene Regulation & Epigenetics', 'Epigenetics',
    'Single-cell Biology', 'Single-cell',
    'Computational Biology',
    # Chinese
    '基因组学', '基因调控', '表观遗传', '单细胞生物学', '计算生物学',
}
_ML_DOMAINS = {
    # English
    'LLM', 'Large Language Model', 'Multimodal', 'Agent', 'Multi-Agent',
    # Chinese
    '大模型', '多模态技术', '智能体', '强化学习_LLM_Agent',
}
_BIOLOGY_KEYWORDS_HEURISTIC = (
    'rna', 'dna', 'cell', 'genom', 'epigenom', 'sequencing',
    'methylation', 'pluripoten', 'epigenetic', 'crispr',
    'protein', 'pathway', 'chromatin', 'transcript',
    '生物', '基因', '表观', '细胞', '测序', '蛋白',
)
_ML_KEYWORDS_HEURISTIC = (
    'llm', 'transformer', 'agent', 'reinforcement', 'multimodal',
    '大模型', '多模态', '智能体',
)


def classify_domain(domain: str) -> str:
    """Return 'biology', 'ml', or 'other' for a given domain label."""
    if not domain:
        return 'other'
    if domain in _BIOLOGY_DOMAINS:
        return 'biology'
    if domain in _ML_DOMAINS:
        return 'ml'
    d_lower = domain.lower()
    if any(k in d_lower for k in _BIOLOGY_KEYWORDS_HEURISTIC):
        return 'biology'
    if any(k in d_lower for k in _ML_KEYWORDS_HEURISTIC):
        return 'ml'
    return 'other'


def paper_links(paper_id: str):
    """Return (canonical_url, pdf_url_or_empty, source_label) for a paper id.

    Handles arXiv / PubMed (PMID:...) / bioRxiv-medRxiv DOI / generic DOI.
    For PubMed and generic DOI we leave PDF empty since open-access PDFs
    aren't reliably available at a predictable URL.
    """
    pid = (paper_id or '').strip()
    if not pid or pid.startswith('['):  # placeholder like "[PAPER_ID]"
        return ('', '', 'unknown')

    # PMID prefix is parsed via the shared `_id_parser` so the rules
    # (case-insensitive, whitespace-tolerant) stay consistent with
    # fetch_fulltext / save_to_zotero / search_pubmed.
    try:
        from _id_parser import strip_pmid_prefix
    except ImportError:
        import os as _os, sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from _id_parser import strip_pmid_prefix
    # Detect a PMID via the shared parser rather than a literal
    # `startswith('PMID:')` — the canonical prefix regex also tolerates
    # forms like "PMID :12345", which the old check leaked into the arXiv
    # branch (producing https://arxiv.org/abs/PMID :12345).
    num = strip_pmid_prefix(pid)
    if num != pid.strip():
        return (
            f"https://pubmed.ncbi.nlm.nih.gov/{num}/",
            "",
            "PubMed",
        )
    if pid.startswith('10.1101/'):
        return (
            f"https://www.biorxiv.org/content/{pid}",
            f"https://www.biorxiv.org/content/{pid}.full.pdf",
            "bioRxiv/medRxiv",
        )
    if pid.startswith('10.') and '/' in pid:
        return (f"https://doi.org/{pid}", "", "DOI")
    # arXiv default
    aid = pid[6:] if pid.lower().startswith('arxiv:') else pid
    aid = aid.strip()
    return (
        f"https://arxiv.org/abs/{aid}",
        f"https://arxiv.org/pdf/{aid}",
        "arXiv",
    )


# ---------------------------------------------------------------------------
# Tag dictionaries
# ---------------------------------------------------------------------------

_DOMAIN_TAGS_ZH = {
    # Biology
    'Genomics': ['Genomics', 'genome'],
    '基因组学': ['基因组学', 'genome'],
    'Gene Regulation & Epigenetics': ['Gene-Regulation-Epigenetics', 'epigenetics'],
    'Gene Regulation': ['Gene-Regulation', 'gene-regulation'],
    '基因调控': ['基因调控', 'gene-regulation'],
    'Single-cell Biology': ['Single-cell-Biology', 'single-cell'],
    '单细胞生物学': ['单细胞生物学', 'single-cell'],
    'Computational Biology': ['Computational-Biology', 'compbio'],
    '计算生物学': ['计算生物学', 'compbio'],
    # Machine learning
    '大模型': ['大模型', 'LLM'],
    '多模态技术': ['多模态', 'Vision-Language'],
    '智能体': ['智能体', 'Agent'],
}
_DOMAIN_TAGS_EN = {
    # Biology
    'Genomics': ['Genomics', 'genome'],
    'Gene Regulation & Epigenetics': ['Gene-Regulation-Epigenetics', 'epigenetics'],
    'Gene Regulation': ['Gene-Regulation', 'gene-regulation'],
    'Single-cell Biology': ['Single-cell-Biology', 'single-cell'],
    'Computational Biology': ['Computational-Biology', 'compbio'],
    # Machine learning
    'LLM': ['LLM', 'Large-Language-Model'],
    'Multimodal': ['Multimodal', 'Vision-Language'],
    'Agent': ['Agent', 'Multi-Agent'],
    'Other': ['paper-note'],
}


def _format_links_line(canonical, pdf, source_label, language='en'):
    """Render a Markdown links bullet for a paper, language-aware."""
    if not canonical:
        return "[arXiv link / PDF link]"
    parts = []
    if source_label and source_label not in ('unknown',):
        parts.append(f"[{source_label}]({canonical})")
    else:
        parts.append(f"[Link]({canonical})")
    if pdf:
        parts.append(f"[PDF]({pdf})")
    return ' | '.join(parts)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _yaml_dq(v) -> str:
    """Escape a value for safe embedding in a double-quoted YAML scalar.

    Without this, a title/author/DOI containing a literal double quote or
    backslash (e.g. `AI: A "New" Approach`) produces structurally invalid
    YAML frontmatter — PyYAML raises ParserError and Obsidian's reader
    breaks. Also collapses embedded newlines so a single scalar stays on
    one line.
    """
    return (str(v).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', ' ').replace('\r', ' '))


def _frontmatter(paper_id, title, authors, domain, source_label, date,
                 tags_yaml, verified_against_pdf=False, doi="",
                 local_pdf_path=""):
    """Shared YAML frontmatter."""
    # Only genuine preprint servers describe a meaningful venue here.
    # "PubMed" is an index and "DOI" is an identifier scheme — neither is
    # a journal, so emitting `journal: "PubMed preprint"` was misleading.
    journal_field = ""
    if source_label in ('bioRxiv/medRxiv', 'bioRxiv', 'medRxiv'):
        journal_field = f'journal: "{_yaml_dq(source_label)} preprint"\n'
    doi_field = f'doi: "{_yaml_dq(doi)}"\n' if doi else ""
    local_pdf_field = (
        f'local_pdf: "{_yaml_dq(local_pdf_path)}"\n'
        if local_pdf_path else ""
    )
    return f'''---
date: "{_yaml_dq(date)}"
paper_id: "{_yaml_dq(paper_id)}"
{doi_field}title: "{_yaml_dq(title)}"
authors: "{_yaml_dq(authors)}"
domain: "{_yaml_dq(domain)}"
{journal_field}tags:
{tags_yaml}
quality_score: "[SCORE]/10"
related_papers: []
{local_pdf_field}zotero_key: ""
created: "{_yaml_dq(date)}"
updated: "{_yaml_dq(date)}"
status: analyzed
verified_against_pdf: {str(verified_against_pdf).lower()}
---

# {title}
'''


def _abstract_only_banner():
    """Banner inserted at the top of any abstract-only generated note."""
    return (
        "\n> [!warning] Abstract-only note — full text not fetched\n"
        "> This note was generated without access to the paper's PDF. Drop\n"
        "> the PDF into `~/Downloads/` (any filename containing the PMID,\n"
        "> DOI tail, or publisher PII works), then re-run\n"
        "> `python3 fetch_fulltext.py --paper-id <ID> --out fulltext.json`\n"
        "> followed by `paper-analyze --fulltext fulltext.json`. The\n"
        "> `verified_against_pdf` frontmatter field will switch to `true`\n"
        "> once the PDF has been read.\n\n"
    )


def _fulltext_excerpt_block(fulltext_json_path):
    """Read a fulltext.json (output of fetch_fulltext.py) and produce a
    Markdown block to splice into the note.

    Validates the canonical schema via `_schemas.load_fulltext()` so a
    stale or hand-edited file fails loud rather than silently producing
    a degraded note. On any failure (missing file, schema mismatch,
    empty text), returns ('', '', '', '') so the caller can fall back
    to the placeholder template + `verified_against_pdf: false`.
    """
    try:
        # Import lazily so other generate_note entry points that don't
        # use fulltext aren't forced to depend on _schemas.
        from _schemas import load_fulltext
    except ImportError:
        # Fall back when generate_note.py is invoked from outside its dir.
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from _schemas import load_fulltext

    try:
        ft = load_fulltext(fulltext_json_path)
    except FileNotFoundError as e:
        logger.warning("fulltext file missing: %s", e)
        return ('', '', '', '')
    except ValueError as e:
        # Schema mismatch or empty text — fail loud per project policy.
        logger.error("fulltext.json invalid: %s", e)
        return ('', '', '', '')
    except Exception as e:
        logger.warning("Could not read fulltext file %s: %s",
                       fulltext_json_path, e)
        return ('', '', '', '')

    abstract = (ft.abstract or '').strip()
    text = (ft.text or '').strip()
    source = ft.source
    pdf_path = ft.pdf_path or ''

    # If abstract detection failed, take the first ~1500 chars of text
    if not abstract and text:
        abstract = text[:1500].strip()

    # Best-effort Methods excerpt: find a "Methods" / "METHODS" / "STAR Methods"
    # header in the text and grab the next ~3000 chars.
    methods = ""
    for marker in ("\nMETHODS\n", "\nMethods\n", "\nSTAR★Methods", "\nSTAR Methods\n",
                   "\nSTAR*METHODS", "\nMaterials and Methods\n",
                   "\nMaterial and Methods\n"):
        idx = text.find(marker)
        if idx != -1:
            tail = text[idx + len(marker):]
            methods = tail[:3000].strip()
            break

    return (abstract, methods, source, pdf_path)


def _load_fulltext_pdf_path(fulltext_json_path):
    """Return the local PDF path recorded in a fulltext.json, if present."""
    if not fulltext_json_path:
        return ""
    try:
        from _schemas import load_fulltext
    except ImportError:
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from _schemas import load_fulltext
    try:
        ft = load_fulltext(fulltext_json_path)
    except Exception as e:
        logger.warning("Could not inspect fulltext PDF path: %s", e)
        return ""
    return ft.pdf_path or ""


def _safe_filename_token(value, fallback="paper"):
    token = re.sub(r'[ /\\:*?"<>|]+', '_', str(value or "")).strip('._')
    token = re.sub(r'_+', '_', token)
    return (token or fallback)[:120]


def archive_fulltext_pdf(fulltext_json_path, note_dir, paper_id, title):
    """Copy a fetched fulltext PDF into the paper note folder.

    Returns the archived PDF path, or "" when no local PDF is available.
    Text-only fulltext sources such as PMC XML intentionally return "".
    """
    pdf_path = _load_fulltext_pdf_path(fulltext_json_path)
    if not pdf_path:
        return ""
    src = Path(pdf_path).expanduser()
    if not src.exists() or src.suffix.lower() != ".pdf":
        logger.warning("fulltext PDF path is not a readable PDF: %s", src)
        return ""

    dest_dir = Path(note_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    id_token = _safe_filename_token(paper_id, fallback="paper")
    title_token = _safe_filename_token(title, fallback="title")
    dest = dest_dir / f"{id_token}__{title_token}.pdf"

    try:
        if src.resolve() == dest.resolve():
            return str(dest)
    except OSError:
        pass

    if dest.exists():
        return str(dest)
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        logger.warning("Could not archive PDF into note folder: %s", e)
        return ""
    return str(dest)


def _biology_template_en(paper_id, title, authors, domain, date,
                         canonical, pdf, source_label, tags_yaml,
                         fulltext_path="", doi="", local_pdf_path=""):
    abstract_block = ""
    methods_block = ""
    fulltext_source = ""
    archived_pdf = ""
    verified = False

    if fulltext_path:
        ab, methods, src, extracted_pdf = _fulltext_excerpt_block(fulltext_path)
        if ab or methods:
            verified = True
            fulltext_source = src
            archived_pdf = local_pdf_path or extracted_pdf
            if ab:
                abstract_block = ab
            if methods:
                methods_block = methods

    fm = _frontmatter(paper_id, title, authors, domain, source_label, date,
                      tags_yaml, verified_against_pdf=verified, doi=doi,
                      local_pdf_path=local_pdf_path)
    links_line = _format_links_line(canonical, pdf, source_label, 'en')

    if verified:
        banner = (
            f"\n> **Fulltext verified** — extracted from "
            f"`{fulltext_source}`"
            + (f" ({archived_pdf})" if archived_pdf else "")
            + ".\n\n"
        )
        abstract_section = (
            "## Abstract (extracted)\n\n"
            + abstract_block
            + "\n\n*The text above was auto-extracted; lightly edit for formatting if needed.*\n"
        )
        methods_section_extra = ""
        if methods_block:
            methods_section_extra = (
                "\n\n### Methods excerpt (auto-extracted)\n\n"
                + methods_block
                + "\n\n*Auto-extracted; verify against the PDF.*\n"
            )
    else:
        banner = _abstract_only_banner()
        abstract_section = (
            "## Abstract\n\n"
            "[Paste the abstract here, lightly edited for formatting. If the paper has\n"
            "structured sections (Background / Results / Conclusion) preserve them as\n"
            "**bold** prefixes.]"
        )
        methods_section_extra = ""

    return fm + banner + f'''
## Core Information
- **Paper ID**: {paper_id}
- **Authors**: {authors}
- **Affiliations**: --
- **Published**: {date}
- **Journal / Venue**: {source_label or '--'}
- **Links**: {links_line}

{abstract_section}{methods_section_extra}

## Research Background & Motivation

- **Field state**: [What is known. Frame in terms of the user's domain.]
- **Gap**: [What is missing — the question this paper addresses.]
- **Why this paper now**: [Recent advance / new technique / new dataset
  that made this study possible or necessary.]

## Methods

### System / model / organism
[Cell line, organism, sample source, or in silico system.]

### Experimental design
[Perturbations, conditions, controls, replicates, sample size.]

### Assays / measurements
[Sequencing modality, imaging, biochemistry, mass spec, etc. Note any
orthogonal validation.]

### Analysis
[Pipelines, statistical models, software versions if reported. Multiple
testing correction, batch handling.]

## Key Results

1. **[Result 1 — one-line claim]**. [Supporting detail; effect size or
   p-value if reported.]
2. **[Result 2]**. [Supporting detail.]
3. **[Result 3]**. [Supporting detail.]

## In-Depth Analysis

### Strengths
- [Concrete, specific strength — e.g. "a rare causal chain from
  perturbation → molecular change → phenotype".]
- [Cross-species replication, orthogonal assay, etc.]
- [Population-level signal, in vivo validation, …]

### Limitations / open questions
- [What the abstract / paper does not resolve.]
- [Generalisability concerns.]
- [Mechanism granularity — what's still in a black box.]

### Relevance to my own work
- [How this connects to the user's ongoing research.]
- [Concrete follow-up the user could do, ideally one-line actionable.]
- *Actionable hook*: [specific cross-reference or analysis to try.]

## Quality Scores

| Dimension | Score | Reasoning |
|---|---|---|
| Novelty | X/10 | [Rewards new mechanism, new orthogonal validation, new resource — not just "first to apply ML method"] |
| Technical quality | X/10 | [Rewards appropriate controls, multiple cell lines, in-vivo confirmation] |
| Experimental rigor | X/10 | [Rewards sample size, replication, statistical correction, blinding] |
| Writing | X/10 | [Clarity, structure of the abstract / paper] |
| Practical utility | X/10 | [Directly applicable framework / reagent / resource for downstream work] |
| **Overall** | **X.X/10** | |

## Related Papers

- *[Related paper 1 title]* — [author, year, venue] — [one-line relationship].
- *[Related paper 2]* — [relationship].
- *[Related paper 3]* — [relationship].

## External Resources

- {source_label or "Source"}: {canonical or "(URL not available)"}
{f"- PDF: {pdf}" if pdf else ""}

> [!tip] Key takeaway
> [One-sentence distillation — the thing you'd tell a labmate over coffee.]

> [!success] Recommendation
> ⭐⭐⭐⭐ [Brief recommendation note + who should read it.]
'''


def _biology_template_zh(paper_id, title, authors, domain, date,
                         canonical, pdf, source_label, tags_yaml):
    fm = _frontmatter(paper_id, title, authors, domain, source_label, date, tags_yaml)
    links_line = _format_links_line(canonical, pdf, source_label, 'zh')
    return fm + f'''
## 核心信息
- **论文 ID**：{paper_id}
- **作者**：{authors}
- **机构**：--
- **发布时间**：{date}
- **期刊 / 来源**：{source_label or '--'}
- **链接**：{links_line}

## 摘要

[粘贴论文摘要原文。若有结构化分节（Background / Results / Conclusion），用
**粗体** 前缀保留分段结构。]

## 研究背景与动机

- **领域现状**：[已知什么。以读者自己研究领域的视角组织叙述。]
- **缺口**：[缺失什么——这篇论文要解决的问题。]
- **为什么是现在**：[近期出现的新技术/新数据让这项研究成为可能或必要。]

## 方法

### 体系 / 模型 / 实验生物
[细胞系、模式生物、样本来源；或纯计算/in silico 体系。]

### 实验设计
[扰动条件、对照、生物学/技术重复、样本量。]

### 测量方法
[测序模式、成像、生化、质谱等；记录任何 orthogonal validation。]

### 分析方法
[Pipeline、统计模型、软件版本；是否做多重检验校正与批次效应处理。]

## 主要结果

1. **[结果 1 —— 一句话结论]**。[支撑细节；如有 effect size / p 值。]
2. **[结果 2]**。[支撑细节。]
3. **[结果 3]**。[支撑细节。]

## 深度分析

### 优势
- [具体、可指认的优势——例如 "首次给出 扰动 → 分子变化 → 表型 的完整因果链"。]
- [跨物种重复、orthogonal 实验、in vivo 验证等。]

### 局限性 / 待解问题
- [摘要/正文未澄清的部分。]
- [适用范围疑虑。]
- [机制层面尚处黑盒的部分。]

### 与我自己工作的关联
- [本文与读者当前研究的接口。]
- [一个可立即着手的 follow-up——一行话可执行。]
- *可执行 hook*：[具体的交叉验证或分析点。]

## 质量评分

| 维度 | 分数 | 评分理由 |
|---|---|---|
| 创新性 | X/10 | [奖励新机制、新 orthogonal validation、新资源——而非仅 "首次套用某 ML 方法"] |
| 技术质量 | X/10 | [奖励合适的对照、多细胞系、in vivo 验证] |
| 实验严谨性 | X/10 | [样本量、重复、多重检验校正、盲法] |
| 写作 | X/10 | [摘要 / 正文的清晰度与结构] |
| 实用价值 | X/10 | [可直接用于下游工作的框架 / 试剂 / 资源] |
| **综合** | **X.X/10** | |

## 相关论文

- *[相关论文 1 标题]* — [作者, 年份, venue] — [一句话关系]。
- *[相关论文 2]* — [关系]。
- *[相关论文 3]* — [关系]。

## 外部资源

- {source_label or "来源"}：{canonical or "(URL not available)"}
{f"- PDF：{pdf}" if pdf else ""}

> [!tip] 核心 takeaway
> [一句话精炼——你会在 coffee chat 里告诉同事的那一句。]

> [!success] 推荐指数
> ⭐⭐⭐⭐ [简评 + 适合谁读。]
'''


def _ml_template_en(paper_id, title, authors, domain, date,
                    canonical, pdf, source_label, tags_yaml):
    """Legacy ML/AI section template (architecture / ablation / baselines)."""
    fm = _frontmatter(paper_id, title, authors, domain, source_label, date, tags_yaml)
    links_line = _format_links_line(canonical, pdf, source_label, 'en')
    return fm + f'''
## Core Information
- **Paper ID**: {paper_id}
- **Authors**: {authors}
- **Affiliation**: [Infer from authors or check paper]
- **Publication Date**: {date}
- **Conference/Journal**: {source_label or '--'}
- **Links**: {links_line}
- **Citations**: [If available]

## Research Problem
[Problem description and explanation]

## Method Overview

### Core Method
1. [Method 1]
   - [Detailed description]
   - [Key steps]
   - [Innovation points]

### Method Architecture
[Architecture description and image references]

### Key Innovations
1. [Innovation 1] - [Why important]
2. [Innovation 2] - [Why important]
3. [Innovation 3] - [Why important]

## Experimental Results

### Datasets
- [Dataset 1]: [Scale, characteristics]
- [Dataset 2]: [Scale, characteristics]

### Experimental Settings
- **Baseline Methods**: [List comparison methods]
- **Evaluation Metrics**: [List metrics]
- **Experimental Environment**: [Hardware, hyperparameters]

### Main Results
[Experimental results table and key findings]

## Deep Analysis

### Research Value
- **Theoretical Contribution**: [Theoretical contribution]
- **Practical Applications**: [Practical application value]
- **Field Impact**: [Potential impact on research field]

### Advantages
- [Advantage 1]
- [Advantage 2]
- [Advantage 3]

### Limitations
- [Limitation 1]
- [Limitation 2]
- [Limitation 3]

## Comparison with Related Papers

### [[Related Paper 1]] - [Relationship]
- **Difference**: [How this method differs]
- **Improvement**: [Improvements compared to others]

### [[Related Paper 2]] - [Relationship]
[Similar format]

## My Comprehensive Evaluation

### Value Scoring
- **Overall Score**: [X.X/10]
- **Breakdown**:
  - Innovation: [X/10]
  - Technical Quality: [X/10]
  - Experiment Thoroughness: [X/10]
  - Writing Quality: [X/10]
  - Practicality: [X/10]

## Related Papers
- [[Related Paper 1]] - [Relationship]
- [[Related Paper 2]] - [Relationship]

## External Resources
- {source_label or 'Source'}: {canonical or '(URL not available)'}
{f"- PDF: {pdf}" if pdf else ""}
'''


def _ml_template_zh(paper_id, title, authors, domain, date,
                    canonical, pdf, source_label, tags_yaml):
    fm = _frontmatter(paper_id, title, authors, domain, source_label, date, tags_yaml)
    links_line = _format_links_line(canonical, pdf, source_label, 'zh')
    return fm + f'''
## 核心信息
- **论文 ID**：{paper_id}
- **作者**：{authors}
- **机构**：[从作者推断或查看论文]
- **发布时间**：{date}
- **会议/期刊**：{source_label or '--'}
- **链接**：{links_line}

## 研究问题
[问题描述中文翻译和解释]

## 方法概述

### 核心方法
1. [方法 1]
   - [详细描述]
   - [关键步骤]
   - [创新点]

### 方法架构
[架构描述与图片引用]

### 关键创新
1. [创新点 1] - [为什么重要]
2. [创新点 2] - [为什么重要]
3. [创新点 3] - [为什么重要]

## 实验结果

### 数据集
- [数据集 1]：[规模、特点]
- [数据集 2]：[规模、特点]

### 实验设置
- **基线方法**：[列出对比方法]
- **评估指标**：[列出指标]

### 主要结果
[实验结果表格与关键发现]

## 深度分析

### 研究价值
- **理论贡献**：[理论上的贡献]
- **实际应用**：[实际应用价值]
- **领域影响**：[对研究领域的潜在影响]

### 优势
- [优势 1]
- [优势 2]

### 局限性
- [局限 1]
- [局限 2]

## 我的综合评价

### 价值评分
- **总体评分**：[X.X/10]
- **分项**：
  - 创新性：[X/10]
  - 技术质量：[X/10]
  - 实验充分性：[X/10]
  - 写作质量：[X/10]
  - 实用性：[X/10]

## 相关论文
- [[相关论文 1]] - [对比关系]
- [[相关论文 2]] - [对比关系]

## 外部资源
- {source_label or '来源'}：{canonical or '(URL not available)'}
{f"- PDF：{pdf}" if pdf else ""}
'''


def generate_note_content(paper_id, title, authors, domain, date,
                          language="zh", fulltext_path="", doi="",
                          local_pdf_path=""):
    """Render the analysis-note Markdown for a paper.

    Dispatches by:
      - language: 'zh' or 'en'
      - domain category: biology / ml / other (other falls back to ml)
      - paper_id source: arXiv / PMID / bioRxiv DOI / generic DOI

    If fulltext_path points to a fulltext.json produced by
    fetch_fulltext.py, the abstract + a Methods excerpt are inlined into
    the note body and frontmatter is marked verified_against_pdf: true.
    Otherwise a banner is added telling the user the note is abstract-only.
    """
    canonical, pdf, source_label = paper_links(paper_id)
    category = classify_domain(domain)

    # Tag dictionary lookup
    tag_dict = _DOMAIN_TAGS_ZH if language == 'zh' else _DOMAIN_TAGS_EN
    base_tag = '论文笔记' if language == 'zh' else 'paper-note'
    tags = [base_tag] + tag_dict.get(domain, [domain.replace(' ', '-')])
    tags_yaml = "\n".join(f'  - {tag}' for tag in tags)

    # English biology path threads fulltext + DOI; others retain legacy
    # behaviour for now (extending to zh / ml is straightforward but the
    # primary use case is English biology notes).
    if category == 'biology' and language == 'en':
        return _biology_template_en(
            paper_id, title, authors, domain, date,
            canonical, pdf, source_label, tags_yaml,
            fulltext_path=fulltext_path, doi=doi,
            local_pdf_path=local_pdf_path,
        )
    if category == 'biology':
        renderer = _biology_template_zh
    else:
        renderer = _ml_template_zh if language == 'zh' else _ml_template_en
    return renderer(paper_id, title, authors, domain, date,
                    canonical, pdf, source_label, tags_yaml)


# ---------------------------------------------------------------------------
# CLI plumbing (unchanged behaviour modulo the new template dispatch)
# ---------------------------------------------------------------------------

def get_vault_path(cli_vault=None):
    """Return the Obsidian vault path from the CLI argument, environment variable, or config file."""
    if cli_vault:
        return cli_vault
    env_path = os.environ.get('OBSIDIAN_VAULT_PATH')
    if env_path:
        return env_path
    try:
        from _config_paths import resolve_config_path
        import yaml
        config_path = resolve_config_path()
        if config_path:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            vault_path = (
                (config.get("output") or {})
                .get("obsidian", {})
                .get("vault_path", "")
            )
            if vault_path:
                return os.path.expanduser(vault_path)
    except Exception as e:
        logger.debug("Could not resolve vault from config: %s", e)
    logger.error("No vault path specified. Set one via --vault, OBSIDIAN_VAULT_PATH, or the paperadar config.")
    sys.exit(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description='Generate paper analysis notes')
    parser.add_argument('--paper-id', type=str, default='[PAPER_ID]', help='Paper ID (arXiv / PMID / bioRxiv DOI)')
    parser.add_argument('--title', type=str, default='[论文标题]', help='Paper title')
    parser.add_argument('--authors', type=str, default='[Authors]', help='Paper authors')
    parser.add_argument('--domain', type=str, default='其他', help='Paper domain')
    parser.add_argument('--vault', type=str, default=None, help='Obsidian vault path')
    parser.add_argument('--language', type=str, default='zh', choices=['zh', 'en'], help='Output language: zh or en')
    parser.add_argument('--fulltext', type=str, default='',
                        help='Path to fulltext.json (from fetch_fulltext.py). '
                             'If provided, abstract + methods are inlined and '
                             'frontmatter is marked verified_against_pdf: true.')
    parser.add_argument('--doi', type=str, default='',
                        help='Paper DOI (carried into frontmatter; '
                             'used for cross-referencing).')
    args = parser.parse_args()

    vault_root = get_vault_path(args.vault)
    papers_dir = os.path.join(vault_root, "20_Research", "Papers")
    date = datetime.now().strftime("%Y-%m-%d")

    paper_title_safe = re.sub(r'[ /\\:*?"<>|]+', '_', args.title).strip('_. ')

    # Sanitize domain to prevent path traversal.
    domain = args.domain.strip('/\\').replace('..', '')
    if not domain:
        domain = '其他' if args.language == 'zh' else 'Other'

    # Folder name uses underscores, not spaces. Vault convention is e.g.
    # `Single-cell_Biology/`, not `Single-cell Biology/`. Frontmatter `domain`
    # field keeps the human-readable form so YAML stays clean.
    domain_dir = domain.replace(' ', '_')

    note_dir = os.path.join(papers_dir, domain_dir)
    os.makedirs(note_dir, exist_ok=True)

    note_path = os.path.join(note_dir, f"{paper_title_safe}.md")
    local_pdf_path = archive_fulltext_pdf(
        args.fulltext, note_dir, args.paper_id, args.title)
    content = generate_note_content(
        args.paper_id, args.title, args.authors, domain, date, args.language,
        fulltext_path=args.fulltext, doi=args.doi,
        local_pdf_path=local_pdf_path,
    )

    try:
        with open(note_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except IOError as e:
        logger.error("Failed to write note: %s", e)
        sys.exit(1)

    msg = "笔记已生成" if args.language == 'zh' else "Note generated"
    print(f"{msg}: {note_path}")
    if local_pdf_path:
        print(f"PDF archived: {local_pdf_path}")
    msg2 = ("请手动编辑笔记内容，替换占位符为实际分析结果"
            if args.language == 'zh'
            else "Please manually edit the note content, replacing placeholders with actual analysis")
    print(msg2)


if __name__ == '__main__':
    main()
