#!/usr/bin/env python3
"""
Keyword linking script for the paperadar skill.
Finds keywords in text and replaces them with Obsidian wikilinks.
"""

import re
import json
import sys
import argparse
import logging
from typing import Dict, List, Set, Tuple

from common_words import COMMON_WORDS

logger = logging.getLogger(__name__)


def parse_markdown_lines(content: str) -> List[Tuple[str, str, str, bool]]:
    """
    Parse markdown content into a list of line tuples, each containing:
    (original_line, line_type, line_content, in_frontmatter).

    Line types:
    - 'frontmatter': YAML frontmatter content
    - 'code': fenced code block
    - 'inline_code': line containing inline backtick code
    - 'wikilink': line containing an existing wikilink
    - 'image': image link
    - 'link': plain markdown link
    - 'heading': heading line (starts with #)
    - 'normal': ordinary prose text

    Args:
        content: markdown content string

    Returns:
        List of tuples: (original_line, line_type, line_content, in_frontmatter)
    """
    lines = []
    in_code_block = False
    code_fence_char = None
    in_frontmatter = False
    frontmatter_count = 0

    for line in content.split('\n'):
        # Check for frontmatter start/end
        if line.strip() == '---':
            frontmatter_count += 1
            if frontmatter_count == 1:
                in_frontmatter = True
                lines.append((line, 'frontmatter', line, True))
                continue
            elif frontmatter_count == 2:
                in_frontmatter = False
                lines.append((line, 'frontmatter', line, False))
                continue

        if in_frontmatter:
            lines.append((line, 'frontmatter', line, True))
            continue

        # Check for fenced code block start/end
        if line.strip().startswith('```'):
            if not in_code_block:
                in_code_block = True
                code_fence_char = '```'
            else:
                in_code_block = False
                code_fence_char = None
            lines.append((line, 'code', line, False))
            continue

        if in_code_block:
            lines.append((line, 'code', line, False))
            continue

        # Classify the line type
        line_type = 'normal'
        processed_content = line

        # Check for heading line
        if line.strip().startswith('#'):
            line_type = 'heading'
            lines.append((line, 'heading', line, False))
            continue

        # Check for inline code — detect so the whole line is skipped downstream.
        # No placeholder substitution: 'inline_code' lines are skipped
        # wholesale (see skip_line_types), so the old __CODE_N__ machinery
        # produced a processed_content nobody consumed — dead work, and a
        # latent trap if that skip were ever removed (the un-restored
        # __CODE_N__ markers would leak into linked output).
        if re.search(r'`[^`]+`', line):
            line_type = 'inline_code'

        # Check for images before wikilinks, because ![[x]] also contains [[x]]
        elif re.search(r'!\[\[.*?\]\]', line):
            line_type = 'image'

        # Check for existing wikilink
        elif re.search(r'\[\[.*?\]\]', line):
            line_type = 'wikilink'

        # Check for plain markdown link
        elif re.search(r'\[.*?\]\(.*?\)', line):
            line_type = 'link'

        lines.append((line, line_type, processed_content, False))

    return lines


def _normalize_path(path: str) -> str:
    """Normalize a note path for comparison.

    Strips trailing dots/slashes/whitespace and the .md extension; lowercased.
    Handles both fully-qualified vault paths and bare filenames (Obsidian
    headings often use just the filename, while explicit Report links use
    the full path).
    """
    if not path:
        return ''
    p = path.strip().rstrip('.').rstrip('/')
    if p.lower().endswith('.md'):
        p = p[:-3]
    return p.lower()


def _is_same_paper(target_path: str, current_paper_paths) -> bool:
    """True if `target_path` refers to any of the entry's "self" notes.

    `current_paper_paths` may be a string (single path) or any iterable of
    strings (multiple candidate paths — typically the heading wikilink path
    AND the `Report:` line wikilink path). Both forms are accepted because
    a heading wikilink can use a bare filename, a typo, or a Unicode form
    (`naïve` vs `naive`) that doesn't match the actual file. We treat the
    Report-line wikilink as authoritative when both are present, but fall
    back to either if the other is missing.

    Comparison is by normalized full path AND by basename so headings like
    `[[Foo.|Foo]]` still match an index entry of `path/to/Foo.md`.
    """
    if not current_paper_paths:
        return False
    if isinstance(current_paper_paths, str):
        candidates = (current_paper_paths,)
    else:
        candidates = tuple(current_paper_paths)
    if not candidates:
        return False
    a = _normalize_path(target_path)
    if not a:
        return False
    a_base = a.split('/')[-1]
    for cand in candidates:
        b = _normalize_path(cand)
        if not b:
            continue
        if a == b or a_base == b.split('/')[-1]:
            return True
    return False


def link_keywords_in_text(
    text: str,
    keyword_index: Dict[str, List[str]],
    existing_wikilinks: Set[str],
    current_paper_path='',
    already_linked: Set[str] = None,
) -> str:
    """
    Insert wikilinks for keywords found in the given text.

    Args:
        text: the prose text to process
        keyword_index: mapping of keyword -> list of note paths
        existing_wikilinks: set of note paths already wikilinked in the document
        current_paper_path: note path(s) for the paper-entry block being processed.
            May be a string (single path) or a list/tuple of candidate paths —
            typically the heading wikilink path AND the `Report:` line wikilink
            path. Both are used so that a Unicode mismatch between the heading
            (`naïve`) and the actual file (`naive`) still suppresses self-links.
            An empty string or empty list disables self-link suppression.
        already_linked: set of lowercased keywords already linked within the
            current block, accumulated across successive line calls to enforce
            the "at most one link per keyword per entry block" policy. Callers
            should pass a `set()` and reuse it across all lines in the same block.

    Returns:
        Processed text with wikilinks inserted.
    """
    if already_linked is None:
        already_linked = set()

    # Filter out common words and keywords that are too short or too long
    filtered_keywords = {}
    for keyword, paths in keyword_index.items():
        keyword_lower = keyword.lower()
        # Skip common words (including biology-domain generics like "Methylation"/"expression")
        if keyword_lower in COMMON_WORDS:
            continue
        # Skip keywords shorter than 3 chars or longer than 30 chars
        if len(keyword) < 3 or len(keyword) > 30:
            continue
        # Skip pure numeric strings
        if keyword.isdigit():
            continue
        filtered_keywords[keyword] = paths

    # Sort by keyword length descending — prefer longer matches
    sorted_keywords = sorted(
        filtered_keywords.keys(),
        key=lambda k: len(k),
        reverse=True
    )

    result = text

    for keyword in sorted_keywords:
        # Skip keywords already linked within this block (first-occurrence-only per block)
        if keyword.lower() in already_linked:
            continue

        # Find all matches without \b word boundaries (supports CJK context);
        # pattern ensures the keyword is not part of a longer ASCII/numeric token.
        pattern = r'(?<![a-zA-Z0-9_-])' + re.escape(keyword) + r'(?![a-zA-Z0-9_-])'
        matches = list(re.finditer(pattern, result, re.IGNORECASE))
        if not matches:
            continue

        note_paths = filtered_keywords[keyword]
        if not note_paths:
            continue

        # Skip keywords shared by multiple papers — too generic to link to any
        # specific paper (e.g. "evaluation" tagged on 50+ papers).
        if len(note_paths) > 1:
            continue
        note_path = note_paths[0]

        # Self-link suppression: if this keyword's target is the same paper as
        # the entry we are currently inside, do not link. Without this, the
        # FOXP2 paper's own entry would self-link "FOXP2" to its own note.
        if _is_same_paper(note_path, current_paper_path):
            continue

        # Among all matches, find the first one not already inside a wikilink; replace only that one.
        replaced = False
        for match in matches:
            start, end = match.span()

            # Check whether this match is already inside an existing wikilink.
            # A match is inside an existing wikilink iff the most recent
            # bracket event before `start` is an UNCLOSED '[[' — i.e. there
            # is no ']]' between that '[[' and `start`. The old
            # rfind('[[')/find(']]') pair could straddle two *separate*
            # wikilinks (`[[A]] keyword [[B]]`) and wrongly suppress a
            # keyword sitting between them.
            open_before = result.rfind('[[', 0, start)
            close_before = result.rfind(']]', 0, start)
            if open_before != -1 and open_before > close_before:
                continue  # already inside an existing wikilink

            # Use the matched text verbatim to preserve original casing
            original_text = match.group(0)
            wikilink = f'[[{note_path}|{original_text}]]'
            result = result[:start] + wikilink + result[end:]
            replaced = True
            break  # first-occurrence-only

        if replaced:
            already_linked.add(keyword.lower())

    return result


def _is_paper_entry_heading(line: str) -> bool:
    """True if `line` is a paper-entry heading (### at the start of trimmed line)."""
    return line.lstrip().startswith('### ')


def _extract_paper_paths_from_block(block_lines: List[Tuple[str, str, str, bool]]) -> List[str]:
    """Return all candidate "self" note paths for a paper-entry block.

    A block can refer to its underlying note via several routes, and they may
    disagree (e.g. the heading uses a Unicode form `naïve` while the actual
    file is ASCII `naive`). Self-link suppression should consider all of them
    so a typo or Unicode-mismatch in one place doesn't defeat the rule.

    Sources collected (in priority order, deduplicated):
      1. Wikilinks on any line containing `**Report**` or `**报告**` —
         these are usually the canonical authored cross-link.
      2. The first wikilink on the block's heading line.
    """
    if not block_lines:
        return []
    paths: List[str] = []
    seen = set()

    def _add(p):
        p = p.strip()
        key = p.lower().rstrip('.').rstrip('/')
        if p and key not in seen:
            seen.add(key)
            paths.append(p)

    for original_line, _ltype, _content, _infm in block_lines:
        if '**Report**' in original_line or '**报告**' in original_line:
            for m in re.finditer(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', original_line):
                _add(m.group(1))
    heading_line = block_lines[0][0]
    m = re.search(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', heading_line)
    if m:
        _add(m.group(1))
    return paths


def link_keywords_in_file(
    input_file: str,
    output_file: str,
    keyword_index: Dict[str, List[str]]
) -> None:
    """
    Process the input file and write linked output.

    Entry-aware: weekly-note documents are split into an overview block
    (everything before the first `### ` heading) and one paper-entry block
    per `### ` heading. The overview block is left untouched. Each paper-entry
    block has self-link suppression enabled for its own note, and the
    "at most one link per keyword per block" rule is applied to avoid
    redundant links to the same keyword.

    Args:
        input_file: path to the input markdown file
        output_file: path to write the processed markdown
        keyword_index: mapping of keyword -> list of note paths
    """
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse into line tuples
    lines = parse_markdown_lines(content)

    # Collect existing wikilinks
    existing_wikilinks = set()
    for original_line, line_type, _, _ in lines:
        if line_type == 'wikilink':
            for match in re.findall(r'\[\[(.*?)\]\]', original_line):
                parts = match.split('|')
                if parts:
                    existing_wikilinks.add(parts[0].lower())

    # Find block boundaries. Each ### heading starts a new paper-entry block;
    # everything before the first ### belongs to the overview block.
    block_starts = [
        i for i, (orig, ltype, _, _) in enumerate(lines)
        if ltype == 'heading' and _is_paper_entry_heading(orig)
    ]

    # Build [(start, end, current_paper_paths, is_overview), ...].
    # current_paper_paths is the list of all candidate self-paths for each
    # paper-entry block, used for self-link suppression. Overview uses [].
    block_ranges: List[Tuple[int, int, List[str], bool]] = []
    if not block_starts:
        # No paper-entry headings found. Treat as a plain document (legacy
        # behaviour: the whole file is normal text, no overview-skip applied).
        block_ranges.append((0, len(lines), [], False))
    else:
        # Overview block: from file start up to the first ###.
        block_ranges.append((0, block_starts[0], [], True))
        for idx, start in enumerate(block_starts):
            end = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
            block_lines = lines[start:end]
            paper_paths = _extract_paper_paths_from_block(block_lines)
            block_ranges.append((start, end, paper_paths, False))

    # Process each block
    skip_line_types = {
        'frontmatter', 'code', 'wikilink', 'image', 'link', 'heading', 'inline_code'
    }
    processed_lines: List[str] = [None] * len(lines)
    for start, end, current_papers, is_overview in block_ranges:
        per_block_linked: Set[str] = set()
        for line_idx in range(start, end):
            original_line, line_type, line_content, _in_fm = lines[line_idx]
            if is_overview:
                # Overview block: pass through unchanged, no auto-linking
                processed_lines[line_idx] = original_line
                continue
            if line_type in skip_line_types:
                processed_lines[line_idx] = original_line
                continue
            processed_lines[line_idx] = link_keywords_in_text(
                line_content,
                keyword_index,
                existing_wikilinks,
                current_paper_path=current_papers,
                already_linked=per_block_linked,
            )

    # Join processed lines
    result = '\n'.join(processed_lines)

    # Write output file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(result)

    # Statistics
    original_links = len(re.findall(r'\[\[.*?\]\]', content))
    new_links = len(re.findall(r'\[\[.*?\]\]', result))
    added_links = new_links - original_links

    logger.info("Processed file: %s", input_file)
    logger.info("  Paper-entry blocks: %d (plus overview)", max(0, len(block_ranges) - 1))
    logger.info("  Original wikilinks: %d", original_links)
    logger.info("  New wikilinks: %d", new_links)
    logger.info("  Added wikilinks: %d", added_links)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description='Link keywords to existing notes')
    parser.add_argument('--index', type=str, required=True,
                        help='Path to keyword index JSON file')
    parser.add_argument('--input', type=str, required=True,
                        help='Input file path (markdown)')
    parser.add_argument('--output', type=str, required=True,
                        help='Output file path (markdown)')

    args = parser.parse_args()

    # Load keyword index
    with open(args.index, 'r', encoding='utf-8') as f:
        index_data = json.load(f)

    keyword_index = index_data.get('keyword_to_notes', {})

    # Filter common words
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    filtered_count = len([k for k in keyword_index if k.lower() in COMMON_WORDS])
    logger.info("Loaded index with %d keywords", len(keyword_index))
    if filtered_count > 0:
        logger.info("  Filtered %d common words", filtered_count)

    link_keywords_in_file(args.input, args.output, keyword_index)

    logger.info("Output saved to: %s", args.output)


if __name__ == '__main__':
    main()
