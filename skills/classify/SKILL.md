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
- `.gtm/<slug>/signals.json` — deep intent signals from `enrich`'s signal scan
  (website / careers / GitHub keyword hits), if present. Each detected signal
  names the `dimension` it `informs` and carries cited `evidence` — use it.
- The ICP — read `icp.criteria.json` at the repo root (or the user-supplied
  path). It has four parts:
  - **`gates`** — boolean must-pass conditions. One failure → tier `Reject`.
  - **`dimensions`** — graded conditions, each worth up to `max_points`.
  - **`thresholds`** — `tier_a` / `tier_b` cutoffs on the normalized 0-100 score.
  - **`anti_icp`** — red flags (soft disqualifiers). Unlike a gate, a single red
    flag is tolerated; `deprioritize_at` (default 2) co-occurring flags demote the
    account to Nurture regardless of score.
  - **`signals`** — keyword groups that map public-source signals to dimensions
    (consumed by `enrich`'s signal scan; you read the *results* in `signals.json`).

## Workflow

1. **Ground the judgment.** Retrieve relevant context for this account:
   - If `k2_api_host` / `k2_api_key` / `k2_corpus_id` are configured, pull
     won-deal patterns, positioning, and case-study evidence from your corpus
     with the bundled retrieval client (real `POST …/search:batch`, no MCP
     server needed):

     ```bash
     python3 ${CLAUDE_PLUGIN_ROOT}/skills/classify/scripts/k2_query.py --slug <slug> --write
     ```

     It writes `.gtm/<slug>/grounding.json` — `status: "ok"` with ranked
     `snippets`, each carrying `text`, `score`, a `source` provenance URI, and
     `metadata` (tier, total_score, ai_posture, persona_titles, outreach_angle,
     signal_tags, company/domain). Cite only what the snippets actually say.
   - If K2 is not configured, or the call returns `status: "skipped"`/
     `"warning"`, the same file carries `local_fallback`: the local corpus
     files under `corpus/` (markdown notes on past wins, ICP rationale). Read
     those instead — local grounding is the no-key path.
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
   **Use `signals.json`:** for each dimension, pull the signal groups whose
   `informs` matches it. A `found: true` signal is concrete evidence to award
   points and should be quoted in the `evidence` line (e.g. "careers page hiring
   LangChain engineers → actively closing AI gap" for `commercial_urgency`). A
   `found: false` AI-product signal is itself evidence of a wide `ai_gap`. Prefer
   a cited signal over a firmographic guess.
4. **Check the red flags.** For each `anti_icp.red_flags` entry, decide
   `present: true|false` with one line of `evidence`. Default to `present: false`
   unless the evidence actually shows it — red flags deprioritize, so don't guess
   them into existence. A lone flag is fine; the score script demotes only when
   `deprioritize_at` or more co-occur.
5. **Write the verdict** to `.gtm/<slug>/classify.json`:

   ```json
   {
     "company_name": "...",
     "gates": [
       {"key": "established",    "passed": true,  "evidence": "Founded 2014 (enrich.json)."},
       {"key": "not_ai_native",  "passed": true,  "evidence": "Core product is a TMS; AI is a recent add-on."}
     ],
     "red_flags": [
       {"key": "ai_native_pivot",     "present": false, "evidence": "AI is an add-on module, not the core product."},
       {"key": "active_displacement", "present": false, "evidence": "No competing AI platform mentioned."}
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

6. **Score deterministically:**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/classify/scripts/score.py --slug <slug>
   ```

   This reads `classify.json` and writes `.gtm/<slug>/score.json` with a
   normalized 0-100 `score` (sum of awarded points / sum of max points), a tier,
   and the breakdown. The score is pure arithmetic over your verdicts — **no LLM
   judges the score**, so it's reproducible and auditable. A failed gate forces
   tier `Reject`; `deprioritize_at`+ red flags force `Nurture` (with
   `deprioritized: true`); otherwise the score maps to `A` / `B` / `Nurture` by
   the ICP thresholds (override with `GTM_TIER_A` / `GTM_TIER_B`, red-flag limit
   with `GTM_RED_FLAG_LIMIT`).
7. **Report** the tier, score, any failed gates or red flags, and the dimensions
   that drove the score, with the evidence lines. The evidence is the point — a
   number with no grounding is what every commodity wrapper already produces.

## Why grounding matters here

A bare LLM can guess ICP fit from firmographics. The defensible version cites
*your* won deals and positioning — that's the difference between a replaceable
wrapper and a tool that encodes what your team has learned. Lead with the
evidence, not the score.

Offline tests: `bash scripts/tests/test_score.sh` (the scorer) and
`bash scripts/tests/test_k2_query.sh` (the K2 retrieval client — runs with an
injected fetcher, no key, no network).
