"""
build.py — render the static site into docs/ from every report in reports/.

The newest report becomes docs/index.html (the landing page).
Every report (incl. newest) also gets docs/weeks/<date>.html so archive
links stay stable. The sidebar lists all weeks. Fully deterministic:
running it again reproduces the same site from the JSON source of truth.

Run locally:   python scripts/build.py   (then open docs/index.html)
"""

import json
import shutil
import pathlib
import datetime as dt

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DOCS = ROOT / "docs"
env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                  autoescape=select_autoescape(["html"]))
tpl = env.get_template("report.html.j2")


def label(report):
    s = dt.date.fromisoformat(report["week_start"])
    e = dt.date.fromisoformat(report["week_end"])
    if s.month == e.month:
        return f"{s.day}\u2013{e.day} {e.strftime('%b %Y')}"
    return f"{s.strftime('%d %b')} \u2013 {e.strftime('%d %b %Y')}"


def main():
    reports = []
    for p in sorted(ROOT.glob("reports/*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        r["_slug"] = r["week_start"]
        r["_label"] = label(r)
        reports.append(r)
    reports.sort(key=lambda r: r["week_start"], reverse=True)
    if not reports:
        print("No reports found in reports/. Run generate.py first.")
        return

    # reset docs/
    if DOCS.exists():
        shutil.rmtree(DOCS)
    (DOCS / "weeks").mkdir(parents=True)
    shutil.copy(ROOT / "assets" / "style.css", DOCS / "style.css")
    shutil.copy(ROOT / "assets" / "app.js", DOCS / "app.js")
    (DOCS / ".nojekyll").write_text("")  # let GitHub Pages serve files as-is

    archive = [{"slug": r["_slug"], "label": r["_label"],
                "href": f"weeks/{r['_slug']}.html"} for r in reports]

    def render(report, root):
        return tpl.render(site=CONFIG["site"], report=report, label=report["_label"],
                          archive=archive, agencies=CONFIG["agencies"],
                          current=report["_slug"], root=root)

    # per-week pages (root = ../ because they live in docs/weeks/)
    for r in reports:
        (DOCS / "weeks" / f"{r['_slug']}.html").write_text(render(r, "../"), encoding="utf-8")
    # landing page = newest (root = ./)
    (DOCS / "index.html").write_text(render(reports[0], ""), encoding="utf-8")

    print(f"Built {len(reports)} report(s) -> docs/  (latest: {reports[0]['_label']})")


if __name__ == "__main__":
    main()
