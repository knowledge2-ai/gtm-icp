---
name: list
description: >-
  Build the GTM hand-off — aggregate every scored account into a ranked CSV
  (one row per account, tier then score) plus a markdown dossier (gates, score
  breakdown, intent signals with evidence, hiring board, and contacts). Run
  after accounts have been scored (and ideally had people resolved). No keys.
---

# List

The pipeline's final stage. The per-account stages each leave one artifact under
`.gtm/<slug>/`; this stage reads across all of them and produces the two things a
GTM team works from: a ranked **CSV** and a per-account **dossier**.

## When to use

- A batch of accounts has been scored and you want the prioritized list.
- The user asks for "the target list", "the CSV", or "a dossier" for outreach.
- You're wrapping up a `pipeline` run and need the hand-off deliverables.

## Inputs

Reads, per account dir under the artifact root:

- `score.json` (**required** — an account without it is skipped) for tier/score.
- `enrich.json` (optional) for company/domain.
- `signals.json` (optional) for detected intent signals + the hiring board.
- `people.json` (optional) for contacts or persona targets.

## Workflow

1. Make sure the accounts you want included have been scored (`score.json`
   exists). Resolving `people` first enriches the list with contacts, but isn't
   required.
2. Run the build script:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/list/scripts/build_list.py
   ```

   - It scans every account dir under the artifact root, ranks accounts **A → B
     → Nurture → Reject** (and by score within a tier), and writes
     `_report/accounts.csv` + `_report/dossier.md` under the artifact root.
   - Reject-tier accounts are listed last and excluded by default; pass
     `--include-reject` to keep them in both outputs.
3. The script prints a one-line summary (accounts, actionable count, output
   paths, top 5). Point the user at the two files.

## Output

- **`_report/accounts.csv`** — columns: `rank, company, domain, tier, score,
  gates_failed, signals_found, hiring_provider, contacts, people_source,
  top_contact, top_contact_title, top_contact_email, top_contact_email_status,
  top_contact_linkedin, rationale`. The top contact is the highest-priority
  persona match for the account; `top_contact_email` carries the address the
  people stage revealed (blank when Apollo had none, with the reason in
  `top_contact_email_status`).
- **`_report/dossier.md`** — one section per account: tier/score, rationale,
  failed gates, score breakdown, detected intent signals (with an evidence
  snippet), the hiring board (provider/slug + how it was discovered), and the
  contacts — or the persona targets when no contacts were resolved.

## Notes

- The dossier mirrors the scoring model so a rep can see *why* an account ranks
  where it does — gates, graded dimensions, and the signals that drove them —
  not just a number.
- Aggregation reads the artifact root directly, so it composes with entity
  fan-out: score N accounts in parallel, then build the list once over all of
  them.
- Stdlib-only and offline-testable: `bash scripts/tests/test_list.sh`.

This is the end of the pipeline: a prioritized, evidence-backed list to act on.
