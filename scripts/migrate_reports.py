"""
migrate_reports.py — bring existing reports/*.json onto the new schema.

Old reports used six free-form themes and the key "theme". The site now uses
the four categories in config.yaml and the key "category". This rewrites the
old files in place so the archive keeps rendering; it is safe to run twice.

    python scripts/migrate_reports.py
"""

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
CATEGORIES = CONFIG["categories"]

OLD_TO_NEW = {
    "humanitarian & protection": "Social and Humanitarian",
    "humanitarian and protection": "Social and Humanitarian",
    "health": "Social and Humanitarian",
    "politics & governance": "Politics, Governance and Diplomacy",
    "politics and governance": "Politics, Governance and Diplomacy",
    "regional dynamics": "Politics, Governance and Diplomacy",
    "economy & development": "Economy and Development",
    "economy and development": "Economy and Development",
    "climate & environment": "Environment and Climate Change",
    "climate and environment": "Environment and Climate Change",
}


def to_category(name):
    n = (name or "").strip().lower()
    if n in OLD_TO_NEW:
        return OLD_TO_NEW[n]
    for c in CATEGORIES:
        if c.lower() == n:
            return c
    return "Politics, Governance and Diplomacy"


def migrate(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    merged = {}
    for s in d.get("sections", []):
        if "theme" in s and "category" not in s:
            changed = True
        cat = to_category(s.get("category") or s.get("theme"))
        s.pop("theme", None)
        mn = s.pop("theme_mn", None) or s.get("category_mn") or cat
        entry = merged.setdefault(cat, {"category": cat, "category_mn": mn, "items": []})
        entry["items"] += s.get("items", [])
    if merged:
        d["sections"] = [merged[c] for c in CATEGORIES if c in merged]
    for s in d.get("sections", []):
        for it in s.get("items", []):
            it.setdefault("tags", [])
    for it in d.get("also_noted", []):
        it.setdefault("tags", [])
    d.setdefault("style_flags", [])
    d.setdefault("issues", [])
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


if __name__ == "__main__":
    files = sorted((ROOT / "reports").glob("*.json"))
    for f in files:
        changed = migrate(f)
        print(f"  {f.name}: {'migrated' if changed else 'already current'}")
    print(f"{len(files)} report(s) checked.")
