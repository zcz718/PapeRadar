"""
Shared config-path resolution for paperadar scripts.

Lookup order (first hit wins):
  1. Explicit path passed in (e.g. from --config CLI flag)
  2. $OBSIDIAN_VAULT_PATH/99_System/Config/research_interests.yaml  (Obsidian users)
  3. ~/.config/paperadar/config.yaml                              (standalone users)
  4. None — caller should fall back to built-in defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


OBSIDIAN_CONFIG_REL = ("99_System", "Config", "research_interests.yaml")
STANDALONE_CONFIG = Path.home() / ".config" / "paperadar" / "config.yaml"


def resolve_config_path(explicit: Optional[str] = None) -> Optional[str]:
    """
    Return the first existing config path, or None if no config is found.

    Args:
        explicit: optional explicit path (from --config). If given, returned
                  as-is even if it doesn't exist (caller decides what to do).
    """
    if explicit:
        return explicit

    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if vault:
        candidate = Path(vault).joinpath(*OBSIDIAN_CONFIG_REL)
        if candidate.exists():
            return str(candidate)

    if STANDALONE_CONFIG.exists():
        return str(STANDALONE_CONFIG)

    return None


def candidate_paths() -> list[str]:
    """All candidate paths in lookup order (for diagnostic/setup messages)."""
    paths = []
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if vault:
        paths.append(str(Path(vault).joinpath(*OBSIDIAN_CONFIG_REL)))
    paths.append(str(STANDALONE_CONFIG))
    return paths
