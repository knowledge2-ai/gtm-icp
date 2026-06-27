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
from collections import namedtuple
from datetime import date
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

# Recency shaping (soft downweight), mirroring the dashboard engine's evidence
# selection: a fresh signal gets up to +RECENCY_BONUS decaying to 0 at the window
# edge; a stale one is penalized up to -2*RECENCY_PENALTY. Tuned so recency only
# *reorders* — a strong keyworded-but-old signal still beats a weak fresh one, and
# an undated signal is neutral (absence of a date never reads as "stale").
RECENCY_WINDOW_DAYS = 365
RECENCY_BONUS = 8
RECENCY_PENALTY = 6
_KEYWORD_RANK = 10  # base weight so keyworded evidence outranks a bare snippet.


# A persona/signal-routed scaffold: it sets the subject and the opening line; the
# ICP `outreach` block still supplies the angle/offer/cta the body fills.
# subject merges {company}; opener merges {company} and {evidence_line}.
_Template = namedtuple("_Template", "name personas signals subject opener")


_TEMPLATES = (
    _Template("exec-ai-urgency", ("ceo", "founder", "chief", "president", "owner"), (),
              "{company} — turning your workflow data into an AI edge",
              "I was looking at {company} and noticed {evidence_line}."),
    _Template("data-advantage", ("data", "analytics", "insight"), ("data_workflow_moat", "ai_gap"),
              "{company}'s data as an AI advantage",
              "Digging into {company}, the part that stood out is {evidence_line}."),
    _Template("workflow-efficiency", ("engineering", "product", "operations", "cto", "ops", "technical"),
              ("commercial_urgency",),
              "An AI opportunity in {company}'s workflows",
              "I was looking at {company} and noticed {evidence_line}."),
)
_DEFAULT_TEMPLATE = _Template("default", (), (),
                              "{company} — turning your workflow data into an AI feature",
                              "I was looking at {company} and noticed {evidence_line}.")


def select_template(persona: str, signal_keys: set[str]) -> _Template:
    """Pick a scaffold by persona (keyword match wins), then by present signals."""
    p = (persona or "").lower()
    for tmpl in _TEMPLATES:
        if any(kw in p for kw in tmpl.personas):
            return tmpl
    for tmpl in _TEMPLATES:
        if any(sig in signal_keys for sig in tmpl.signals):
            return tmpl
    return _DEFAULT_TEMPLATE


def _parse_iso(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _recency_adjustment(published_at: object, reference: date) -> int:
    """Soft recency shaping for the evidence sort. 0 (neutral) when undated."""
    published = _parse_iso(published_at)
    if published is None:
        return 0
    age_days = max((reference - published).days, 0)
    if age_days <= RECENCY_WINDOW_DAYS:
        return round(RECENCY_BONUS * (1 - age_days / RECENCY_WINDOW_DAYS))
    overage = min((age_days - RECENCY_WINDOW_DAYS) / RECENCY_WINDOW_DAYS, 2.0)
    return -round(RECENCY_PENALTY * overage)


def _freshest(items: list[dict]) -> str | None:
    """Most recent published_at across evidence items (ISO YYYY-MM-DD) or None."""
    dates = sorted(
        (str(it.get("published_at")) for it in items
         if isinstance(it, dict) and _parse_iso(it.get("published_at"))),
        reverse=True,
    )
    return dates[0] if dates else None


def _evidence_from_signals(signals: dict, reference: date) -> list[dict]:
    """Pull the citable evidence the drafts may lean on, freshest signal first.

    Each detected signal becomes one item carrying the most recent date among its
    evidence; the list is then recency-sorted (soft downweight) so personalize
    leads on a current signal rather than a stale one — without dropping a strong
    keyworded-but-undated signal below a weak fresh one.
    """
    out = []
    for s in signals.get("signals_detected", []):
        if not isinstance(s, dict) or not s.get("found"):
            continue
        ev = [e for e in (s.get("evidence") or []) if isinstance(e, dict)]
        # Prefer the freshest dated snippet; fall back to the first.
        dated = sorted(
            (e for e in ev if _parse_iso(e.get("published_at"))),
            key=lambda e: str(e.get("published_at")), reverse=True,
        )
        pick = dated[0] if dated else (ev[0] if ev else {})
        out.append({"signal": s.get("key"), "informs": s.get("informs"),
                    "keywords": (s.get("matched_keywords") or [])[:3],
                    "snippet": pick.get("snippet", ""),
                    "published_at": _freshest(ev)})
    boards = signals.get("hiring_boards", {}) if isinstance(signals.get("hiring_boards"), dict) else {}
    postings = boards.get("postings") or []
    if boards.get("provider") and postings:
        titles = ", ".join(p.get("title", "") for p in postings[:2] if isinstance(p, dict) and p.get("title"))
        out.append({"signal": "hiring_board", "informs": "commercial_urgency", "keywords": [],
                    "snippet": f"{len(postings)} open role(s) on your {boards['provider']} board"
                               + (f" incl. {titles}" if titles else ""),
                    "published_at": _freshest(postings)})

    def _rank(item: dict) -> int:
        base = _KEYWORD_RANK if item.get("keywords") else 0
        return base + _recency_adjustment(item.get("published_at"), reference)

    # Stable sort: undated items keep their original (signal-declaration) order.
    return sorted(out, key=_rank, reverse=True)


def _evidence_line(company: str, evidence: list[dict]) -> str:
    if not evidence:
        return f"{company} sits on workflow data that looks like a strong base for an AI layer"
    top = evidence[0]
    kws = ", ".join(top.get("keywords") or [])
    if kws:
        return f"{company} is hiring around {kws}"
    snippet = (top.get("snippet") or "").strip("…").strip()
    return snippet or f"public signals at {company} worth a closer look"


def check_guardrails(body: str, do_not_say: list[str]) -> list[str]:
    """Return the `do_not_say` phrases that appear in `body` (case-insensitive).

    The positioning block's "what not to say" list — overpromises, competitor
    bashing, unverifiable claims. The deterministic draft rarely trips these, but
    the check earns its keep on the `personalize` skill's LLM rewrite, which must
    re-run it: a banned phrase that survives is a hard stop before sending.
    """
    low = body.lower()
    return [phrase for phrase in (do_not_say or []) if phrase.lower() in low]


def _draft_for(recipient: dict, company: str, evidence: list[dict], cfg: dict,
               signal_keys: set[str], do_not_say: list[str] | None = None) -> dict:
    name = (recipient.get("name") or "").strip()
    first = name.split()[0] if name else ""
    greeting = f"Hi {first}," if first else "Hi there,"
    persona = recipient.get("persona") or recipient.get("title", "")
    template = select_template(persona, signal_keys)
    line = _evidence_line(company, evidence)
    body = "\n\n".join([
        greeting,
        template.opener.format(company=company, evidence_line=line),
        cfg["angle"],
        f"{cfg['offer']} {cfg['cta']}",
    ])
    return {
        "recipient": name,
        "to": recipient.get("email", ""),
        "to_status": recipient.get("email_status", ""),
        "title": recipient.get("title", ""),
        "persona": persona,
        "persona_priority": recipient.get("persona_priority", "unknown"),
        "channel": "email",
        "template": template.name,
        "subject": template.subject.format(company=company),
        "body": body,
        "cta": cfg["cta"],
        "grounded_on": evidence[:3],
        "guardrail_warnings": check_guardrails(body, do_not_say or []),
        "status": "template",
    }


def gather_personalize(score: dict, signals: dict, people: dict, enrich: dict,
                       outreach_cfg: dict | None = None, *,
                       positioning: dict | None = None,
                       reference_date: date | None = None) -> dict:
    """Build template drafts for an account. Pure — injectable for offline tests.

    ``reference_date`` anchors the recency downweight; defaults to today and is
    injectable so evidence ordering is deterministic under test. ``positioning``
    is the ICP's seller block — `value_pillars` (passed through so the skill's
    rewrite leads on a real pillar) and `do_not_say` (forbidden claims each draft
    is checked against).
    """
    cfg = {**DEFAULT_OUTREACH, **(outreach_cfg or {})}
    positioning = positioning or {}
    value_pillars = positioning.get("value_pillars", [])
    do_not_say = positioning.get("do_not_say", [])
    reference = reference_date or date.today()
    company = score.get("company_name") or enrich.get("company_name") or "your team"
    domain = enrich.get("domain") or enrich.get("website") or signals.get("domain") or ""
    evidence = _evidence_from_signals(signals, reference)
    signal_keys = {k for e in evidence for k in (e.get("signal"), e.get("informs")) if k}

    contacts = [p for p in people.get("people", []) if isinstance(p, dict)]
    targets = [t for t in people.get("persona_targets", []) if isinstance(t, dict)]
    if contacts:
        recipients = contacts
    else:
        # No resolved contacts: address the persona role instead of a person.
        recipients = [{"name": "", "title": t.get("title", ""), "persona": t.get("title", ""),
                       "persona_priority": t.get("priority", "unknown")} for t in targets]

    drafts = [_draft_for(r, company, evidence, cfg, signal_keys, do_not_say) for r in recipients]

    warnings = []
    if not evidence:
        warnings.append("no public signals detected — drafts use a generic angle; "
                        "strengthen the grounding before sending.")
    if not recipients:
        warnings.append("no contacts or persona targets — run the people stage first.")
    elif contacts and not any(d.get("to") for d in drafts):
        warnings.append("contacts resolved but none have a revealed email — drafts "
                        "name a person but aren't send-ready (Apollo had no email).")
    flagged = sorted({p for d in drafts for p in d.get("guardrail_warnings", [])})
    if flagged:
        warnings.append("drafts contain forbidden claims (positioning.do_not_say): "
                        + ", ".join(flagged) + " — rewrite before sending.")

    return {"company_name": company, "domain": domain, "tier": score.get("tier"),
            "value_pillars": value_pillars, "do_not_say": do_not_say,
            "drafts": drafts, "warnings": warnings}


def _load_outreach_cfg(criteria_path: Path) -> dict:
    if criteria_path.exists():
        cfg = gtm_lib.read_json(criteria_path).get("outreach")
        if isinstance(cfg, dict):
            return cfg
    return {}


def _load_positioning(criteria_path: Path) -> dict:
    if criteria_path.exists():
        pos = gtm_lib.read_json(criteria_path).get("positioning")
        if isinstance(pos, dict):
            return pos
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
                                 _load_outreach_cfg(args.criteria),
                                 positioning=_load_positioning(args.criteria))

    path = gtm_lib.write_json(gtm_lib.stage_path(args.slug, "personalize"), out)
    print(json.dumps({"slug": args.slug, "tier": tier, "drafts": len(out.get("drafts", [])),
                      "grounded": bool(out.get("drafts") and out["drafts"][0].get("grounded_on")),
                      "warnings": len(out.get("warnings", [])), "artifact": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
