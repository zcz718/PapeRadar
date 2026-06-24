# start-my-week

> Weekly journal-catch-up machine — a Claude Code / Codex skill that pulls the
> past week's most relevant biology papers from arXiv (q-bio), bioRxiv, medRxiv,
> PubMed, and Semantic Scholar, scores them against your research interests,
> and writes a ranked weekly note — either into your **Obsidian vault** or as
> plain Markdown files in any folder you choose.

This skill is adapted from
[**evil-read-arxiv**](https://github.com/juliye2025/evil-read-arxiv) by
[juliye2025](https://github.com/juliye2025). The upstream project is published
without a license; this adaptation is released under MIT **with attribution**.
See [LICENSE](./LICENSE).

## What this fork adds on top of upstream

- **Multi-source search** — adds bioRxiv, medRxiv, and PubMed (E-utilities) to
  the original arXiv + Semantic Scholar pipeline (`search_biorxiv.py`,
  `search_pubmed.py`).
- **Top-tier journal coverage** — `prioritize_journals` in the config restricts
  PubMed and Semantic Scholar to Nature, Science, Cell, and their subjournals.
  arXiv / bioRxiv / medRxiv preprints are always included regardless.
- **Obsidian-optional** — works entirely without Obsidian. Standalone mode
  writes plain `.md` files to any output directory you choose. No wikilinks,
  no vault assumptions.
- **First-run wizard** — `python scripts/init_config.py` (or Codex's
  conversational version) asks three questions and writes your config in under
  a minute.
- **Biology-focused example config** — `config.example.yaml` ships with generic
  example domains (genomics, gene regulation & epigenetics, single-cell,
  computational biology, ML-for-biology) that you replace with your own topics.
- **Bilingual output** — weekly notes can render in English or Chinese
  (`language: en|zh` in config).
- **Sharper keyword filtering** — short / all-caps keywords use word-boundary
  regex so `ONT` no longer matches inside `in-context`.
- **Weekly catch-up default** — all sources default to a **7-day** window
  (configurable with `--days`).
- **`show_keywords.py`** — print the active research interests in human or
  JSON form, so Codex can walk you through editing them.
- **Optional Zotero sync** — `save_to_zotero.py` pushes recommendations into a
  date-named Zotero collection, reuses matching existing items, preserves
  citation identifiers, archives fetched PDFs under the Obsidian paper folder,
  and attaches PDFs or linked PDF URLs when Zotero file storage is full.
- **Agent-readable vault notes** — `materialize_weekly_notes.py` turns
  `arxiv_filtered.json` into a weekly index plus one per-paper scaffold with
  citation IDs, PDF/Zotero links, abstract, and review slots for future agents.

## Install

```bash
git clone https://github.com/zcz718/start-my-week.git
cd start-my-week
pip install -r requirements.txt
```

### First-run wizard (recommended)

Run the interactive wizard — it asks about Obsidian, Zotero, and output
preferences, then writes your config:

```bash
python scripts/init_config.py
```

The wizard detects whether `$OBSIDIAN_VAULT_PATH` is already set and offers
sensible defaults for everything.

### Obsidian mode (manual setup)

If you use Obsidian and prefer to set up manually:

```bash
export OBSIDIAN_VAULT_PATH="/path/to/your/obsidian-vault"
mkdir -p "$OBSIDIAN_VAULT_PATH/99_System/Config"
cp config.example.yaml "$OBSIDIAN_VAULT_PATH/99_System/Config/research_interests.yaml"
# then edit research_interests.yaml to match your topics
```

### Standalone mode (no Obsidian required)

If you don't use Obsidian, run the wizard or manually copy the config:

```bash
mkdir -p ~/.config/start-my-week
cp config.example.yaml ~/.config/start-my-week/config.yaml
# Edit the file: set output.mode to "standalone" and output.standalone.output_dir
```

Weekly notes will be written to `~/start-my-week-output/` (or whatever you
configure) as plain `.md` files with no wikilinks.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OBSIDIAN_VAULT_PATH` | Optional — only if `output.mode: obsidian` | Root of your Obsidian vault |
| `NCBI_API_KEY` | No | Bumps PubMed rate limit from 3 → 10 req/s |
| `ZOTERO_API_KEY`, `ZOTERO_USER_ID` | No | Enable Zotero sync |
| `UNPAYWALL_EMAIL` | No | Your contact email; enables the Unpaywall PDF source (their ToS requires one). Unset = Unpaywall skipped. |

## Usage

### As a Claude Code or Codex skill (single source of truth)

The same `SKILL.md` drives both runners. Keep **one** canonical copy of this
repo and symlink it into whichever skill roots you use — no second copy to
keep in sync:

```bash
# Claude Code
ln -s "$(pwd)" ~/.claude/skills/start-my-week
# Codex (optional — if you also use Codex)
ln -s "$(pwd)" ~/.codex/skills/start-my-week
```

`SKILL.md` resolves its own `SKILL_DIR` at runtime (it probes
`~/.claude/skills`, then `~/.codex/skills`, then falls back to the file's own
directory), so the identical body works under both. The scripts in
`scripts/` are platform-neutral.

**Triggering.** In Codex, the explicit `$start-my-week …` form is the most
reliable. In Claude Code, invoke the `start-my-week` skill (or ask in natural
language). Examples:

> "Run my weekly paper recommendations."
> "What keywords am I tracking?"
> "Add single-cell methods to my research interests."

On first run, if no config is found, the agent walks you through the wizard
conversationally.

> **Dependencies.** Before the first run, install the requirements into the
> interpreter the skill will use: `python3 -m pip install -r requirements.txt`.
> If your default `python3` lacks them, point the skill at the right
> interpreter with `export START_MY_WEEK_PYTHON=/path/to/python`. The skill
> preflight-checks this and fails loud with instructions if deps are missing.

### Standalone CLI

```bash
# See your active keywords
python scripts/show_keywords.py

# Run the full search (arXiv + S2 + bioRxiv + medRxiv + PubMed, 7-day window)
python scripts/search_arxiv.py \
  --output arxiv_filtered.json \
  --max-results 200 --top-n 10 --days 7 \
  --categories "q-bio.GN,q-bio.QM,q-bio.CB,q-bio.MN"

# Materialize the weekly index and per-paper knowledge notes
python scripts/materialize_weekly_notes.py --input arxiv_filtered.json

# JSON dump of the active config (for programmatic edits)
python scripts/show_keywords.py --json
```

Config is auto-detected from the standard lookup paths; pass `--config
/path/to/research_interests.yaml` to override.

## Top-tier journal filtering

Set `prioritize_journals` in your config to restrict PubMed and Semantic
Scholar results to specific journals:

```yaml
prioritize_journals:
  - "Nature"
  - "Science"
  - "Cell"
  - "Nature Methods"
  - "Nature Genetics"
  # ... full list in config.example.yaml
```

Leave the list empty (or omit the key) to get papers from all journals — the
original behaviour.

arXiv, bioRxiv, and medRxiv preprints are **never filtered** by this list, so
cutting-edge preprints always get through.

## Inspecting and adapting your keywords

The active keyword set lives in your config file. To inspect it:

```bash
python scripts/show_keywords.py
```

Output looks like:

```
### Genomics & Genome Biology    [priority: 4]
  arXiv categories: q-bio.GN, q-bio.PE
  keywords:
    - genome
    - genomics
    - genome assembly
    ...
```

To change it, just edit the YAML — or ask Codex inside the skill, e.g.
*"Add Hi-C and chromatin loops to my Gene Regulation & Epigenetics domain."*

## Repository layout

```
start-my-week/
├── SKILL.md                  # Claude Code / Codex skill driver
├── README.md                 # this file
├── LICENSE                   # MIT (with upstream attribution)
├── requirements.txt
├── config.example.yaml       # template for research_interests.yaml
├── .gitignore
├── agents/openai.yaml        # Codex skill interface metadata
├── scripts/
│   ├── init_config.py        # first-run setup wizard
│   ├── search_arxiv.py       # main orchestrator (arXiv + S2 + dispatch to bio)
│   ├── search_biorxiv.py     # bioRxiv / medRxiv
│   ├── search_pubmed.py      # PubMed via E-utilities
│   ├── materialize_weekly_notes.py # weekly index + paper-note scaffolds
│   ├── fetch_fulltext.py     # multi-source full-text/PDF fetch chain
│   ├── generate_note.py      # PDF-verified per-paper deep-analysis note
│   ├── save_to_zotero.py     # optional Zotero collection + PDF sync
│   ├── scan_existing_notes.py # index vault notes (Obsidian mode)
│   ├── link_keywords.py      # auto-wikilink research keywords
│   ├── show_keywords.py      # inspect/dump research_interests.yaml
│   ├── common_words.py       # stop-word list for keyword linking
│   └── _config_paths.py, _env_resolve.py, _id_parser.py,
│       _schemas.py, _scoring.py, _atomic.py   # shared utilities
└── tests/                    # pytest suite
```

## License

MIT — see [LICENSE](./LICENSE).

Original work (arXiv-only pipeline, Chinese-default workflow, scoring framework)
© juliye2025 ([evil-read-arxiv](https://github.com/juliye2025/evil-read-arxiv)).

> **Upstream licensing note.** The upstream project is published **without a
> license**, which under default copyright means its author retains all rights.
> This adaptation is offered under MIT *with attribution* in good faith, but if
> you intend to build on or redistribute it, please consult the upstream author
> ([juliye2025](https://github.com/juliye2025)) regarding the original portions.

Bio-source extensions, Zotero sync, bilingual output, keyword-discovery
tooling, Obsidian-optional standalone mode, and top-tier journal filtering
© Chuzhi Zhao, released under MIT.
