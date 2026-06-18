#!/usr/bin/env python3
"""Retrieve grounding snippets for an account from a K2 corpus (no MCP needed).

`classify` and `personalize` are stronger when they cite *your* won-deal
patterns, positioning, and case studies instead of guessing. This script is the
real retrieval path: it queries a Knowledge2 corpus over plain HTTP and returns
compact, citable snippets the skill can ground on.

Contract (matches the live K2 REST API):

    POST {host}/v1/corpora/{corpus_id}/search:batch
    headers: X-API-Key: <k2_api_key>, accept/content-type application/json
    body:    {"queries": ["..."], "top_k": <=20}

Configuration comes from the plugin userConfig, surfaced as env vars:

    K2_API_HOST   e.g. https://api.knowledge2.ai
    K2_API_KEY    sent as X-API-Key
    K2_CORPUS_ID  the corpus to search

If any of the three is missing the script does NOT fail — it returns a
`local-fallback` result naming the local `corpus/` files, which is the no-key
grounding path. The skill reads either the returned snippets (K2) or those
local files. Network/HTTP errors degrade to a `warning` with the same fallback,
never an exception.

Reads  .gtm/<slug>/enrich.json (+ signals.json) to build the queries.
Writes .gtm/<slug>/grounding.json

stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

USER_AGENT = "gtm-icp/0.1 (+https://github.com/knowledge2-ai/gtm-icp)"
# The /search:batch endpoint hard-caps top_k server-side; mirror it so we never
# ask for more than the API will return (no silent truncation).
SEARCH_BATCH_MAX_TOP_K = 20
PLUGIN_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# HTTP (the real K2 call) — injectable for offline tests
# --------------------------------------------------------------------------- #
def http_post_json(url, payload, headers, timeout=12.0):
    """POST JSON, return (parsed_json, error). Never raises."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(5_000_000).decode("utf-8", errors="replace")
            return (json.loads(raw) if raw else {}), None
    except HTTPError as exc:
        body = exc.read(100_000).decode("utf-8", errors="replace")
        return None, f"K2 HTTP {exc.code}: {body[:200]}"
    except (URLError, TimeoutError) as exc:
        return None, f"K2 connection failed: {exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"K2 returned invalid JSON: {exc}"


# --------------------------------------------------------------------------- #
# Response compaction (tolerant of the batch response shape)
# --------------------------------------------------------------------------- #
def _first_list(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, list):
            return v
    return []


def _hit_text(hit):
    for k in ("text", "content", "chunk", "snippet", "body"):
        v = hit.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Some shapes nest the text under a `document` object.
    doc = hit.get("document")
    if isinstance(doc, dict):
        return _hit_text(doc)
    return ""


def _hit_score(hit):
    for k in ("score", "relevance", "similarity", "distance"):
        v = hit.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _hit_metadata(hit):
    """The structured fields K2 attaches to a hit.

    The live /search:batch response carries them under `custom_metadata`
    (tier, total_score, ai_posture, persona_titles, outreach_angle, signal_tags,
    company/domain, …); older/other shapes use a plain `metadata`. Read either.
    """
    for k in ("custom_metadata", "metadata"):
        v = hit.get(k)
        if isinstance(v, dict) and v:
            return v
    return {}


def _hit_source(hit):
    """A citable source URI for the hit, if present (system_metadata.provenance)."""
    sysmeta = hit.get("system_metadata")
    if isinstance(sysmeta, dict):
        prov = sysmeta.get("provenance")
        if isinstance(prov, dict) and isinstance(prov.get("source_uri"), str):
            return prov["source_uri"]
        if isinstance(sysmeta.get("source_uri"), str):
            return sysmeta["source_uri"]
    return None


def compact_results(payload, *, cap=8):
    """Flatten a /search:batch payload into ranked, deduped citable snippets.

    The batch endpoint nests groups under `responses` (one per query); each
    group holds its hits under `results`. We tolerate the common key spellings
    (`results`/`responses`/`matches`/`hits`/`documents`) so a minor server-side
    rename doesn't silently drop grounding.
    """
    if not isinstance(payload, dict):
        return []
    groups = _first_list(payload, ["results", "responses", "queries"])
    if not groups:
        # Single ungrouped hit list.
        groups = [payload]
    snippets = []
    seen = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        query = group.get("query") if isinstance(group.get("query"), str) else None
        hits = _first_list(group, ["matches", "hits", "documents", "results", "chunks"])
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            text = _hit_text(hit)
            if not text:
                continue
            key = text[:160]
            if key in seen:
                continue
            seen.add(key)
            snippets.append({
                "text": text,
                "score": _hit_score(hit),
                "metadata": _hit_metadata(hit),
                "source": _hit_source(hit),
                "query": query,
            })
    snippets.sort(key=lambda s: s["score"], reverse=True)
    return snippets[:cap]


# --------------------------------------------------------------------------- #
# Retrieval entry point
# --------------------------------------------------------------------------- #
def search_corpus(queries, *, host, api_key, corpus_id, top_k=5, timeout=12.0,
                  poster=http_post_json, cap=8):
    """Query a K2 corpus and return compacted grounding snippets.

    Returns a dict with `status`:
      - "skipped"        — K2 not configured (host/key/corpus missing).
      - "ok"             — snippets retrieved.
      - "warning"        — the call failed; caller should fall back to local.
    Never raises.
    """
    queries = [q for q in (queries or []) if isinstance(q, str) and q.strip()]
    if not (host and api_key and corpus_id):
        return {"status": "skipped", "reason": "K2 not configured (need host, key, corpus id)",
                "snippets": []}
    if not queries:
        return {"status": "skipped", "reason": "no queries built", "snippets": []}
    url = f"{host.rstrip('/')}/v1/corpora/{corpus_id}/search:batch"
    body = {"queries": queries, "top_k": max(1, min(top_k, SEARCH_BATCH_MAX_TOP_K))}
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "User-Agent": USER_AGENT,
        "X-API-Key": api_key,
    }
    payload, err = poster(url, body, headers, timeout)
    if err:
        return {"status": "warning", "warning": err, "snippets": []}
    return {"status": "ok", "snippets": compact_results(payload, cap=cap),
            "queries": queries}


# --------------------------------------------------------------------------- #
# Query building + local fallback
# --------------------------------------------------------------------------- #
def build_queries(enrich, signals=None):
    """Turn an enriched account into a few retrieval queries for the corpus."""
    company = (enrich or {}).get("company_name") or (enrich or {}).get("name") or ""
    vertical = (enrich or {}).get("vertical") or (enrich or {}).get("industry") or ""
    queries = []
    if company:
        queries.append(f"won deals and ICP fit patterns for companies like {company}".strip())
    if vertical:
        queries.append(f"{vertical} positioning, case studies, and won-deal evidence".strip())
    # A signal-driven query sharpens retrieval toward the detected intent.
    for s in (signals or {}).get("signals_detected", []) or []:
        if s.get("found") and s.get("matched_keywords"):
            queries.append(f"{vertical or company} AI gap: {', '.join(s['matched_keywords'][:3])}".strip())
            break
    if not queries:
        queries.append("ideal customer profile won-deal patterns and positioning")
    # Dedupe, preserve order.
    seen, out = set(), []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def resolve_k2_config(env):
    """Resolve K2 host / key / corpus id from env, accepting common aliases.

    The plugin's canonical vars are K2_API_HOST + K2_API_KEY + K2_CORPUS_ID, but
    an existing Knowledge2 deployment may expose K2_BASE_URL and named per-corpus
    ids (K2_EVIDENCE_CORPUS_ID etc., the evidence corpus being the won-deal
    grounding classify wants). Accept those so the script runs against either
    naming without re-exporting vars by hand.
    """
    host = env.get("K2_API_HOST") or env.get("K2_BASE_URL")
    api_key = env.get("K2_API_KEY")
    corpus_id = (env.get("K2_CORPUS_ID")
                 or env.get("K2_EVIDENCE_CORPUS_ID")
                 or env.get("K2_CANDIDATE_CORPUS_ID"))
    return host, api_key, corpus_id


def local_corpus_files(root=None):
    """List local corpus markdown files — the no-key grounding source."""
    base = Path(root) if root else PLUGIN_ROOT / "corpus"
    if not base.is_dir():
        return []
    return sorted(str(p.relative_to(PLUGIN_ROOT)) for p in base.glob("**/*.md"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Retrieve K2 corpus grounding for an account.")
    ap.add_argument("--slug", help="account slug under the artifact root")
    ap.add_argument("--query", action="append", help="explicit query (repeatable); overrides slug-built queries")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--write", action="store_true", help="write grounding.json into the account dir")
    args = ap.parse_args(argv)

    host, api_key, corpus_id = resolve_k2_config(os.environ)

    enrich = signals = {}
    acct_dir = None
    if args.slug:
        acct_dir = gtm_lib.account_dir(args.slug)
        enrich = _load_json(acct_dir / "enrich.json")
        signals = _load_json(acct_dir / "signals.json")

    queries = args.query or build_queries(enrich, signals)
    result = search_corpus(queries, host=host, api_key=api_key, corpus_id=corpus_id,
                           top_k=args.top_k)

    # Always attach the local fallback so the skill can ground with no key.
    if result["status"] != "ok" or not result.get("snippets"):
        result.setdefault("status", "skipped")
        result["local_fallback"] = local_corpus_files()
    result["queries"] = queries

    if args.write and acct_dir is not None:
        acct_dir.mkdir(parents=True, exist_ok=True)
        (acct_dir / "grounding.json").write_text(json.dumps(result, indent=2))
        print(f"wrote {acct_dir / 'grounding.json'} (status={result['status']})")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
