#!/usr/bin/env bash
# Offline test for personalize.py — grounded template drafts, no network/keys.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
P="$HERE/../personalize.py"

python3 - "$P" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("personalize", sys.argv[1])
pz = importlib.util.module_from_spec(spec); spec.loader.exec_module(pz)

score = {"company_name": "Acme", "tier": "A", "rationale": "84/100 -> tier A"}
enrich = {"company_name": "Acme", "domain": "acme.example"}
signals = {"signals_detected": [
    {"key": "ai_hiring", "informs": "commercial_urgency", "found": True,
     "matched_keywords": ["langchain", "vector database"],
     "evidence": [{"snippet": "…hiring a Machine Learning Engineer for LangChain…"}]},
    {"key": "ai_product_surface", "informs": "ai_gap", "found": False, "matched_keywords": []}],
    "hiring_boards": {"provider": "greenhouse", "board_slug": "acme91",
                      "postings": [{"title": "ML Engineer"}, {"title": "Data Eng"}]}}

# --- 1. With a revealed contact -> grounded draft addressed to the real email. ---
people = {"source": "apollo", "people": [
    {"name": "Dana Lee", "title": "VP Product", "persona": "Chief Product Officer",
     "persona_priority": "primary", "email": "dana@acme.example",
     "email_status": "verified", "revealed": True}], "persona_targets": []}
out = pz.gather_personalize(score, signals, people, enrich)
assert len(out["drafts"]) == 1, out
d = out["drafts"][0]
assert d["recipient"] == "Dana Lee" and d["status"] == "template", d
assert d["to"] == "dana@acme.example" and d["to_status"] == "verified", d  # send-ready address
assert d["body"].startswith("Hi Dana,"), d["body"]
assert "Acme" in d["subject"], d["subject"]
# Body is grounded on the detected hiring signal, not generic.
assert "langchain" in d["body"].lower(), d["body"]
keys = {g["signal"] for g in d["grounded_on"]}
assert "ai_hiring" in keys and "hiring_board" in keys, keys
assert out["warnings"] == [], out["warnings"]

# --- 1b. Contact resolved but no email -> draft names the person, warns not send-ready. ---
people_noemail = {"source": "apollo", "people": [
    {"name": "Pat Roe", "title": "VP Product", "persona": "Chief Product Officer",
     "persona_priority": "primary", "email": "", "email_status": "unavailable"}],
    "persona_targets": []}
out_ne = pz.gather_personalize(score, signals, people_noemail, enrich)
assert out_ne["drafts"][0]["to"] == "", out_ne["drafts"][0]
assert any("send-ready" in w for w in out_ne["warnings"]), out_ne["warnings"]

# --- 2. No contacts -> address the persona targets by role, no first name. ---
people2 = {"source": "local", "people": [],
           "persona_targets": [{"title": "CTO / VP Engineering", "priority": "primary"}]}
out2 = pz.gather_personalize(score, signals, people2, enrich)
d2 = out2["drafts"][0]
assert d2["recipient"] == "" and d2["body"].startswith("Hi there,"), d2["body"]
assert d2["persona"] == "CTO / VP Engineering", d2

# --- 3. No signals -> generic angle + a warning to strengthen grounding. ---
out3 = pz.gather_personalize(score, {"signals_detected": []}, people, enrich)
d3 = out3["drafts"][0]
assert d3["grounded_on"] == [], d3
assert "workflow data" in d3["body"], d3["body"]
assert any("generic angle" in w for w in out3["warnings"]), out3["warnings"]

# --- 4. ICP outreach block overrides the default angle/cta. ---
cfg = {"angle": "Custom angle here.", "cta": "Grab 15 min?"}
out4 = pz.gather_personalize(score, signals, people, enrich, cfg)
assert "Custom angle here." in out4["drafts"][0]["body"], out4["drafts"][0]["body"]
assert out4["drafts"][0]["cta"] == "Grab 15 min?", out4["drafts"][0]

print("OK: grounded draft w/ contact, persona-role fallback, no-signal warning, ICP override")
PY

echo "PASS test_personalize.sh"
