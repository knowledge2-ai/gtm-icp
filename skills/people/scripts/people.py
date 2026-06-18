#!/usr/bin/env python3
"""Find the right contacts inside a qualified account (the `people` stage).

Qualification (discover -> enrich -> classify -> score) tells you *which*
accounts to pursue. This stage answers *who* to reach inside each one: it maps
the ICP's buying personas to real contacts via Apollo's people-search API.

Apollo-first with a no-key fallback that still produces value:

  * With APOLLO_API_KEY set, search people at the account's domain for the
    persona titles the ICP declares. Apollo's people-search only returns a
    teaser (first name + obfuscated last initial + title + id), so this stage
    then calls Apollo's People Match endpoint to reveal the real name + email,
    returning fully-resolved contacts (name, title, matched persona, email,
    LinkedIn). The reveal spends Apollo credits — it's the point of the stage.
  * Without a key (or --local), return the *persona targets* — the exact titles
    a rep should go find — so the stage is still actionable with zero secrets.
    This mirrors the boundary in enrich: verified contact data needs a paid key.

By default a Reject-tier account is skipped (don't spend Apollo credits on an
account the rubric already disqualified); pass --force to search anyway.

Personas come from `icp.criteria.json`:

    "personas": [
      {"title": "Chief Product Officer", "priority": "primary",
       "apollo_titles": ["chief product officer", "vp product", "head of product"]}
    ]

Reads  .gtm/<slug>/enrich.json (falls back to input.json) for domain/company,
       .gtm/<slug>/score.json   (optional) for the tier gate.
Writes .gtm/<slug>/people.json

stdlib only. Apollo is called over urllib; no dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

USER_AGENT = "gtm-icp/0.1 (+https://github.com/knowledge2-ai/gtm-icp)"
# Verify against current Apollo docs before relying in production:
# https://docs.apollo.io/reference/people-search
APOLLO_PEOPLE_SEARCH = "https://api.apollo.io/api/v1/mixed_people/api_search"
# People Match (enrichment) — reveals full name/email; spends credits. Opt-in.
# https://docs.apollo.io/reference/bulk-people-enrichment
APOLLO_PEOPLE_MATCH = "https://api.apollo.io/api/v1/people/bulk_match"

# Used when the ICP declares no `personas`.
DEFAULT_PERSONAS = [
    {"title": "Chief Product Officer", "priority": "primary",
     "apollo_titles": ["chief product officer", "vp product", "head of product"]},
    {"title": "CTO / VP Engineering", "priority": "primary",
     "apollo_titles": ["chief technology officer", "vp engineering", "head of engineering"]},
    {"title": "Head of Data / AI", "priority": "secondary",
     "apollo_titles": ["chief data officer", "vp data", "head of data"]},
]


def _clean_domain(value: str) -> str:
    return (value or "").replace("https://", "").replace("http://", "").strip("/").split("/")[0]


def _personas_from_icp(criteria_path: Path) -> list[dict]:
    if criteria_path.exists():
        declared = gtm_lib.read_json(criteria_path).get("personas")
        if isinstance(declared, list) and declared:
            return [p for p in declared if isinstance(p, dict)]
    return DEFAULT_PERSONAS


def _target_titles(personas: list[dict]) -> list[str]:
    """Flatten persona apollo_titles into a de-duplicated, order-stable list."""
    seen, out = set(), []
    for persona in personas:
        for title in persona.get("apollo_titles") or [persona.get("title", "")]:
            t = (title or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _terms(value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (value or "").lower())
            if len(t) > 1 and t not in {"of", "and", "the"}}


def _match_persona(title: str, personas: list[dict]) -> dict:
    """Best persona for a person's title by term overlap with its apollo_titles."""
    title_terms = _terms(title)
    if not title_terms:
        return personas[0] if personas else {}
    best = (0, {})
    for persona in personas:
        haystack = persona.get("title", "") + " " + " ".join(persona.get("apollo_titles", []))
        overlap = len(title_terms & _terms(haystack))
        if overlap > best[0]:
            best = (overlap, persona)
    return best[1] or (personas[0] if personas else {})


def _person_name(item: dict) -> str:
    """Best display name from a person record.

    Apollo's people-*search* endpoint (`mixed_people/api_search`) is a teaser: it
    returns `first_name` and an obfuscated `last_name_obfuscated` (e.g. "S."), but
    NOT the full `name`/`last_name` — those, plus email, are gated behind the paid
    People Match/Enrichment endpoint. So we build the best name available: full
    `name` if a reveal/match populated it, else first + obfuscated last initial.
    """
    if item.get("name"):
        return item["name"]
    first = item.get("first_name") or ""
    last = item.get("last_name") or item.get("last_name_obfuscated") or ""
    return " ".join(p for p in (first, last) if p).strip()


def _email_status(item: dict) -> str:
    """Email availability for a person record.

    A revealed record carries a real `email` + `email_status`. The search teaser
    carries neither — only a `has_email` boolean. Surface that as
    `available_unrevealed` so a slot reads honestly ("email exists, not yet
    revealed") rather than looking like a verified address we don't have.
    """
    email = item.get("email") or ""
    if email and "email_not_unlocked" not in email:
        return item.get("email_status") or "verified"
    if item.get("email_status"):
        return item["email_status"]
    if item.get("has_email"):
        return "available_unrevealed"
    return ""


def _compact_people(payload: dict) -> list[dict]:
    raw = payload.get("people") or payload.get("contacts") or []
    items = raw if isinstance(raw, list) else []
    out = []
    for item in items[:25]:
        if not isinstance(item, dict):
            continue
        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        location = ", ".join(p for p in [item.get("city"), item.get("state"), item.get("country")] if p)
        email = item.get("email") or ""
        # Apollo returns a locked placeholder until a reveal credit is spent.
        if "email_not_unlocked" in email:
            email = ""
        out.append({
            "name": _person_name(item),
            "title": item.get("title") or "",
            "email": email,
            "email_status": _email_status(item),
            "linkedin_url": item.get("linkedin_url") or "",
            "location": location,
            "organization_name": org.get("name") or "",
            "apollo_id": item.get("id") or "",
            # api_search is a teaser; a record is only fully `revealed` once a
            # People Match call has populated the real email.
            "revealed": bool(email),
        })
    return out


def apollo_search_people(domain: str, titles: list[str], api_key: str, *,
                         per_page: int = 8, timeout: float = 12.0) -> dict:
    """POST Apollo people-search by domain + titles. Returns {status, people}."""
    params: list[tuple[str, str | int]] = [
        ("per_page", max(1, min(per_page, 100))),
        ("q_organization_domains_list[]", domain),
        ("include_similar_titles", "true"),
    ]
    for title in titles:
        params.append(("person_titles[]", title))
    url = f"{APOLLO_PEOPLE_SEARCH}?{urlencode(params)}"
    req = Request(url, data=b"{}", method="POST", headers={
        "accept": "application/json", "content-type": "application/json",
        "cache-control": "no-cache", "x-api-key": api_key, "User-Agent": USER_AGENT,
    })
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read(2_000_000).decode("utf-8", errors="replace"))
    return {"status": "ok", "people": _compact_people(payload)}


def apollo_bulk_match(ids: list[str], api_key: str, *, reveal_emails: bool = True,
                      timeout: float = 20.0) -> list[dict]:
    """Reveal full name/email for Apollo person ids via the People Match endpoint.

    This SPENDS Apollo enrichment credits — one per matched person, plus a reveal
    credit when `reveal_personal_emails` surfaces a personal email — so it is
    never the default path; only `gather_people(..., reveal=True)` calls it.
    Returns the raw `matches` list aligned to `ids` (a missed id yields a null).
    Verify the contract against current Apollo docs before production:
    https://docs.apollo.io/reference/bulk-people-enrichment
    """
    body = {
        "details": [{"id": pid} for pid in ids if pid],
        "reveal_personal_emails": bool(reveal_emails),
    }
    req = Request(APOLLO_PEOPLE_MATCH, data=json.dumps(body).encode("utf-8"),
                  method="POST", headers={
                      "accept": "application/json", "content-type": "application/json",
                      "cache-control": "no-cache", "x-api-key": api_key, "User-Agent": USER_AGENT,
                  })
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read(4_000_000).decode("utf-8", errors="replace"))
    matches = payload.get("matches")
    return matches if isinstance(matches, list) else []


def reveal_people(people: list[dict], api_key: str, *, matcher=apollo_bulk_match,
                  reveal_emails: bool = True) -> tuple[list[dict], list[str]]:
    """Upgrade teaser contacts to revealed name/email via Apollo People Match.

    Opt-in and credit-spending. Mutates `people` in place, overlaying the
    revealed `name`/`email`/`linkedin_url` and flipping `revealed=True` for any
    contact whose email unlocked. Returns (people, warnings). Never raises — a
    failed reveal leaves the teaser contacts untouched.
    """
    ids = [p.get("apollo_id") for p in people if p.get("apollo_id") and not p.get("revealed")]
    if not ids:
        return people, []
    try:
        matches = matcher(ids, api_key, reveal_emails=reveal_emails)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return people, [f"apollo reveal failed, kept teaser contacts: {exc}"]
    by_id = {m["id"]: m for m in (matches or [])
             if isinstance(m, dict) and m.get("id")}
    unlocked = 0
    for person in people:
        match = by_id.get(person.get("apollo_id"))
        if not match:
            continue
        name = _person_name(match)
        if name:
            person["name"] = name
        if match.get("linkedin_url"):
            person["linkedin_url"] = match["linkedin_url"]
        email = match.get("email") or ""
        if "email_not_unlocked" in email:
            email = ""
        if email:
            person["email"] = email
            person["email_status"] = match.get("email_status") or "verified"
            person["revealed"] = True
            unlocked += 1
        else:
            person["email_status"] = _email_status(match)
    warnings = [] if unlocked else ["apollo reveal returned no unlocked emails."]
    return people, warnings


def gather_people(account: dict, personas: list[dict], *, api_key: str | None,
                  searcher=apollo_search_people, per_page: int = 8,
                  reveal: bool = True, matcher=apollo_bulk_match) -> dict:
    """Resolve contacts for the account, degrading to persona targets w/o a key.

    `searcher(domain, titles, api_key, per_page=...)` is injectable for offline
    tests. Never raises — a failed Apollo call falls back to persona targets.

    Apollo's people-search only returns a teaser (first name + obfuscated last +
    title + id), so by default this follows it with a People Match call
    (`matcher`, injectable) to unlock the real name/email. Both `searcher` and
    `matcher` are injectable for offline tests; `reveal=False` keeps the teaser
    only (used by the offline teaser test).
    """
    company = account.get("company_name") or account.get("company") or ""
    domain = _clean_domain(account.get("domain") or account.get("website") or "")
    titles = _target_titles(personas)
    persona_targets = [{"title": p.get("title"), "priority": p.get("priority") or "unknown",
                        "apollo_titles": p.get("apollo_titles", [])} for p in personas]

    base = {"company_name": company, "domain": domain, "titles_targeted": titles}

    if not api_key:
        return {**base, "source": "local", "people": [], "persona_targets": persona_targets,
                "warnings": ["APOLLO_API_KEY not set — returning persona targets "
                             "(the titles to pursue); no verified contacts."]}
    if not domain:
        return {**base, "source": "local", "people": [], "persona_targets": persona_targets,
                "warnings": ["no domain on the account — cannot search Apollo."]}

    try:
        result = searcher(domain, titles, api_key, per_page=per_page)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {**base, "source": "local", "people": [], "persona_targets": persona_targets,
                "warnings": [f"apollo people-search failed, returning persona targets: {exc}"]}

    people = []
    for person in result.get("people", []):
        persona = _match_persona(person.get("title", ""), personas)
        people.append({**person,
                       "persona": persona.get("title") or person.get("title", ""),
                       "persona_priority": persona.get("priority") or "unknown"})
    reveal_warnings = []
    if people and reveal:
        people, reveal_warnings = reveal_people(people, api_key, matcher=matcher)

    if not people:
        warnings = ["Apollo returned no contacts for the targeted titles."]
    elif not any(p.get("revealed") for p in people):
        warnings = ["Apollo People Match unlocked no emails for these contacts "
                    "(no email on file, or out of credits). Showing the search "
                    "teaser — real, targeted contacts with name/title but no email."]
    else:
        warnings = []
    return {**base, "source": "apollo", "people": people,
            "persona_targets": [] if people else persona_targets,
            "warnings": warnings + reveal_warnings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find contacts in a qualified account.")
    ap.add_argument("--slug", required=True, help="account slug under the artifact root")
    ap.add_argument("--criteria", type=Path, default=Path("icp.criteria.json"),
                    help="ICP file declaring the buying `personas`")
    ap.add_argument("--per-page", type=int, default=8, help="contacts to request from Apollo")
    ap.add_argument("--local", action="store_true", help="force the no-key persona-target path")
    ap.add_argument("--force", action="store_true", help="search even a Reject-tier account")
    args = ap.parse_args(argv)

    acct_dir = gtm_lib.account_dir(args.slug)
    src = acct_dir / "enrich.json"
    account = gtm_lib.read_json(src if src.exists() else (acct_dir / "input.json"))

    score_path = acct_dir / "score.json"
    tier = gtm_lib.read_json(score_path).get("tier") if score_path.exists() else None
    if tier == "Reject" and not args.force:
        out = {"company_name": account.get("company_name"), "tier": tier, "source": "skipped",
               "people": [], "persona_targets": [],
               "warnings": ["tier is Reject — skipped people search (use --force to override)."]}
    else:
        personas = _personas_from_icp(args.criteria)
        api_key = None if args.local else os.environ.get("APOLLO_API_KEY", "").strip() or None
        out = gather_people(account, personas, api_key=api_key, per_page=args.per_page)
        out["tier"] = tier

    path = gtm_lib.write_json(gtm_lib.stage_path(args.slug, "people"), out)
    print(json.dumps({"slug": args.slug, "tier": tier, "source": out.get("source"),
                      "people": len(out.get("people", [])),
                      "persona_targets": len(out.get("persona_targets", [])),
                      "warnings": len(out.get("warnings", [])), "artifact": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
