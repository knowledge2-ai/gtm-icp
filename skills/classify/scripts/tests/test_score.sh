#!/usr/bin/env bash
# Offline test for score.py — hard gates + graded dimensions + tier thresholds.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCORE="$HERE/../score.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

mkdir -p "$WORK/.gtm/strong" "$WORK/.gtm/midfit" "$WORK/.gtm/gated"

# Strong fit: all gates pass, 84/100 of points -> tier A (>=75 default).
cat >"$WORK/.gtm/strong/classify.json" <<'JSON'
{"company_name": "Strong",
 "gates": [
   {"key": "established",   "passed": true, "evidence": "founded 2012"},
   {"key": "not_ai_native", "passed": true, "evidence": "legacy TMS"}
 ],
 "dimensions": [
   {"key": "ai_gap",             "points_awarded": 27, "max_points": 30, "evidence": "x"},
   {"key": "data_workflow_moat", "points_awarded": 22, "max_points": 25, "evidence": "y"},
   {"key": "commercial_urgency", "points_awarded": 15, "max_points": 20, "evidence": "z"},
   {"key": "budget_access",      "points_awarded": 12, "max_points": 15, "evidence": "w"},
   {"key": "feasibility",        "points_awarded":  8, "max_points": 10, "evidence": "v"}
 ]}
JSON

# 84/100 -> tier A
out="$(GTM_ARTIFACT_ROOT="$WORK/.gtm" python3 "$SCORE" --slug strong)"
echo "$out" | grep -q '"score": 84.0' || fail "expected score 84.0 ($out)"
echo "$out" | grep -q '"tier": "A"'   || fail "expected tier A ($out)"

# Mid fit: all gates pass, 62/100 -> tier B (60 <= 62 < 75).
cat >"$WORK/.gtm/midfit/classify.json" <<'JSON'
{"company_name": "Midfit",
 "gates": [{"key": "established", "passed": true, "evidence": "founded 2018"}],
 "dimensions": [
   {"key": "ai_gap", "points_awarded": 40, "max_points": 60, "evidence": "x"},
   {"key": "moat",   "points_awarded": 22, "max_points": 40, "evidence": "y"}
 ]}
JSON

out_b="$(GTM_ARTIFACT_ROOT="$WORK/.gtm" python3 "$SCORE" --slug midfit)"
echo "$out_b" | grep -q '"score": 62.0' || fail "expected score 62.0 ($out_b)"
echo "$out_b" | grep -q '"tier": "B"'   || fail "expected tier B ($out_b)"

# Gate failure: high points but a failed gate -> tier Reject regardless.
cat >"$WORK/.gtm/gated/classify.json" <<'JSON'
{"company_name": "Gated",
 "gates": [
   {"key": "established",   "passed": true,  "evidence": "founded 2010"},
   {"key": "not_ai_native", "passed": false, "evidence": "AI-native founding premise"}
 ],
 "dimensions": [
   {"key": "ai_gap", "points_awarded": 30, "max_points": 30, "evidence": "x"}
 ]}
JSON

out_r="$(GTM_ARTIFACT_ROOT="$WORK/.gtm" python3 "$SCORE" --slug gated)"
echo "$out_r" | grep -q '"tier": "Reject"'        || fail "expected tier Reject ($out_r)"
echo "$out_r" | grep -q '"not_ai_native"'         || fail "expected failed gate listed ($out_r)"

echo "PASS test_score.sh"
