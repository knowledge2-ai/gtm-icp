#!/usr/bin/env bash
# Offline test for k2_query.py — search_corpus with an injected poster (no network,
# no key), response compaction, query building, and the local fallback.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
K2Q="$HERE/../k2_query.py"

python3 - "$K2Q" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("k2_query", sys.argv[1])
k2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(k2)

# --- not configured -> skipped, never an error -----------------------------
r = k2.search_corpus(["x"], host=None, api_key=None, corpus_id=None)
assert r["status"] == "skipped", r
assert r["snippets"] == [], r

# --- configured: inject a poster returning a /search:batch-shaped payload ---
CAPTURED = {}
def fake_poster(url, payload, headers, timeout):
    CAPTURED["url"] = url
    CAPTURED["payload"] = payload
    CAPTURED["headers"] = headers
    # Mirrors the live /search:batch shape: groups under `responses`, hits under
    # `results`, structured fields under `custom_metadata`, provenance under
    # `system_metadata.provenance`.
    return ({"responses": [
        {"results": [
            {"text": "Won Acme TMS — incumbent logistics, shallow AI add-on.", "score": 0.91,
             "custom_metadata": {"tier": "A", "total_score": 82},
             "system_metadata": {"provenance": {"source_uri": "k2://acme/evidence/1"}}},
            {"text": "Lost deal — AI-native startup, no workflow moat.", "score": 0.40},
            {"text": "Won Acme TMS — incumbent logistics, shallow AI add-on.", "score": 0.91},  # dup
        ]},
        {"results": [
            {"content": "Positioning: ground AI on proprietary dispatch data.", "relevance": 0.77},
        ]},
    ]}, None)

r = k2.search_corpus(["won deals", "logistics positioning"], host="https://api.knowledge2.ai/",
                     api_key="k2-secret", corpus_id="corp-123", top_k=5, poster=fake_poster)
assert r["status"] == "ok", r

# Request was shaped to the real contract.
assert CAPTURED["url"] == "https://api.knowledge2.ai/v1/corpora/corp-123/search:batch", CAPTURED["url"]
assert CAPTURED["payload"]["queries"] == ["won deals", "logistics positioning"], CAPTURED["payload"]
assert CAPTURED["payload"]["top_k"] == 5, CAPTURED["payload"]
assert CAPTURED["headers"]["X-API-Key"] == "k2-secret", "auth header must be X-API-Key"

# Snippets compacted: deduped, ranked by score, text + metadata surfaced.
snips = r["snippets"]
texts = [s["text"] for s in snips]
assert len(texts) == 3, texts                       # dup collapsed (4 hits -> 3)
assert texts[0].startswith("Won Acme TMS"), texts   # highest score first (0.91)
assert snips[0]["metadata"] == {"tier": "A", "total_score": 82}, snips[0]  # custom_metadata captured
assert snips[0]["source"] == "k2://acme/evidence/1", snips[0]              # provenance source_uri captured
assert any(t.startswith("Positioning") for t in texts), texts  # second query group merged in

# top_k is clamped to the server cap.
big = k2.search_corpus(["q"], host="h", api_key="k", corpus_id="c", top_k=999, poster=fake_poster)
assert CAPTURED["payload"]["top_k"] == k2.SEARCH_BATCH_MAX_TOP_K, CAPTURED["payload"]["top_k"]

# --- transport error -> warning + local fallback, never raises -------------
def boom_poster(url, payload, headers, timeout):
    return None, "K2 connection failed: timed out"
r = k2.search_corpus(["q"], host="h", api_key="k", corpus_id="c", poster=boom_poster)
assert r["status"] == "warning" and r["snippets"] == [], r

# --- query building from an enriched account -------------------------------
qs = k2.build_queries(
    {"company_name": "Acme Freight", "vertical": "logistics"},
    {"signals_detected": [{"found": True, "matched_keywords": ["langchain", "llm"]}]},
)
assert any("Acme Freight" in q for q in qs), qs
assert any("logistics" in q for q in qs), qs
assert any("langchain" in q for q in qs), qs   # signal-driven query included
assert len(qs) == len(set(qs)), qs             # deduped

print("OK: skipped/ok/warning paths, contract shape, compaction, query building")
PY

echo "PASS test_k2_query.sh"
