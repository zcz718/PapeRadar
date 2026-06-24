# paperadar

A weekly radar for the papers that matter to *you*. Point it at your research
interests and it sweeps the past week across arXiv, Semantic Scholar, OpenAlex,
and Crossref — plus CORE and the biomedical servers (bioRxiv, medRxiv, PubMed)
when you want them — scores everything against your topics, and hands you a
ranked note, written into your Obsidian vault or as plain Markdown in any folder.

It runs as a [Claude Code](https://www.anthropic.com/claude-code) / Codex skill,
so you can just say *"run my weekly papers"* and read the results, or drive the
scripts directly from the command line.

## What it does

- Pulls the last 7 days of new work from every source you've enabled.
- Ranks it against your keywords and arXiv categories, so the order reflects
  what you care about rather than raw popularity.
- Writes a ranked weekly note plus a short scaffold per paper (abstract, links,
  citation IDs, slots for your own notes).
- Optionally files everything in a dated Zotero collection.

Point it at machine learning, physics, economics, genomics, or whatever you
study — you describe your field once and it tunes to your topics.

## Tell the agent your research focus first

The single most useful thing you can do before your first run is describe what
you work on. Inside the skill, just tell the agent:

> "I study machine learning methods for protein structure prediction and drug
> design."

The agent turns that one sentence into your interest profile — domains,
keywords, and arXiv categories — and saves it to your config under
`research_brief`. From then on it reads that brief on every run and never asks
again. Your config file *is* the memory; the brief lives there, not in a prompt
you have to repeat.

You can revisit it any time:

> "What am I tracking?" · "Add diffusion models to my interests." · "Drop the
> economics domain."

## Quickstart

```bash
git clone https://github.com/zcz718/paperadar.git
cd paperadar
pip install -r requirements.txt
python scripts/init_config.py     # or just ask the agent to set you up
```

The wizard asks for your research brief, whether you use Obsidian and Zotero,
and where notes should go, then writes your config. If you'd rather not run it,
the agent walks you through the same thing conversationally on first use.

## Running it

### As a skill

Keep one copy of this repo and symlink it into whichever runner you use:

```bash
ln -s "$(pwd)" ~/.claude/skills/paperadar      # Claude Code
ln -s "$(pwd)" ~/.codex/skills/paperadar       # Codex (optional)
```

`SKILL.md` finds its own directory at runtime, so the same body works under
both. Once it's linked, invoke it three ways:

- **Slash command** — type `/paperadar` in Claude Code.
- **Natural language** — "run my weekly recommendations", "what am I tracking?"
- **Codex** — the explicit `$paperadar …` form, which is the most reliable trigger there.

> **Dependencies.** Before the first run, install the requirements into the
> interpreter the skill will use: `python3 -m pip install -r requirements.txt`.
> If your default `python3` doesn't have them, point the skill at the right one
> with `export PAPERADAR_PYTHON=/path/to/python`. The skill preflight-checks
> this and fails loudly with instructions if anything is missing.

### From the command line

```bash
# See what you're tracking (and your saved brief)
python scripts/show_keywords.py

# Run the search — categories come from your config; 7-day window
python scripts/search_arxiv.py \
  --config /path/to/research_interests.yaml \
  --output arxiv_filtered.json \
  --max-results 200 --top-n 10 --days 7 \
  --categories "cs.LG,cs.AI,stat.ML"

# Turn the results into a weekly index + per-paper notes
python scripts/materialize_weekly_notes.py --input arxiv_filtered.json
```

The config is auto-detected from the standard locations; pass `--config` to
override. Lookup order: `$OBSIDIAN_VAULT_PATH/99_System/Config/research_interests.yaml`,
then `~/.config/paperadar/config.yaml`, then built-in defaults.

## Configuration

Everything lives in one YAML file (`config.example.yaml` is the template).

**Output mode.** `obsidian` writes wikilinked notes into a vault; `standalone`
writes plain Markdown into a folder of your choice — no vault required.

**Research domains.** Named groups of `keywords`, `arxiv_categories`, and a
`priority` (1–5). The union of all your `arxiv_categories` decides which arXiv
papers get fetched, so adding `physics.comp-ph` or `econ.GN` is all it takes to
follow a new field. `priority` (3 is neutral) weights how a topic's matches
rank — a priority-5 topic rises to the top, a priority-1 topic sinks — without
ever excluding a relevant paper.

**Relevance, not source tiers.** Every accessible source is searched, and a
paper surfaces on keyword relevance — not on which site it came from (a
biophysics paper in PubMed still reaches a physicist). What it takes to be
included is a real keyword match (about one title hit); what it takes to rank
near the top is that match landing in a high-`priority` topic.

**Sources beyond arXiv + Semantic Scholar** (both always run and both span
every discipline):

| Setting | Source | Behaviour |
|---|---|---|
| `crossref.enabled: auto` | Crossref (~180M DOIs, all fields) | On by default — no key needed. Catches freshly-registered papers across every field, often before aggregators index them. |
| `bio_sources: true` | bioRxiv, medRxiv, PubMed | Searched by default. Set `false` only to skip them (e.g. to keep a non-biomedical run lean). |
| `openalex.enabled: auto` | OpenAlex (~270M works, all fields) | Needs `OPENALEX_API_KEY` ([free](https://openalex.org), under a minute) — a key is required to reach OpenAlex at all. Skipped silently without one. |
| `core.enabled: auto` | CORE (~400M OA works) | Open-access repositories — theses, working papers, deposits. Needs `CORE_API_KEY` ([free](https://core.ac.uk/services/api)). Skipped without one. |

Every source returns results in one schema and gets re-scored — source-neutrally
— against your config, so adding a source only ever widens coverage.

**Journal filter.** `prioritize_journals` restricts PubMed and Semantic Scholar
to a list of venues (journals or conferences). Preprints, OpenAlex, Crossref,
and CORE are never filtered, so nothing cutting-edge gets dropped. Leave it
empty to disable.

**Scoring (advanced).** The defaults are tuned, but an optional `scoring:` block
lets you set them from YAML instead of editing source: `min_relevance` (the
inclusion gate — lower to see more), the title/abstract/category match weights,
the recency buckets, and the `weights:` that blend the final ranking. See the
commented block in `config.example.yaml`.

### Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `OBSIDIAN_VAULT_PATH` | Obsidian mode | Root of your vault |
| `OPENALEX_API_KEY` | OpenAlex source | Free; unset = source skipped |
| `OPENALEX_EMAIL` | OpenAlex (optional) | Joins the faster "polite pool" |
| `CORE_API_KEY` | CORE source | Free; unset = source skipped |
| `CROSSREF_EMAIL` | Crossref (optional) | Polite pool; falls back to `UNPAYWALL_EMAIL` |
| `NCBI_API_KEY` | PubMed (optional) | Raises the rate limit 3→10 req/s |
| `ZOTERO_API_KEY`, `ZOTERO_USER_ID` | Zotero sync | `ZOTERO_USER_ID` is the numeric ID |
| `UNPAYWALL_EMAIL` | Unpaywall PDF fetch | Their ToS requires a contact email |
| `PAPERADAR_PYTHON` | Pinning the interpreter | Use if default `python3` lacks deps |

Keys exported in your shell rc files are picked up automatically, even from
non-interactive runners.

## Repository layout

```
paperadar/
├── SKILL.md                  # Claude Code / Codex skill driver
├── config.example.yaml       # template for research_interests.yaml
├── requirements.txt
├── agents/openai.yaml        # Codex interface metadata
├── scripts/
│   ├── init_config.py        # first-run setup wizard
│   ├── search_arxiv.py       # orchestrator (arXiv + S2 + dispatch)
│   ├── search_openalex.py    # OpenAlex (cross-disciplinary)
│   ├── search_crossref.py    # Crossref (DOI registry, any field)
│   ├── search_core.py        # CORE (open-access repositories)
│   ├── search_biorxiv.py     # bioRxiv / medRxiv
│   ├── search_pubmed.py      # PubMed via E-utilities
│   ├── materialize_weekly_notes.py  # weekly index + paper scaffolds
│   ├── fetch_fulltext.py     # multi-source full-text/PDF fetch chain
│   ├── generate_note.py      # PDF-verified deep-analysis note
│   ├── save_to_zotero.py     # optional Zotero sync
│   ├── show_keywords.py      # inspect your config + brief
│   └── … shared helpers (_config_paths, _env_resolve, _scoring, …)
└── tests/                    # pytest suite
```

## Credits & thanks

paperadar grew out of [**evil-read-arxiv**](https://github.com/juliye2025/evil-read-arxiv)
by [juliye2025](https://github.com/juliye2025) — the arXiv + Semantic Scholar
search and scoring skeleton that gave this project its start. Thank you for the
head start.

evil-read-arxiv is published without a license, so as a courtesy: if you'd like
to build on *that* original code, it's worth checking in with the author first.
Everything added in paperadar — the OpenAlex and multi-source search, the
Obsidian and Zotero integrations, the brief-based onboarding, and the
orchestration in `SKILL.md` and `scripts/` — is released under MIT.

## License

MIT — see [LICENSE](./LICENSE).
