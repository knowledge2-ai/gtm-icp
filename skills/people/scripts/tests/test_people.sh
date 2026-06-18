#!/usr/bin/env bash
# Offline test for people.py — Apollo people-search mapped to ICP personas, no network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PEOPLE="$HERE/../people.py"

fail() { echo "FAIL: $1" >&2; exit 1; }

python3 - "$PEOPLE" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("people", sys.argv[1])
people = importlib.util.module_from_spec(spec); spec.loader.exec_module(people)

PERSONAS = [
    {"title": "Chief Product Officer", "priority": "primary",
     "apollo_titles": ["chief product officer", "vp product", "head of product"]},
    {"title": "Head of Data / AI", "priority": "secondary",
     "apollo_titles": ["chief data officer", "head of data", "head of ai"]},
]
account = {"company_name": "Incumbent", "domain": "https://incumbent.example/"}

# --- 1. No key -> persona targets, still actionable, no contacts. ---
out = people.gather_people(account, PERSONAS, api_key=None)
assert out["source"] == "local", out
assert out["people"] == [], out
titles = {p["title"] for p in out["persona_targets"]}
assert titles == {"Chief Product Officer", "Head of Data / AI"}, out["persona_targets"]
# Titles to pursue are flattened from the personas, domain is cleaned.
assert out["domain"] == "incumbent.example", out["domain"]
assert "chief product officer" in out["titles_targeted"], out["titles_targeted"]
assert "head of ai" in out["titles_targeted"], out["titles_targeted"]

# --- 2. With a key (injected searcher) -> contacts mapped to the right persona. ---
# The searcher returns already-compacted people (apollo_search_people compacts
# the raw payload internally); reuse _compact_people so the test mirrors that.
def fake_search(domain, titles, api_key, *, per_page=8):
    assert domain == "incumbent.example", domain
    assert "chief product officer" in titles, titles
    raw = {"people": [
        {"name": "Dana Lee", "title": "VP of Product", "email": "dana@incumbent.example",
         "email_status": "verified", "linkedin_url": "https://linkedin.com/in/danalee",
         "city": "Austin", "state": "TX", "country": "USA",
         "organization": {"name": "Incumbent Inc"}},
        {"name": "Sam Okafor", "title": "Head of Data Platform", "email": "",
         "email_status": "guessed", "organization": {"name": "Incumbent Inc"}},
    ]}
    return {"status": "ok", "people": people._compact_people(raw)}

out = people.gather_people(account, PERSONAS, api_key="fake-key", searcher=fake_search)
assert out["source"] == "apollo", out
assert out["persona_targets"] == [], "persona targets suppressed once real people found"
by_name = {p["name"]: p for p in out["people"]}
assert by_name["Dana Lee"]["persona"] == "Chief Product Officer", by_name["Dana Lee"]
assert by_name["Dana Lee"]["persona_priority"] == "primary", by_name["Dana Lee"]
assert by_name["Dana Lee"]["location"] == "Austin, TX, USA", by_name["Dana Lee"]
assert by_name["Dana Lee"]["revealed"] is True, by_name["Dana Lee"]      # real email present
assert by_name["Dana Lee"]["email_status"] == "verified", by_name["Dana Lee"]
assert by_name["Sam Okafor"]["persona"] == "Head of Data / AI", by_name["Sam Okafor"]
assert by_name["Sam Okafor"]["persona_priority"] == "secondary", by_name["Sam Okafor"]

# --- 2b. The real api_search teaser shape: first_name + obfuscated last + flags. ---
# The live people-search endpoint returns NO `name`/`email` — only first_name, an
# obfuscated last initial, title, id, and `has_email`. Compaction must surface a
# usable partial identity, not blanks, and mark the slot unrevealed.
def teaser_search(domain, titles, api_key, *, per_page=8):
    raw = {"people": [
        {"id": "ap-001", "first_name": "Dana", "last_name_obfuscated": "L.",
         "title": "VP of Product", "has_email": True, "has_direct_phone": True},
    ]}
    return {"status": "ok", "people": people._compact_people(raw)}

out = people.gather_people(account, PERSONAS, api_key="fake-key", searcher=teaser_search)
teaser = out["people"][0]
assert teaser["name"] == "Dana L.", teaser                       # first + obfuscated last initial
assert teaser["email"] == "", teaser                             # no email in the teaser
assert teaser["email_status"] == "available_unrevealed", teaser  # flagged, not faked
assert teaser["revealed"] is False, teaser
assert teaser["apollo_id"] == "ap-001", teaser
assert teaser["persona"] == "Chief Product Officer", teaser
assert any("reveal" in w.lower() for w in out["warnings"]), out["warnings"]  # honest reveal note

# --- 3. A failing Apollo call degrades to persona targets, never raises. ---
def boom(domain, titles, api_key, *, per_page=8):
    raise TimeoutError("apollo down")
out = people.gather_people(account, PERSONAS, api_key="fake-key", searcher=boom)
assert out["source"] == "local", out
assert out["persona_targets"], "should fall back to persona targets on failure"
assert any("failed" in w for w in out["warnings"]), out["warnings"]

print("OK: persona targets w/o key, Apollo contacts mapped to personas, failure degrades")
PY

echo "PASS test_people.sh"
