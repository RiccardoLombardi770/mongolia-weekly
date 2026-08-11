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

# A plain tool user-agent is refused outright by several news sites' front-end
# protection, and Google's link-resolution endpoint expects a browser. These are
# ordinary public pages; the header only keeps us from being turned away.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
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


def full_name_terms(roster):
    """Roster terms specific enough to identify a person on their own.

    A bare surname is not: "Шарифи" pulled in eleven articles about unrelated
    Tajik and Iranian people, and because a roster match waives the Mongolia
    requirement, every one of them was collected as Mongolian media coverage.
    Only a term with at least two words earns that waiver.
    """
    return [t for t in roster_terms(roster) if len(t.split()) >= 2]


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
    # Google throttles a burst of searches with 429/503. Backing off costs a few
    # seconds; not backing off costs the week — every query failing at once
    # produces a page that says nobody mentioned us.
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code in (429, 503):
                last = f"{r.status_code} {r.reason}"
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    else:
        return [], f"[search] '{query}' failed after 3 attempts: {last}"
    if r.status_code != 200:
        return [], f"[search] '{query}' failed: {last}"
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


GOOGLE_DECODE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def google_real_url(wrapped_url, html):
    """Turn a news.google.com/rss/articles/... link into the publisher's URL.

    These links do not redirect over HTTP — the hop is done in JavaScript, so
    following them just lands back on news.google.com and what we downloaded is
    Google's own page. For a year that is exactly what happened: every article
    body was the 11-character string "Google News", and the tone of every
    mention was judged from the search snippet alone.

    The page carries the signature and timestamp needed to ask Google for the
    destination, which is what the browser itself does. Returns "" on any
    failure — this is best-effort, and the caller carries on with the snippet.
    """
    sg = re.search(r'data-n-a-sg="([^"]+)"', html or "")
    ts = re.search(r'data-n-a-ts="([^"]+)"', html or "")
    if not (sg and ts):
        return ""
    article_id = wrapped_url.rstrip("/").split("/")[-1].split("?")[0]
    inner = json.dumps([
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
          None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        article_id, int(ts.group(1)), sg.group(1),
    ])
    payload = [[["Fbv4je", inner, None, "generic"]]]
    try:
        r = requests.post(
            GOOGLE_DECODE,
            data="f.req=" + urllib.parse.quote(json.dumps(payload)),
            headers={**HEADERS,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception:
        return ""
    # The reply is Google's ")]}'" prelude followed by JSON whose payload is
    # itself a JSON string: ["garturlres","https://…",1]
    m = re.search(r'garturlres\\",\\"(https?://[^\\"]+)', r.text)
    return m.group(1) if m else ""


def resolve(url):
    """Follow a link to the publisher, decoding Google News wrappers."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return url, None
    if "news.google.com" not in urlparse(r.url).netloc:
        return r.url, r
    real = google_real_url(url, getattr(r, "text", ""))
    if not real:
        return r.url, r
    try:
        r2 = requests.get(real, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        # A publisher that refuses us leaves nothing better than the wrapper, but
        # the real URL is still worth recording — it names the outlet.
        return (r2.url, r2) if getattr(r2, "text", "") else (real, None)
    except Exception:
        return real, None


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


# Searching "UNDP Mongolia" also returns pieces about UNDP somewhere else
# entirely; nothing required the article to concern Mongolia at all, so Tehran
# Times, Guardian Nigeria and Armenpress arrived as Mongolian media mentions.
MONGOLIA_TERMS = [t.lower() for t in CONFIG.get("mongolia_terms", [])] + [
    "монгол", "улаанбаатар", "монголия",
]


def about_mongolia(text):
    low = (text or "").lower()
    return any(t in low for t in MONGOLIA_TERMS)


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


STOPWORDS = {"the", "a", "an", "of", "for", "and", "in", "on", "to", "with", "at",
             "by", "from", "as", "is", "are", "its", "new", "un", "united", "nations"}


def title_key(title):
    """A headline reduced to its distinctive words, for spotting syndication.

    "UN and Mongolia mark 50 years of partnership" and "Mongolia, UN mark 50
    years of partnership" are the same story; comparing the significant words
    catches that, while staying blunt enough not to merge unrelated pieces.
    """
    words = re.findall(r"[\wЀ-ӿ]+", (title or "").lower())
    keep = [w for w in words if w not in STOPWORDS and len(w) > 2]
    return " ".join(sorted(keep)[:8]) if len(keep) >= 3 else ""


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


def report_fetch_quality(items, issues):
    """Say plainly whether we are reading articles or just search snippets.

    The tone judgement is only as good as the text behind it. If a link never
    leaves news.google.com, what we downloaded is Google's interstitial page,
    and the model ends up judging a 400-character snippet as though it were the
    article. That failure is invisible in the output, so it is counted here.
    """
    if not items:
        return
    stuck = [c for c in items if "news.google.com" in (c.get("url") or "")]
    real = [c for c in items if len(c.get("body") or "") > 1000]
    thin = [c for c in items if 0 < len(c.get("body") or "") <= 1000]
    empty = [c for c in items if not (c.get("body") or "")]
    lengths = sorted(len(c.get("body") or "") for c in items)
    median = lengths[len(lengths) // 2] if lengths else 0

    print("\nFetch quality:")
    print(f"  {len(real)} full article(s) (>1000 chars) | {len(thin)} thin (<=1000) | "
          f"{len(empty)} empty | median {median:,} chars")
    if stuck:
        print(f"  ! {len(stuck)} link(s) never left news.google.com — for these the "
              f"model sees the search snippet, not the article")
        issues.append(
            f"{len(stuck)} of {len(items)} article(s) could not be followed past Google "
            f"News, so their tone and summary rest on a short search snippet rather than "
            f"the full text.")
    if len(real) < len(items) / 2:
        issues.append(
            f"Only {len(real)} of {len(items)} article(s) yielded full text this week; "
            f"tone judgements on the rest are based on very little.")


def main():
    data, issues = SL.load_workbook_data()
    outlets = SL.outlets(data)
    roster = SL.roster(data)
    days = CONFIG.get("lookback_days", 7)

    terms = CORE_TERMS + agency_terms() + roster_terms(roster)

    # Queries: generic UN-in-Mongolia, one per agency, one per named official.
    # Broad enough to catch coverage that names us without naming an agency, and
    # thematic in Mongolian — most local reporting never uses the Latin acronym,
    # and searching the acronym alone missed whole subject areas.
    queries = [("\"United Nations\" Mongolia", "en"),
               ("UN Resident Coordinator Mongolia", "en"),
               ("United Nations Mongolia report", "en"),
               ("НҮБ Монгол", "mn"),
               ("НҮБ-ын суурин зохицуулагч", "mn"),
               ("НҮБ хүүхэд", "mn"),            # children
               ("НҮБ эрүүл мэнд", "mn"),        # health
               ("НҮБ боловсрол", "mn"),         # education
               ("НҮБ уур амьсгал", "mn"),       # climate
               ("НҮБ жендэр", "mn"),            # gender
               ("НҮБ хөгжлийн хөтөлбөр", "mn"), # UNDP, spelled out
               ("НҮБ төсөл Монгол", "mn")]      # UN project(s) in Mongolia
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
    search_failures = 0
    for q, lang in queries:
        items, err = google_news(q, lang)
        if err:
            issues.append(err)
            search_failures += 1
        candidates += items
        time.sleep(1)
    if search_failures:
        print(f"  ! {search_failures} of {len(queries)} search(es) failed")

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
    # The same press release is routinely syndicated under several URLs and
    # slightly different headlines; an exact-match key let those through as
    # separate mentions, which the reviewer flagged week after week.
    seen, seen_titles, filtered = set(), set(), []
    dropped_offtopic = []
    full_names = full_name_terms(roster)
    for c in candidates:
        key = (c.get("url") or c.get("title", "")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        tkey = title_key(c.get("title", ""))
        if tkey and tkey in seen_titles:
            continue
        if tkey:
            seen_titles.add(tkey)
        if not within_window(c, days):
            continue
        blob = c["title"] + " " + c.get("snippet", "")
        if not mentions_un(blob, terms):
            continue
        # A named official is reason enough on its own — a profile of the
        # Resident Coordinator need not repeat the country's name. Full names
        # only: a surname alone matches too many unrelated people.
        if MM.get("require_mongolia", True) and not about_mongolia(blob):
            if not mentions_un(blob, full_names):
                dropped_offtopic.append(c)
                continue
        filtered.append(c)

    if dropped_offtopic:
        print(f"Dropped {len(dropped_offtopic)} item(s) that mention the United Nations "
              f"but not Mongolia")

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

        report_fetch_quality(filtered, issues)

    # Drop anything whose full text turned out not to mention us at all.
    keep = [c for c in filtered
            if c.get("matched_terms") or not c.get("fetch_ok", True)]

    # "We searched and found nothing" and "we could not search" look identical
    # in the output, and the page would state that nobody mentioned us. Refuse
    # to produce that: stopping here leaves last week's page in place, and the
    # Monday email already reports a media page that did not update.
    if not keep and search_failures > len(queries) / 2:
        print(f"\nSTOPPING: {search_failures} of {len(queries)} searches failed and "
              f"nothing was collected. This is a search outage, not a quiet week — "
              f"writing an empty week would state something untrue. Re-run later.")
        sys.exit(1)

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
