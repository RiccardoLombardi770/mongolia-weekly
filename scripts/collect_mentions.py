"""
collect_mentions.py — find the week's media mentions of the United Nations in
Mongolia, and fetch enough of each article to judge it.

No AI here. It searches, filters, downloads the article text, and — crucially —
records any outbound link to a United Nations domain, because that link is the
only way to be certain which of our publications an outlet was citing.

Output: data/raw_mentions.json
"""

import json
import pathlib
import re
import sys
import time
import datetime as dt
import urllib.parse
from urllib.parse import urlparse

import requests
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sources_loader as SL          # noqa: E402
from index_un import parse_feed, strip_tags, find_date   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
MM = CONFIG.get("media_monitoring", {})
OUT = ROOT / "data" / "raw_mentions.json"

HEADERS = {"User-Agent": "mongolia-weekly/2.0 (UN Mongolia media monitoring)"}
TIMEOUT = MM.get("fetch_timeout", 25)
UN_DOMAINS = [d.lower() for d in MM.get("un_domains", [])]

# Terms that make an article a candidate mention. Cyrillic included because
# most Mongolian coverage never uses the Latin acronym.
CORE_TERMS = [
    "United Nations", "UN Mongolia", "UN Resident Coordinator",
    "Resident Coordinator", "UN Country Team", "UNRCO",
    "НҮБ", "Нэгдсэн Үндэстний", "Суурин зохицуулагч",
]


def agency_terms():
    return list(CONFIG.get("agencies", []))


def roster_terms(roster):
    out = []
    for p in roster:
        out.append(p["name"])
        out += p.get("aliases", [])
    return [t for t in out if t and len(t) > 3]


def google_news(query, lang="en"):
    hl, gl, ceid = ("mn-MN", "MN", "MN:mn") if lang == "mn" else ("en-US", "US", "US:en")
    q = urllib.parse.quote(query)
    url = (f"https://news.google.com/rss/search?q={q}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [], f"[search] '{query}' failed: {e}"
    items = []
    for title, link, date, summary in parse_feed(r.content):
        outlet = "Google News"
        if " - " in title:
            title, outlet = title.rsplit(" - ", 1)
        items.append({"title": title.strip(), "url": link, "outlet_hint": outlet.strip(),
                      "published": date, "snippet": summary[:400], "query": query,
                      "language": lang})
    return items, None


def within_window(item, days):
    d = item.get("published") or ""
    try:
        return (dt.date.today() - dt.date.fromisoformat(d[:10])).days <= days
    except Exception:
        return True     # undated: keep, the model sees the date it can find


def resolve(url):
    """Google News wraps links; follow to the publisher."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        return r.url, r
    except Exception:
        return url, None


def un_links_in(html, base_url):
    """Every outbound link to a UN domain — the strongest possible evidence."""
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = m.group(1)
        if href.startswith("/"):
            continue
        host = urlparse(href).netloc.lower().replace("www.", "")
        if any(host == d or host.endswith("." + d) for d in UN_DOMAINS):
            out.append(href.split("#")[0])
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:10]


def mentions_un(text, terms):
    low = (text or "").lower()
    return [t for t in terms if t.lower() in low]


def outlet_for(url, hint, outlets):
    host = urlparse(url).netloc.lower().replace("www.", "")
    for o in outlets:
        if host == o["domain"] or host.endswith("." + o["domain"]):
            return o["name"], o["domain"], o["language"], o["priority"], True
    return (hint or host or "Unknown"), host, "", 3, False


def main():
    data, issues = SL.load_workbook_data()
    outlets = SL.outlets(data)
    roster = SL.roster(data)
    days = CONFIG.get("lookback_days", 7)

    terms = CORE_TERMS + agency_terms() + roster_terms(roster)

    # Queries: generic UN-in-Mongolia, one per agency, one per named official.
    queries = [("\"United Nations\" Mongolia", "en"),
               ("UN Resident Coordinator Mongolia", "en"),
               ("НҮБ Монгол", "mn"),
               ("НҮБ-ын суурин зохицуулагч", "mn")]
    for ag in agency_terms():
        queries.append((f"{ag} Mongolia", "en"))
    for p in roster:
        queries.append((f"\"{p['name']}\" Mongolia", "en"))
        for alias in p.get("aliases", [])[:2]:
            queries.append((f"\"{alias}\"", "mn" if re.search(r"[\u0400-\u04FF]", alias) else "en"))
    # Outlets that publish a feed get scanned directly too.
    feed_outlets = [o for o in outlets if o.get("rss_url")]

    print(f"Searching {len(queries)} quer(ies) + {len(feed_outlets)} outlet feed(s)...")
    candidates = []
    for q, lang in queries:
        items, err = google_news(q, lang)
        if err:
            issues.append(err)
        candidates += items
        time.sleep(1)

    for o in feed_outlets:
        try:
            r = requests.get(o["rss_url"], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            for title, link, date, summary in parse_feed(r.content):
                candidates.append({"title": title, "url": link,
                                   "outlet_hint": o["name"], "published": date,
                                   "snippet": summary[:400], "query": "outlet feed",
                                   "language": o["language"]})
        except Exception as e:
            issues.append(f"[outlet feed] {o['name']}: {e}")

    # Dedupe + window + cheap pre-filter on title/snippet.
    seen, filtered = set(), []
    for c in candidates:
        key = (c.get("url") or c.get("title", "")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if not within_window(c, days):
            continue
        if not (mentions_un(c["title"] + " " + c.get("snippet", ""), terms)):
            continue
        filtered.append(c)

    # Priority: known outlets first, then most recent.
    for c in filtered:
        name, domain, lang, prio, known = outlet_for(c["url"], c.get("outlet_hint"), outlets)
        c.update({"outlet": name, "outlet_domain": domain, "known_outlet": known,
                  "priority": prio})
        if lang:
            c["language"] = lang
    filtered.sort(key=lambda c: (c["priority"], c.get("published") or ""), reverse=False)

    cap = int(MM.get("max_articles_to_fetch", 60))
    filtered = filtered[:cap]
    print(f"{len(filtered)} candidate mention(s) after filtering")

    # Fetch the full text — needed for tone, prominence and source matching.
    if MM.get("fetch_full_text", True):
        for i, c in enumerate(filtered, 1):
            final_url, r = resolve(c["url"])
            c["url"] = final_url
            if r is None or not getattr(r, "text", ""):
                c["fetch_ok"] = False
                c["body"] = ""
                c["un_links"] = []
                issues.append(f"[fetch] could not read {final_url}")
                continue
            html = r.text
            body = strip_tags(html)
            c["fetch_ok"] = len(body) > 400
            c["body"] = body[:6000]
            c["un_links"] = un_links_in(html, final_url)
            c["matched_terms"] = mentions_un(body, terms)[:8]
            if not c.get("published"):
                c["published"] = find_date(body[:3000])
            # re-resolve outlet now that redirects are followed
            name, domain, lang, prio, known = outlet_for(final_url, c.get("outlet_hint"), outlets)
            c.update({"outlet": name, "outlet_domain": domain, "known_outlet": known})
            if i % 10 == 0:
                print(f"  fetched {i}/{len(filtered)}")
            time.sleep(0.6)

    # Drop anything whose full text turned out not to mention us at all.
    keep = [c for c in filtered
            if c.get("matched_terms") or not c.get("fetch_ok", True)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
         "lookback_days": days, "count": len(keep),
         "issues": issues, "mentions": keep},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Collected {len(keep)} mention(s) -> {OUT.relative_to(ROOT)}")
    for i in issues[:15]:
        print("  !", i)


if __name__ == "__main__":
    main()
