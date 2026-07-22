"""
match_sources.py — work out which of our publications an outlet was citing.

Two tiers, deliberately kept apart because they are not equally trustworthy:

  Tier A — the article contains a link to a United Nations domain. That is
           evidence, not inference: confidence 100.
  Tier B — no link. We score every publication in the index against the
           article (distinctive shared words, shared figures, date proximity,
           agency match) and keep the best few. This is a probability, and it
           is labelled as one. Below media_monitoring.match_min_confidence the
           mention is shown as "source not identified" rather than guessed.

Tier B candidates are handed to the model in generate_mentions.py for a final
judgement; this file does the cheap deterministic narrowing so the model only
ever sees three options instead of several hundred.

No network, no API key. Import it, or run it to see what it would match.
"""

import json
import math
import pathlib
import re
import datetime as dt
from urllib.parse import urlparse, urlunparse

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
MM = CONFIG.get("media_monitoring", {})
INDEX = ROOT / "index" / "un_publications.json"

STOP = set("""the a an and or of in on at to for from with by as is are was were be been
this that these those it its his her their our your they we you he she i not no but if
then than so such which who whom what when where how also more most other some any all
said says say told according report reports reported new news year years month months
week weeks day days percent per cent united nations un mongolia mongolian ulaanbaatar
""".split())

NUM_RE = re.compile(r"\b\d[\d,.]*\b")
WORD_RE = re.compile(r"[A-Za-z\u0400-\u04FF]{4,}")


def load_index():
    if not INDEX.exists():
        return []
    try:
        return json.loads(INDEX.read_text(encoding="utf-8")).get("publications", [])
    except Exception:
        return []


def norm_url(u):
    try:
        p = urlparse(u.lower())
        path = p.path.rstrip("/")
        return urlunparse(("https", p.netloc.replace("www.", ""), path, "", "", ""))
    except Exception:
        return (u or "").lower().rstrip("/")


def tokens(text):
    return [w.lower() for w in WORD_RE.findall(text or "") if w.lower() not in STOP]


def numbers(text):
    out = set()
    for n in NUM_RE.findall(text or ""):
        clean = n.replace(",", "").rstrip(".")
        if len(clean.replace(".", "")) >= 2:   # ignore 1-digit noise
            out.add(clean)
    return out


def build_idf(pubs):
    df, n = {}, max(1, len(pubs))
    for p in pubs:
        for t in set(tokens(p.get("title", "") + " " + p.get("summary", ""))):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1 + n / c) for t, c in df.items()}, n


def date_gap(article_date, pub_date):
    try:
        a = dt.date.fromisoformat((article_date or "")[:10])
        p = dt.date.fromisoformat((pub_date or "")[:10])
    except Exception:
        return None
    return (a - p).days


def score(mention, pub, idf):
    """0-100 deterministic plausibility that `pub` is what `mention` cites."""
    art_text = " ".join([mention.get("title", ""), mention.get("snippet", ""),
                         mention.get("body", "")[:3000]])
    art_tokens = set(tokens(art_text))
    pub_tokens = set(tokens(pub.get("title", "") + " " + pub.get("summary", "")))
    if not pub_tokens:
        return 0, []

    shared = art_tokens & pub_tokens
    weight = sum(idf.get(t, 1.0) for t in shared)
    max_weight = sum(idf.get(t, 1.0) for t in pub_tokens) or 1.0
    lexical = min(1.0, weight / max_weight)          # 0-1

    shared_nums = numbers(art_text) & numbers(pub.get("title", "") + " " + pub.get("summary", ""))
    numeric = min(1.0, len(shared_nums) / 2.0)       # two shared figures is strong

    gap = date_gap(mention.get("published"), pub.get("date"))
    if gap is None:
        temporal = 0.3
    elif gap < -2:
        temporal = 0.0                               # published after the article
    elif gap <= 14:
        temporal = 1.0
    elif gap <= 45:
        temporal = 0.6
    elif gap <= 120:
        temporal = 0.3
    else:
        temporal = 0.1

    agency = 0.0
    ag = (pub.get("agency") or "").lower()
    if ag and ag in art_text.lower():
        agency = 1.0
    elif any(a.lower() in art_text.lower() for a in CONFIG.get("agencies", [])):
        agency = 0.4

    raw = 0.45 * lexical + 0.25 * numeric + 0.20 * temporal + 0.10 * agency
    evidence = []
    if shared:
        top = sorted(shared, key=lambda t: -idf.get(t, 0))[:6]
        evidence.append("shared wording: " + ", ".join(top))
    if shared_nums:
        evidence.append("shared figures: " + ", ".join(sorted(shared_nums)[:4]))
    if gap is not None and 0 <= gap <= 14:
        evidence.append(f"published {gap} day(s) before the article")
    return int(round(raw * 100)), evidence


def match(mention, pubs=None, idf=None, top_n=None):
    """Return a dict describing the best available source attribution."""
    pubs = load_index() if pubs is None else pubs
    top_n = top_n or int(MM.get("match_candidates", 3))
    by_url = {norm_url(p["url"]): p for p in pubs}

    # ---- Tier A: an explicit link in the article body.
    for link in mention.get("un_links", []) or []:
        key = norm_url(link)
        hit = by_url.get(key)
        if hit:
            return {"matched": True, "method": "explicit_link", "confidence": 100,
                    "title": hit.get("title", ""), "url": hit["url"],
                    "date": hit.get("date", ""), "agency": hit.get("agency", ""),
                    "evidence": ["the article links directly to this page"],
                    "candidates": []}
    if mention.get("un_links"):
        link = mention["un_links"][0]
        return {"matched": True, "method": "explicit_link", "confidence": 95,
                "title": "", "url": link, "date": "",
                "agency": urlparse(link).netloc.replace("www.", ""),
                "evidence": ["the article links to this United Nations page "
                             "(not yet in our index)"],
                "candidates": []}

    # ---- Tier B: inference.
    if not pubs:
        return {"matched": False, "method": "none", "confidence": 0,
                "evidence": ["no publications indexed yet"], "candidates": []}
    if idf is None:
        idf, _ = build_idf(pubs)

    scored = []
    for p in pubs:
        s, ev = score(mention, p, idf)
        if s > 0:
            scored.append({"score": s, "evidence": ev, "title": p.get("title", ""),
                           "url": p["url"], "date": p.get("date", ""),
                           "agency": p.get("agency", "")})
    scored.sort(key=lambda c: -c["score"])
    top = scored[:top_n]
    if not top:
        return {"matched": False, "method": "none", "confidence": 0,
                "evidence": [], "candidates": []}

    best = top[0]
    threshold = int(MM.get("match_min_confidence", 60))
    return {"matched": best["score"] >= threshold,
            "method": "inferred" if best["score"] >= threshold else "none",
            "confidence": best["score"], "title": best["title"], "url": best["url"],
            "date": best["date"], "agency": best["agency"],
            "evidence": best["evidence"], "candidates": top}


def match_all(mentions):
    pubs = load_index()
    idf, _ = build_idf(pubs) if pubs else ({}, 0)
    for m in mentions:
        m["un_source"] = match(m, pubs, idf)
    return mentions


if __name__ == "__main__":
    raw = ROOT / "data" / "raw_mentions.json"
    if not raw.exists():
        print("Run scripts/collect_mentions.py first.")
    else:
        d = json.loads(raw.read_text(encoding="utf-8"))
        for m in match_all(d.get("mentions", []))[:20]:
            s = m["un_source"]
            print(f"{m.get('outlet','?'):22s} {s['method']:14s} {s['confidence']:3d}  "
                  f"{(s.get('title') or s.get('url') or '')[:70]}")
