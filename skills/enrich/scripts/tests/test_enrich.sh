#!/usr/bin/env bash
# Offline test for enrich.py — local fallback path, no network, no keys.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENRICH="$HERE/../enrich.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

cat >"$WORK/account.json" <<'JSON'
{"company_name": "Acme Robotics", "domain": "acme.example",
 "industry": "Industrial Automation", "employee_count": 320}
JSON

# Force local mode so the test never touches the network.
out="$(GTM_ARTIFACT_ROOT="$WORK/.gtm" APOLLO_API_KEY="" \
  python3 "$ENRICH" --input "$WORK/account.json" --local)"

echo "$out" | grep -q '"enrichment_source": "local"' || fail "expected local enrichment_source ($out)"

artifact="$WORK/.gtm/acme-robotics/enrich.json"
[ -f "$artifact" ] || fail "enrich.json not written at $artifact"
grep -q '"company_name": "Acme Robotics"' "$artifact" || fail "company_name not preserved"
grep -q '"employee_count": 320' "$artifact" || fail "firmographics not preserved"

# --- vertical mapping: Apollo `industry` -> an ICP priority vertical ----------
python3 - "$ENRICH" <<'PY' || fail "vertical mapping"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("enrich", sys.argv[1])
enrich = importlib.util.module_from_spec(spec); spec.loader.exec_module(enrich)

PV = ["automotive", "logistics", "healthcare admin", "manufacturing", "warehouse"]
# Direct substring hit.
assert enrich._map_vertical("Automotive", PV) == "automotive"
# Alias: Apollo phrasing -> priority vertical (only because it's in PV).
assert enrich._map_vertical("transportation/trucking/railroad", PV) == "logistics"
assert enrich._map_vertical("Hospital & Health Care", PV) == "healthcare admin"
# Description can supply the signal when industry is generic.
assert enrich._map_vertical("Software", PV, "fleet telematics for trucking") == "logistics"
# Target not in this ICP's list -> no match.
assert enrich._map_vertical("Legal Services", PV) is None
# Empty -> None.
assert enrich._map_vertical("", PV) is None

# enrich() sets `vertical` from industry without clobbering an explicit one.
out = enrich.enrich({"company_name": "Freight Co", "industry": "Trucking"},
                    local=True, verticals=PV)
assert out["vertical"] == "logistics", out
out2 = enrich.enrich({"company_name": "X", "industry": "Trucking", "vertical": "preset"},
                     local=True, verticals=PV)
assert out2["vertical"] == "preset", out2
print("OK: vertical mapping")
PY

echo "PASS test_enrich.sh"
