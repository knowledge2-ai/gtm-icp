---
name: report
description: >-
  Build an investor-grade Excel workbook (.xlsx) from scored ICP artifacts —
  Overview funnel, dollar TAM/SAM/SOM market sizing, ICP methodology, per-track
  ranked accounts with dimension breakdowns, A-tier scoring evidence, and a
  contact list. Run after accounts are scored (and ideally have people +
  personalize). Requires openpyxl. No keys.
---

# Report

A presentation layer on top of the pipeline. Where `list` writes a working CSV +
dossier for a rep, `report` assembles a styled, multi-sheet **`.xlsx`** an
operator can put in front of investors — the same evidence, formatted for a deck.

## When to use

- The user asks for "a spreadsheet", "an Excel file", "an investor report", or
  "something to show investors / the board".
- A batch has been scored (and ideally had `people` + `personalize` run) and you
  want a polished, multi-sheet deliverable rather than the raw CSV.

## Requirements

- **openpyxl** (`python3 -m pip install openpyxl`). The script exits with an
  install hint if it's missing. Confirm with the user before installing.

## Inputs

Reads, per account dir under each track's artifact root:

- `score.json` (**required** — non-scored / Reject accounts are skipped) — tier, score, rationale.
- `classify.json` (optional) — per-dimension `points_awarded` + evidence, gate/red-flag verdicts.
- `enrich.json` (optional) — firmographics (employees, founded, revenue, country, vertical, domain).
- `people.json` (optional) — resolved contacts (email-domain mismatches are flagged).
- `personalize.json` (optional) — counted for the "outreach drafts" funnel metric.

The **addressable-market counts** (universe / sample / per-vertical) are external
Apollo figures the workbook cannot derive — supply them as inputs (see below).

## Workflow

1. Make sure the accounts you want included are scored. Running `people` and
   `personalize` first enriches the Contacts sheet and the drafts metric.
2. **(Optional — for the Market Size sheet)** gather the live addressable counts
   with the discover skill's search in count mode, e.g.:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/discover/scripts/apollo_search.py --count \
     --tags telematics "field service management" "construction software" "legal software" \
     --founded-max 2024
   ```

   Run it once broad (for `--universe`) and once tight (for `--sample`); for the
   per-vertical table, run `--count` per tag and save a `{tag: count}` JSON.
3. Build the workbook (one `--track` per ICP rubric — `ROOT:CRITERIA:LABEL`):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/report/scripts/build_report.py \
     --track ".gtm:icp.criteria.json:Revenue" \
     --track ".gtm-dp:icp.design-partner.json:Design Partner" \
     --acv 50000 --universe 280031 --sample 36356 \
     --vertical-counts vertical_counts.json \
     --core-tags telematics "field service management" "construction software" "legal software" \
     --out _report/investor_report.xlsx \
     --title "GTM / ICP Target Pipeline" --subtitle "Two-track account qualification"
   ```

   - For a **single track**, omit `--track` and pass `--criteria icp.criteria.json`;
     the root comes from `GTM_ARTIFACT_ROOT` (default `.gtm`).
   - Omit `--universe`/`--sample` to skip the Market Size sheet entirely.
   - The script prints a one-line JSON summary (output path, sheets, funnel counts).

## Output

A single `.xlsx` with these sheets:

- **Overview** — KPI band + pipeline funnel (every count computed from artifacts)
  and a Data-sources footnote.
- **Market Size** *(only with `--universe`/`--sample`)* — TAM / SAM / SOM in
  dollars at the `--acv` assumption, a TAM→SAM→SOM bar chart, the A-tier target
  pool (validated + hit-rate projection), and addressable market by vertical.
- **Methodology** — each track's gates, weighted dimensions, and tier thresholds,
  read straight from its criteria file.
- **`<Track>` Accounts** — one sheet per track: firmographics, tier/score
  (color-coded A green / B amber), the per-dimension point breakdown as columns,
  top contact, and the scoring rationale.
- **Scoring Detail** — for every A-tier account, each dimension's points and the
  evidence snippet that justified them.
- **Contacts** — every resolved contact with persona, email, and LinkedIn;
  cross-domain email matches flagged "⚠ verify".

## Notes

- **Honesty by construction.** Funnel, hit rate, contacts, and drafts are derived
  from the artifacts; only the Apollo market counts and the ACV are operator
  inputs, and both are labeled as such on the sheets. The A-tier projection is
  shown as an extrapolation with the full-TAM figure marked an upper bound.
- **Dimension columns are derived from each criteria file**, so the report is not
  pinned to any particular rubric — add a dimension and a column appears.
- Composes with entity fan-out and two-track scoring: score N accounts under each
  root, then build one workbook across all tracks.
- openpyxl only (no network, no keys). The GUI app holding the file open will show
  a cached copy — quit it fully before reopening a rebuilt workbook.

This turns a scored pipeline run into a board-ready spreadsheet.
