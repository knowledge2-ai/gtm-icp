---
name: personalize
description: >-
  Draft grounded outreach for a qualified account's contacts — one message each,
  grounded on the intent signals enrich actually found (a hiring signal, a
  product gap), not generic flattery. A deterministic script writes template
  drafts with no key; then rewrite each body to sound human, grounded on the
  same evidence. Use after people (and ideally list).
---

# Personalize

The pipeline's outreach stage. `list` says who to reach and why; this stage turns
that into first-draft messages a rep can edit and send.

## When to use

- An account is qualified (A/B) and has contacts (or persona targets) from the
  people stage, and the user wants outreach copy.
- The user asks to "draft emails", "write the outreach", or "personalize" for an
  account.

Reject-tier accounts are skipped by default (`--force` to override).

## Inputs

Per account dir under the artifact root:

- `score.json` (**required**) — tier + rationale.
- `signals.json` (optional) — the evidence the drafts are grounded on.
- `people.json` (optional) — contacts to address (or persona targets if no key).
- `enrich.json` (optional) — company + domain.
- `icp.criteria.json` — an optional `outreach` block (`angle`, `offer`, `cta`)
  overrides the default messaging; an optional `positioning` block supplies
  `value_pillars` (what you actually win on) and `do_not_say` (forbidden claims).

## Workflow

1. Make sure the account has been scored and had `people` run.
2. Generate the template drafts (no key required):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/personalize/scripts/personalize.py --slug <slug>
   ```

   This writes `.gtm/<slug>/personalize.json` — one draft per contact (or per
   persona target when no contacts were resolved), each with a `subject`,
   `body`, `cta`, a `template` name (the persona/signal-routed scaffold used),
   and a `grounded_on` list: the exact signals the body is allowed to cite,
   ordered **freshest first** (a recent signal outranks a stale one; undated
   signals stay neutral, so a strong older signal isn't dropped). Drafts start
   with `status: "template"`.
3. **Elevate the copy.** For each draft, rewrite the `body` so it reads like a
   real person wrote it — specific, short, no filler — **grounded only on that
   draft's `grounded_on` evidence**. Lead on the **most recent** signal
   (`grounded_on` is already recency-ordered, and items carry a `published_at`
   when known) — do not anchor on something that reads as stale or years-old.
   Do not invent facts, numbers, or signals that aren't in `grounded_on`; if the
   grounding is thin (a `no public signals` warning), say less rather than
   fabricate. **Tie the offer to one `value_pillars` entry** (top-level in
   `personalize.json`) — say what you actually win on, backed by its proof —
   and **never use a `do_not_say` phrase**. Each draft carries
   `guardrail_warnings`: any forbidden phrase the template tripped. After you
   rewrite, re-check your copy against `do_not_say` and clear those warnings —
   a banned claim left in is a hard stop. Follow the selected `template`'s
   framing, keep the subject tight, set `status: "llm"` on the drafts you
   rewrite, and write the updated `personalize.json` back.
4. Surface the drafts to the user for review. **Do not send anything** — outreach
   is sent by the user through their own tool; this stage only drafts.

## Output

`personalize.json`:

```json
{
  "company_name": "...", "domain": "...", "tier": "A",
  "drafts": [
    {"recipient": "Dana Lee", "to": "dana@acme.example", "to_status": "verified",
     "title": "VP Product", "persona": "Chief Product Officer",
     "channel": "email", "template": "exec-ai-urgency",
     "subject": "...", "body": "...", "cta": "...",
     "grounded_on": [{"signal": "ai_hiring", "snippet": "...hiring for LangChain...",
                      "published_at": "2026-05-01"}],
     "status": "template"}
  ],
  "warnings": []
}
```

## Notes

- **`to` is the send address.** It carries the email the people stage revealed,
  with `to_status` (e.g. `verified`) for confidence. When a contact resolved but
  Apollo had no email, `to` is blank and a warning flags the draft as not
  send-ready — it still names a real person to reach another way.
- **Grounded, not guessed.** The whole point is that "I noticed you're hiring
  LangChain engineers" beats "I hope this email finds you well." The
  `grounded_on` list is the contract — copy that strays from it is a regression.
- **No-key path is real.** The template draft is genuinely usable on its own;
  the model just makes it sharper. Same open-core boundary as enrich/people.
- The script is stdlib-only and offline-testable:
  `bash scripts/tests/test_personalize.sh`.

This is the last stage: from an ICP brief to grounded, ready-to-edit outreach.
