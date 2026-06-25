#!/usr/bin/env python3
"""
show_keywords.py — Display the active research_interests.yaml in a readable form.

Designed to be invoked by Codex when the user asks "what keywords am I tracking?",
"show my research interests", "add a keyword", etc. Codex can then offer to edit
the YAML file based on the user's described pipeline.

Usage:
    python show_keywords.py                       # uses $OBSIDIAN_VAULT_PATH/.../research_interests.yaml
    python show_keywords.py --config path.yaml    # explicit config path
    python show_keywords.py --json                # machine-readable output for programmatic editing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Reuse the existing config loader (with hardcoded fallback) from search_papers.py
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from search_papers import load_research_config
    from _config_paths import resolve_config_path
except ImportError as e:
    print(f"ERROR: could not import dependencies: {e}", file=sys.stderr)
    sys.exit(2)


def render_text(config: dict, source_label: str) -> str:
    lines = []
    lines.append(f"Research interests (loaded from: {source_label})")
    lines.append("=" * 70)

    brief = (config.get("research_brief") or "").strip()
    if brief:
        lines.append("")
        lines.append(f"Research brief: {brief}")
        lines.append("-" * 70)

    domains = config.get("research_domains", {})
    if not domains:
        lines.append("(no research_domains defined)")
    else:
        for name, dom in domains.items():
            keywords = dom.get("keywords", []) or []
            cats = dom.get("arxiv_categories", []) or []
            priority = dom.get("priority", "—")
            lines.append("")
            lines.append(f"### {name}    [priority: {priority}]")
            if cats:
                lines.append(f"  arXiv categories: {', '.join(cats)}")
            if keywords:
                lines.append("  keywords:")
                for kw in keywords:
                    lines.append(f"    - {kw}")
            else:
                lines.append("  (no keywords)")

    excluded = config.get("excluded_keywords", []) or []
    lines.append("")
    lines.append("-" * 70)
    if excluded:
        lines.append(f"Excluded keywords ({len(excluded)}): {', '.join(excluded)}")
    else:
        lines.append("Excluded keywords: (none)")

    s2_key = config.get("semantic_scholar_api_key")
    lines.append(f"Semantic Scholar API key: {'configured' if s2_key else 'not set'}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Show the active research_interests.yaml config")
    p.add_argument("--config", default=None,
                   help="Path to research_interests.yaml (default: "
                        "$OBSIDIAN_VAULT_PATH/99_System/Config/research_interests.yaml)")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable text")
    args = p.parse_args()

    config_path = resolve_config_path(args.config)
    if not config_path:
        print("WARNING: no --config given and OBSIDIAN_VAULT_PATH not set; "
              "falling back to built-in defaults.", file=sys.stderr)
        # load_research_config returns the hardcoded fallback when path is missing/unreadable.
        # Silence its logger.error() for the intentional-fallback case.
        import logging
        _log = logging.getLogger("search_papers")
        _prev = _log.level
        _log.setLevel(logging.CRITICAL)
        config = load_research_config("/__nonexistent__")
        _log.setLevel(_prev)
        source_label = "<built-in defaults>"
    else:
        config = load_research_config(config_path)
        source_label = config_path
        if not os.path.exists(config_path):
            print(f"WARNING: {config_path} not found; using built-in defaults.",
                  file=sys.stderr)
            source_label = f"<built-in defaults> (intended: {config_path})"

    if args.json:
        print(json.dumps({
            "source": source_label,
            "config": config,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_text(config, source_label))

    return 0


if __name__ == "__main__":
    sys.exit(main())
