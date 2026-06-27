#!/usr/bin/env python3
"""Deep-enrich an account with ICP-relevant *signals* from public sources.

Firmographics (enrich.py) tell you size/industry. Signals tell you *intent*:
a company hiring LangChain engineers is actively closing an AI gap — direct
evidence for an ICP's commercial-urgency / ai-gap dimensions, not a guess.

This script fetches the homepage, careers/jobs pages, public ATS job boards
(Greenhouse / Lever / Ashby), and GitHub repos for an account, then scans them
for the keyword groups the ICP declares. Each group names the scoring
`dimension` it informs, so the detected signals flow straight into classify's
evidence. ATS boards expose structured per-posting text via public JSON APIs —
far more reliable than scraping a `/careers` HTML page — and are where hiring
signals like "LangChain" actually live. Absence is evidence too: "checked job
boards + github, found no AI hiring" legitimately widens an incumbent's ai_gap.

Signal groups come from `icp.criteria.json`:

    "signals": [
      {"key": "ai_hiring", "informs": "commercial_urgency",
       "keywords": ["langchain", "llm", "rag", "vector database"],
       "interpretation": "Hiring AI/LLM talent = feels AI pressure, building in-house."}
    ]

Reads  .gtm/<slug>/enrich.json  (falls back to input.json) for domain/company.
Writes .gtm/<slug>/signals.json

stdlib only. No key required; GITHUB_TOKEN (optional) raises the GitHub rate
limit. All fetches are best-effort and SSRF-guarded — failures degrade to
warnings, never raise.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import gtm_lib  # noqa: E402

USER_AGENT = "gtm-icp/0.1 (+https://github.com/knowledge2-ai/gtm-icp)"
GITHUB_SEARCH = "https://api.github.com/search/repositories"
# Pages worth scanning for intent signals, relative to the domain root.
CANDIDATE_PATHS = ["", "/careers", "/jobs", "/company/careers", "/about"]
SNIPPET_RADIUS = 110


# --------------------------------------------------------------------------- #
# Fetching (SSRF-guarded, HTML -> text)
# --------------------------------------------------------------------------- #
def _is_public_url(url: str) -> bool:
    """Only http(s) to a publicly-routable host — blocks SSRF to internal nets."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " ").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Publish-date capture (recency). A fetched page / job posting / repo carries a
# date so the personalize stage can lead on the freshest signal instead of stale
# news. Undated sources stay neutral downstream — absence of a date never reads
# as "old". Mirrors the dashboard engine's text.extract_published_date.
# --------------------------------------------------------------------------- #
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:.\-_]+)\s*=\s*["']([^"']*)["']""")
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.IGNORECASE)
_TIME_RE = re.compile(r"""<time\b[^>]*\bdatetime\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_DATE_META_KEYS = {
    "article:published_time", "date", "pubdate", "publishdate",
    "publication_date", "dc.date", "dc.date.issued", "datepublished",
}


def _to_iso_date(raw: str | None) -> str | None:
    """Normalize a date string (ISO 8601, epoch ms, or RFC 2822) to YYYY-MM-DD."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):  # Lever postings use epoch milliseconds.
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat() if parsed is not None else None


def _html_published_date(html: str, last_modified: str | None = None) -> str | None:
    """Best-effort publish date for a fetched page: in-page meta/JSON-LD/<time>,
    then the HTTP Last-Modified header. Returns YYYY-MM-DD or None (neutral)."""
    for tag in _META_TAG_RE.findall(html or ""):
        attrs = {key.lower(): value for key, value in _ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in _DATE_META_KEYS:
            iso = _to_iso_date(attrs.get("content"))
            if iso:
                return iso
    for match in _JSONLD_DATE_RE.findall(html or ""):
        iso = _to_iso_date(match)
        if iso:
            return iso
    for match in _TIME_RE.findall(html or ""):
        iso = _to_iso_date(match)
        if iso:
            return iso
    return _to_iso_date(last_modified)


# ATS board links embedded in careers/homepage HTML — the reliable way to learn
# a company's *real* board slug instead of guessing it from the name.
_ATS_REF_PATTERNS = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]+)", re.I)),
]
_ATS_SLUG_STOP = {"embed", "job_board", "for", "v0", "v1", "boards", "postings", "job-board"}


def _extract_ats_refs(html: str) -> list[dict]:
    """Find embedded ATS board links in raw HTML -> [{provider, slug}, ...]."""
    refs, seen = [], set()
    for provider, pat in _ATS_REF_PATTERNS:
        for slug in pat.findall(html or ""):
            slug = slug.strip().lower()
            key = (provider, slug)
            if slug and slug not in _ATS_SLUG_STOP and key not in seen:
                seen.add(key)
                refs.append({"provider": provider, "slug": slug})
    return refs


def http_get_text(url: str, timeout: float = 8.0) -> tuple[str | None, list[dict], str | None, str | None]:
    """Return (text, ats_refs, published_at, error). Never raises; SSRF-guarded.

    ats_refs are ATS board links found in the raw HTML before tag-stripping, so
    we keep them out of the keyword-scanned text (a URL containing "api" must not
    fire the workflow signal) while still learning the company's real board slug.
    published_at is the page's best-effort publish date (or None) so the freshest
    pages can lead in personalize. gather_signals also accepts the older 3-tuple
    form, so injected/offline fetchers need not supply a date.
    """
    if not _is_public_url(url):
        return None, [], None, f"skipped non-public url: {url}"
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            last_modified = resp.headers.get("Last-Modified")
            raw = resp.read(2_000_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return None, [], None, f"fetch failed {url}: {exc}"
    return _html_to_text(raw), _extract_ats_refs(raw), _html_published_date(raw, last_modified), None


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only — for owner/company token comparison."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _filter_repos(items: list, company: str, domain: str) -> list:
    """Keep only repos that plausibly belong to *this* account.

    A bare `"<company>" OR "<domain>"` GitHub search is noisy: any repo that
    merely mentions the name in its description matches. We tighten to repos
    that are actually owned by the company — normalized `owner.login` equals or
    contains the company/domain token (or vice-versa) — or whose `homepage`
    points at the domain. Forks are dropped (they mirror someone else's code),
    and the survivors are ranked by stars.
    """
    domain_root = domain.split(".")[0] if domain else ""
    targets = [t for t in (_norm(company), _norm(domain_root)) if len(t) >= 3]
    kept = []
    for it in items:
        if it.get("fork"):
            continue
        owner = _norm((it.get("owner") or {}).get("login", ""))
        homepage = (it.get("homepage") or "").lower()
        owner_match = any(
            owner and (owner == t or t in owner or owner in t) for t in targets
        )
        home_match = bool(domain) and domain.lower() in homepage
        if owner_match or home_match:
            kept.append(it)
    kept.sort(key=lambda it: it.get("stargazers_count") or 0, reverse=True)
    return kept


def github_repos(company: str, domain: str, timeout: float = 8.0) -> dict:
    """Best-effort GitHub repo metadata for the account. Never raises."""
    query = quote_plus(f'"{company}" OR "{domain}"')
    # Pull a wider candidate set, then filter to repos actually owned by the
    # account (see _filter_repos) and keep the top 5 by stars.
    url = f"{GITHUB_SEARCH}?q={query}&per_page=30&sort=stars"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
            payload = json.loads(resp.read(1_000_000).decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "warning", "warning": f"github search failed: {exc}", "repositories": []}
    matched = _filter_repos(payload.get("items") or [], company, domain)
    repos = [{
        "name": it.get("full_name"), "url": it.get("html_url"),
        "description": it.get("description") or "", "language": it.get("language") or "",
        "stars": it.get("stargazers_count") or 0, "updated_at": it.get("updated_at"),
    } for it in matched[:5]]
    return {"status": "ok", "repositories": repos}


# --------------------------------------------------------------------------- #
# Hiring boards (public ATS JSON APIs — no key, structured per-posting text)
# --------------------------------------------------------------------------- #
def http_get_json(url: str, timeout: float = 8.0) -> tuple[object | None, str | None]:
    """Return (parsed_json, error). Never raises; SSRF-guarded."""
    if not _is_public_url(url):
        return None, f"skipped non-public url: {url}"
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(4_000_000).decode("utf-8", errors="replace")), None
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return None, f"fetch failed {url}: {exc}"


def _board_slugs(company: str, domain: str) -> list[str]:
    """Guess ATS board tokens from the company name and domain (best-effort)."""
    cands = []
    if domain:
        cands.append(domain.split(".")[0])
    if company:
        cands.append(re.sub(r"[^a-z0-9]+", "", company.lower()))
        cands.append(re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"))
    seen, out = set(), []
    for s in cands:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:3]


def _parse_greenhouse(data) -> list[dict]:
    out = []
    for j in (data or {}).get("jobs", []) or []:
        out.append({"title": j.get("title", ""), "url": j.get("absolute_url", ""),
                    "published_at": _to_iso_date(j.get("updated_at") or j.get("first_published")),
                    "text": f"{j.get('title','')} {_html_to_text(j.get('content','') or '')}"})
    return out


def _parse_lever(data) -> list[dict]:
    out = []
    for j in data or []:
        if not isinstance(j, dict):
            continue
        desc = j.get("descriptionPlain") or _html_to_text(j.get("description", "") or "")
        out.append({"title": j.get("text", ""), "url": j.get("hostedUrl", ""),
                    "published_at": _to_iso_date(j.get("createdAt")),
                    "text": f"{j.get('text','')} {desc}"})
    return out


def _parse_ashby(data) -> list[dict]:
    out = []
    for j in (data or {}).get("jobs", []) or []:
        desc = j.get("descriptionPlain") or _html_to_text(j.get("descriptionHtml") or j.get("description") or "")
        out.append({"title": j.get("title", ""), "url": j.get("jobUrl") or j.get("applyUrl") or "",
                    "published_at": _to_iso_date(j.get("publishedDate") or j.get("publishedAt")),
                    "text": f"{j.get('title','')} {desc}"})
    return out


ATS_BOARDS = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", _parse_greenhouse),
    ("lever", "https://api.lever.co/v0/postings/{slug}?mode=json", _parse_lever),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", _parse_ashby),
]


def hiring_boards(company: str, domain: str, *, fetcher=http_get_json,
                  discovered: list[dict] | None = None) -> dict:
    """Find the account's public ATS board and return its job postings.

    Two strategies, precise first:
      1. `discovered` — (provider, slug) pairs scraped from ATS links embedded in
         the company's own careers/homepage HTML. This is the *real* board slug,
         so it closes the gap where a slug can't be guessed from the name (e.g.
         "samsara" → board token "samsara-careers").
      2. Fall back to guessing slugs from the company name and domain.

    A company uses one ATS, so we stop at the first board that returns postings.
    A 404 on a wrong slug is expected and silent. Never raises.
    """
    templates = {provider: (template, parse) for provider, template, parse in ATS_BOARDS}

    for ref in (discovered or []):
        tp = templates.get(ref.get("provider"))
        slug = ref.get("slug")
        if not tp or not slug:
            continue
        template, parse = tp
        data, err = fetcher(template.format(slug=slug))
        if err:
            continue
        postings = parse(data)
        if postings:
            return {"status": "ok", "provider": ref["provider"], "board_slug": slug,
                    "postings": postings[:25], "discovery": "careers-link"}

    slugs = _board_slugs(company, domain)
    for provider, template, parse in ATS_BOARDS:
        for slug in slugs:
            data, err = fetcher(template.format(slug=slug))
            if err:
                continue
            postings = parse(data)
            if postings:
                return {"status": "ok", "provider": provider, "board_slug": slug,
                        "postings": postings[:25], "discovery": "slug-guess"}
    return {"status": "not_found", "provider": None, "board_slug": None, "postings": [],
            "note": "no public Greenhouse/Lever/Ashby board matched slugs: " + ", ".join(slugs)}


# --------------------------------------------------------------------------- #
# Signal decay + combinations
# --------------------------------------------------------------------------- #
# A signal's predictive power fades with age — a "hiring LangChain engineers"
# post from 200 days ago is not the live buying intent a 10-day-old one is.
# Stolen from gtm-starter-kit's signal-scoring model: full weight when fresh,
# zero past ~6 months. Undated evidence stays NEUTRAL (full weight) — absence of
# a date never reads as "old", consistent with the recency-capture note above.
DECAY_BANDS = [(30, 1.0), (60, 0.75), (90, 0.5), (180, 0.25)]  # (max_age_days, multiplier)
DEFAULT_SIGNAL_POINTS = 20.0   # per-signal base weight when the ICP declares none
COMBINATION_BONUS = 10.0       # awarded once when >=2 distinct fresh signals co-fire


def _decay_multiplier(age_days: int | None) -> float:
    """Age (in days) -> score multiplier per the decay bands; undated -> 1.0."""
    if age_days is None:
        return 1.0
    age_days = max(0, age_days)
    for max_age, mult in DECAY_BANDS:
        if age_days <= max_age:
            return mult
    return 0.0


def _parse_iso_date(value: object) -> date | None:
    """Parse a YYYY-MM-DD(...) string to a date; None if absent/unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _freshest_age_days(evidence: list[dict], today: date) -> int | None:
    """Smallest age across dated evidence items; None when all are undated."""
    ages = [(today - d).days for e in evidence
            if (d := _parse_iso_date(e.get("published_at"))) is not None]
    return min(ages) if ages else None


def summarize_signals(detected: list[dict], today: date, *,
                      points_by_key: dict[str, float] | None = None) -> dict:
    """Apply age-decay to each found signal and reward co-firing fresh signals.

    Mutates each *found* entry in ``detected`` with ``age_days``,
    ``decay_multiplier``, ``weighted_points`` and ``expired`` so classify/
    personalize can lead on live intent and discount stale news. Returns a
    summary: the fresh/expired splits, an optional combination bonus (>=2 fresh
    signals), and a weighted score (sum of fresh weights + any bonus).
    """
    points_by_key = points_by_key or {}
    fresh, expired = [], []
    weighted = 0.0
    for sig in detected:
        if not sig.get("found"):
            continue
        age = _freshest_age_days(sig.get("evidence", []), today)
        mult = _decay_multiplier(age)
        base = float(points_by_key.get(sig.get("key"), DEFAULT_SIGNAL_POINTS))
        weight = round(base * mult, 1)
        sig["age_days"] = age
        sig["decay_multiplier"] = mult
        sig["weighted_points"] = weight
        sig["expired"] = mult == 0.0
        entry = {"key": sig.get("key"), "informs": sig.get("informs"),
                 "age_days": age, "decay_multiplier": mult, "weighted_points": weight}
        if mult == 0.0:
            expired.append(entry)
        else:
            fresh.append(entry)
            weighted += weight

    combination = None
    if len(fresh) >= 2:
        combination = {
            "co_firing": [f["key"] for f in fresh],
            "distinct_dimensions": sorted({f["informs"] for f in fresh if f["informs"]}),
            "bonus": COMBINATION_BONUS,
        }
        weighted += COMBINATION_BONUS

    return {
        "reference_date": today.isoformat(),
        "fresh": fresh,
        "expired": expired,
        "combination": combination,
        "weighted_score": round(weighted, 1),
    }


# --------------------------------------------------------------------------- #
# Keyword scanning
# --------------------------------------------------------------------------- #
def scan_text(text: str, keywords: list[str]) -> list[tuple[str, str]]:
    """Return (keyword, snippet) for each keyword found (word-boundary, case-insensitive)."""
    hits, low = [], text.lower()
    for kw in keywords:
        m = re.search(r"\b" + re.escape(kw.lower()) + r"\b", low)
        if not m:
            continue
        start = max(0, m.start() - SNIPPET_RADIUS)
        end = min(len(text), m.end() + SNIPPET_RADIUS)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        hits.append((kw, ("…" + snippet + "…") if snippet else kw))
    return hits


def gather_signals(account: dict, signal_groups: list[dict], *,
                   fetcher=http_get_text, gh=github_repos, boards=hiring_boards,
                   today: date | None = None) -> dict:
    """Fetch public sources for the account and detect ICP signal keywords.

    `fetcher(url) -> (text, ats_refs, error)`, `gh(company, domain) -> {...}`, and
    `boards(company, domain, discovered=...) -> {...}` are injectable so this runs
    fully offline under test. `today` pins the decay reference date (tests pin it;
    production defaults to the current UTC date).
    """
    company = account.get("company_name") or account.get("company") or ""
    domain = (account.get("domain") or account.get("website") or "").replace(
        "https://", "").replace("http://", "").strip("/")
    warnings: list[str] = []

    # 1. Collect text per source URL.
    urls: list[str] = []
    if domain:
        urls += [f"https://{domain}{p}" for p in CANDIDATE_PATHS]
    # Any careers/source URLs the discover/enrich step already found.
    for key in ("careers_urls", "source_url", "linkedin_urls"):
        val = account.get(key)
        urls += val if isinstance(val, list) else ([val] if isinstance(val, str) else [])
    seen, source_texts = set(), []
    discovered_refs, seen_refs = [], set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result = fetcher(url)
        # Accept the date-aware 4-tuple and the older (text, refs, err) form so
        # injected/offline fetchers keep working — they just supply no date.
        if len(result) >= 4:
            text, refs, published_at, err = result[0], result[1], result[2], result[3]
        else:
            text, refs, err = result
            published_at = None
        if err:
            warnings.append(err)
        for ref in refs or []:
            rk = (ref.get("provider"), ref.get("slug"))
            if rk not in seen_refs:
                seen_refs.add(rk)
                discovered_refs.append(ref)
        if text:
            source_texts.append((url, text, published_at))

    # 2. GitHub repos -> a synthetic text blob (name + description + language).
    gh_result = gh(company, domain) if (company or domain) else {"status": "skipped", "repositories": []}
    if gh_result.get("warning"):
        warnings.append(gh_result["warning"])
    for repo in gh_result.get("repositories", []):
        blob = f"{repo.get('name','')} {repo.get('description','')} {repo.get('language','')}"
        source_texts.append((f"github:{repo.get('name') or 'repo'}", blob, _to_iso_date(repo.get("updated_at"))))

    # 2c. Public ATS job board -> one rich text source per posting. Prefer a
    #     board slug discovered from links on the company's own careers pages.
    boards_result = (boards(company, domain, discovered=discovered_refs)
                     if (company or domain or discovered_refs)
                     else {"status": "skipped", "postings": []})
    if boards_result.get("note"):
        warnings.append(boards_result["note"])
    provider = boards_result.get("provider")
    for post in boards_result.get("postings", []):
        title = (post.get("title") or "role")[:50]
        source_texts.append((f"hiring:{provider}:{title}", post.get("text", ""), post.get("published_at")))

    # 3. Scan every source against every signal group.
    detected = []
    for group in signal_groups:
        keywords = group.get("keywords", [])
        evidence, matched = [], set()
        for source, text, published_at in source_texts:
            for kw, snippet in scan_text(text, keywords):
                matched.add(kw)
                item = {"source": source, "keyword": kw, "snippet": snippet}
                if published_at:
                    item["published_at"] = published_at
                evidence.append(item)
        detected.append({
            "key": group.get("key"),
            "informs": group.get("informs"),
            "interpretation": group.get("interpretation", ""),
            "found": bool(matched),
            "matched_keywords": sorted(matched),
            "evidence": evidence[:8],
        })

    # 4. Age-decay each found signal and reward co-firing fresh signals, so
    #    downstream stages lead on live intent and discount stale news.
    points_by_key = {g.get("key"): g["points"] for g in signal_groups if g.get("points") is not None}
    summary = summarize_signals(detected, today or datetime.now(timezone.utc).date(),
                                points_by_key=points_by_key)

    return {
        "company_name": company,
        "domain": domain,
        "sources_checked": [u for u, _, _ in source_texts],
        "signals_detected": detected,
        "signal_summary": summary,
        "hiring_boards": {
            "status": boards_result.get("status"),
            "provider": provider,
            "board_slug": boards_result.get("board_slug"),
            "discovery": boards_result.get("discovery"),
            "postings": [{"title": p.get("title"), "url": p.get("url"), "published_at": p.get("published_at")}
                         for p in boards_result.get("postings", [])],
        },
        "github": gh_result,
        "warnings": warnings,
    }


def _load_signal_groups(criteria_path: Path) -> list[dict]:
    if not criteria_path.exists():
        return []
    return gtm_lib.read_json(criteria_path).get("signals", []) or []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect ICP signals for an account.")
    ap.add_argument("--slug", required=True, help="account slug under the artifact root")
    ap.add_argument("--criteria", type=Path, default=Path("icp.criteria.json"),
                    help="ICP file declaring the `signals` keyword groups")
    args = ap.parse_args(argv)

    acct_dir = gtm_lib.account_dir(args.slug)
    enrich_path = acct_dir / "enrich.json"
    src = enrich_path if enrich_path.exists() else (acct_dir / "input.json")
    account = gtm_lib.read_json(src)

    groups = _load_signal_groups(args.criteria)
    if not groups:
        out = {"company_name": account.get("company_name"), "signals_detected": [],
               "warnings": ["no `signals` groups declared in ICP; nothing to scan"]}
    else:
        out = gather_signals(account, groups)

    path = gtm_lib.write_json(gtm_lib.stage_path(args.slug, "signals"), out)
    found = [s["key"] for s in out.get("signals_detected", []) if s.get("found")]
    board = out.get("hiring_boards", {})
    print(json.dumps({"slug": args.slug, "signals_found": found,
                      "sources": len(out.get("sources_checked", [])),
                      "hiring_board": board.get("provider"),
                      "postings": len(board.get("postings", [])),
                      "warnings": len(out.get("warnings", [])), "artifact": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
