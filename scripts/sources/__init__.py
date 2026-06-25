"""paperadar source adapters.

One module per external catalogue. Every module exposes a single entry point —
`search_<name>(config, days=7, target_date=None) -> list[dict]` — returning the
shared paperadar paper schema (see `search_papers.filter_and_score_papers`). The
orchestrator (`scripts/search_papers.py`) discovers them by bare module name via
its `_EXTRA_SOURCES` registry / bio-source imports, so adding a catalogue is one
module here plus one registration line there.

Shared helpers used by these adapters (`_http`, `_query`, `_env_resolve`,
`_config_paths`, `_id_parser`) live one level up in `scripts/`; each module adds
that parent directory to `sys.path` on import.
"""
