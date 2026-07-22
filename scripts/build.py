"""
build.py — render the static site into docs/ from the JSON in reports/ and mentions/.

Two sections, one site:
  docs/index.html            newest weekly report
  docs/weeks/<date>.html     every weekly report
  docs/media/index.html      newest mentions + the cumulative archive
  docs/media/<date>.html     every week's mentions

Fully deterministic: running it again reproduces the same site from the JSON,
which stays the single source of truth.

Run locally:   python scripts/build.py   (then open docs/index.html)
"""

import json
import shutil
import pathlib
import datetime as dt
from collections import Counter, defaultdict

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DOCS = ROOT / "docs"
TONES = CONFIG.get("media_monitoring", {}).get("tone_labels",
                                               ["Supportive", "Neutral", "Critical"])

env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                  autoescape=select_autoescape(["html"]))


def undate(value):
    """UN Editorial Manual date form: 14 July 2026."""
    try:
        d = dt.date.fromisoformat(str(value)[:10])
    except Exception:
        return value
    return f"{d.day} {d.strftime('%B %Y')}"


env.filters["undate"] = undate

report_tpl = env.get_template("report.html.j2")
media_tpl = env.get_template("media.html.j2")


def label(start, end):
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    if s.month == e.month:
        return f"{s.day}\u2013{e.day} {e.strftime('%b %Y')}"
    return f"{s.strftime('%d %b')} \u2013 {e.strftime('%d %b %Y')}"


def load(folder):
    out = []
    for p in sorted((ROOT / folder).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! {p.name} could not be read ({e}); skipped")
            continue
        if not d.get("week_start"):
            continue
        d["_slug"] = d["week_start"]
        d["_label"] = label(d["week_start"], d.get("week_end", d["week_start"]))
        out.append(d)
    out.sort(key=lambda d: d["week_start"], reverse=True)
    return out


def tone_counts(mentions):
    c = Counter(m.get("tone", "Neutral") for m in mentions)
    return [(t, c.get(t, 0)) for t in TONES]


def compute_stats(week_doc, all_docs):
    week = {"total": len(week_doc.get("mentions", [])),
            "tones": tone_counts(week_doc.get("mentions", []))}

    every = [m for d in all_docs for m in d.get("mentions", [])]
    if not every:
        return {"week": week, "all": {"total": 0}}

    by_outlet = defaultdict(list)
    by_agency = defaultdict(list)
    for m in every:
        by_outlet[m.get("outlet") or "Unknown"].append(m)
        for ag in m.get("agencies", []) or []:
            by_agency[ag].append(m)

    def rank(d, n=8):
        rows = sorted(d.items(), key=lambda kv: -len(kv[1]))[:n]
        return [(k, {"total": len(v), "tones": tone_counts(v)}) for k, v in rows]

    matched = sum(1 for m in every if (m.get("un_source") or {}).get("matched"))
    weeks = [{"slug": d["_slug"], "label": d["_label"],
              "total": len(d.get("mentions", [])),
              "tones": tone_counts(d.get("mentions", []))}
             for d in sorted(all_docs, key=lambda d: d["week_start"])][-12:]

    return {
        "week": week,
        "all": {
            "total": len(every),
            "since": undate(min(d["week_start"] for d in all_docs)),
            "outlet_count": len(by_outlet),
            "matched_pct": round(100 * matched / len(every)),
            "weeks": len(all_docs),
            "by_outlet": rank(by_outlet),
            "by_agency": rank(by_agency),
            "by_week": weeks,
        },
    }


def main():
    reports = load("reports")
    mentions = load("mentions")

    if not reports and not mentions:
        print("Nothing to build: reports/ and mentions/ are both empty.")
        return

    if DOCS.exists():
        shutil.rmtree(DOCS)
    (DOCS / "weeks").mkdir(parents=True)
    (DOCS / "media").mkdir(parents=True)
    shutil.copy(ROOT / "assets" / "style.css", DOCS / "style.css")
    shutil.copy(ROOT / "assets" / "app.js", DOCS / "app.js")
    (DOCS / ".nojekyll").write_text("")

    # ---------------- weekly report pages ----------------
    if reports:
        archive = [{"slug": r["_slug"], "label": r["_label"],
                    "href": f"weeks/{r['_slug']}.html"} for r in reports]

        def render_report(r, root):
            return report_tpl.render(site=CONFIG["site"], report=r, label=r["_label"],
                                     archive=archive, agencies=CONFIG["agencies"],
                                     current=r["_slug"], root=root)

        for r in reports:
            (DOCS / "weeks" / f"{r['_slug']}.html").write_text(
                render_report(r, "../"), encoding="utf-8")
        (DOCS / "index.html").write_text(render_report(reports[0], ""), encoding="utf-8")

    # ---------------- media pages ----------------
    if mentions:
        m_archive = [{"slug": d["_slug"], "label": d["_label"],
                      "href": f"media/{d['_slug']}.html"} for d in mentions]

        def render_media(d, root, is_index):
            return media_tpl.render(site=CONFIG["site"], doc=d, label=d["_label"],
                                    archive=m_archive, agencies=CONFIG["agencies"],
                                    tones=TONES, current=d["_slug"], root=root,
                                    is_index=is_index,
                                    stats=compute_stats(d, mentions))

        for d in mentions:
            (DOCS / "media" / f"{d['_slug']}.html").write_text(
                render_media(d, "../", False), encoding="utf-8")
        (DOCS / "media" / "index.html").write_text(
            render_media(mentions[0], "../", True), encoding="utf-8")
    elif reports:
        # placeholder so the sidebar link never 404s before the first run
        (DOCS / "media" / "index.html").write_text(
            "<!DOCTYPE html><meta charset='utf-8'>"
            "<title>In the Media</title>"
            "<body style='font-family:Georgia,serif;max-width:40rem;margin:8rem auto;"
            "padding:0 1.5rem;color:#1c1a17;background:#faf8f3'>"
            "<h1 style='font-weight:500'>In the Media</h1>"
            "<p>No media mentions have been recorded yet. This page fills in after the "
            "first Monday run of the media monitoring.</p>"
            "<p><a href='../index.html'>Back to the weekly report</a></p></body>",
            encoding="utf-8")

    print(f"Built {len(reports)} report(s) and {len(mentions)} media week(s) -> docs/")


if __name__ == "__main__":
    main()
