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
import xml.etree.ElementTree as ET

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


def google_source_map(content):
    """link -> publisher domain, from the <source url="..."> of each RSS item.

    Google News hands back links of the form news.google.com/rss/articles/CBMi...
    which redirect through JavaScript, not HTTP — so following them leaves us on
    news.google.com and the real publisher stays unknown. The publisher is right
    there in the feed, in an element parse_feed() has no use for elsewhere.
    """
    out = {}
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return out
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        link = src = ""
        for ch in it:
            tag = ch.tag.split("}")[-1]
            if tag == "link":
                link = (ch.text or ch.get("href") or "").strip()
            elif tag == "source":
                src = (ch.get("url") or "").strip()
        if link and src:
            out[link] = urlparse(src).netloc.lower().replace("www.", "")
    return out


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
    sources = google_source_map(r.content)
    items = []
    for title, link, date, summary in parse_feed(r.content):
        outlet = "Google News"
        if " - " in title:
            title, outlet = title.rsplit(" - ", 1)
        items.append({"title": title.strip(), "url": link, "outlet_hint": outlet.strip(),
                      "publisher_domain": sources.get(link, ""),
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


def outlet_for(url, hint, outlets, publisher_domain=""):
    # publisher_domain comes from the feed and is authoritative when present;
    # the URL's host is only a fallback, and is news.google.com for anything
    # that arrived through a Google News search.
    host = (publisher_domain
            or urlparse(url).netloc.lower().replace("www.", ""))
    for o in outlets:
        if host == o["domain"] or host.endswith("." + o["domain"]):
            return o["name"], o["domain"], o["language"], o["priority"], True
    return (hint or host or "Unknown"), host, "", 3, False


def is_our_domain(host):
    host = (host or "").lower().replace("www.", "")
    return bool(host) and any(host == d or host.endswith("." + d) for d in UN_DOMAINS)


# Fallback for the rare item whose feed entry carries no <source url>: the
# publisher name Google gives us. Deliberately narrow — it must not swallow an
# outlet that merely reports on us, only pages we publish ourselves.
OWN_NAME_PATTERNS = [
    "united nations", "нэгдсэн үндэстн", "reliefweb",
    "unicef", "undp", "unfpa", "unesco", "unido", "unodc", "unhcr",
    "world health organization", "world food programme",
    "international organization for migration",
    "food and agriculture organization", "international labour organization",
    "un women", "un news", "un environment",
]


def is_our_publication(c):
    """True when we published this ourselves, rather than being written about."""
    if is_our_domain(c.get("publisher_domain") or c.get("outlet_domain")):
        return True
    if c.get("publisher_domain"):
        return False        # the feed told us the publisher; trust it over the name
    name = (c.get("outlet_hint") or c.get("outlet") or "").lower()
    return any(p in name for p in OWN_NAME_PATTERNS)


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
        name, domain, lang, prio, known = outlet_for(
            c["url"], c.get("outlet_hint"), outlets, c.get("publisher_domain"))
        c.update({"outlet": name, "outlet_domain": domain, "known_outlet": known,
                  "priority": prio})
        if lang:
            c["language"] = lang

    # Our own press releases are not media coverage of us. Google News indexes
    # undp.org and un.org alongside real outlets, and without this they crowd
    # out the actual mentions — 55 of 60 in the week of 10 August 2026.
    if MM.get("exclude_own_publications", True):
        ours = [c for c in filtered if is_our_publication(c)]
        filtered = [c for c in filtered if not is_our_publication(c)]
        if ours:
            by_name = {}
            for c in ours:
                by_name[c.get("outlet", "?")] = by_name.get(c.get("outlet", "?"), 0) + 1
            listed = ", ".join(f"{k} ({v})" for k, v in
                               sorted(by_name.items(), key=lambda kv: -kv[1])[:6])
            print(f"Excluded {len(ours)} item(s) published by us: {listed}")
        if not filtered:
            issues.append(
                "Every candidate this week was one of our own publications; the media "
                "page has no third-party coverage to show. Check the outlet list in "
                "sources/sources.xlsx, or set media_monitoring.exclude_own_publications "
                "to false to include our own pages again.")
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
            name, domain, lang, prio, known = outlet_for(
                final_url, c.get("outlet_hint"), outlets, c.get("publisher_domain"))
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
