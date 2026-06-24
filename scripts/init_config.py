#!/usr/bin/env python3
"""
init_config.py — first-run setup wizard for the start-my-week skill.

Asks the user a few questions, then writes a config.yaml to the appropriate
location (Obsidian vault if they use Obsidian, otherwise XDG-style
~/.config/start-my-week/config.yaml).

Usage:
    python scripts/init_config.py            # interactive wizard
    python scripts/init_config.py --force    # overwrite existing config
    python scripts/init_config.py --dry-run  # show what would be written, don't write
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
EXAMPLE_CONFIG = REPO_DIR / "config.example.yaml"
STANDALONE_CONFIG = Path.home() / ".config" / "start-my-week" / "config.yaml"


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        ans = input(f"{question}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        print("  (please answer)")


def yesno(question: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{question} [{d}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  (answer y or n)")


def detect_existing_config() -> Path | None:
    """Return the first config that already exists, or None."""
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if vault:
        p = Path(vault) / "99_System" / "Config" / "research_interests.yaml"
        if p.exists():
            return p
    if STANDALONE_CONFIG.exists():
        return STANDALONE_CONFIG
    return None


def patch_config_text(text: str, *, mode: str, vault_path: str,
                      output_dir: str, language: str) -> str:
    """
    Minimal in-place patch of the example YAML so we don't need a YAML
    round-tripper (which would lose comments).
    """
    out = text

    # language
    out = out.replace('language: "en"', f'language: "{language}"')

    # output.mode
    out = out.replace('mode: "standalone"', f'mode: "{mode}"')

    # vault_path
    out = out.replace('vault_path: ""', f'vault_path: "{vault_path}"')

    # output_dir
    out = out.replace(
        'output_dir: "~/start-my-week-output"',
        f'output_dir: "{output_dir}"',
    )

    return out


def main() -> int:
    p = argparse.ArgumentParser(description="First-run setup wizard for start-my-week")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing config without prompting")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be written, don't write anything")
    args = p.parse_args()

    if not EXAMPLE_CONFIG.exists():
        print(f"ERROR: example config not found at {EXAMPLE_CONFIG}", file=sys.stderr)
        return 2

    existing = detect_existing_config()
    if existing and not args.force:
        print(f"A config already exists at: {existing}")
        if not yesno("Overwrite it?", default=False):
            print("Aborted. (Use --force to overwrite without asking.)")
            return 0

    print()
    print("┌─────────────────────────────────────────────────────────────────┐")
    print("│  start-my-week — first-run setup                                 │")
    print("└─────────────────────────────────────────────────────────────────┘")
    print()
    print("This wizard will create your config file. You can re-edit it any")
    print("time — or run `python scripts/show_keywords.py` to inspect.")
    print()

    # Q1: Obsidian
    use_obsidian = yesno("Do you use Obsidian as your note-taking app?", default=False)

    vault_path = ""
    if use_obsidian:
        env_vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
        if env_vault:
            print(f"  (detected $OBSIDIAN_VAULT_PATH = {env_vault})")
        vault_path = prompt(
            "  Path to your Obsidian vault",
            default=env_vault or None,
        )
        vault_path = os.path.expanduser(vault_path)
        if not Path(vault_path).is_dir():
            print(f"  WARNING: {vault_path} doesn't look like an existing directory.")
            if not yesno("  Use it anyway?", default=False):
                return 1

    # Q2: output dir (only relevant for standalone)
    if use_obsidian:
        output_dir = "~/start-my-week-output"  # not used, but write a sensible default
    else:
        output_dir = prompt(
            "Where should plain-markdown weekly notes go?",
            default="~/start-my-week-output",
        )

    # Q3: Zotero
    use_zotero = yesno("Do you use Zotero for reference management?", default=False)
    zotero_msg = ""
    if use_zotero:
        print()
        print("  Zotero credentials are read from environment variables (not stored")
        print("  in the YAML). Add these to your ~/.zshrc or ~/.bashrc:")
        print()
        print("    export ZOTERO_API_KEY=\"your-api-key\"")
        print("    export ZOTERO_USER_ID=\"your-user-id\"")
        print()
        print("  Get them at https://www.zotero.org/settings/keys")
        zotero_msg = "Zotero sync will activate when ZOTERO_API_KEY + ZOTERO_USER_ID are set in env."

    # Q4: language
    lang = prompt("Output language (en/zh)", default="en")
    if lang not in ("en", "zh"):
        print(f"  (treating '{lang}' as 'en')")
        lang = "en"

    # Build the patched config
    text = EXAMPLE_CONFIG.read_text()
    text = patch_config_text(
        text,
        mode="obsidian" if use_obsidian else "standalone",
        vault_path=vault_path,
        output_dir=output_dir,
        language=lang,
    )

    # Decide destination
    if use_obsidian:
        dest = Path(vault_path) / "99_System" / "Config" / "research_interests.yaml"
    else:
        dest = STANDALONE_CONFIG

    print()
    print("─" * 65)
    print(f"Will write config to: {dest}")
    print("─" * 65)

    if args.dry_run:
        print()
        print("--- config preview ---")
        print(text)
        print("--- end preview ---")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print(f"✓ Config written to {dest}")

    # Also create the standalone output dir if applicable
    if not use_obsidian:
        out = Path(os.path.expanduser(output_dir))
        out.mkdir(parents=True, exist_ok=True)
        print(f"✓ Created output directory: {out}")

    if zotero_msg:
        print()
        print(f"NOTE: {zotero_msg}")

    print()
    print("Next steps:")
    print("  • Inspect your active keywords:")
    print("      python scripts/show_keywords.py")
    print("  • Edit the config to customise your research interests:")
    print(f"      $EDITOR {dest}")
    print("  • In Codex, ask: \"$start-my-week Run my weekly paper recommendations.\"")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
