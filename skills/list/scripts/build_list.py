#!/usr/bin/env python3
"""Assemble the GTM hand-off list across every scored account (the `list` stage).

The per-account stages each write one artifact under `.gtm/<slug>/`. This stage
reads across all of them and produces the two deliverables a GTM team actually
works from:

  * a **ranked CSV** — one row per account, sorted by tier then score, with the
    driving signals and the top contact, ready to drop into a CRM or sheet;
  * a **markdown dossier** — one section per account with the gate verdicts,
    score breakdown, detected intent signals (with evidence), the hiring board,
    and the contacts (or persona targets) from the people stage.

It reads `score.json` (the marker of a scored account) plus, when present,
`enrich.json`, `signals.json`, and `people.json` from each account directory.
Reject-tier accounts are listed last (and omitted from the dossier unless
--include-reject) so the actionable accounts sit at the top.

Writes (under the artifact root, default `.gtm/`):
    _report/accounts.csv
    _report/dossier.md

stdlib only. No network, no keys.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

# Actionable first, Reject last; unknown tiers sort between Nurture and Reject.
TIER_RANK = {"A": 0, "B": 1, "Nurture": 2, "Reject": 4}
CSV_FIELDS = [
    "rank", "company", "domain", "tier", "score", "gates_failed",
    "signals_found", "hiring_provider", "contacts", "people_source",
    "top_contact", "top_contact_title", "top_contact_email",
    "top_contact_email_status", "top_contact_linkedin", "rationale",
]


def _read(path: Path) -> dict:
    try:
        return gtm_lib.read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _top_contact(people: dict) -> dict:
    """Pick the contact to lead with: a primary-persona person, else the first."""
    contacts = [p for p in people.get("people", []) if isinstance(p, dict)]
    if not contacts:
        return {}
    primary = [p for p in contacts if p.get("persona_priority") == "primary"]
    return (primary or contacts)[0]


def load_account(acct_dir: Path) -> dict | None:
    """Build one hand-off record for an account dir, or None if it isn't scored."""
    score = _read(acct_dir / "score.json")
    if not score:
        return None
    enrich = _read(acct_dir / "enrich.json")
    signals = _read(acct_dir / "signals.json")
    people = _read(acct_dir / "people.json")

    found_signals = [s for s in signals.get("signals_detected", []) if s.get("found")]
    boards = signals.get("hiring_boards", {}) if isinstance(signals.get("hiring_boards"), dict) else {}
    contacts = [p for p in people.get("people", []) if isinstance(p, dict)]
    top = _top_contact(people)

    return {
        "slug": acct_dir.name,
        "company": score.get("company_name") or enrich.get("company_name") or acct_dir.name,
        "domain": enrich.get("domain") or enrich.get("website") or signals.get("domain") or "",
        "tier": score.get("tier") or "",
        "score": score.get("score"),
        "gates_failed": score.get("gates_failed", []),
        "dimension_breakdown": score.get("dimension_breakdown", []),
        "rationale": score.get("rationale", ""),
        "found_signals": found_signals,
        "hiring_boards": boards,
        "people_source": people.get("source") or "",
        "contacts": contacts,
        "persona_targets": [t for t in people.get("persona_targets", []) if isinstance(t, dict)],
        "top_contact": top,
    }


def collect_accounts(root: Path) -> list[dict]:
    if not root.exists():
        return []
    records = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        rec = load_account(child)
        if rec:
            records.append(rec)
    return records


def rank(records: list[dict]) -> list[dict]:
    def key(r):
        return (TIER_RANK.get(r["tier"], 3), -(r.get("score") or 0), r["company"].lower())
    return sorted(records, key=key)


def to_csv(records: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for i, r in enumerate(records, start=1):
        top = r["top_contact"]
        writer.writerow({
            "rank": i,
            "company": r["company"],
            "domain": r["domain"],
            "tier": r["tier"],
            "score": r["score"],
            "gates_failed": "; ".join(r["gates_failed"]),
            "signals_found": "; ".join(s.get("key", "") for s in r["found_signals"]),
            "hiring_provider": r["hiring_boards"].get("provider") or "",
            "contacts": len(r["contacts"]),
            "people_source": r["people_source"],
            "top_contact": top.get("name", ""),
            "top_contact_title": top.get("title", ""),
            "top_contact_email": top.get("email", ""),
            "top_contact_email_status": top.get("email_status", ""),
            "top_contact_linkedin": top.get("linkedin_url", ""),
            "rationale": r["rationale"],
        })
    return out.getvalue()


def _account_section(r: dict) -> list[str]:
    score = r["score"]
    head = f"## {r['company']} — Tier {r['tier']}" + (f" ({score}/100)" if score is not None else "")
    lines = [head, "", f"- Domain: {r['domain'] or 'n/a'}", f"- Rationale: {r['rationale']}"]
    if r["gates_failed"]:
        lines.append(f"- Failed gates: {', '.join(r['gates_failed'])}")

    if r["dimension_breakdown"]:
        lines += ["", "### Score breakdown", ""]
        lines += [f"- {b}" for b in r["dimension_breakdown"]]

    lines += ["", "### Intent signals", ""]
    if r["found_signals"]:
        for s in r["found_signals"]:
            kws = ", ".join(s.get("matched_keywords", []))
            snippet = (s.get("evidence") or [{}])[0].get("snippet", "")
            lines.append(f"- **{s.get('key')}** (informs {s.get('informs')}): {kws}"
                         + (f" — {snippet}" if snippet else ""))
    else:
        lines.append("- No intent signals detected (absence widens the AI gap).")
    boards = r["hiring_boards"]
    if boards.get("provider"):
        disc = boards.get("discovery")
        n = len(boards.get("postings", []))
        lines.append(f"- Hiring board: {boards['provider']}/{boards.get('board_slug')}"
                     + (f" ({disc})" if disc else "") + f" — {n} open role(s)")

    if r["contacts"]:
        lines += ["", "### Contacts", ""]
        for p in r["contacts"]:
            persona = f"{p.get('persona')}, {p.get('persona_priority')}"
            # Lead with the revealed email; fall back to the availability status.
            email = p.get("email") or (f"email {p.get('email_status')}" if p.get("email_status") else "")
            tail = " · ".join(x for x in [email, p.get("linkedin_url")] if x)
            lines.append(f"- **{p.get('name')}** — {p.get('title')} ({persona})"
                         + (f" · {tail}" if tail else ""))
    elif r["persona_targets"]:
        lines += ["", "### Persona targets (no contacts resolved)", ""]
        for t in r["persona_targets"]:
            lines.append(f"- {t.get('title')} ({t.get('priority')})")

    lines.append("")
    return lines


def to_dossier(records: list[dict], total_scored: int | None = None) -> str:
    """`records` are the accounts to render; `total_scored` is the full scored
    count (so the header is honest even when Reject accounts are filtered out)."""
    actionable = len([r for r in records if r["tier"] != "Reject"])
    total = len(records) if total_scored is None else total_scored
    lines = ["# ICP Qualification Dossier", "",
             f"{actionable} actionable account(s); {total} scored total.", ""]
    for r in records:
        lines += _account_section(r)
    return "\n".join(lines).rstrip() + "\n"


def build(root: Path, *, include_reject: bool) -> dict:
    records = rank(collect_accounts(root))
    csv_records = records if include_reject else [r for r in records if r["tier"] != "Reject"] or records
    dossier_records = records if include_reject else [r for r in records if r["tier"] != "Reject"]

    report_dir = root / "_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "accounts.csv"
    md_path = report_dir / "dossier.md"
    csv_path.write_text(to_csv(csv_records), encoding="utf-8")
    md_path.write_text(to_dossier(dossier_records, total_scored=len(records)), encoding="utf-8")
    return {
        "accounts": len(records),
        "actionable": len([r for r in records if r["tier"] != "Reject"]),
        "csv": str(csv_path),
        "dossier": str(md_path),
        "top": [{"company": r["company"], "tier": r["tier"], "score": r["score"]}
                for r in records[:5]],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the ranked GTM hand-off list.")
    ap.add_argument("--include-reject", action="store_true",
                    help="include Reject-tier accounts in the CSV and dossier")
    args = ap.parse_args(argv)

    out = build(gtm_lib.artifact_root(), include_reject=args.include_reject)
    if not out["accounts"]:
        out["note"] = "no scored accounts found under the artifact root; run score first"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
