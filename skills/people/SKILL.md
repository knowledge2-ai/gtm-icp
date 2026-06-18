---
name: people
description: >-
  Find the right contacts inside a qualified account — map the ICP's buying
  personas (CPO, CTO, Head of Data, …) to real people via Apollo's people-search
  API. Apollo-first when APOLLO_API_KEY is set; without a key it returns the
  persona targets (the titles to pursue) so the stage is still actionable. Use
  after an account scores A/B in classify+score.
---

# People

Qualification tells you *which* accounts to pursue. This stage answers *who* to
reach inside each one: it turns the ICP's buying `personas` into a contact list
for outreach.

## When to use

- An account has been scored (A or B tier) and you need names/titles to contact.
- The user asks "who do we reach out to at <account>?"
- You're assembling the GTM hand-off list and need contacts per account.

Don't burn Apollo credits on Reject-tier accounts — the script skips them by
default (override with `--force`).

## Inputs

Per-account artifacts under `.gtm/<slug>/`:

- `enrich.json` (or `input.json`) — for the company name and domain.
- `score.json` (optional) — for the tier gate (Reject → skipped unless `--force`).
- `icp.criteria.json` — declares the buying `personas`, each with a `priority`
  and the `apollo_titles` to search for.

## Workflow

1. Make sure the account has been enriched (and ideally scored) — the people
   stage reads `enrich.json` and `score.json`.
2. Run the people script:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/people/scripts/people.py --slug <slug>
   ```

   - With `APOLLO_API_KEY` set, this POSTs Apollo's people-search by the
     account domain for every persona title the ICP declares
     (`include_similar_titles` on), compacts the results, and **maps each
     contact back to its persona** by title overlap — so a "VP of Product"
     lands under the "Chief Product Officer" persona with its `priority`.
     Apollo's people-*search* only returns a teaser (first name, obfuscated
     last initial, title, person id), so the stage then calls Apollo's **People
     Match** endpoint to reveal the full name + real email. Each matched
     contact comes back `revealed: true` with a verified `email` when Apollo
     holds one; contacts with no email on file resolve to a full name +
     `email_status: unavailable` (`revealed: false`). The match spends Apollo
     credits — that's the deliverable of this stage.
   - Without a key (or with `--local`), it runs the **no-key fallback**: it
     returns the `persona_targets` — the exact titles a rep should go find.
     Still actionable, zero secrets. Verified contact data (emails, direct
     dials) is the deliberate paid-key boundary, same as in `enrich`.
3. The script writes `.gtm/<slug>/people.json` and prints a one-line summary
   (slug, tier, source, people count, persona-target count). Surface `source`
   so the user knows whether they got real contacts or persona targets.
4. For a batch, loop per qualified account — one `people.json` each. (Good place
   to fan out subagents, one account per slug.)

## Output

`people.json`:

```json
{
  "company_name": "...", "domain": "...", "tier": "A",
  "source": "apollo",
  "titles_targeted": ["chief product officer", "vp product", ...],
  "people": [
    {"name": "Josh Nguyen", "title": "Head of Data & AI Products",
     "persona": "Head of Data / AI", "persona_priority": "secondary",
     "email": "josh.nguyen@example.com", "email_status": "verified",
     "linkedin_url": "...", "location": "...", "organization_name": "...",
     "apollo_id": "6725...", "revealed": true}
  ],
  "persona_targets": [],
  "warnings": []
}
```

When `source` is `local`, `people` is empty and `persona_targets` carries the
titles to pursue. With `source: apollo`, each contact carries its full name; the
`revealed` flag tells you whether a verified `email` was unlocked (`true`) or
Apollo had no email on file (`false`, `email_status: unavailable`).

## Notes

- **One ICP, many personas.** Personas live in `icp.criteria.json` so the same
  rubric that scores the account also defines who to sell to. Edit them there.
- **Apollo is sync REST** — fine from a backend-free skill, same as `enrich`.
  Verify the endpoint against current Apollo docs before production use.
- The script is stdlib-only and offline-testable:
  `bash scripts/tests/test_people.sh`.

This is the contact-resolution stage. Next is the GTM hand-off list.
