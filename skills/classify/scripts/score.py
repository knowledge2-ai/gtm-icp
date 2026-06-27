#!/usr/bin/env python3
"""Deterministically score a classified account against an ICP.

The classify *judgment* is made by the LLM (the skill), which writes a
classify.json holding (a) a pass/fail verdict per hard gate and (b) a graded
points award per scoring dimension, each with grounded evidence. This script
turns those verdicts into a transparent, reproducible score + tier — no LLM in
the loop, so the same classify.json always yields the same result.

This mirrors real ICP rubrics that combine *hard gates* (a single failure
disqualifies, regardless of score) with *weighted, graded dimensions*.

Expected classify.json shape:
    {
      "company_name": "Acme",
      "gates": [
        {"key": "founded_pre_2025", "passed": true,  "evidence": "..."},
        {"key": "not_ai_native",    "passed": true,  "evidence": "..."}
      ],
      "dimensions": [
        {"key": "ai_gap",            "points_awarded": 24, "max_points": 30, "evidence": "..."},
        {"key": "data_workflow_moat","points_awarded": 20, "max_points": 25, "evidence": "..."}
      ]
    }

Tiers (thresholds read from the ICP file, env override, then 75/60 default):
    any gate failed        -> "Reject"
    score >= tier_a        -> "A"
    score >= tier_b        -> "B"
    else                   -> "Nurture"

Writes .gtm/<slug>/score.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402


def _thresholds(criteria_path: Path) -> tuple[float, float]:
    tier_a, tier_b = 75.0, 60.0
    if criteria_path.exists():
        th = (gtm_lib.read_json(criteria_path).get("thresholds") or {})
        tier_a = float(th.get("tier_a", tier_a))
        tier_b = float(th.get("tier_b", tier_b))
    if os.environ.get("GTM_TIER_A"):
        tier_a = float(os.environ["GTM_TIER_A"])
    if os.environ.get("GTM_TIER_B"):
        tier_b = float(os.environ["GTM_TIER_B"])
    return tier_a, tier_b


def _red_flag_limit(criteria_path: Path) -> int:
    """How many anti-ICP red flags co-occur before an account is deprioritized.

    Default 2 (stolen from gtm-starter-kit's "2+ red flags = deprioritize"),
    overridable via the ICP file's `anti_icp.deprioritize_at` or env.
    """
    limit = 2
    if criteria_path.exists():
        anti = (gtm_lib.read_json(criteria_path).get("anti_icp") or {})
        limit = int(anti.get("deprioritize_at", limit))
    if os.environ.get("GTM_RED_FLAG_LIMIT"):
        limit = int(os.environ["GTM_RED_FLAG_LIMIT"])
    return limit


def score_account(classify: dict, *, tier_a: float, tier_b: float,
                  red_flag_limit: int = 2) -> dict:
    gates = classify.get("gates", [])
    dimensions = classify.get("dimensions", [])
    if not dimensions:
        raise ValueError("classify.json has no dimensions to score")

    gates_failed = [g["key"] for g in gates if not g.get("passed")]
    # Red flags (anti-ICP): soft disqualifiers. A hard gate auto-Rejects on a
    # single failure; red flags only *deprioritize*, and only once
    # `red_flag_limit` of them co-occur — one tolerable flag shouldn't sink an
    # otherwise strong account, but a cluster should.
    red_flags_present = [r["key"] for r in classify.get("red_flags", []) if r.get("present")]
    awarded = sum(float(d.get("points_awarded", 0)) for d in dimensions)
    max_points = sum(float(d.get("max_points", 0)) for d in dimensions)
    if max_points <= 0:
        raise ValueError("dimension max_points sum to zero; cannot score")
    score = round(100 * awarded / max_points, 1)

    if gates_failed:
        tier = "Reject"
    elif score >= tier_a:
        tier = "A"
    elif score >= tier_b:
        tier = "B"
    else:
        tier = "Nurture"

    deprioritized = not gates_failed and len(red_flags_present) >= red_flag_limit
    if deprioritized:
        tier = "Nurture"

    breakdown = [
        f"{d['key']} {d.get('points_awarded', 0)}/{d.get('max_points', 0)}"
        for d in dimensions
    ]
    if gates_failed:
        rationale = f"Rejected on gate(s): {', '.join(gates_failed)} (score would be {score})."
    elif deprioritized:
        rationale = (f"{score}/100 but deprioritized to Nurture on "
                     f"{len(red_flags_present)} red flag(s): {', '.join(red_flags_present)}.")
    else:
        rationale = f"{score}/100 [{'; '.join(breakdown)}] -> tier {tier}"

    return {
        "company_name": classify.get("company_name"),
        "score": score,
        "tier": tier,
        "gates_failed": gates_failed,
        "red_flags_present": red_flags_present,
        "deprioritized": deprioritized,
        "dimension_breakdown": breakdown,
        "rationale": rationale,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a classified account.")
    ap.add_argument("--slug", required=True, help="account slug under the artifact root")
    ap.add_argument("--criteria", type=Path, default=Path("icp.criteria.json"),
                    help="ICP file to read tier thresholds from")
    args = ap.parse_args(argv)

    tier_a, tier_b = _thresholds(args.criteria)
    classify = gtm_lib.read_json(gtm_lib.stage_path(args.slug, "classify"))
    out = score_account(classify, tier_a=tier_a, tier_b=tier_b,
                        red_flag_limit=_red_flag_limit(args.criteria))
    path = gtm_lib.write_json(gtm_lib.stage_path(args.slug, "score"), out)
    print(json.dumps({"slug": args.slug, "score": out["score"], "tier": out["tier"],
                      "gates_failed": out["gates_failed"],
                      "red_flags_present": out["red_flags_present"],
                      "deprioritized": out["deprioritized"], "artifact": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
