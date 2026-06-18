# gtm-icp

**GTM-as-code: a backend-free Claude plugin that finds B2B accounts matching your ICP, then enriches, classifies and scores them into a prioritized target list for outreach — grounded on your own corpus, bring your own keys.**

> Working name. The pipeline runs end to end —
> `discover → enrich (+ signals) → classify → score → people → list → personalize`
> — from an ICP brief to a ranked, evidence-backed target list with contacts and
> grounded first-draft outreach.

Most ICP/lead-scoring tools are hosted SaaS that mark up data you already pay
for and score accounts with a generic prompt. `gtm-icp` flips both:

- **No hosted backend.** The pipeline is a set of Claude skills with bundled,
  stdlib-only Python scripts. Clone it, install it as a plugin, run it.
- **Bring your own keys, no markup.** Enrichment calls your Apollo key directly
  (sync REST). The orchestration and LLM reasoning cost cents; the data is
  yours at vendor cost, not resold.
- **Grounded, not guessed.** Classification cites *your* won-deal patterns,
  positioning, and case studies — from a local corpus or a K2 corpus — instead
  of a bare LLM judgment. That grounding is the part a commodity wrapper can't
  trivially copy.
- **A real no-key path.** Without any paid key, enrich runs firmographic-only
  and classify grounds on local corpus files. You get a working
  clone-and-run demo with zero secrets.

## Pipeline stages

| Stage | Status | What it does |
|-------|--------|--------------|
| **`discover`** | **implemented** | Find ICP-fit companies from a brief — Perplexity-grounded, with no-key DuckDuckGo + seed-list fallbacks. |
| **`enrich`** | **implemented** | Firmographics (Apollo-first, no-key fallback) **+ deep intent signals** from website, public ATS job boards (Greenhouse/Lever/Ashby), and GitHub — mapped to ICP scoring dimensions. |
| **`classify`** | **implemented** | Corpus-grounded ICP verdict — hard gates + graded dimensions → deterministic 0-100 score & A/B/Nurture/Reject tier. |
| **`people`** | **implemented** | Apollo people search — map the ICP's buying personas to real contacts per qualified account; no-key persona-target fallback. |
| **`list`** | **implemented** | Aggregate every scored account into a ranked CSV + per-account markdown dossier (gates, signals, contacts) for GTM hand-off. |
| **`personalize`** | **implemented** | Draft grounded outreach per contact from the detected signals — deterministic template with no key, rewritten by the model on the same evidence. |

## Quickstart (no keys)

Requires Python 3.10+. From the repo root:

```bash
# 1. Discover companies from a seed list (deterministic, no key) — or use
#    --brief "<ICP description>" once PERPLEXITY_API_KEY is set.
printf 'Acme Robotics, acme.example\n' > /tmp/seeds.csv
python3 skills/discover/scripts/discover.py --seeds /tmp/seeds.csv

# 2. Enrich a discovered (or supplied) account (local, no paid key)
python3 skills/enrich/scripts/enrich.py --slug acme-example --local

# 2b. Collect deep intent signals — scans the website, public ATS job boards
#     (Greenhouse/Lever/Ashby), and GitHub for the ICP's signal keywords
#     (e.g. hiring LangChain engineers). No key needed.
python3 skills/enrich/scripts/signals.py --slug acme-example

# 3. Classify is the LLM step — in Claude, run /gtm-icp:classify acme-robotics
#    Claude reads enrich.json + icp.criteria.json, grounds on corpus/, and
#    writes .gtm/acme-robotics/classify.json. Then score it:
python3 skills/classify/scripts/score.py --slug acme-robotics

# 4. Find contacts in a qualified account (no key -> persona targets; with
#    APOLLO_API_KEY -> real contacts mapped to the ICP's personas).
python3 skills/people/scripts/people.py --slug acme-robotics

# 5. Build the hand-off list across every scored account ->
#    .gtm/_report/accounts.csv + .gtm/_report/dossier.md
python3 skills/list/scripts/build_list.py

# 6. Draft grounded outreach per contact (no-key template; in Claude,
#    /gtm-icp:personalize acme-robotics then rewrites it on the same evidence).
python3 skills/personalize/scripts/personalize.py --slug acme-robotics
```

Per-account artifacts land in `.gtm/<slug>/`; the hand-off CSV + dossier land in
`.gtm/_report/` (all git-ignored). See `skills/_shared/artifact-structure.md`.

## As a Claude plugin

Installed as a plugin, the stages are slash commands:

- `/gtm-icp:discover <ICP brief | --seeds path>` — find ICP-fit companies
- `/gtm-icp:enrich <company | domain | account.json>`
- `/gtm-icp:classify <slug>`
- `/gtm-icp:people <slug>` — find contacts in a qualified account
- `/gtm-icp:list [--include-reject]` — build the ranked CSV + dossier hand-off
- `/gtm-icp:personalize <slug>` — draft grounded outreach for an account's contacts
- `/gtm-icp:pipeline <ICP brief | company | accounts.json>` — runs discover→enrich→classify→score→people→list→personalize end to end

Configure keys via the plugin's `userConfig` (`apollo_api_key`,
`perplexity_api_key`, `k2_api_host` / `k2_api_key` / `k2_corpus_id`) — all
optional; absent keys fall back to the local path.

## Configuration

| Key | Used by | Effect when absent |
|-----|---------|--------------------|
| `apollo_api_key` | enrich, people | Firmographic-only enrichment; people returns persona targets (titles) instead of contacts. |
| `perplexity_api_key` | discover | Discovery falls back to no-key DuckDuckGo + a seed list; supply accounts directly with `--seeds`. |
| `k2_api_host` + `k2_api_key` + `k2_corpus_id` | classify, personalize | Grounding (via `skills/classify/scripts/k2_query.py` → `POST …/search:batch`) falls back to local `corpus/` files. |

The ICP model has three parts (see `icp.criteria.json`): **hard gates** (any
failure → `Reject`), **graded scoring dimensions** (partial points up to
`max_points` each), and **tier thresholds** on the normalized 0-100 score. Tier
cutoffs default to the ICP file's `thresholds` (example: A=75, B=60), overridable
via `GTM_TIER_A` / `GTM_TIER_B`. Artifact root: `GTM_ARTIFACT_ROOT` (default
`.gtm`).

## Testing

Offline, no network, no keys:

```bash
bash skills/enrich/scripts/tests/test_enrich.sh
bash skills/enrich/scripts/tests/test_signals.sh
bash skills/classify/scripts/tests/test_score.sh
bash skills/classify/scripts/tests/test_k2_query.sh
bash skills/discover/scripts/tests/test_discover.sh
bash skills/people/scripts/tests/test_people.sh
bash skills/list/scripts/tests/test_list.sh
bash skills/personalize/scripts/tests/test_personalize.sh
```

## Design notes

The open-core boundary is deliberate: everything backend-free lives here as
skills (interactive, BYO-keys, developer-first). The parts that genuinely need
a server — scheduling, durable multi-day sequences, team state — are a separate
hosted layer, not part of this OSS core.

## License

MIT — see [LICENSE](LICENSE).
