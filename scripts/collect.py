"""
collect.py — gather the week's news from free public sources.

No AI here, no API key. It just fetches candidate articles from
ReliefWeb, GDELT and any RSS feeds, filters them for relevance to
Mongolia, deduplicates, and writes data/raw_news.json.

Run locally:   python scripts/collect.py
Output:        data/raw_news.json
"""

import json
import pathlib
import datetime as dt
import xml.etree.ElementTree as ET

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "raw_news.json"

HEADERS = {"User-Agent": "un-mongolia-weekly/1.0 (situational awareness)"}
TIMEOUT = 30


def _within_lookback(published: str, days: int) -> bool:
    """Best-effort date filter; if we can't parse a date, keep the item."""
    if not published:
        return True
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = dt.datetime.strptime(published.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
            return d >= cutoff
        except ValueError:
            continue
    return True


def _is_relevant(text: str) -> bool:
    text = (text or "").lower()
    terms = CONFIG["mongolia_terms"] + [q.split()[0] for q in CONFIG["regional_terms"]]
    return any(t.lower() in text for t in terms)


def fetch_reliefweb():
    cfg = CONFIG["sources"]["reliefweb"]
    if not cfg.get("enabled"):
        return []
    url = "https://api.reliefweb.int/v1/reports"
    params = {"appname": "un-mongolia-weekly"}
    payload = {
        "filter": {"field": "country", "value": cfg["country"]},
        "fields": {"include": ["title", "url", "source", "date", "body-html"]},
        "sort": ["date:desc"],
        "limit": 40,
    }
    items = []
    try:
        r = requests.post(url, params=params, json=payload, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for entry in r.json().get("data", []):
            f = entry.get("fields", {})
            published = (f.get("date") or {}).get("created", "")[:10]
            if not _within_lookback(published, CONFIG["lookback_days"]):
                continue
            src = ", ".join(s.get("name", "") for s in f.get("source", [])) or "ReliefWeb"
            items.append({
                "title": f.get("title", ""),
                "url": f.get("url", ""),
                "source": src,
                "published": published,
                "snippet": (f.get("body-html", "") or "")[:600],
            })
    except Exception as e:
        print(f"  [reliefweb] skipped: {e}")
    return items


def fetch_gdelt():
    cfg = CONFIG["sources"]["gdelt"]
    if not cfg.get("enabled"):
        return []
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    items = []
    for query in cfg["queries"]:
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "timespan": f"{CONFIG['lookback_days']}d",
            "maxrecords": cfg.get("max_records", 50),
            "sort": "DateDesc",
        }
        try:
            r = requests.get(base, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for a in r.json().get("articles", []):
                items.append({
                    "title": a.get("title", ""),
                    "url": a.get("url", ""),
                    "source": a.get("domain", "GDELT"),
                    "published": a.get("seendate", "")[:8],
                    "snippet": "",
                })
        except Exception as e:
            print(f"  [gdelt:{query}] skipped: {e}")
    return items


def fetch_rss():
    items = []
    for feed in CONFIG["sources"].get("rss_feeds", []):
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                desc = (it.findtext("description") or "").strip()
                if not (_is_relevant(title) or _is_relevant(desc)):
                    continue
                items.append({
                    "title": title,
                    "url": (it.findtext("link") or "").strip(),
                    "source": feed["name"],
                    "published": (it.findtext("pubDate") or "").strip(),
                    "snippet": desc[:600],
                })
        except Exception as e:
            print(f"  [rss:{feed['name']}] skipped: {e}")
    return items


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = (it.get("url") or it.get("title", "")).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def main():
    print("Collecting news...")
    all_items = []
    all_items += fetch_reliefweb()
    all_items += fetch_gdelt()
    all_items += fetch_rss()
    all_items = dedupe(all_items)

    payload = {
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_days": CONFIG["lookback_days"],
        "count": len(all_items),
        "items": all_items,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Collected {len(all_items)} items -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
