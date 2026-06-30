---
description: "Build an investor-grade Excel workbook (.xlsx) from scored ICP artifacts: funnel, market sizing, methodology, ranked accounts, scoring evidence, and contacts."
argument-hint: "[--track ROOT:CRITERIA:LABEL ...] [--acv N] [--universe N --sample N] [--out path.xlsx]"
---

# Report (shorthand)

Assemble a styled, multi-sheet `.xlsx` to present a scored pipeline run to investors.

## Invocation

`/gtm-icp:report [--track ROOT:CRITERIA:LABEL ...] [--acv N] [--universe N --sample N] [--vertical-counts file.json] [--out path.xlsx]`

## Behavior

Read `skills/report/SKILL.md` and execute its workflow with the arguments below as the skill input. This file is a thin delegator — do not reimplement skill logic here.

$ARGUMENTS
