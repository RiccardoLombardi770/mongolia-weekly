"""
index_un.py — keep a rolling index of our own publications.

This is what makes the back-link possible. When an outlet writes "according to
UNICEF", the report can only point at the exact press release if we already
know what UNICEF published. So every Monday this crawls the pages listed in the
un_sites sheet, adds anything new, and drops anything older than
media_monitoring.un_index_days (default 180).

The index is committed to the repository (index/un_publications.json) so it
accumulates week after week instead of starting from zero each run.

Run locally:   python scripts/index_un.py
"""

import json
import pathlib
import re
import sys
import time
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sources_loader as SL  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
MM = CONFIG.get("media_monitoring", {})
OUT = ROOT / "index" / "un_publications.json"

HEADERS = {"User-Agent": "mongolia-weekly/2.0 (UN Mongolia media monitoring)"}
TIMEOUT = MM.get("fetch_timeout", 25)

DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), lambda m: f"{m[1]}-{m[2]}-{m[3]}"),
    (re.compile(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{4})", re.I),
     lambda m: _iso(m[3], m[2], m[1])),
    (re.compile(r"(January|February|March|April|May|June|July|August|September|"
                r"October|November|December)\s+(\d{1,2}),\s*(\d{4})", re.I),
     lambda m: _iso(m[3], m[1], m[2])),
]
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def _iso(y, month_name, d):
    m = MONTHS.index(month_name.lower()) + 1
    return f"{int(y):04d}-{m:02d}-{int(d):02d}"


def find_date(text):
    for rx, fn in DATE_PATTERNS:
        m = rx.search(text or "")
        if m:
            try:
                iso = fn(m)
                dt.date.fromisoformat(iso)
                return iso
            except Exception:
                continue
    return ""


def strip_tags(html):
    html = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#39;", "'")
                .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", html).strip()


def parse_feed(content):
    """RSS or Atom -> list of (title, link, date, summary)."""
    out = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return out
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def txt(*names):
            for n in names:
                for ch in it:
                    if ch.tag.split("}")[-1] == n:
                        if ch.text and ch.text.strip():
                            return ch.text.strip()
                        if n == "link" and ch.get("href"):
                            return ch.get("href")
            return ""
        out.append((txt("title"), txt("link"), find_date(txt("pubDate", "published", "updated")),
                    strip_tags(txt("description", "summary", "content"))[:600]))
    return out


def discover_links(base_url, html, host):
    """Pull plausible article links out of a listing page."""
    links = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href, label = m.group(1), strip_tags(m.group(2))
        if not label or len(label) < 25:
            continue
        url = urljoin(base_url, href)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or host not in p.netloc:
            continue
        if re.search(r"(?i)\.(jpg|png|pdf|zip|docx?)$", p.path):
            continue
        if p.path.count("/") < 2:
            continue
        links.append((label.strip()[:300], url.split("#")[0]))
    # keep order, drop duplicates
    seen, out = set(), []
    for label, url in links:
        if url in seen:
            continue
        seen.add(url)
        out.append((label, url))
    return out[:40]


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r


def harvest_site(site, issues):
    """Return list of publication dicts for one un_sites row."""
    agency = site.get("agency") or ""
    found = []
    host = urlparse(site["url"]).netloc.replace("www.", "")

    candidates = [site["feed_url"]] if site.get("feed_url") else []
    candidates += [urljoin(site["url"], p) for p in ("rss.xml", "feed", "feed/", "rss")]

    for feed_url in candidates:
        if not feed_url:
            continue
        try:
            r = fetch(feed_url)
            items = parse_feed(r.content)
            if items:
                for title, link, date, summary in items:
                    if title and link:
                        found.append({"agency": agency, "title": title, "url": link,
                                      "date": date, "summary": summary,
                                      "source_page": site["url"], "via": "feed"})
                return found
        except Exception:
            continue

    # No feed: scrape the listing page.
    try:
        r = fetch(site["url"])
    except Exception as e:
        issues.append(f"[un_sites] could not read {site['url']}: {e}")
        return found
    for label, url in discover_links(site["url"], r.text, host):
        found.append({"agency": agency, "title": label, "url": url,
                      "date": find_date(url) or "", "summary": "",
                      "source_page": site["url"], "via": "listing"})
    if not found:
        issues.append(f"[un_sites] no publications found at {site['url']} — "
                      f"the page layout may have changed, or the URL may be wrong")
    return found


def enrich(pub):
    """Open a publication with no date/summary and fill both in."""
    try:
        r = fetch(pub["url"])
    except Exception:
        return pub
    text = strip_tags(r.text)
    if not pub.get("date"):
        pub["date"] = find_date(text[:4000]) or find_date(r.text[:6000])
    if not pub.get("summary"):
        pub["summary"] = text[:600]
    pub["body"] = text[:4000]
    return pub


def main():
    data, issues = SL.load_workbook_data()
    sites = SL.un_sites(data)
    if not sites:
        sites = [{"agency": "UN", "name": d, "url": f"https://{d}", "feed_url": ""}
                 for d in MM.get("un_domains", [])[:1]]

    existing = {}
    if OUT.exists():
        try:
            for p in json.loads(OUT.read_text(encoding="utf-8")).get("publications", []):
                existing[p["url"]] = p
        except Exception as e:
            issues.append(f"[index] existing index unreadable ({e}); rebuilding from scratch")

    print(f"Indexing {len(sites)} UN site(s)...")
    fresh = []
    for site in sites:
        pubs = harvest_site(site, issues)
        print(f"  {site['url']} -> {len(pubs)}")
        fresh += pubs
        time.sleep(1)

    added = 0
    for p in fresh:
        if p["url"] in existing:
            continue
        if not p.get("date") or not p.get("summary"):
            p = enrich(p)
            time.sleep(0.5)
        p["first_seen"] = dt.date.today().isoformat()
        existing[p["url"]] = p
        added += 1

    # Rolling window.
    cutoff = dt.date.today() - dt.timedelta(days=int(MM.get("un_index_days", 180)))
    kept = []
    for p in existing.values():
        ref = p.get("date") or p.get("first_seen") or ""
        try:
            if dt.date.fromisoformat(ref[:10]) >= cutoff:
                kept.append(p)
        except Exception:
            kept.append(p)   # undated: keep, the matcher will weight it lower
    kept.sort(key=lambda p: p.get("date") or p.get("first_seen") or "", reverse=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
         "window_days": MM.get("un_index_days", 180),
         "count": len(kept), "issues": issues, "publications": kept},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Index: {len(kept)} publication(s), {added} new -> {OUT.relative_to(ROOT)}")
    for i in issues:
        print("  !", i)


if __name__ == "__main__":
    main()
