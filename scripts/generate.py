"""
generate.py — turn raw_news.json into a structured, reviewed report.

Pipeline:
  1. DRAFT     — Claude writes the English report as structured JSON, sorted
                 into the four categories set in config.yaml.
  2. REVIEW    — a critic pass scores it 0-100 and lists issues; Claude
                 revises; repeat until the score reaches the threshold or
                 max_iterations is hit (the self-review loop).
  3. STYLE     — the deterministic UN Editorial Manual linter runs over the
                 English, and flags politically sensitive names for the human
                 reviewer instead of deciding them itself.
  4. TRANSLATE — the final English becomes Mongolian, using the office
                 glossary from the shared workbook.
  5. SAVE      — reports/<week_start>.json (source of truth for the site)

Needs the ANTHROPIC_API_KEY environment variable.
"""

import json
import os
import re
import sys
import pathlib
import datetime as dt

import yaml
from anthropic import Anthropic

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sources_loader as SL                          # noqa: E402
from un_style import lint_object, STYLE_PROMPT       # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
RAW = ROOT / "data" / "raw_news.json"

# This is a once-a-week cron run — losing the whole report to a transient
# "Overloaded" (529) or other 5xx from the API is expensive to lose, so retry
# harder than the SDK's default of 2 before giving up. The SDK already backs
# off between attempts.
client = Anthropic(max_retries=6)
MODEL = CONFIG["model"]
MAXTOK = CONFIG["max_output_tokens"]
# Translation is a mechanical, glossary-constrained task — a lighter model
# handles it well at a fraction of the cost. Falls back to MODEL if unset.
TRANSLATE_MODEL = CONFIG.get("translate_model") or MODEL
CATEGORIES = CONFIG["categories"]


# ---------- helpers ----------

def call(system, user, model=None):
    # A plain string system prompt is cached whole (ephemeral). The review loop
    # and multi-attempt retries reuse the same DRAFT/REVIEW system prompts within
    # a run, so a cache hit skips most of that input on the repeat calls. Pass a
    # pre-built list of blocks (see revise_system()) to control the cache split.
    system_param = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if isinstance(system, str) else system
    )
    msg = client.messages.create(
        model=model or MODEL, max_tokens=MAXTOK,
        system=system_param, messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def parse_json(text):
    """Pull a JSON object out of a model reply, tolerating stray prose/fences."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model reply. First 400 chars:\n" + text[:400])
    return json.loads(cleaned[start:end + 1])


def call_json(system, user, model=None, label="call"):
    """Call the model and parse its reply as JSON, retrying malformed replies.

    Attempt 1 is as given; attempt 2 nudges the model to reply with JSON only.
    From attempt 3 on, instead of regenerating from scratch (which tends to
    reproduce the same mistake, e.g. an unescaped quote inside an article
    snippet), the model is asked to repair the exact syntax error in what it
    already wrote — much likelier to succeed than a fresh roll of the dice.

    The repair only makes sense when there is something to repair: if the model
    replied with nothing at all, sending it an empty "Broken JSON:" block just
    earns a puzzled question back, so fall back to a plain re-ask instead.
    """
    json_only = ("\n\nIMPORTANT: reply with the JSON object ONLY. No preamble, "
                 "no explanation, no code fences.")
    last_err = last_text = None
    for attempt in range(3):
        repairable = bool(last_text and last_text.strip())
        if attempt >= 2 and repairable:
            sys_prompt = ("Fix ONLY the JSON syntax error below; do not change any wording, "
                          "facts, numbers, or structure. Return the corrected JSON object only.")
            prompt = f"Parse error: {last_err}\n\nBroken JSON:\n{last_text}"
        elif attempt == 0:
            sys_prompt, prompt = system, user
        else:
            sys_prompt = (system + json_only if isinstance(system, str)
                          else list(system) + [{"type": "text", "text": json_only}])
            prompt = user
        text = call(sys_prompt, prompt, model=model)
        try:
            return parse_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err, last_text = e, text
            print(f"  {label} attempt {attempt + 1}: could not parse JSON, retrying...")
    raise last_err


# ---------- prompts (tune these to change tone / structure) ----------

STRUCTURE_NOTE = f"""
CATEGORIES — every item goes in exactly one. Use only categories with real content
this week, in this order:
{json.dumps(CATEGORIES, ensure_ascii=False)}

What belongs where:
- "Economy and Development": macroeconomy, budget, currency, trade, mining and
  commodities, investment, infrastructure, employment, development finance and
  development cooperation projects.
- "Politics, Governance and Diplomacy": parliament and government, elections, law
  and justice, public administration, corruption, foreign policy, bilateral and
  multilateral relations, treaties and summits.
- "Environment and Climate Change": climate policy and finance, emissions, air
  quality, water, desertification and land degradation, biodiversity, mining's
  environmental dimension, disaster risk and preparedness.
- "Social and Humanitarian": dzud and its effects on herder households, food
  security, health, education, social protection, gender, migration and
  displacement, human rights, humanitarian response and needs.
If a story straddles two categories, choose the one carrying its main news value
and record the other as a secondary tag.

SECONDARY TAGS (0-2 per item, drawn only from this list, shown on the card):
{json.dumps(CONFIG.get('secondary_tags', []), ensure_ascii=False)}

UN AGENCIES for tagging each item (0-3 that would plausibly care):
{json.dumps(CONFIG['agencies'], ensure_ascii=False)}

Return ONLY valid JSON with this exact shape:
{{
  "week_start": "YYYY-MM-DD", "week_end": "YYYY-MM-DD",
  "insights": ["4-6 short bullet strings summarising the week"],
  "sections": [
    {{"category": "<one of the categories>",
      "items": [
        {{"text": "one curated paragraph, ~2-4 sentences, source named in-line",
          "source": "<outlet>", "url": "<link>",
          "agencies": ["WFP", "IOM"], "tags": ["Dzud"]}}
      ]}}
  ],
  "also_noted": [
    {{"text": "weaker/minor item, one sentence", "source": "<outlet>",
      "url": "<link>", "agencies": [], "tags": []}}
  ]
}}
"""

DRAFT_SYSTEM = f"""You are a senior analyst producing a weekly media brief on Mongolia for
the United Nations Resident Coordinator's Office in Mongolia. Audience: United Nations staff
needing situational awareness. The brief covers Mongolia broadly — it is not limited to
United Nations activity — and includes external developments that matter to Mongolia
(relations with China and the Russian Federation, coal and commodity prices, mining,
regional climate and dzud conditions).

Rules:
- Curate, don't dump. Group by category; write one tight paragraph per item in the measured,
  neutral register of an institutional brief. Always name the source outlet.
- Do NOT pad. If an item is weak, minor, or thinly sourced, put it in "also_noted", never in
  a main section. Better a short brief than a padded one.
- Never invent facts, numbers, quotes, or sources. If the material is thin, say less.
- Tag each item with the United Nations agencies most likely to care (0-3), for the reader's
  filter.
{STYLE_PROMPT}
{STRUCTURE_NOTE}"""

REVIEW_SYSTEM = f"""You are a rigorous editor reviewing a United Nations media brief before
human sign-off. Check: (1) every item names a source; (2) nothing looks invented or
exaggerated beyond the inputs; (3) weak or minor items are in also_noted, not main sections;
(4) neutral institutional tone; (5) every item sits in the right category, and the category
name is exactly one of {json.dumps(CATEGORIES, ensure_ascii=False)}; (6) agency tags are
plausible; (7) no padding; (8) UN Editorial Manual style is respected — UN short-form country
names, "14 July 2026" dates, "per cent", British spellings with -ize endings.
Return ONLY JSON: {{"confidence": <0-100>, "issues": ["specific, actionable fixes"]}}.
Score honestly; 90+ means genuinely ready for a human reviewer."""

REVISE_SUFFIX = "\nRevise the draft to resolve the listed issues. Return the full corrected JSON only."


def revise_system():
    # Two blocks so the DRAFT_SYSTEM portion re-hits the cache entry written by
    # draft()'s call, instead of caching "DRAFT_SYSTEM + suffix" as a separate blob.
    return [
        {"type": "text", "text": DRAFT_SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": REVISE_SUFFIX},
    ]


def translate_system(glossary):
    gloss = ""
    if glossary:
        pairs = "\n".join(f"  {k} = {v}" for k, v in list(glossary.items())[:80])
        gloss = ("\nUse this office glossary verbatim; do not re-translate these terms:\n"
                 + pairs)
    return ("You are a professional English-to-Mongolian (Cyrillic) translator working for a "
            "United Nations office. Translate every human-readable string (insights, category "
            "names, item text) into natural, formal Mongolian. Keep source names, URLs, agency "
            "acronyms, tags and the JSON structure and keys unchanged. Return ONLY the "
            "translated JSON with the same shape." + gloss)


# ---------- pipeline ----------

def draft(raw):
    user = "Here are this week's candidate articles as JSON:\n\n" + json.dumps(raw, ensure_ascii=False)
    return call_json(DRAFT_SYSTEM, user, label="draft")


# Formats seen from the model despite the prompt asking for YYYY-MM-DD, so a
# stray one doesn't silently corrupt the output filename and break the site
# build (which indexes reports by this field).
_DATE_FALLBACKS = ("%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d/%m/%Y")


def normalise_week_dates(report):
    for key in ("week_start", "week_end"):
        value = report.get(key)
        if not value:
            continue
        try:
            dt.date.fromisoformat(value)
            continue
        except ValueError:
            pass
        for fmt in _DATE_FALLBACKS:
            try:
                report[key] = dt.datetime.strptime(value, fmt).date().isoformat()
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"could not parse '{key}': {value!r} into an ISO date")
    return report


def review_loop(report):
    threshold = CONFIG["review"]["confidence_threshold"]
    log = []
    for i in range(1, CONFIG["review"]["max_iterations"] + 1):
        # A failed critique or revision must not cost us the whole report: the
        # draft in hand is already publishable, the review only refines it.
        try:
            verdict = call_json(REVIEW_SYSTEM, json.dumps(report, ensure_ascii=False),
                                label="review")
        except Exception as e:
            print(f"  ! review pass {i} failed ({type(e).__name__}); keeping the draft as it is")
            break
        score = int(verdict.get("confidence", 0))
        issues = verdict.get("issues", [])
        print(f"  review pass {i}: confidence={score}")
        for issue in issues:
            print(f"    - {issue}")
        log.append({"pass": i, "confidence": score, "issues": issues})
        report["confidence"] = score
        if score >= threshold or not issues:
            break
        fix = ("Draft JSON:\n" + json.dumps(report, ensure_ascii=False) +
               "\n\nIssues to fix:\n" + json.dumps(issues, ensure_ascii=False))
        try:
            report = call_json(revise_system(), fix, label="revise")
        except Exception as e:
            print(f"  ! revision failed ({type(e).__name__}); keeping the previous draft")
            break
        report["confidence"] = score
    report["review_log"] = log
    return report


# Safety net: the prompt asks for the four categories, but if the model returns a
# near-miss or an old theme name we route it rather than dumping it in Also noted.
FALLBACK_MAP = {
    "humanitarian": "Social and Humanitarian",
    "protection": "Social and Humanitarian",
    "health": "Social and Humanitarian",
    "social": "Social and Humanitarian",
    "human rights": "Social and Humanitarian",
    "education": "Social and Humanitarian",
    "migration": "Social and Humanitarian",
    "politics": "Politics, Governance and Diplomacy",
    "governance": "Politics, Governance and Diplomacy",
    "diplomacy": "Politics, Governance and Diplomacy",
    "foreign": "Politics, Governance and Diplomacy",
    "regional": "Politics, Governance and Diplomacy",
    "security": "Politics, Governance and Diplomacy",
    "economy": "Economy and Development",
    "economic": "Economy and Development",
    "development": "Economy and Development",
    "trade": "Economy and Development",
    "mining": "Economy and Development",
    "finance": "Economy and Development",
    "climate": "Environment and Climate Change",
    "environment": "Environment and Climate Change",
    "energy": "Environment and Climate Change",
    "disaster": "Environment and Climate Change",
}


def normalise_categories(report):
    """Route anything the model wrote outside the four categories."""
    fixed, unknown = [], []
    for s in report.get("sections", []):
        name = (s.get("category") or s.get("theme") or "").strip()
        low = name.lower()
        match = next((c for c in CATEGORIES if c.lower() == low), None)
        if not match:
            match = next((c for c in CATEGORIES if low and low in c.lower()), None)
        if not match:
            for key, target in FALLBACK_MAP.items():
                if key in low:
                    match = target
                    unknown.append(f"{name} -> {target}")
                    break
        if match:
            s["category"] = match
            fixed.append(s)
        else:
            unknown.append(f"{name or '(unnamed)'} -> Also noted")
            report.setdefault("also_noted", []).extend(s.get("items", []))
    # merge duplicates and apply the configured display order
    merged = {}
    for s in fixed:
        merged.setdefault(s["category"], {"category": s["category"], "items": []})
        merged[s["category"]]["items"] += s.get("items", [])
    report["sections"] = [merged[c] for c in CATEGORIES if c in merged]
    return report, unknown


def translate(report_en, glossary):
    # Only the fields that actually need translating go to the model. Sending
    # review_log/issues/style_flags too would bloat the prompt with prose the
    # translator doesn't need, and risks the reply being cut off mid-JSON.
    payload = {
        "week_start": report_en["week_start"], "week_end": report_en["week_end"],
        "insights": report_en["insights"],
        "sections": [{"category": s["category"], "items": s["items"]}
                     for s in report_en.get("sections", [])],
        "also_noted": report_en.get("also_noted", []),
    }
    user = json.dumps(payload, ensure_ascii=False)

    mn, last_err = None, None
    try:
        mn = call_json(translate_system(glossary), user, model=TRANSLATE_MODEL, label="translate")
    except (ValueError, json.JSONDecodeError) as e:
        last_err = e

    if mn is None:
        # Don't lose an entire week's drafting + review over a translation hiccup:
        # publish English in both tabs and flag it, rather than crash the run.
        print(f"  translation failed after retries ({last_err}); publishing English "
              f"text in the MN tab too this week.")
        mn = payload
        report_en.setdefault("issues", []).append(
            "Mongolian translation failed this week ({}); the MN tab currently shows "
            "English text. Re-run scripts/generate.py, or translate the 'en' fields "
            "manually in this PR.".format(type(last_err).__name__ if last_err else "unknown error"))

    out = {
        "week_start": report_en["week_start"], "week_end": report_en["week_end"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "confidence": report_en.get("confidence", 0),
        "insights": [{"en": e, "mn": m} for e, m in
                     zip(report_en["insights"], mn.get("insights", report_en["insights"]))],
        "sections": [], "also_noted": [],
        "style_flags": report_en.get("style_flags", []),
        "issues": report_en.get("issues", []),
        "review_log": report_en.get("review_log", []),
    }
    for s_en, s_mn in zip(report_en["sections"], mn.get("sections", report_en["sections"])):
        out["sections"].append({
            "category": s_en["category"],
            "category_mn": s_mn.get("category", s_en["category"]),
            "items": [{"en": ie["text"], "mn": im.get("text", ie["text"]),
                       "source": ie.get("source", ""), "url": ie.get("url", ""),
                       "agencies": ie.get("agencies", []), "tags": ie.get("tags", [])}
                      for ie, im in zip(s_en["items"], s_mn.get("items", s_en["items"]))],
        })
    for ie, im in zip(report_en.get("also_noted", []),
                      mn.get("also_noted", report_en.get("also_noted", []))):
        out["also_noted"].append({"en": ie.get("text", ""), "mn": im.get("text", ie.get("text", "")),
                                  "source": ie.get("source", ""), "url": ie.get("url", ""),
                                  "agencies": ie.get("agencies", []), "tags": ie.get("tags", [])})
    return out


def main():
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    n = raw.get("count", 0)
    print(f"Generating report from {n} items...")
    if n == 0:
        print("No news collected this run; skipping report generation (no PR will open).")
        return

    cap = CONFIG.get("max_items_to_model", 120)
    items = raw.get("items", [])[:cap]
    for it in items:
        it["snippet"] = (it.get("snippet") or "")[:300]
    raw["items"] = items
    raw["count"] = len(items)
    print(f"  using {len(items)} items after trimming")

    report_en = draft(raw)
    report_en = normalise_week_dates(report_en)
    report_en = review_loop(report_en)
    report_en, unknown = normalise_categories(report_en)
    if unknown:
        print(f"  moved items from unrecognised categor(ies) to Also noted: {unknown}")

    data, wb_issues = SL.load_workbook_data()
    report_en["issues"] = list(raw.get("issues", [])) + wb_issues
    if unknown:
        report_en["issues"].append(
            "the model produced categor(ies) outside the four configured, rerouted as: "
            + "; ".join(unknown))

    if CONFIG.get("style", {}).get("linter", True):
        report_en, changes, flags = lint_object(
            report_en, protected=SL.protected_names(data),
            flag_terms=CONFIG.get("style", {}).get("flag_only_terms", []))
        report_en["style_flags"] = [{"term": t, "context": c} for t, c in flags]
        print(f"  style linter applied {len(changes)} correction(s), "
              f"{len(flags)} term(s) flagged for review")

    report = translate(report_en, SL.glossary(data))

    out_path = ROOT / "reports" / f"{report['week_start']}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {out_path.relative_to(ROOT)} (confidence {report['confidence']})")


if __name__ == "__main__":
    main()
