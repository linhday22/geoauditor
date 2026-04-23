#!/usr/bin/env python3
"""Enrich SF poster targets with HQ neighborhood, walking cluster, and sticky-note copy.

Reads poster_targets.csv (produced by `auditor.py --sf-only`) and writes
poster_targets_final.csv with three extra columns for the physical campaign.

Neighborhood + cluster data is hand-curated — these are best-guess HQ locations
based on publicly known addresses; verify before pinning posters.
"""

import csv
from pathlib import Path

# Best-guess HQ neighborhoods. Addresses in comments are the basis for the guess.
NEIGHBORHOODS: dict[str, str] = {
    "Brex":       "FiDi",          # 650 California St
    "Deel":       "FiDi",          # 650 California St
    "Notion":     "FiDi",          # 548 Market St
    "Mercury":    "FiDi",          # 548 Market St
    "Superhuman": "FiDi",          # 1 Bush St
    "Metabase":   "FiDi",          # 660 4th St area
    "Figma":      "Union Square",  # 760 Market St
    "Retool":     "Union Square",  # 77 Geary St
    "Rippling":   "Union Square",  # 55 Stockton St
    "Airtable":   "SoMa",          # 155 5th St
    "Hex":        "SoMa",          # SoMa (YC-era)
    "Census":     "SoMa",
    "Arcade":     "SoMa",
    "Pylon":      "SoMa",
    "Default":    "SoMa",
    "Replo":      "SoMa",
    "Hightouch":  "SoMa",
    "Linear":     "SoMa",
    "Vercel":     "SoMa",
    "Webflow":    "SoMa",          # 398 11th St
    "Gusto":      "Mission Bay",   # 525 20th St (Dogpatch border)
}

# 4 walking clusters. FiDi is ~8 min end-to-end; Union Square is ~5 min;
# SoMa stretches ~20 min between Moscone and 11th St; Mission Bay is its own stop.
CLUSTERS: dict[str, list[str]] = {
    "Cluster A: FiDi Montgomery corridor": [
        "Brex", "Deel", "Notion", "Mercury", "Superhuman", "Metabase",
    ],
    "Cluster B: Union Square / Mid-Market spine": [
        "Figma", "Retool", "Rippling",
    ],
    "Cluster C: SoMa Moscone–Folsom corridor": [
        "Airtable", "Hex", "Census", "Arcade", "Pylon", "Default",
        "Replo", "Hightouch", "Linear", "Vercel", "Webflow",
    ],
    "Cluster D: Mission Bay / Dogpatch": [
        "Gusto",
    ],
}

# Sticky-note templates keyed on top_gap. One sentence, addressed to the team,
# with the specific fact from the audit — not a generic pitch.
GAP_NOTE_TEMPLATES: dict[str, str] = {
    "schema": (
        "{company} team — your homepage has zero JSON-LD schema, so ChatGPT "
        "literally can't tell what your pages are. This is a 10-minute fix."
    ),
    "decision": (
        "{company} team — no /vs or /alternatives pages means you're ceding "
        "the #1 page type LLMs cite for B2B buyer queries. We can help."
    ),
    "spa": (
        "{company} team — your homepage is JS-rendered, so GPTBot loads a "
        "near-empty page. You're effectively invisible to AI search today."
    ),
    "blog": (
        "{company} team — no crawlable /blog = invisible to 44.5% of AI "
        "search entry points (AthenaHQ State of AI Search 2026)."
    ),
    "none": (
        "{company} team — your GEO fundamentals are clean. The next frontier "
        "is tracking your actual citation share in ChatGPT and Perplexity."
    ),
}


def main() -> None:
    src = Path("poster_targets.csv")
    dst = Path("poster_targets_final.csv")

    rows = list(csv.DictReader(src.open()))

    cluster_lookup: dict[str, str] = {}
    for cluster_name, companies in CLUSTERS.items():
        for c in companies:
            cluster_lookup[c] = cluster_name

    enriched = []
    for r in rows:
        name = r["company"]
        template = GAP_NOTE_TEMPLATES.get(r["top_gap"], GAP_NOTE_TEMPLATES["none"])
        enriched.append({
            **r,
            "hq_neighborhood": NEIGHBORHOODS.get(name, "Unknown"),
            "walking_cluster": cluster_lookup.get(name, "Unclustered"),
            "personalized_poster_note": template.format(company=name),
        })

    with dst.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(enriched[0].keys()))
        w.writeheader()
        w.writerows(enriched)

    print(f"Wrote {len(enriched)} rows → {dst}\n")

    score = {r["company"]: int(r["total_score"]) for r in enriched}
    gap = {r["company"]: r["top_gap"] for r in enriched}
    covered = {r["company"] for r in enriched}

    print("=" * 78)
    print(f"{'POSTER CAMPAIGN — WALKING CLUSTERS':^78}")
    print("=" * 78)
    for cluster_name, companies in CLUSTERS.items():
        in_set = [c for c in companies if c in covered]
        in_set.sort(key=lambda c: -score.get(c, 0))
        print(f"\n{cluster_name}  ({len(in_set)} companies)")
        for c in in_set:
            print(f"  • {c:<12s}  score={score[c]:>3d}  gap={gap[c]:<8s}  "
                  f"({NEIGHBORHOODS.get(c, '?')})")

    unclustered = [r["company"] for r in enriched
                   if r["company"] not in cluster_lookup]
    if unclustered:
        print(f"\nUNCLUSTERED ({len(unclustered)}):  {', '.join(unclustered)}")
    print()


if __name__ == "__main__":
    main()
