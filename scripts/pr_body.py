"""
pr_body.py — write the text of Monday's pull request.

The point is that the reviewer should not have to open the JSON to know what
needs attention. Everything that needs a human decision — sensitive country
names, sources that could not be identified, feeds that stopped working — is
listed here, at the top of the review.

Writes data/pr_body.md, which the workflow passes to create-pull-request.
"""

import json
import pathlib
import datetime as dt

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "pr_body.md"


def newest(folder):
    files = sorted((ROOT / folder).glob("*.json"))
    if not files:
        return None, None
    p = files[-1]
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return p, None


def undate(v):
    try:
        d = dt.date.fromisoformat(str(v)[:10])
        return f"{d.day} {d.strftime('%B %Y')}"
    except Exception:
        return v


def main():
    lines = []
    rpath, report = newest("reports")
    mpath, mentions = newest("mentions")

    lines.append("An automated draft is ready for review.\n")
    lines.append("**To publish:** read the files in the *Files changed* tab, edit any "
                 "wording directly in the JSON (`en` / `mn` fields), then click "
                 "**Merge pull request**. The site rebuilds and goes live on its own.\n")

    # ---- the weekly report
    if report:
        n_items = sum(len(s.get("items", [])) for s in report.get("sections", []))
        lines.append(f"### Weekly report — {undate(report.get('week_start'))} to "
                     f"{undate(report.get('week_end'))}")
        lines.append(f"- Self-review confidence: **{report.get('confidence', 0)}/100** "
                     f"(threshold {CONFIG['review']['confidence_threshold']})")
        lines.append(f"- {n_items} item(s) across {len(report.get('sections', []))} "
                     f"categor(ies), {len(report.get('also_noted', []))} in Also noted")
        for s in report.get("sections", []):
            lines.append(f"  - {s.get('category')}: {len(s.get('items', []))}")
        lines.append("")

    # ---- the media page
    if mentions:
        ms = mentions.get("mentions", [])
        tones = {}
        for m in ms:
            tones[m.get("tone", "?")] = tones.get(m.get("tone", "?"), 0) + 1
        matched = sum(1 for m in ms if (m.get("un_source") or {}).get("matched"))
        lines.append(f"### In the Media — {len(ms)} mention(s)")
        lines.append(f"- Self-review confidence: **{mentions.get('confidence', 0)}/100**")
        if tones:
            lines.append("- Tone: " + ", ".join(f"{v} {k.lower()}" for k, v in tones.items()))
        lines.append(f"- Source publication identified for {matched} of {len(ms)}")
        low = [m for m in ms
               if (m.get("un_source") or {}).get("method") == "inferred"
               and (m.get("un_source") or {}).get("confidence", 0) < 75]
        if low:
            lines.append("- **Check these inferred sources** (confidence below 75%):")
            for m in low:
                src = m["un_source"]
                lines.append(f"  - {m.get('outlet')} → {src.get('title') or src.get('url')} "
                             f"({src.get('confidence')}%) — {src.get('reason', '')}")
        crit = [m for m in ms if m.get("tone") == "Critical"]
        if crit:
            lines.append("- **Critical in tone** (worth a second read before publishing):")
            for m in crit:
                lines.append(f"  - {m.get('outlet')}: {m.get('title')} — {m.get('url')}")
        lines.append("")

    # ---- things a human must decide
    flags = (report or {}).get("style_flags", []) + (mentions or {}).get("style_flags", [])
    if flags:
        lines.append("### Sensitive names — decide before merging")
        lines.append("These were left exactly as drafted. United Nations usage on these "
                     "is a matter for the office, not for the tool.\n")
        for f in flags:
            lines.append(f"- **{f['term']}** — {f['context']}")
        lines.append("")

    issues = (report or {}).get("issues", []) + (mentions or {}).get("issues", [])
    seen, uniq = set(), []
    for i in issues:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    if uniq:
        lines.append("### Source problems")
        lines.append("Fix these in `sources/sources.xlsx` and upload the file again; "
                     "they do not block this week's report.\n")
        for i in uniq[:40]:
            lines.append(f"- {i}")
        if len(uniq) > 40:
            lines.append(f"- …and {len(uniq) - 40} more")
        lines.append("")

    if not flags and not uniq:
        lines.append("No sensitive names flagged and no source problems this week.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
