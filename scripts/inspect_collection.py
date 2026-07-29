"""
inspect_collection.py — judge the quality of news collection and the shared
workbook, with zero API calls and zero cost. Prints a human-readable report
to the console (visible directly in the Actions log, no download needed).

Run: python scripts/inspect_collection.py
Requires: data/raw_news.json to already exist (run collect.py first).
"""

import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sources_loader as SL   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw_news.json"

STOP = set("the a an and or of in on at to for from with by as is are was were will "
           "be been this that these those mongolia mongolian said says say new news".split())
WORD = re.compile(r"[A-Za-z]{4,}")


def normalise(title):
    words = {w.lower() for w in WORD.findall(title or "") if w.lower() not in STOP}
    return words


def near_duplicate_clusters(items, threshold=0.6):
    """Group items whose titles share most of their distinctive words —
    the kind of near-duplicate that exact-string dedupe misses."""
    clusters = []
    used = set()
    for i, a in enumerate(items):
        if i in used:
            continue
        wa = normalise(a.get("title", ""))
        if not wa:
            continue
        group = [a]
        for j, b in enumerate(items[i + 1:], start=i + 1):
            if j in used:
                continue
            wb = normalise(b.get("title", ""))
            if not wb:
                continue
            overlap = len(wa & wb) / max(1, min(len(wa), len(wb)))
            if overlap >= threshold:
                group.append(b)
                used.add(j)
        if len(group) > 1:
            clusters.append(group)
            used.add(i)
    return clusters


def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main():
    # ---- Part 1: the shared workbook ----
    section("1. SHARED WORKBOOK (sources/sources.xlsx)")
    data, issues = SL.load_workbook_data()
    for sheet, rows in data.items():
        print(f"  {sheet:10s} {len(rows)} active row(s)")
    if issues:
        print(f"\n  {len(issues)} issue(s) found — these get skipped, not fatal:")
        for i in issues:
            print(f"    ! {i}")
    else:
        print("\n  No issues. The workbook is being read correctly.")

    queries, feeds = SL.news_sources(data)
    print(f"\n  -> This produces {len(queries)} search(es) and {len(feeds)} feed(s) "
          f"for the collector.")

    # ---- Part 2: the collected news ----
    if not RAW.exists():
        print("\nNo data/raw_news.json yet — run scripts/collect.py first.")
        return
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    items = raw.get("items", [])

    section("2. NEWS COLLECTION QUALITY")
    print(f"  Total items collected: {len(items)}")
    if raw.get("issues"):
        print(f"  {len(raw['issues'])} collection issue(s) (failed feeds/searches):")
        for i in raw["issues"][:10]:
            print(f"    ! {i}")

    by_query = Counter(it.get("suggested_category") or "(no category hint)" for it in items)
    print("\n  Breakdown by suggested category:")
    for cat, n in by_query.most_common():
        print(f"    {n:4d}  {cat}")

    clusters = near_duplicate_clusters(items)
    dup_count = sum(len(c) - 1 for c in clusters)
    print(f"\n  Near-duplicate titles NOT caught by exact-match dedupe: "
          f"{dup_count} item(s) in {len(clusters)} cluster(s)")
    if clusters:
        print("  (these look like the same story returned by more than one search —"
              " example clusters below)")
        for c in clusters[:8]:
            print(f"    - {len(c)}x: \"{c[0]['title'][:80]}\"")
            for extra in c[1:3]:
                print(f"           ~ \"{extra['title'][:80]}\"")

    cap = 120
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        cap = cfg.get("max_items_to_model", 120)
    except Exception:
        pass
    print(f"\n  config.yaml sends the first {cap} of these {len(items)} to Claude, "
          f"in whatever order they were collected (no relevance ranking applied "
          f"before the cut, as of this version of collect.py).")
    print(f"  -> If {dup_count} of them are near-duplicates, roughly that many "
          f"'wasted slots' may be pushing genuinely distinct stories past the cut.")

    section("What this tells you")
    print("  - If the workbook section above shows issues, that's exactly what "
          "'updating sources' would surface — fix them in Excel and re-run this "
          "same check before spending any tokens.")
    print("  - If near-duplicate clusters are large, the news selection quality "
          "problem is upstream of Claude: it's a collection/dedupe issue, not a "
          "drafting issue. Worth fixing before judging the AI's report quality.")
    print("  - None of this used the Anthropic API. Zero tokens spent.")


if __name__ == "__main__":
    main()
