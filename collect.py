"""
collect.py — gather the week's news from free public sources.

No AI here, no API key. It queries Google News RSS (the reliable workhorse),
any extra RSS feeds, and GDELT (best-effort), filters for relevance to
Mongolia, deduplicates, and writes data/raw_news.json.

Run locally:   python scripts/collect.py
Output:        data/raw_news.json
"""

import json
import time
import pathlib
import datetime as dt
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "raw_news.json"

HEADERS = {"User-Agent": "un-mongolia-weekly/1.0 (situational awareness)"}
TIMEOUT = 30


def _is_relevant(text: str) -> bool:
    text = (text or "").lower()
    terms = CONFIG["mongolia_terms"] + [q.split()[0] for q in CONFIG["regional_terms"]]
    return any(t.lower() in text for t in terms)


def _parse_rss(content):
    """Return list of (title, link, description, pubDate) from RSS bytes."""
    out = []
    root = ET.fromstring(content)
    for it in root.iter("item"):
        out.append((
            (it.findtext("title") or "").strip(),
            (it.findtext("link") or "").strip(),
            (it.findtext("description") or "").strip(),
            (it.findtext("pubDate") or "").strip(),
        ))
    return out


def fetch_google_news():
    cfg = CONFIG["sources"].get("google_news", {})
    if not cfg.get("enabled"):
        return []
    hl, gl = cfg.get("hl", "en-US"), cfg.get("gl", "US")
    items = []
    for query in cfg.get("queries", []):
        q = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:en"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for title, link, desc, pub in _parse_rss(r.content):
                # Google News titles look like "Headline - Publisher"
                source = "Google News"
                if " - " in title:
                    title, source = title.rsplit(" - ", 1)
                items.append({"title": title, "url": link, "source": source,
                              "published": pub, "snippet": desc[:600]})
        except Exception as e:
            print(f"  [google-news:{query}] skipped: {e}")
        time.sleep(1)  # be polite
    return items


def fetch_rss():
    items = []
    for feed in CONFIG["sources"].get("rss_feeds", []):
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for title, link, desc, pub in _parse_rss(r.content):
                if not (_is_relevant(title) or _is_relevant(desc)):
                    continue
                items.append({"title": title, "url": link, "source": feed["name"],
                              "published": pub, "snippet": desc[:600]})
        except Exception as e:
            print(f"  [rss:{feed['name']}] skipped: {e}")
    return items


def fetch_gdelt():
    """Best-effort only: GDELT rate-limits shared IPs, so failures are fine."""
    cfg = CONFIG["sources"].get("gdelt", {})
    if not cfg.get("enabled"):
        return []
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    items = []
    for query in cfg.get("queries", []):
        params = {"query": query, "mode": "ArtList", "format": "json",
                  "timespan": f"{CONFIG['lookback_days']}d",
                  "maxrecords": cfg.get("max_records", 40), "sort": "DateDesc"}
        ok = False
        for attempt in range(2):  # one retry with a longer pause
            try:
                r = requests.get(base, params=params, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code == 429:
                    time.sleep(6)
                    continue
                r.raise_for_status()
                for a in r.json().get("articles", []):
                    items.append({"title": a.get("title", ""), "url": a.get("url", ""),
                                  "source": a.get("domain", "GDELT"),
                                  "published": a.get("seendate", "")[:8], "snippet": ""})
                ok = True
                break
            except Exception as e:
                print(f"  [gdelt:{query}] attempt {attempt+1} skipped: {e}")
        time.sleep(3)  # spacing between queries to avoid 429
        if not ok:
            continue
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
    all_items += fetch_google_news()
    all_items += fetch_rss()
    all_items += fetch_gdelt()
    all_items = dedupe(all_items)

    OUT.parent.mkdir(parents=True, exist_ok=True)   # <-- the fix: ensure data/ exists
    payload = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "lookback_days": CONFIG["lookback_days"],
               "count": len(all_items), "items": all_items}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Collected {len(all_items)} items -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
