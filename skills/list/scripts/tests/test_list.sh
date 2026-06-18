#!/usr/bin/env bash
# Offline test for build_list.py — aggregates per-account artifacts into a
# ranked CSV + dossier. Filesystem fixtures only, no network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LIST="$HERE/../build_list.py"

python3 - "$LIST" <<'PY'
import importlib.util, json, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("build_list", sys.argv[1])
bl = importlib.util.module_from_spec(spec); spec.loader.exec_module(bl)

root = Path(tempfile.mkdtemp()) / ".gtm"

def write(slug, **files):
    d = root / slug; d.mkdir(parents=True)
    for name, payload in files.items():
        (d / f"{name}.json").write_text(json.dumps(payload))

# A strong A-tier with signals + a primary contact.
write("acme",
      score={"company_name": "Acme", "score": 84, "tier": "A", "gates_failed": [],
             "dimension_breakdown": ["ai_gap 24/30", "data_workflow_moat 20/25"],
             "rationale": "84/100 -> tier A"},
      enrich={"company_name": "Acme", "domain": "acme.example"},
      signals={"signals_detected": [
          {"key": "ai_hiring", "informs": "commercial_urgency", "found": True,
           "matched_keywords": ["langchain"], "evidence": [{"snippet": "…hiring LangChain eng…"}]},
          {"key": "ai_product_surface", "informs": "ai_gap", "found": False, "matched_keywords": []}],
          "hiring_boards": {"provider": "greenhouse", "board_slug": "acme91",
                            "discovery": "careers-link", "postings": [{"title": "ML Eng"}]}},
      people={"source": "apollo", "people": [
          {"name": "Dana Lee", "title": "VP Product", "persona": "Chief Product Officer",
           "persona_priority": "primary", "email": "dana@acme.example",
           "email_status": "verified", "revealed": True,
           "linkedin_url": "https://linkedin.com/in/danalee"},
          {"name": "Sam Okafor", "title": "Head of Data", "persona": "Head of Data / AI",
           "persona_priority": "secondary", "email": "", "email_status": "unavailable"}],
          "persona_targets": []})

# A B-tier with no contacts (no-key people path -> persona targets).
write("midco",
      score={"company_name": "MidCo", "score": 62, "tier": "B", "gates_failed": [],
             "dimension_breakdown": ["ai_gap 18/30"], "rationale": "62/100 -> tier B"},
      enrich={"company_name": "MidCo", "domain": "midco.example"},
      people={"source": "local", "people": [],
              "persona_targets": [{"title": "CTO / VP Engineering", "priority": "primary"}]})

# A Reject — must sort last and be excluded from the default dossier.
write("ainative",
      score={"company_name": "AINative", "score": 70, "tier": "Reject",
             "gates_failed": ["not_ai_native"], "dimension_breakdown": [],
             "rationale": "Rejected on gate(s): not_ai_native (score would be 70)."})

# A dir with no score.json must be ignored entirely.
(root / "_report").mkdir(parents=True, exist_ok=True)  # pre-existing report dir is skipped
(root / "draft").mkdir(); (root / "draft" / "input.json").write_text("{}")

recs = bl.rank(bl.collect_accounts(root))
order = [r["company"] for r in recs]
assert order == ["Acme", "MidCo", "AINative"], order  # A, B, Reject; "draft" ignored

# Top contact for Acme is the PRIMARY-persona person, not the first listed by luck.
assert recs[0]["top_contact"]["name"] == "Dana Lee", recs[0]["top_contact"]

out = bl.build(root, include_reject=False)
assert out["accounts"] == 3 and out["actionable"] == 2, out

csv_text = (root / "_report" / "accounts.csv").read_text()
header, *rows = [r for r in csv_text.splitlines() if r]
assert header.startswith("rank,company,domain,tier,score"), header
assert "top_contact_email" in header, header  # revealed email gets its own column
# Reject excluded from the default CSV; Acme row carries signals + contact + email.
assert "AINative" not in csv_text, "Reject should be excluded by default"
acme_row = next(r for r in rows if r.startswith("1,Acme"))
assert "ai_hiring" in acme_row and "greenhouse" in acme_row and "Dana Lee" in acme_row, acme_row
assert "dana@acme.example" in acme_row, "revealed email must land in the CSV row"

dossier = (root / "_report" / "dossier.md").read_text()
assert "# ICP Qualification Dossier" in dossier
assert "2 actionable account(s); 3 scored total." in dossier
assert "## Acme — Tier A (84/100)" in dossier
assert "ai_hiring" in dossier and "Dana Lee" in dossier
assert "dana@acme.example" in dossier, "revealed email must show in the dossier contact line"
assert "Hiring board: greenhouse/acme91 (careers-link)" in dossier
assert "AINative" not in dossier, "Reject excluded from default dossier"
# MidCo with no contacts falls back to its persona targets.
assert "Persona targets (no contacts resolved)" in dossier
assert "CTO / VP Engineering" in dossier

# --include-reject brings the rejected account into both outputs.
out2 = bl.build(root, include_reject=True)
assert "AINative" in (root / "_report" / "accounts.csv").read_text()
assert "## AINative — Tier Reject" in (root / "_report" / "dossier.md").read_text()

print("OK: ranked A>B>Reject, primary contact chosen, signals+contacts in CSV/dossier, reject gating")
PY

echo "PASS test_list.sh"
