#!/usr/bin/env python3
"""
Scan existing notes and build a keyword index.
Used by the start-my-week skill to scan notes in an Obsidian vault and build a
mapping from keywords to note paths.
"""

import os
import re
import json
import sys
import argparse
import logging
from pathlib import Path
from typing import List, Dict, Set, Tuple

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    # Match the loud-fail discipline of search_pubmed/search_biorxiv: without
    # PyYAML, frontmatter (titles, tags) is ignored and the index degrades to
    # filename stems only — warn instead of silently producing a sparse index.
    print("[scan_existing_notes] WARNING: PyYAML not installed — note "
          "frontmatter (titles, tags) will be ignored and the keyword index "
          "will be sparse. Install with: pip install PyYAML", file=sys.stderr)

from common_words import COMMON_WORDS

logger = logging.getLogger(__name__)


def _atomic_write(path, obj):
    """Thin wrapper around `_atomic.atomic_write_json` with the
    repo's standard default-encoder. Lives here so the three write
    sites in this file don't each re-import."""
    try:
        from _atomic import atomic_write_json
    except ImportError:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from _atomic import atomic_write_json
    atomic_write_json(obj, path)


def parse_frontmatter(content: str) -> Dict:
    """
    Parse YAML frontmatter from a markdown file.

    Args:
        content: markdown file contents

    Returns:
        Parsed frontmatter as a dict, or an empty dict if absent or unparseable.
    """
    # Locate the opening and closing --- delimiters
    frontmatch = re.match(r'^---\s*\n(.*?)^---\s*\n', content, re.MULTILINE | re.DOTALL)

    if not frontmatch:
        return {}

    if not _HAS_YAML:
        return {}  # can't parse without PyYAML; frontmatter skipped silently

    try:
        frontmatter_str = frontmatch.group(1)
        frontmatter_data = yaml.safe_load(frontmatter_str)
        return frontmatter_data or {}
    except Exception as e:
        logger.warning("Error parsing frontmatter: %s", e)
        return {}


def extract_keywords_from_title(title: str) -> List[str]:
    """
    Extract keywords from a paper title.

    Args:
        title: paper title string

    Returns:
        List of extracted keywords.
    """
    if not title:
        return []

    keywords = []

    # Strategy 1: extract leading acronym or proper noun (all-caps word).
    # e.g. extract "BLIP" from "BLIP: Bootstrapping..."
    main_keyword = re.match(r'^([A-Z]{2,})(?:\s*:|\s+)', title)
    if main_keyword:
        keywords.append(main_keyword.group(1))

    # Strategy 2: extract the full text before the colon when the title uses an
    # "ACRONYM: subtitle" format.
    colon_match = title.split(':')
    if len(colon_match) >= 2 and len(colon_match[0].strip()) > 2:
        before_colon = colon_match[0].strip()
        # Only keep tokens of a reasonable length.
        if 3 <= len(before_colon) <= 20:
            keywords.append(before_colon)

    # Strategy 3: extract hyphenated technical terms (e.g. Vision-Language,
    # Fine-Tuning, In-Context). Only match clear technical terms; avoid
    # over-splitting generic phrases.
    tech_terms = re.findall(r'\b[A-Z][a-z]*(?:-[A-Z][a-z]*)+\b', title)
    for term in tech_terms:
        term_clean = term.strip()
        # Only keep tokens of a reasonable length.
        if 3 <= len(term_clean) <= 20:
            # Filter out common/stop words.
            if term_clean.lower() not in COMMON_WORDS:
                keywords.append(term_clean)

    # Deduplicate while preserving insertion order.
    keywords = list(dict.fromkeys(keywords))

    return keywords


def scan_notes_directory(papers_dir: Path, vault_path: Path) -> List[Dict]:
    """
    Scan all markdown notes under the Papers directory.

    Args:
        papers_dir: path to the Papers directory
        vault_path: path to the vault root

    Returns:
        List of note info dicts.
    """
    notes = []

    # Recursively find all .md files.
    for md_file in papers_dir.rglob('*.md'):
        # Skip auto-generated image-folder index files. extract-paper-images
        # writes 20_Research/Papers/<domain>/<paper>/images/index.md and these
        # are not real notes — including them produces a junk "index" keyword
        # that maps to multiple papers.
        if md_file.name == 'index.md' and md_file.parent.name == 'images':
            continue
        try:
            with open(md_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            # Parse YAML frontmatter.
            frontmatter = parse_frontmatter(content)

            # Build the note info dict.
            # Compute path relative to vault root, always using forward slashes.
            rel_path = md_file.relative_to(vault_path)
            note_info = {
                'path': str(rel_path).replace('\\', '/'),  # forward slashes
                'filename': md_file.name,
                'short_name': md_file.stem,  # stem (no .md extension) for short wikilinks
                'path_str': str(rel_path),  # string form for correct encoding
                'title': frontmatter.get('title', md_file.stem),
                'tags': frontmatter.get('tags', []),
            }

            # Extract keywords from the title.
            title_keywords = extract_keywords_from_title(note_info['title'])
            note_info['title_keywords'] = title_keywords

            # Extract keywords from tags, keeping only meaningful ones.
            tag_keywords = []
            for tag in note_info['tags']:
                if isinstance(tag, list):
                    for sub_tag in tag:
                        if isinstance(sub_tag, str):
                            # Only keep tags of length 3–20; filter common words.
                            if 3 <= len(sub_tag) <= 20 and sub_tag.lower() not in COMMON_WORDS:
                                tag_keywords.append(sub_tag)
                elif isinstance(tag, str):
                    if 3 <= len(tag) <= 20 and tag.lower() not in COMMON_WORDS:
                        tag_keywords.append(tag)

            note_info['tag_keywords'] = tag_keywords

            notes.append(note_info)

        except Exception as e:
            logger.warning("Error reading %s: %s", md_file, e)
            continue

    return notes


def build_keyword_index(notes: List[Dict]) -> Dict[str, List[str]]:
    """
    Build a mapping from keywords to note paths.

    Args:
        notes: list of note info dicts (as returned by scan_notes_directory)

    Returns:
        Dict mapping lowercase keyword strings to lists of note paths.
    """
    # Use sets for deduplication to avoid O(n) membership tests on lists.
    keyword_sets: Dict[str, set] = {}

    def _add_keyword(keyword_lower: str, path: str):
        if 3 <= len(keyword_lower) <= 30 and keyword_lower not in COMMON_WORDS:
            if keyword_lower not in keyword_sets:
                keyword_sets[keyword_lower] = set()
            keyword_sets[keyword_lower].add(path)

    # Pre-compute tag → owners so we can include only uniquely-owned tags.
    # Tags shared across multiple notes stay excluded — linking "LINE-1" to one
    # paper when 3 papers carry that tag would be wrong.
    tag_owners: Dict[str, set] = {}
    for note in notes:
        for tag in note.get('tag_keywords', []):
            tag_owners.setdefault(tag.lower(), set()).add(note['path'])

    for note in notes:
        # Title-extracted keywords (acronyms, model names) are always safe.
        for keyword in note['title_keywords']:
            _add_keyword(keyword.lower(), note['path'])

        # Tags become keywords only when this note is the sole owner.
        for tag in note.get('tag_keywords', []):
            tag_lc = tag.lower()
            if len(tag_owners.get(tag_lc, ())) == 1:
                _add_keyword(tag_lc, note['path'])

        # Use the filename stem as a keyword, but only its meaningful part.
        if 'short_name' in note:
            short_name = note['short_name']
            # Strip trailing version numbers and common suffixes.
            clean_short = re.sub(r'(-\d{4}\.\d{4,5}|-v\d+)$', '', short_name)

            # Add to index only if the cleaned stem is a reasonable length.
            if 3 <= len(clean_short) <= 40 and clean_short.lower() not in COMMON_WORDS:
                _add_keyword(clean_short.lower(), note['path'])

    # Convert sets to lists for JSON-serialisable output.
    keyword_index = {k: list(v) for k, v in keyword_sets.items()}
    return keyword_index


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description='Scan existing notes and build keyword index')
    parser.add_argument('--vault', type=str,
                        default=os.environ.get('OBSIDIAN_VAULT_PATH', ''),
                        help='Path to Obsidian vault (or set OBSIDIAN_VAULT_PATH env var)')
    parser.add_argument('--output', type=str, default='existing_notes_index.json',
                        help='Output JSON file path')
    parser.add_argument('--papers-dir', type=str,
                        default='20_Research/Papers',
                        help='Relative path to Papers directory')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    if not args.vault:
        # Standalone mode — no vault configured. Emit an empty index so
        # downstream steps (link_keywords.py) work without crashing.
        logger.warning(
            "No vault path supplied (--vault or OBSIDIAN_VAULT_PATH). "
            "Running in standalone mode: writing empty index to %s", args.output
        )
        output = {"notes": [], "keyword_to_notes": {}}
        _atomic_write(args.output, output)
        logger.info("Empty index written (standalone mode).")
        return

    vault_path = Path(args.vault)
    papers_dir = vault_path / args.papers_dir

    if not papers_dir.exists():
        logger.warning(
            "Papers directory not found: %s — writing empty index instead.", papers_dir
        )
        output = {"notes": [], "keyword_to_notes": {}}
        _atomic_write(args.output, output)
        logger.info("Empty index written.")
        return

    logger.info("Scanning notes in: %s", papers_dir)

    notes = scan_notes_directory(papers_dir, vault_path)
    logger.info("Found %d notes", len(notes))

    keyword_index = build_keyword_index(notes)
    logger.info("Built index with %d keywords", len(keyword_index))

    # Assemble the output payload.
    output = {
        'notes': notes,
        'keyword_to_notes': keyword_index
    }

    # Persist results — atomic write (see scripts/_atomic.py for rationale).
    _atomic_write(args.output, output)

    logger.info("Index saved to: %s", args.output)

    logger.info("=== Keyword Index Statistics ===")
    logger.info("Total notes: %d", len(notes))
    logger.info("Total keywords: %d", len(keyword_index))

    # Warn loudly if the index is so thin link_keywords.py will be effectively a no-op.
    # Common cause: existing notes lack title acronyms and lack uniquely-owned tags.
    if notes and len(keyword_index) < max(3, len(notes) // 4):
        logger.warning(
            "Keyword index is sparse (%d keywords for %d notes). "
            "link_keywords.py will likely add few or no wikilinks. "
            "To improve coverage, give your existing paper notes specific, "
            "uniquely-owned tags in frontmatter (e.g. tags: [Springer-LTR, flamenco-cluster]).",
            len(keyword_index), len(notes)
        )

    if len(keyword_index) > 0:
        logger.info("=== Sample Keywords ===")
        sample_keywords = sorted(keyword_index.items())[:10]
        for keyword, paths in sample_keywords:
            logger.info("  %s: %d notes", keyword, len(paths))


if __name__ == '__main__':
    main()
