#!/usr/bin/env python3
"""Draft grounded outreach for a qualified account's contacts (the `personalize` stage).

The list stage produces *who* to reach and *why* (the dossier). This stage turns
that into first-draft outreach — one message per contact (or per persona target
when no contacts were resolved), grounded on the intent signals the enrich stage
actually found, not generic flattery.

Like the rest of the pipeline this has a real no-LLM path: the script fills a
deterministic template from the evidence (clone-and-run, zero keys). The
`personalize` skill then has Claude rewrite each body to sound human and
specific — grounded on the *same* `grounded_on` evidence, inventing nothing. So
the artifact is useful on its own and better with the model on top.

Reads per account dir under the artifact root:
    score.json    (required) tier + rationale; Reject is skipped
    signals.json  (optional) the evidence the drafts are grounded on
    people.json   (optional) contacts / persona targets to address
    enrich.json   (optional) company + domain
Optionally reads an `outreach` block from icp.criteria.json for the angle/offer/cta.

Writes .gtm/<slug>/personalize.json

stdlib only. No network, no keys.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

# Defensible defaults; override per-ICP via an "outreach" block in the criteria file.
DEFAULT_OUTREACH = {
    "angle": "The interesting part isn't bolting on AI for its own sake — it's that "
             "you already own the workflow data most teams have to go buy. That's the "
             "hard part; the AI layer on top is the fast part.",
    "offer": "We map where that data could become an AI feature in about two weeks, "
             "grounded in your actual product.",
    "cta": "Worth a short call to compare notes?",
}


def _evidence_from_signals(signals: dict) -> list[dict]:
    """Pull the concrete, citable evidence the drafts are allowed to lean on."""
    out = []
    for s in signals.get("signals_detected", []):
        if not isinstance(s, dict) or not s.get("found"):
            continue
        ev = s.get("evidence") or []
        snippet = ev[0].get("snippet", "") if ev and isinstance(ev[0], dict) else ""
        out.append({"signal": s.get("key"), "informs": s.get("informs"),
                    "keywords": (s.get("matched_keywords") or [])[:3], "snippet": snippet})
    boards = signals.get("hiring_boards", {}) if isinstance(signals.get("hiring_boards"), dict) else {}
    postings = boards.get("postings") or []
    if boards.get("provider") and postings:
        titles = ", ".join(p.get("title", "") for p in postings[:2] if isinstance(p, dict) and p.get("title"))
        out.append({"signal": "hiring_board", "informs": "commercial_urgency", "keywords": [],
                    "snippet": f"{len(postings)} open role(s) on your {boards['provider']} board"
                               + (f" incl. {titles}" if titles else "")})
    return out


def _evidence_line(company: str, evidence: list[dict]) -> str:
    if not evidence:
        return f"{company} sits on workflow data that looks like a strong base for an AI layer"
    top = evidence[0]
    kws = ", ".join(top.get("keywords") or [])
    if kws:
        return f"{company} is hiring around {kws}"
    snippet = (top.get("snippet") or "").strip("…").strip()
    return snippet or f"public signals at {company} worth a closer look"


def _draft_for(recipient: dict, company: str, evidence: list[dict], cfg: dict) -> dict:
    name = (recipient.get("name") or "").strip()
    first = name.split()[0] if name else ""
    greeting = f"Hi {first}," if first else "Hi there,"
    line = _evidence_line(company, evidence)
    body = "\n\n".join([
        greeting,
        f"I was looking at {company} and noticed {line}.",
        cfg["angle"],
        f"{cfg['offer']} {cfg['cta']}",
    ])
    return {
        "recipient": name,
        "to": recipient.get("email", ""),
        "to_status": recipient.get("email_status", ""),
        "title": recipient.get("title", ""),
        "persona": recipient.get("persona") or recipient.get("title", ""),
        "persona_priority": recipient.get("persona_priority", "unknown"),
        "channel": "email",
        "subject": f"{company} — turning your workflow data into an AI feature",
        "body": body,
        "cta": cfg["cta"],
        "grounded_on": evidence[:3],
        "status": "template",
    }


def gather_personalize(score: dict, signals: dict, people: dict, enrich: dict,
                       outreach_cfg: dict | None = None) -> dict:
    """Build template drafts for an account. Pure — injectable for offline tests."""
    cfg = {**DEFAULT_OUTREACH, **(outreach_cfg or {})}
    company = score.get("company_name") or enrich.get("company_name") or "your team"
    domain = enrich.get("domain") or enrich.get("website") or signals.get("domain") or ""
    evidence = _evidence_from_signals(signals)

    contacts = [p for p in people.get("people", []) if isinstance(p, dict)]
    targets = [t for t in people.get("persona_targets", []) if isinstance(t, dict)]
    if contacts:
        recipients = contacts
    else:
        # No resolved contacts: address the persona role instead of a person.
        recipients = [{"name": "", "title": t.get("title", ""), "persona": t.get("title", ""),
                       "persona_priority": t.get("priority", "unknown")} for t in targets]

    drafts = [_draft_for(r, company, evidence, cfg) for r in recipients]

    warnings = []
    if not evidence:
        warnings.append("no public signals detected — drafts use a generic angle; "
                        "strengthen the grounding before sending.")
    if not recipients:
        warnings.append("no contacts or persona targets — run the people stage first.")
    elif contacts and not any(d.get("to") for d in drafts):
        warnings.append("contacts resolved but none have a revealed email — drafts "
                        "name a person but aren't send-ready (Apollo had no email).")

    return {"company_name": company, "domain": domain, "tier": score.get("tier"),
            "drafts": drafts, "warnings": warnings}


def _load_outreach_cfg(criteria_path: Path) -> dict:
    if criteria_path.exists():
        cfg = gtm_lib.read_json(criteria_path).get("outreach")
        if isinstance(cfg, dict):
            return cfg
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft grounded outreach for an account.")
    ap.add_argument("--slug", required=True, help="account slug under the artifact root")
    ap.add_argument("--criteria", type=Path, default=Path("icp.criteria.json"),
                    help="ICP file; an optional `outreach` block overrides the default angle")
    ap.add_argument("--force", action="store_true", help="draft even a Reject-tier account")
    args = ap.parse_args(argv)

    acct_dir = gtm_lib.account_dir(args.slug)

    def read(name):
        p = acct_dir / f"{name}.json"
        return gtm_lib.read_json(p) if p.exists() else {}

    score = read("score")
    tier = score.get("tier")
    if not score:
        out = {"company_name": None, "drafts": [],
               "warnings": ["no score.json — run classify/score before personalize."]}
    elif tier == "Reject" and not args.force:
        out = {"company_name": score.get("company_name"), "tier": tier, "drafts": [],
               "warnings": ["tier is Reject — skipped outreach (use --force to override)."]}
    else:
        out = gather_personalize(score, read("signals"), read("people"), read("enrich"),
                                 _load_outreach_cfg(args.criteria))

    path = gtm_lib.write_json(gtm_lib.stage_path(args.slug, "personalize"), out)
    print(json.dumps({"slug": args.slug, "tier": tier, "drafts": len(out.get("drafts", [])),
                      "grounded": bool(out.get("drafts") and out["drafts"][0].get("grounded_on")),
                      "warnings": len(out.get("warnings", [])), "artifact": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
