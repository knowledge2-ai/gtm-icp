---
name: classify
description: >-
  Judge whether an enriched account fits the ICP, grounded on your own knowledge
  corpus (won-deal patterns, positioning, case studies) rather than a generic
  prompt. Emits a pass/fail verdict per hard gate and a graded score per
  dimension, then runs a deterministic scorer to produce a 0-100 fit score and an
  A/B/Nurture/Reject tier. Use after enrich.
---

# Classify

Decide if an account fits the Ideal Customer Profile — and *why* — then score it
deterministically. This is the stage where corpus grounding is the edge: the
judgment is made against what has actually won, not a bare LLM guess.

## When to use

- An account has been enriched (`.gtm/<slug>/enrich.json` exists) and needs an
  ICP fit verdict + score.
- The user wants to know not just the score but the evidence behind it.

## Inputs

- `.gtm/<slug>/enrich.json` — the firmographic profile from `enrich`.
- The ICP — read `icp.criteria.json` at the repo root (or the user-supplied
  path). It has three parts:
  - **`gates`** — boolean must-pass conditions. One failure → tier `Reject`.
  - **`dimensions`** — graded conditions, each worth up to `max_points`.
  - **`thresholds`** — `tier_a` / `tier_b` cutoffs on the normalized 0-100 score.

## Workflow

1. **Ground the judgment.** Retrieve relevant context for this account:
   - If `k2_api_host` / `k2_api_key` are configured, query the corpus via the
     bundled K2 stdio MCP shim for won-deal patterns, positioning, and
     case-study evidence matching the account's vertical/size.
   - Otherwise, read local corpus files under `corpus/` (markdown notes on past
     wins, ICP rationale). Local grounding is the no-key path.
   - **You are the agent here** — there is no model call to orchestrate. Reason
     over the enrichment + the grounded evidence directly.
2. **Evaluate the gates.** For each gate, decide `passed: true|false` and write
   one line of `evidence` citing the enrichment field or grounded snippet. If a
   gate genuinely can't be evaluated from the evidence, mark it `passed: false`
   and say why — don't assume a pass. Any failed gate disqualifies the account.
3. **Grade the dimensions.** For each scoring dimension, award `points_awarded`
   between 0 and `max_points` (carry `max_points` through from the ICP), with one
   line of `evidence`. Partial credit is expected — reserve full points for
   strong, cited evidence. Do not invent firmographics absent from `enrich.json`.
4. **Write the verdict** to `.gtm/<slug>/classify.json`:

   ```json
   {
     "company_name": "...",
     "gates": [
       {"key": "established",    "passed": true,  "evidence": "Founded 2014 (enrich.json)."},
       {"key": "not_ai_native",  "passed": true,  "evidence": "Core product is a TMS; AI is a recent add-on."}
     ],
     "dimensions": [
       {"key": "ai_gap",             "points_awarded": 24, "max_points": 30, "evidence": "..."},
       {"key": "data_workflow_moat", "points_awarded": 20, "max_points": 25, "evidence": "..."},
       {"key": "commercial_urgency", "points_awarded": 12, "max_points": 20, "evidence": "..."},
       {"key": "budget_access",      "points_awarded": 11, "max_points": 15, "evidence": "..."},
       {"key": "feasibility",        "points_awarded":  7, "max_points": 10, "evidence": "..."}
     ]
   }
   ```

5. **Score deterministically:**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/classify/scripts/score.py --slug <slug>
   ```

   This reads `classify.json` and writes `.gtm/<slug>/score.json` with a
   normalized 0-100 `score` (sum of awarded points / sum of max points), a tier,
   and the breakdown. The score is pure arithmetic over your verdicts — **no LLM
   judges the score**, so it's reproducible and auditable. A failed gate forces
   tier `Reject`; otherwise the score maps to `A` / `B` / `Nurture` by the ICP
   thresholds (override with `GTM_TIER_A` / `GTM_TIER_B`).
6. **Report** the tier, score, any failed gates, and the dimensions that drove
   the score, with the evidence lines. The evidence is the point — a number with
   no grounding is what every commodity wrapper already produces.

## Why grounding matters here

A bare LLM can guess ICP fit from firmographics. The defensible version cites
*your* won deals and positioning — that's the difference between a replaceable
wrapper and a tool that encodes what your team has learned. Lead with the
evidence, not the score.

Offline scorer test: `bash scripts/tests/test_score.sh`.
