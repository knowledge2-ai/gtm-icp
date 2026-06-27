---
name: enrich
description: >-
  Enrich a B2B account before ICP classification — firmographics (industry,
  size, revenue, location) plus deep intent signals scraped from the company
  website, careers/jobs pages, and GitHub. Apollo-first for firmographics when
  APOLLO_API_KEY is set; signals need no paid key. Use after discover, or
  directly on accounts the user supplies.
---

# Enrich

Turn a bare account record (a name + domain, or a discover result) into a
firmographic profile the `classify` skill can judge against the ICP.

## When to use

- The user hands you a company, a domain, or a list of accounts to qualify.
- A `discover` run produced raw accounts that need firmographic fill-in.
- You have an account but are missing size / industry / revenue to score it.

## Inputs

A single account record as JSON with at least one of `domain` / `website`, plus
any fields already known (`company_name`, `industry`, `employee_count`, …). The
record lives at `.gtm/<slug>/input.json` (write it there first if the user gave
you the account conversationally — see `_shared/artifact-structure.md`).

## Workflow

1. Resolve the account slug (`scripts/gtm_lib.py:slugify` on the company name or
   domain) and make sure `.gtm/<slug>/input.json` exists.
2. Run the enrichment script:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/enrich/scripts/enrich.py --slug <slug>
   ```

   - With `APOLLO_API_KEY` set, this calls Apollo's organization-enrichment
     endpoint by domain and maps firmographics. Apollo is a **synchronous REST
     API**, so it works fine from a skill with no backend.
   - Without a key (or with `--local`), it runs the **no-key fallback**:
     firmographic-only, sourced from what the record already carries. This is the
     clone-and-run path — no paid vendor required to qualify on firmographics.
3. The script writes `.gtm/<slug>/enrich.json` and prints a one-line summary
   (slug, enrichment_source, artifact path). Surface the `enrichment_source` to
   the user so they know whether the data came from Apollo or the local path.
4. **Collect deep intent signals** (no paid key required):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/enrich/scripts/signals.py --slug <slug>
   ```

   This fetches the homepage, careers pages, **public ATS job boards
   (Greenhouse / Lever / Ashby)**, and GitHub repos, then scans them for the
   keyword groups the ICP declares under `signals` — each group names the
   scoring `dimension` it informs. The ATS boards are public JSON APIs; the
   script first looks for a board link (`boards.greenhouse.io/<slug>`,
   `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`) embedded in the company's
   own careers HTML — that's the *real* slug, so it resolves boards a name-based
   guess can't (e.g. Sourcegraph's board is `sourcegraph91`). It falls back to
   guessing the slug from the name/domain when no link is in the HTML (JS-only
   careers pages won't expose one). Boards give structured per-posting text
   (where hiring signals actually live) and are surfaced in `signals.json` under
   `hiring_boards` (provider + slug + `discovery` provenance + posting
   titles/URLs) for the GTM rep. It writes `.gtm/<slug>/signals.json`
   with, per group, the `matched_keywords`, the `evidence` (source URL + snippet),
   and a `found` flag. Example: an ICP `ai_hiring` group (`langchain`, `llm`, …)
   that `informs` `commercial_urgency` will fire on a careers page hiring LangChain
   engineers — turning "they feel AI pressure" into a cited fact. **Absence is
   evidence too**: `found: false` on AI-product keywords legitimately widens an
   incumbent's `ai_gap`. `GITHUB_TOKEN` is optional (raises the GitHub rate
   limit); all fetches are best-effort and degrade to warnings.
   - **Signal decay + combinations.** `signals.json` also carries a
     `signal_summary`: each found signal is age-decayed from its evidence date
     (0–30d full weight, then 75/50/25%, expiring past ~6 months — undated stays
     neutral), weighted by the signal's `points` (declared per group in
     `icp.criteria.json`), and a one-time combination bonus is added when ≥2
     *fresh* signals co-fire. `weighted_score` is the headline. The point: a
     stale "hiring LangChain" post from last year shouldn't read as live intent,
     and two fresh signals together predict more than either alone. `classify`
     should lean on **fresh** signals and discount `expired` ones.
5. For a batch, loop both scripts per account — one artifact dir each. (Entity
   fan-out across many accounts is the natural place to dispatch parallel
   subagents; keep each account's enrich isolated to its own slug.)

## Notes

- **Contacts vs firmographics.** The local path qualifies on *firmographics*
  only. Verified contact data (direct dials, valid emails) requires a paid
  vendor key — that's a deliberate boundary, not a gap to paper over.
- **Clay** is async/webhook-based, so it does not fit a backend-free skill the
  way Apollo's sync REST does. Prefer Apollo (or a direct provider) here.
- **Signals feed the score, not just the file.** The point of `signals.json` is
  that `classify` cites it as per-dimension evidence — a hiring signal is what
  makes a `commercial_urgency` score defensible instead of a guess. Don't collect
  signals and ignore them.
- Both scripts are stdlib-only and offline-testable:
  `bash scripts/tests/test_enrich.sh` and `bash scripts/tests/test_signals.sh`.

This is the data stage. The judgment happens next in `classify`.
