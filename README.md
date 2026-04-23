# GEO Readiness Auditor

A small Python toolkit that **crawls B2B marketing sites** and scores **GEO (Generative Engine Optimization) readiness** for outbound and campaigns. It estimates how hard a site is for AI crawlers and answer engines to use: blog discoverability, whether the homepage is a thin JS shell, JSON-LD structured data, and comparison / alternatives / “vs” style decision pages.

## What it does

- **CLI batch audits** — Read a CSV of companies, fetch each site in parallel, write scored results to CSV (with per-dimension notes and suggested outreach hooks).
- **Local web UI** — Flask serves `web/index.html` and exposes JSON APIs to run single or batch audits; results persist to `audit_results.csv`.
- **Resilient fetching** — Retries with backoff, per-run URL cache, browser-like `User-Agent` fallback on 401/403/429 or Cloudflare-style challenge pages.
- **Optional headless Chrome** — Playwright can render the homepage when the site is WAF-blocked or heavily client-rendered, so schema and link signals better match what a real browser (and some bots) see.
- **Smarter signals** — JSON-LD parsed including `@graph` envelopes; comparison content detected via homepage links, common paths, and sitemap URLs (with guards against editorial blog slugs that only look like “vs” pages).

## Requirements

- Python 3.9+ recommended  
- Network access for HTTP fetches  
- For headless rendering: Chromium installed for Playwright (one-time)

## Install

```bash
cd geo-readiness-auditor
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

If you only want the CLI without Playwright rendering, you can skip the `playwright install` step and use `--no-render` (see below).

## CLI usage

```bash
# Audit all rows in companies.csv → audit_results.csv (default output)
python3 auditor.py --input companies.csv

# Custom output path
python3 auditor.py -i companies.csv -o poster_targets.csv

# Only companies whose location looks like San Francisco
python3 auditor.py -i companies.csv --sf-only -o sf_only.csv

# Faster run: no Playwright (more false negatives on WAF / SPA homepages)
python3 auditor.py -i companies.csv --no-render

# Same as --no-render
GEO_AUDITOR_NO_RENDER=1 python3 auditor.py -i companies.csv
```

**Parallelism:** `--workers N` (default `6`). Lower this if you enable Playwright and see CPU or browser contention.

**Input deduplication:** Rows with the same normalized `domain` are deduped; the first occurrence wins.

### Input CSV columns

| Column   | Required | Description |
|----------|----------|-------------|
| `company`| Yes      | Display name |
| `domain` | Yes      | Hostname only or URL (e.g. `example.com`, `https://www.example.com`) |
| `location` | No   | Free text; used only with `--sf-only` |

### Output CSV

Each row includes: `company`, `domain`, `location`, `is_sf`, `total_score`, `blog_score`, `spa_score`, `schema_score`, `decision_score`, `top_gap`, `outreach_hook`, `notes`.

The `notes` field concatenates human-readable evidence for each dimension (and may note redirects or headless rendering).

## Scoring (0–100 total)

Higher **total** = more urgent GEO gaps (for the AthenaHQ-style narrative in hooks).

| Dimension | Max points | Meaning (simplified) |
|-----------|------------|------------------------|
| **blog**  | 30 | No reachable, substantive `/blog`-style content (also tries `/resources`, `/insights`, `/journal`, etc.). |
| **spa**   | 25 | Homepage looks like a thin shell or very little server-rendered text vs scripts. |
| **schema**| 25 | Missing or unusable JSON-LD on the homepage (`@graph` and nested nodes are considered). |
| **decision** | 20 | No credible comparison / alternatives / migration / “vs” decision URLs found (binary 0 or 20). |

**`top_gap`** is the dimension with the highest non-zero penalty (or `none` if the total is 0).

When the homepage never becomes “usable” after fetch + optional render, **inconclusive** penalties on blog/spa/schema are **capped** so one bad fetch does not stack three separate 10-point hits.

## Web app

```bash
python3 server.py
```

Open **http://localhost:8080** (bound to `127.0.0.1:8080`).

**Endpoints**

- `GET /` — Web UI  
- `GET /api/audits` — List saved audits from `audit_results.csv` (merged with optional enrichment from `poster_targets_final.csv` if present)  
- `POST /api/audit` — JSON body: `{ "company", "domain", "location" }`  
- `POST /api/audit-batch` — JSON body: `{ "companies": [ { ... }, ... ] }`  

> Port **8080** is used because some browsers block “unsafe” ports; 6000 is avoided for that reason.

## Project layout

| Path | Role |
|------|------|
| `auditor.py` | Core crawl, scoring, sitemap/robots helpers, CLI |
| `renderer.py` | Lazy Playwright Chromium wrapper (thread-safe lock for concurrent audits) |
| `server.py` | Flask API + static UI |
| `web/index.html` | Frontend for the auditor |
| `requirements.txt` | Python dependencies |
| `companies.csv` | Example / seed input list |

## Design notes

- Audits are **heuristic**, not a substitute for a full SEO or LLM-visibility product. Sites that block automation, geo-route, or A/B test HTML may still skew scores.
- Playwright adds latency and uses a shared browser process; use `--no-render` for quick sweeps when you accept more noise.
- Hooks in `outreach_hook` are templates; always align them with the actual `notes` before sending mail.

## License

Specify your license in this repository if you open-source or distribute the project.
