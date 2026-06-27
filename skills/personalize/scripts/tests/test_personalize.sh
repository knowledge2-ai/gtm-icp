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

# --- 5. Recency: a fresh signal outranks a stale one, even both keyworded. ---
import datetime
ref = datetime.date(2026, 6, 13)
sig_dated = {"signals_detected": [
    {"key": "ai_hiring", "informs": "commercial_urgency", "found": True,
     "matched_keywords": ["langchain"],
     "evidence": [{"snippet": "stale 2023 hiring blurb", "published_at": "2023-01-01"}]},
    {"key": "ai_product_surface", "informs": "ai_gap", "found": True,
     "matched_keywords": ["gpt"],
     "evidence": [{"snippet": "fresh 2026 ai launch", "published_at": "2026-06-01"}]}]}
exec_person = {"people": [{"name": "Dana Lee", "title": "CEO", "persona": "CEO",
    "persona_priority": "primary", "email": "d@acme.example", "email_status": "verified"}],
    "persona_targets": []}
out5 = pz.gather_personalize(score, sig_dated, exec_person, enrich, reference_date=ref)
d5 = out5["drafts"][0]
# Declared second but freshest -> recency reorders it to the front.
assert d5["grounded_on"][0]["signal"] == "ai_product_surface", d5["grounded_on"]
assert d5["grounded_on"][0]["published_at"] == "2026-06-01", d5["grounded_on"][0]
# CEO persona -> exec scaffold.
assert d5["template"] == "exec-ai-urgency", d5["template"]
assert "AI edge" in d5["subject"], d5["subject"]

# --- 6. Persona routing: an engineering target gets the workflow scaffold. ---
eng_person = {"people": [], "persona_targets": [{"title": "VP Engineering", "priority": "primary"}]}
out6 = pz.gather_personalize(score, sig_dated, eng_person, enrich, reference_date=ref)
d6 = out6["drafts"][0]
assert d6["template"] == "workflow-efficiency", d6["template"]
assert "workflows" in d6["subject"], d6["subject"]

# --- 7. Positioning (stolen from gtm-starter-kit): value pillars surfaced for the
#        LLM rewrite, and "what not to say" forbidden claims flagged per draft. ---
assert pz.check_guardrails("We promise GUARANTEED ROI here", ["guaranteed roi"]) == ["guaranteed roi"]
assert pz.check_guardrails("clean, specific copy", ["10x", "best-in-class"]) == []

positioning = {
    "value_pillars": [{"name": "Grounded on your own data", "proof": "Relay case study: 6 wks to first AI feature"}],
    "do_not_say": ["guaranteed ROI", "10x"],
}
cfg_bad = {"angle": "We deliver guaranteed ROI and 10x your pipeline.", "cta": "Call?"}
out7 = pz.gather_personalize(score, signals, people, enrich, cfg_bad, positioning=positioning)
# Pillars + forbidden list pass through so the skill's rewrite can use them.
assert out7["value_pillars"] == positioning["value_pillars"], out7.get("value_pillars")
assert out7["do_not_say"] == ["guaranteed ROI", "10x"], out7.get("do_not_say")
# The offending draft flags both banned phrases, and the account warns.
d7 = out7["drafts"][0]
assert set(d7["guardrail_warnings"]) == {"guaranteed ROI", "10x"}, d7.get("guardrail_warnings")
assert any("forbidden" in w.lower() for w in out7["warnings"]), out7["warnings"]

# A clean body (default angle) trips no guardrails.
out7b = pz.gather_personalize(score, signals, people, enrich, positioning=positioning)
assert out7b["drafts"][0]["guardrail_warnings"] == [], out7b["drafts"][0].get("guardrail_warnings")
assert not any("forbidden" in w.lower() for w in out7b["warnings"]), out7b["warnings"]

print("OK: grounded draft w/ contact, persona-role fallback, no-signal warning, "
      "ICP override, recency reorder, persona routing, positioning guardrails")
PY

echo "PASS test_personalize.sh"
