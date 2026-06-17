#!/usr/bin/env python3
"""Discover companies that fit an ICP, from a natural-language brief.

Provider cascade (ported from icp_engine, self-contained, stdlib + urllib):

  1. Perplexity Sonar (primary) — structured, web-grounded company list.
     Requires PERPLEXITY_API_KEY. Grounds on icp.criteria.json.
  2. DuckDuckGo HTML (no-key fallback) — best-effort scrape of search
     results; may return little when DDG serves a JS page.
  3. Seed list (--seeds <file>) — deterministic, no network: parse a
     "Name, domain" / bare-domain list you already have.

Each discovered company is written as .gtm/<slug>/input.json so the rest of
the pipeline (enrich -> classify -> score -> people -> list) consumes it with
no changes. A roll-up lands at .gtm/_discover/candidates.json.

Usage:
    python discover.py --brief "Series B logistics SaaS in NA, 100-500 emp, hiring ML"
    python discover.py --seeds my_list.csv
    python discover.py --brief "..." --max 15 --provider duckduckgo
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

USER_AGENT = "gtm-icp-discovery/0.1 (+https://github.com/gemini2026/gtm-icp)"
SEARCH_TIMEOUT = 10
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = os.environ.get("GTM_PERPLEXITY_MODEL", "sonar")

# Hosts that are search/aggregator/social noise, never the company's own site.
BLOCKED_HOSTS = {
    "duckduckgo.com", "google.com", "bing.com", "linkedin.com", "github.com",
    "x.com", "twitter.com", "facebook.com", "youtube.com", "crunchbase.com",
    "g2.com", "capterra.com", "instagram.com", "producthunt.com",
}
# Social/code hosts we keep as *references* attached to a candidate.
EXTERNAL_HOSTS = {"github.com": "github", "linkedin.com": "linkedin"}


# --- inlined helpers (from icp_engine text.py / enrichment.py) ----------------

def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_domain(domain: str) -> str:
    domain = domain.strip()
    if not domain:
        return ""
    parsed = urlparse(domain if "://" in domain else f"https://{domain}")
    host = (parsed.hostname or parsed.path.split("/", 1)[0]).lower()
    return host or ""


@dataclass
class Candidate:
    company: str
    domain: str
    source_url: str
    source_title: str = ""
    notes: str = ""
    github_urls: list[str] = field(default_factory=list)
    linkedin_urls: list[str] = field(default_factory=list)


# --- Perplexity (primary) -----------------------------------------------------

COMPANIES_SCHEMA = {
    "type": "object",
    "required": ["companies"],
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["company", "domain"],
                "properties": {
                    "company": {"type": "string"},
                    "domain": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


def _perplexity(brief: str, criteria_md: str, max_results: int) -> tuple[list[Candidate], list[str]]:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return [], ["PERPLEXITY_API_KEY not set."]
    system = (
        "You are a B2B GTM research analyst. Find real, currently-operating companies "
        "that fit the ICP criteria below using live web research. For each, return its "
        "bare primary website domain (e.g. acme.com) and a one-sentence reason it fits. "
        "Do not invent companies or domains.\n\nICP CRITERIA:\n" + (criteria_md or "(none provided)")
    )
    user = f"Research brief: {brief}\n\nReturn up to {max_results} distinct companies."
    body = json.dumps({
        "model": PERPLEXITY_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 1024,
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": {"schema": COMPANIES_SCHEMA}},
    }).encode("utf-8")
    req = Request(PERPLEXITY_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": USER_AGENT,
    })
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read(5_000_000).decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [], [f"Perplexity request failed: {exc}"]

    choices = payload.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        content = str((choices[0].get("message") or {}).get("content") or "")
    citations = [c for c in (payload.get("citations") or []) if isinstance(c, str)]
    try:
        companies = json.loads(content).get("companies", []) if content else []
    except json.JSONDecodeError:
        return [], ["Perplexity returned non-JSON content."]

    out, seen = [], set()
    for item in companies:
        if not isinstance(item, dict):
            continue
        domain = normalize_domain(str(item.get("domain") or ""))
        if not domain or "." not in domain or domain in seen:
            continue
        seen.add(domain)
        matching = [u for u in citations if domain in u.lower()]
        out.append(Candidate(
            company=str(item.get("company") or "").strip() or domain,
            domain=domain,
            source_url=matching[0] if matching else f"https://{domain}",
            source_title="Perplexity research result",
            notes=str(item.get("reason") or "").strip() or "Sourced via Perplexity research.",
        ))
        if len(out) >= max_results:
            break
    return out, [] if out else ["Perplexity returned no usable companies."]


# --- DuckDuckGo HTML (no-key fallback) ----------------------------------------

class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = {k: (v or "") for k, v in attrs}
        href = a.get("href", "")
        if href and ("result__a" in a.get("class", "") or "/l/?" in href or href.startswith("http")):
            self._href, self._text = href, []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, normalize_whitespace(" ".join(self._text))))
            self._href, self._text = None, []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)


def _unwrap_ddg(url: str) -> str:
    parsed = urlparse(url)
    if (not parsed.netloc or "duckduckgo.com" in parsed.netloc) and parsed.path.startswith("/l/"):
        vals = parse_qs(parsed.query).get("uddg")
        if vals:
            return vals[0]
    return url


def _name_from(title: str, domain: str) -> str:
    if title:
        first = re.split(r"[-|:]", title, maxsplit=1)[0].strip()
        if first and len(first) <= 80:
            return first
    stem = domain.removeprefix("www.").split(".", 1)[0]
    return " ".join(p.capitalize() for p in re.split(r"[-_]", stem) if p)


def _candidates_from_links(links: list[tuple[str, str]], max_results: int) -> list[Candidate]:
    by_domain: dict[str, Candidate] = {}
    refs: list[tuple[str, str]] = []  # (bucket, url)
    for raw_url, raw_title in links:
        url = _unwrap_ddg(raw_url)
        title = normalize_whitespace(html.unescape(raw_title))
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if not host:
            continue
        bucket = next((b for h, b in EXTERNAL_HOSTS.items() if host == h or host.endswith(f".{h}")), "")
        if bucket:
            refs.append((bucket, url))
            continue
        if host in {h.removeprefix("www.") for h in BLOCKED_HOSTS}:
            continue
        domain = normalize_domain(host)
        if not domain or "." not in domain:
            continue
        if domain not in by_domain and len(by_domain) < max_results:
            by_domain[domain] = Candidate(
                company=_name_from(title, domain), domain=domain, source_url=url,
                source_title=title, notes=f"Discovered from search: {title}" if title else "Discovered from search.",
            )
    # Attach github/linkedin refs to a candidate whose domain stem appears in the ref URL.
    for cand in by_domain.values():
        stem = cand.domain.split(".", 1)[0]
        for bucket, url in refs:
            if stem and stem in url.lower():
                (cand.github_urls if bucket == "github" else cand.linkedin_urls).append(url)
    return list(by_domain.values())


def _duckduckgo(query: str, max_results: int) -> tuple[list[Candidate], list[str]]:
    req = Request(f"https://duckduckgo.com/html/?q={quote_plus(query)}",
                  headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read(1_500_000).decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        return [], [f"DuckDuckGo search failed: {exc}"]
    parser = _LinkParser()
    parser.feed(body)
    cands = _candidates_from_links(parser.links, max_results)
    return cands, [] if cands else ["No company domains discovered from search results."]


# --- Seed list (deterministic, no network) ------------------------------------

def parse_seed_companies(seed_text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for line in seed_text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,|\t]", cleaned) if p.strip()]
        first = parts[0].lower() if parts else ""
        second = parts[1].lower() if len(parts) > 1 else ""
        if first in {"company", "name", "account"} and second in {"domain", "website", "url"}:
            continue  # header row
        pairs: list[tuple[str, str]] = []
        looks_domain = lambda s: "." in normalize_domain(s)
        if len(parts) >= 2 and all(looks_domain(p) for p in parts):
            pairs = [("", normalize_domain(p)) for p in parts]
        elif len(parts) >= 2 and looks_domain(parts[1]):
            pairs = [(parts[0], normalize_domain(parts[1]))]
        elif len(parts) == 1 and looks_domain(parts[0]):
            pairs = [("", normalize_domain(parts[0]))]
        for company, domain in pairs:
            if domain:
                out.append(Candidate(
                    company=company or _name_from("", domain), domain=domain,
                    source_url=f"https://{domain}", source_title="Manual seed",
                    notes="Manually seeded.",
                ))
    return out


# --- orchestration ------------------------------------------------------------

def discover(brief: str, criteria_md: str, *, max_results: int, provider: str) -> tuple[list[Candidate], list[str], str]:
    warnings: list[str] = []
    want_pplx = provider in {"auto", "perplexity"} and os.environ.get("PERPLEXITY_API_KEY")
    if want_pplx:
        cands, w = _perplexity(brief, criteria_md, max_results)
        warnings += w
        if cands:
            return cands, warnings, "perplexity"
        warnings.append("Falling back to DuckDuckGo.")
    cands, w = _duckduckgo(brief, max_results)
    warnings += w
    return cands, warnings, ("duckduckgo" if cands else "none")


def _criteria_markdown() -> str:
    path = Path("icp.criteria.json")
    if not path.exists():
        return ""
    data = gtm_lib.read_json(path)
    lines = [f"# {data.get('name', 'ICP')}"]
    gates = data.get("gates", [])
    if gates:
        lines.append("\n## Hard gates (a company must satisfy all of these)")
        for g in gates:
            lines.append(f"- {g.get('key')}: {g.get('description', '')}")
    dims = data.get("dimensions", [])
    if dims:
        lines.append("\n## Scoring dimensions (stronger on these = better fit)")
        for d in dims:
            lines.append(f"- {d.get('key')} (up to {d.get('max_points')} pts): {d.get('description', '')}")
    verticals = data.get("priority_verticals", [])
    if verticals:
        lines.append("\n## Priority verticals\n" + ", ".join(verticals))
    # Back-compat: older flat-criteria ICP files.
    for c in data.get("criteria", []):
        lines.append(f"- {c.get('key')}: {c.get('description', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover ICP-fit companies.")
    ap.add_argument("--brief", help="natural-language ICP brief")
    ap.add_argument("--seeds", type=Path, help="path to a Name,domain seed list (no network)")
    ap.add_argument("--max", type=int, default=10, help="max companies (default 10)")
    ap.add_argument("--provider", default="auto", choices=["auto", "perplexity", "duckduckgo"])
    args = ap.parse_args(argv)

    if args.seeds:
        candidates = parse_seed_companies(args.seeds.read_text(encoding="utf-8"))
        warnings, provider = ([] if candidates else ["No companies parsed from seed list."]), "seeds"
    elif args.brief:
        candidates, warnings, provider = discover(
            args.brief, _criteria_markdown(), max_results=args.max, provider=args.provider)
    else:
        ap.error("provide --brief or --seeds")

    written = []
    for cand in candidates:
        slug = gtm_lib.slugify(cand.domain or cand.company)
        record = {
            "company_name": cand.company,
            "domain": cand.domain,
            "source_url": cand.source_url,
            "discovery_notes": cand.notes,
            "github_urls": cand.github_urls,
            "linkedin_urls": cand.linkedin_urls,
        }
        gtm_lib.write_json(gtm_lib.stage_path(slug, "input").with_name("input.json"), record)
        written.append({"slug": slug, "company": cand.company, "domain": cand.domain})

    rollup = gtm_lib.artifact_root() / "_discover" / "candidates.json"
    gtm_lib.write_json(rollup, {"provider": provider, "count": len(written),
                                "warnings": warnings, "candidates": written})
    print(json.dumps({"provider": provider, "discovered": len(written),
                      "warnings": warnings, "rollup": str(rollup)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
