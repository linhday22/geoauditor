#!/usr/bin/env python3
"""GEO Readiness Auditor — scrapes company sites and scores GEO urgency.

Built for AthenaHQ outbound + the SF poster campaign.

Usage:
    python auditor.py --input companies.csv
    python auditor.py --input companies.csv --sf-only --output poster_targets.csv
    python auditor.py --input companies.csv --no-render   # skip Playwright (faster)
    GEO_AUDITOR_NO_RENDER=1 python auditor.py -i companies.csv  # same as --no-render

Optional headless rendering (recommended for WAF / JS-heavy homepages):
    pip install -r requirements.txt
    playwright install chromium

Input CSV columns:  company, domain, location   (location optional; used by --sf-only)

Scoring (0-100, higher = more urgent GEO need):
    blog       0-30   no crawlable /blog → invisible to 44.5% of AI entry points
    spa        0-25   JS-rendered homepage → AI crawlers see almost nothing
    schema     0-25   no JSON-LD → AI models can't parse what the page *is*
    decision   0-20   no /compare or /alternatives → loses the pages LLMs cite most
"""

from __future__ import annotations

import argparse
import csv
import os
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

TIMEOUT = 8
MAX_RETRIES = 3
BACKOFFS = (0.5, 2.0, 4.0)

USER_AGENT = (
    "Mozilla/5.0 (compatible; AthenaHQ-GEOReadinessAuditor/1.0; "
    "+https://athenahq.ai)"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

SF_TOKENS = ("san francisco", " sf,", " sf ", ", sf")

# Module-level options set from main() before ThreadPool runs
_AUDIT_OPTS: dict[str, Any] = {"no_render": False}

BLOG_PATHS = [
    "/blog",
    "/blog/",
    "/resources",
    "/insights",
    "/learn",
    "/knowledge",
    "/journal",
    "/journal/",
    "/stories",
    "/library",
    "/news",
    "/newsroom",
    "/guides",
]
BLOG_LINK_TEXTS = frozenset(
    {
        "blog",
        "resources",
        "insights",
        "learn",
        "journal",
        "stories",
        "library",
        "news",
        "newsroom",
        "guides",
        "customers",
        "podcast",
    }
)
BLOG_HREF_RES = [
    re.compile(r"/blog(/|$)", re.I),
    re.compile(r"/resources(/|$)", re.I),
    re.compile(r"/insights(/|$)", re.I),
    re.compile(r"/learn(/|$)", re.I),
    re.compile(r"/journal(/|$)", re.I),
    re.compile(r"/stories(/|$)", re.I),
    re.compile(r"/library(/|$)", re.I),
    re.compile(r"/news(/|$)", re.I),
    re.compile(r"/newsroom(/|$)", re.I),
    re.compile(r"/guides(/|$)", re.I),
]

COMPARISON_PATHS_BASE = [
    "/compare",
    "/compare/",
    "/comparisons",
    "/comparisons/",
    "/alternatives",
    "/alternatives/",
    "/vs",
    "/vs/",
]

COMPARISON_HREF_PATTERNS = [
    re.compile(r"/compare(/|$)", re.I),
    re.compile(r"/comparisons?(/|$)", re.I),
    re.compile(r"/alternatives?(/|$)", re.I),
    re.compile(r"/vs[-/]", re.I),
    re.compile(r"-vs-", re.I),
    re.compile(r"/competitors?(/|$)", re.I),
    re.compile(r"/versus(/|$)", re.I),
    re.compile(r"/alternative-to", re.I),
    re.compile(r"migrate-from", re.I),
    re.compile(r"/migration(/|$)", re.I),
]

DECISION_URL_PATH_RES = [
    re.compile(r"/compare(/|$)", re.I),
    re.compile(r"/comparisons?(/|$)", re.I),
    re.compile(r"/alternatives?(/|$)", re.I),
    re.compile(r"/vs[-/]", re.I),
    re.compile(r"-vs-", re.I),
    re.compile(r"/competitors?(/|$)", re.I),
    re.compile(r"/versus(/|$)", re.I),
    re.compile(r"/alternative-to", re.I),
    re.compile(r"migrate-from", re.I),
]

SPA_FRAMEWORK_HINTS = [
    "__NEXT_DATA__",
    "data-reactroot",
    "ng-app",
    "__NUXT__",
    "_nuxt/",
    "gatsby-focus-wrapper",
    "window.__INITIAL_STATE__",
    "data-vue",
    "v-app",
]


@dataclass
class AuditResult:
    company: str
    domain: str
    location: str
    is_sf: bool
    total_score: int
    blog_score: int
    spa_score: int
    schema_score: int
    decision_score: int
    top_gap: str
    outreach_hook: str
    notes: str = ""


def make_session(*, browser: bool = False) -> requests.Session:
    s = requests.Session()
    ua = BROWSER_USER_AGENT if browser else USER_AGENT
    s.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def looks_like_challenge(html: Optional[str], status: int) -> bool:
    if not html:
        return status in (401, 403, 429, 503)
    h = html.lower()
    return (
        "just a moment" in h
        or "cf-chl" in h
        or "__cf_chl" in h
        or "checking your browser" in h
        or ("cloudflare" in h and "ray id" in h)
    )


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_sitemap_xml(xml_text: str) -> tuple[str, list[str]]:
    """Return (root_kind, list of loc URLs). root_kind is sitemapindex, urlset, or unknown."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "unknown", []
    kind = _local_tag(root.tag).lower()
    locs: list[str] = []
    for elem in root.iter():
        if _local_tag(elem.tag).lower() == "loc" and elem.text:
            locs.append(elem.text.strip())
    return kind, locs


def collect_sitemap_page_urls(fetcher: "UrlFetcher", domain: str, max_locs: int = 2500) -> list[str]:
    """Discover page URLs from robots.txt + sitemap.xml (one level of index)."""
    base = f"https://{domain}"
    seen_sitemaps: set[str] = set()
    page_urls: list[str] = []
    seen_pages: set[str] = set()

    def add_page_url(u: str) -> None:
        if u in seen_pages:
            return
        seen_pages.add(u)
        if url_matches_decision(u):
            page_urls.insert(0, u)
        else:
            page_urls.append(u)
        while len(page_urls) > max_locs:
            page_urls.pop()

    r = fetcher.get(f"{base}/robots.txt")
    if r is not None and r.status_code < 400 and r.text:
        for line in r.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                u = line.split(":", 1)[1].strip()
                if u.startswith("http"):
                    seen_sitemaps.add(u)

    for default in (f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"):
        seen_sitemaps.add(default)

    for sm_url in sorted(seen_sitemaps)[:25]:
        resp = fetcher.get(sm_url)
        if resp is None or resp.status_code >= 400 or not resp.text:
            continue
        kind, locs = parse_sitemap_xml(resp.text)
        if kind == "sitemapindex":
            for child in locs[:50]:
                cr = fetcher.get(child)
                if cr is None or cr.status_code >= 400 or not cr.text:
                    continue
                _, child_locs = parse_sitemap_xml(cr.text)
                for u in child_locs:
                    add_page_url(u)
                if len(page_urls) >= max_locs:
                    break
        else:
            for u in locs:
                add_page_url(u)
        if len(page_urls) >= max_locs:
            break
    return page_urls


def decision_path_probes(domain: str) -> list[str]:
    brand = domain.split(".")[0].lower()
    extra = [
        f"/why-{brand}",
        f"/{brand}-vs",
        f"/vs-{brand}",
    ]
    out: list[str] = []
    for p in COMPARISON_PATHS_BASE + extra:
        if p not in out:
            out.append(p)
    return out


def href_matches_decision(href: str) -> bool:
    h = href.lower()
    return any(p.search(h) for p in COMPARISON_HREF_PATTERNS)


def url_matches_decision(u: str) -> bool:
    """True if URL plausibly denotes comparison / migration / alternatives content."""
    try:
        path = urlparse(u).path.lower()
    except Exception:
        return False
    full = u.lower()
    matched = any(p.search(path) or p.search(full) for p in DECISION_URL_PATH_RES)
    if not matched:
        return False
    # Blog posts often use "-vs-" editorially ("the-vs-code-method"); require stronger signals
    # or a *-vs-* slug where both sides are substantive (length) to count as decision content.
    if "/blog" in path:
        if re.search(r"migrate-from|/compare|alternativ|competitor|/vs/", path, re.I):
            return True
        if "-vs-" in path:
            slug = path.rstrip("/").split("/")[-1]
            parts = slug.split("-vs-", 1)
            if len(parts) == 2:
                left, right = parts[0], parts[1]
                if len(left) >= 4 and len(right) >= 4:
                    return True
        return False
    return True


def same_site(url: str, domain: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return True
        return host == domain.lower()
    except Exception:
        return False


def normalize_href_to_url(href: str, domain: str, base_url: str) -> Optional[str]:
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"https://{domain}{href}"
    return urljoin(base_url, href)


class UrlFetcher:
    """Per-audit GET cache + retry + browser UA fallback."""

    def __init__(self) -> None:
        self._session = make_session(browser=False)
        self._browser_session: Optional[requests.Session] = None
        self._cache: dict[str, Optional[requests.Response]] = {}

    def _browser(self) -> requests.Session:
        if self._browser_session is None:
            self._browser_session = make_session(browser=True)
        return self._browser_session

    def _needs_fallback(self, resp: Optional[requests.Response]) -> bool:
        if resp is None:
            return False
        if resp.status_code in (401, 403, 429):
            return True
        return looks_like_challenge(resp.text, resp.status_code)

    def _get_once(self, session: requests.Session, url: str) -> Optional[requests.Response]:
        try:
            return session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            return None

    def _get_with_retries(self, session: requests.Session, url: str) -> Optional[requests.Response]:
        last: Optional[requests.Response] = None
        for attempt in range(MAX_RETRIES):
            last = self._get_once(session, url)
            if last is not None and last.status_code < 500:
                return last
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
        return last

    def get(self, url: str) -> Optional[requests.Response]:
        if url in self._cache:
            return self._cache[url]
        r = self._get_with_retries(self._session, url)
        if self._needs_fallback(r):
            r = self._get_with_retries(self._browser(), url)
        self._cache[url] = r
        return r


def visible_text_len(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.extract()
    return len(soup.get_text(separator=" ", strip=True))


def framework_hints(html: str) -> list[str]:
    low = html.lower()
    return [h for h in SPA_FRAMEWORK_HINTS if h.lower() in low]


def collect_schema_types(obj: Any, out: set[str]) -> None:
    """Walk JSON-LD (including @graph / nested nodes) and collect all @type values."""
    if isinstance(obj, dict):
        t = obj.get("@type")
        if t is not None:
            if isinstance(t, list):
                for x in t:
                    out.add(str(x))
            else:
                out.add(str(t))
        for k, v in obj.items():
            if k == "@context":
                continue
            collect_schema_types(v, out)
    elif isinstance(obj, list):
        for it in obj:
            collect_schema_types(it, out)


def score_blog(fetcher: UrlFetcher, domain: str, homepage_html: str) -> tuple[int, str]:
    """0-30. Penalize absent or JS-shell blogs."""
    candidates: list[str] = []
    seen: set[str] = set()
    base_url = f"https://{domain}"

    if homepage_html:
        soup = BeautifulSoup(homepage_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(strip=True).lower()
            href_l = href.lower()
            if text not in BLOG_LINK_TEXTS and not any(r.search(href_l) for r in BLOG_HREF_RES):
                continue
            url = normalize_href_to_url(href, domain, base_url)
            if url and same_site(url, domain) and url not in seen:
                seen.add(url)
                candidates.append(url)

    for p in BLOG_PATHS:
        url = f"https://{domain}{p}"
        if url not in seen:
            seen.add(url)
            candidates.append(url)

    blocked = False
    thin: tuple[str, int, int] | None = None
    for url in candidates:
        r = fetcher.get(url)
        if r is None:
            continue
        if r.status_code in (401, 403):
            blocked = True
            continue
        if r.status_code >= 400:
            continue
        text_len = visible_text_len(r.text)
        if text_len >= 600:
            return 0, f"blog reachable at {r.url} ({text_len} chars visible)"
        if thin is None:
            thin = (r.url, text_len, r.status_code)

    if thin:
        url, text_len, status = thin
        return (
            20,
            f"{url} returned {status} but thin content ({text_len} chars) — likely JS-rendered",
        )
    if blocked:
        return (
            10,
            "blog candidates returned 401/403 to our UA — inconclusive",
        )
    return 30, "no reachable /blog, /resources, or /insights"


def score_spa(
    domain: str, homepage_html: str, homepage_status: int
) -> tuple[int, str]:
    """0-25. Penalize JS-rendered homepages."""
    if homepage_status in (401, 403, 429):
        return 10, f"homepage returned {homepage_status} to our UA — inconclusive"
    if not homepage_html:
        return 20, "homepage unreachable"
    text_len = visible_text_len(homepage_html)
    soup = BeautifulSoup(homepage_html, "html.parser")
    script_tags = len(soup.find_all("script"))
    frameworks = framework_hints(homepage_html)

    if text_len < 400 and script_tags >= 3:
        return 25, f"SPA shell: {text_len} chars visible, {script_tags} scripts"
    if frameworks and text_len < 1500:
        return (
            20,
            f"JS framework ({frameworks[0]}) with thin server-rendered content ({text_len} chars)",
        )
    if text_len < 800:
        return 15, f"thin homepage content ({text_len} chars)"
    if frameworks:
        return 5, f"JS framework ({frameworks[0]}) but content is pre-rendered"
    return 0, f"server-rendered ({text_len} chars visible)"


def score_schema(homepage_html: str, homepage_status: int) -> tuple[int, str]:
    """0-25. Penalize missing or empty JSON-LD."""
    if homepage_status in (401, 403, 429):
        return 10, f"homepage returned {homepage_status} to our UA — inconclusive"
    if not homepage_html:
        return 20, "homepage unreachable"
    soup = BeautifulSoup(homepage_html, "html.parser")
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    if not scripts:
        return 25, "no JSON-LD schema on homepage"
    types_found: set[str] = set()
    for s in scripts:
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        collect_schema_types(data, types_found)
    if not types_found:
        return 20, f"{len(scripts)} JSON-LD block(s) but malformed/empty"
    return 0, f"JSON-LD types: {', '.join(sorted(types_found))}"


def score_decision_content(
    fetcher: UrlFetcher, domain: str, homepage_html: str
) -> tuple[int, str]:
    """0 or 20 only. Evidence of comparison / vs / alternatives pages → 0."""
    base_url = f"https://{domain}"
    verified: list[str] = []

    def verify_url(u: str) -> bool:
        if u in verified:
            return True
        r = fetcher.get(u)
        if r is None or r.status_code >= 400:
            return False
        if visible_text_len(r.text) >= 400:
            verified.append(u)
            return True
        return False

    # 1) Homepage links
    if homepage_html:
        soup = BeautifulSoup(homepage_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href_matches_decision(href):
                continue
            full = normalize_href_to_url(href, domain, base_url)
            if full and same_site(full, domain) and verify_url(full):
                return 0, f"decision content linked from homepage ({full[:80]})"

    # 2) Canonical path probes
    for path in decision_path_probes(domain):
        u = f"https://{domain}{path}"
        if verify_url(u):
            return 0, f"decision-support page at {path}"

    # 3) Sitemap URLs matching decision patterns (sample verify)
    try:
        raw_c = [u for u in collect_sitemap_page_urls(fetcher, domain) if url_matches_decision(u)]
    except Exception:
        raw_c = []

    def decision_rank(u: str) -> tuple[int, str]:
        ul = u.lower()
        if "-vs-" in ul or "/vs/" in ul:
            return (0, ul)
        if "compare" in ul:
            return (1, ul)
        if "alternativ" in ul:
            return (2, ul)
        if "competitor" in ul:
            return (3, ul)
        if "migrate" in ul or "versus" in ul:
            return (4, ul)
        return (5, ul)

    candidates = sorted(raw_c, key=decision_rank)
    for u in candidates[:25]:
        if verify_url(u):
            return 0, f"decision URL from sitemap ({u[:80]})"

    return 20, "no comparison/alternatives content found"


def cap_blocked_home_penalties(
    blog_s: int,
    spa_s: int,
    schema_s: int,
    blog_n: str,
    spa_n: str,
    schema_n: str,
    apply_cap: bool,
) -> tuple[int, int, int]:
    """When homepage never became usable, cap soft 401/403 inconclusive at 10 total."""
    if not apply_cap:
        return blog_s, spa_s, schema_s
    notes = (blog_n, spa_n, schema_n)
    scores = [blog_s, spa_s, schema_s]

    def is_soft(s: int, n: str) -> bool:
        nlow = n.lower()
        return s == 10 and (
            "inconclusive" in nlow
            or "401/403" in nlow
            or "returned 401" in nlow
            or "returned 403" in nlow
            or "returned 429" in nlow
        )

    soft = [is_soft(scores[i], notes[i]) for i in range(3)]
    if sum(1 for x in soft if x) < 2:
        return blog_s, spa_s, schema_s
    soft_idx = [i for i in range(3) if soft[i]]
    raw = [scores[i] for i in soft_idx]
    total = sum(raw)
    if total <= 10:
        return blog_s, spa_s, schema_s
    # Integer partition of 10 proportional to raw weights
    allocated: list[int] = []
    cum = 0
    for x in raw[:-1]:
        v = int(round(x * 10 / total))
        allocated.append(v)
        cum += v
    allocated.append(10 - cum)
    for idx, val in zip(soft_idx, allocated):
        scores[idx] = val
    return scores[0], scores[1], scores[2]


def pick_top_gap(r: AuditResult) -> str:
    gaps = [
        ("blog", r.blog_score),
        ("spa", r.spa_score),
        ("schema", r.schema_score),
        ("decision", r.decision_score),
    ]
    gaps.sort(key=lambda g: -g[1])
    return gaps[0][0] if gaps[0][1] > 0 else "none"


def generate_hook(r: AuditResult) -> str:
    name = r.company
    gap = r.top_gap
    if gap == "blog":
        return (
            f"Hey — ran {name}'s site through our GEO audit and couldn't find a "
            f"crawlable /blog. Per AthenaHQ's 2026 report that's 44.5% of AI-search "
            f"entry points gone. Worth 15 min to see what ChatGPT says about you today?"
        )
    if gap == "spa":
        return (
            f"Hey — {name}'s homepage is JS-rendered, which means GPTBot and "
            f"PerplexityBot load a near-empty page. Your site is effectively "
            f"invisible to AI search. Quick look at the fix?"
        )
    if gap == "schema":
        return (
            f"Hey — {name} has no JSON-LD schema on the homepage. That's what tells "
            f"ChatGPT *what* your page is. Adding it is a same-week win. Open to a chat?"
        )
    if gap == "decision":
        return (
            f"Hey — {name} has no /vs or /alternatives pages. Those are the single most-cited "
            f"page type in LLM answers to B2B buyer queries. Want to see which competitors "
            f"are eating that traffic?"
        )
    return (
        f"Hey — {name}'s fundamentals look clean in our GEO audit. The next frontier is "
        f"tracking how often ChatGPT/Perplexity *actually* surface you in buyer queries. "
        f"That's what AthenaHQ measures — interested?"
    )


def is_sf_location(loc: str) -> bool:
    l = f" {loc.lower().strip()} "
    return any(t in l for t in SF_TOKENS)


def normalize_domain(raw: str) -> str:
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.rstrip("/")


def usable_homepage(html: Optional[str], status: int) -> bool:
    if not html or len(html) < 200:
        return False
    if looks_like_challenge(html, status):
        return False
    return True


def audit_company(row: dict) -> AuditResult:
    company = (row.get("company") or row.get("name") or "").strip()
    domain = normalize_domain(row.get("domain", ""))
    location = (row.get("location") or "").strip()

    fetcher = UrlFetcher()
    home_url = f"https://{domain}"
    home = fetcher.get(home_url)
    homepage_status = home.status_code if home is not None else 0
    raw_html = home.text if (home and home.status_code < 400) else (home.text if home else "")

    canonical_domain = domain
    redirect_note = ""
    final_home_url = home_url
    if home is not None and home.url:
        final_home_url = home.url
        final_host = re.sub(r"^www\.", "", (urlparse(home.url).netloc or "").lower())
        if final_host and final_host != domain:
            canonical_domain = final_host
            redirect_note = f" (redirected {domain} → {canonical_domain})"

    needs_render = False
    if not _AUDIT_OPTS.get("no_render", False):
        if homepage_status in (401, 403, 429):
            needs_render = True
        elif raw_html and looks_like_challenge(raw_html, homepage_status):
            needs_render = True
        elif (
            raw_html
            and homepage_status < 400
            and visible_text_len(raw_html) < 400
            and framework_hints(raw_html)
        ):
            needs_render = True

    headless_note = ""
    if needs_render:
        try:
            from renderer import render_url

            render_target = final_home_url
            st, html = render_url(render_target)
            if html and len(html) >= 500:
                raw_html = html
                # Many sites return 401/403 to scripts but still ship HTML; score on body.
                if usable_homepage(html, st if st else 200):
                    homepage_status = 200
                else:
                    homepage_status = st if st and st > 0 else 200
                headless_note = " || headless: homepage rendered in Chromium"
        except Exception:
            pass

    homepage_html = raw_html
    if homepage_html and looks_like_challenge(homepage_html, homepage_status):
        # Still a challenge page after render — scoring will treat as inconclusive / thin
        pass

    blog_s, blog_n = score_blog(fetcher, canonical_domain, homepage_html)
    spa_s, spa_n = score_spa(canonical_domain, homepage_html, homepage_status)
    schema_s, schema_n = score_schema(homepage_html, homepage_status)
    dec_s, dec_n = score_decision_content(fetcher, canonical_domain, homepage_html)

    apply_cap = not usable_homepage(homepage_html, homepage_status)
    blog_s, spa_s, schema_s = cap_blocked_home_penalties(
        blog_s, spa_s, schema_s, blog_n, spa_n, schema_n, apply_cap
    )

    total = blog_s + spa_s + schema_s + dec_s

    result = AuditResult(
        company=company,
        domain=domain,
        location=location,
        is_sf=is_sf_location(location),
        total_score=total,
        blog_score=blog_s,
        spa_score=spa_s,
        schema_score=schema_s,
        decision_score=dec_s,
        top_gap="",
        outreach_hook="",
        notes=(
            f"blog: {blog_n} || spa: {spa_n} || schema: {schema_n} || decision: {dec_n}"
            f"{redirect_note}{headless_note}"
        ),
    )
    result.top_gap = pick_top_gap(result)
    result.outreach_hook = generate_hook(result)
    return result


def main():
    ap = argparse.ArgumentParser(description="GEO Readiness Auditor (AthenaHQ)")
    ap.add_argument("--input", "-i", required=True, help="CSV: company, domain, location")
    ap.add_argument("--output", "-o", default="audit_results.csv")
    ap.add_argument("--sf-only", action="store_true", help="Filter output to SF companies")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--no-render",
        action="store_true",
        help="Skip Playwright headless rendering (faster; more false positives on WAF/SPA sites)",
    )
    args = ap.parse_args()

    _AUDIT_OPTS["no_render"] = bool(args.no_render) or (
        os.environ.get("GEO_AUDITOR_NO_RENDER", "").lower() in ("1", "true", "yes")
    )

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"error: input not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    with in_path.open(newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("domain") or "").strip()]

    # Dedupe by domain (first row wins)
    seen_dom: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        d = normalize_domain(r.get("domain", ""))
        if d in seen_dom:
            continue
        seen_dom.add(d)
        deduped.append(r)
    rows = deduped

    print(f"Auditing {len(rows)} companies with {args.workers} workers...\n")
    t0 = time.time()

    results: list[AuditResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(audit_company, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                flag = "SF" if r.is_sf else "  "
                print(f"  [{flag}] {r.company:<28s} score={r.total_score:>3d}  gap={r.top_gap}")
            except Exception as e:
                print(f"  ERR  {row.get('company', '?'):<28s} {e}", file=sys.stderr)

    results.sort(key=lambda r: -r.total_score)

    if args.sf_only:
        results = [r for r in results if r.is_sf]
        print(f"\nFiltered to {len(results)} SF-based companies.")

    out_path = Path(args.output)
    if results:
        with out_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))

    elapsed = time.time() - t0
    print(f"\nWrote {len(results)} rows → {out_path} in {elapsed:.1f}s\n")
    print("=" * 92)
    print(f"{'TOP PROSPECTS BY GEO URGENCY':^92}")
    print("=" * 92)
    for i, r in enumerate(results[:10], 1):
        flag = "SF" if r.is_sf else "  "
        print(
            f"\n{i:>2}. [{flag}] {r.company:<24s} score={r.total_score:>3d}  "
            f"(blog={r.blog_score}, spa={r.spa_score}, schema={r.schema_score}, dec={r.decision_score})"
        )
        print(f"       {r.outreach_hook}")
    print()


if __name__ == "__main__":
    main()
