#!/usr/bin/env python3
"""Flask server — wraps auditor.py with an HTTP API and serves the React UI.

Run:
    python3 server.py
    open http://localhost:8080

Endpoints:
    GET  /                 → serves web/index.html
    GET  /api/audits       → list all previously audited companies
    POST /api/audit        → run audit on one company (body: company, domain, location)
    POST /api/audit-batch  → run audits on a list
"""

import csv
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from auditor import audit_company

ROOT = Path(__file__).parent
WEB = ROOT / "web"
RESULTS_CSV = ROOT / "audit_results.csv"
FINAL_CSV = ROOT / "poster_targets_final.csv"

app = Flask(__name__, static_folder=None)


def load_audits() -> list[dict]:
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open() as f:
        return list(csv.DictReader(f))


def load_poster_enrichment() -> dict[str, dict]:
    """Neighborhood/cluster/note lookup keyed by company name."""
    if not FINAL_CSV.exists():
        return {}
    with FINAL_CSV.open() as f:
        return {
            r["company"]: {
                "hq_neighborhood": r.get("hq_neighborhood", ""),
                "walking_cluster": r.get("walking_cluster", ""),
                "personalized_poster_note": r.get("personalized_poster_note", ""),
            }
            for r in csv.DictReader(f)
        }


def merge_enrichment(rows: list[dict]) -> list[dict]:
    enrich = load_poster_enrichment()
    for r in rows:
        e = enrich.get(r.get("company", ""), {})
        r["hq_neighborhood"] = e.get("hq_neighborhood", r.get("hq_neighborhood", ""))
        r["walking_cluster"] = e.get("walking_cluster", r.get("walking_cluster", ""))
        r["personalized_poster_note"] = e.get(
            "personalized_poster_note", r.get("personalized_poster_note", "")
        )
    return rows


def persist_audit(result_dict: dict) -> None:
    rows = load_audits()
    rows = [r for r in rows if r.get("domain") != result_dict["domain"]]
    rows.append({k: str(v) for k, v in result_dict.items()})
    fields = list(result_dict.keys())
    with RESULTS_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/api/audits")
def api_list():
    return jsonify(merge_enrichment(load_audits()))


@app.post("/api/audit")
def api_audit():
    body = request.get_json(silent=True) or {}
    domain = (body.get("domain") or "").strip()
    company = (body.get("company") or "").strip() or domain
    location = (body.get("location") or "").strip()
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    try:
        result = audit_company(
            {"company": company, "domain": domain, "location": location}
        )
    except Exception as e:
        return jsonify({"error": f"audit failed: {e}"}), 500

    data = asdict(result)
    persist_audit(data)
    # attach enrichment if we have it
    return jsonify(merge_enrichment([data])[0])


@app.post("/api/audit-batch")
def api_audit_batch():
    body = request.get_json(silent=True) or {}
    rows = body.get("companies") or []
    results = []
    for row in rows:
        domain = (row.get("domain") or "").strip()
        if not domain:
            continue
        try:
            r = audit_company(
                {
                    "company": (row.get("company") or domain),
                    "domain": domain,
                    "location": row.get("location") or "",
                }
            )
            data = asdict(r)
            persist_audit(data)
            results.append(data)
        except Exception as e:
            results.append({"domain": domain, "error": str(e)})
    return jsonify(merge_enrichment(results))


if __name__ == "__main__":
    # 6000 is blocked by Chrome/Chromium (ERR_UNSAFE_PORT); 8080 is a safe local dev port.
    print("GEO Readiness Auditor — UI at http://localhost:8080")
    app.run(host="127.0.0.1", port=8080, debug=False)
