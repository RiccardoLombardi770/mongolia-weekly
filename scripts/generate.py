"""
generate.py — turn raw_news.json into a structured, reviewed report.

Pipeline:
  1. DRAFT   — Claude writes the English report as structured JSON.
  2. REVIEW  — a critic pass scores it 0-100 and lists issues;
               Claude revises; repeat until score >= threshold
               or max_iterations is reached (the self-review loop).
  3. TRANSLATE — the final English is translated into Mongolian.
  4. SAVE    — reports/<week_start>.json  (source of truth for the site)

Needs the ANTHROPIC_API_KEY environment variable.
Run locally:   python scripts/generate.py
"""

import json
import os
import re
import pathlib
import datetime as dt

import yaml
from anthropic import Anthropic

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
RAW = ROOT / "data" / "raw_news.json"

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
MODEL = CONFIG["model"]
MAXTOK = CONFIG["max_output_tokens"]


# ---------- helpers ----------

def call(system, user):
    msg = client.messages.create(
        model=MODEL, max_tokens=MAXTOK,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def parse_json(text):
    """Pull a JSON object out of a model reply, tolerating stray prose/fences."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model reply. First 400 chars:\n" + text[:400])
    return json.loads(cleaned[start:end + 1])


# ---------- prompts (tune these to change tone / structure) ----------

STRUCTURE_NOTE = f"""
Themes (use only those with real content this week, in this order):
{json.dumps(CONFIG['themes'], ensure_ascii=False)}

UN agencies for tagging each item (0-3 that would plausibly care):
{json.dumps(CONFIG['agencies'], ensure_ascii=False)}

Return ONLY valid JSON with this exact shape:
{{
  "week_start": "YYYY-MM-DD", "week_end": "YYYY-MM-DD",
  "insights": ["4-6 short bullet strings summarising the week"],
  "sections": [
    {{"theme": "<one of the themes>",
      "items": [
        {{"text": "one curated paragraph, ~2-4 sentences, source named in-line",
          "source": "<outlet>", "url": "<link>",
          "agencies": ["WFP", "IOM"]}}
      ]}}
  ],
  "also_noted": [
    {{"text": "weaker/minor item, one sentence", "source": "<outlet>",
      "url": "<link>", "agencies": []}}
  ]
}}
"""

DRAFT_SYSTEM = f"""You are a senior analyst producing a weekly media-insights brief for
the UN Resident Coordinator's Office in Mongolia. Audience: UN staff needing situational
awareness. Cover Mongolia domestically PLUS external developments that matter to Mongolia
(China/Russia ties, coal/commodity prices, mining, regional climate/dzud).

Rules:
- Curate, don't dump. Group by theme; write one tight paragraph per item in the measured,
  neutral register of an institutional brief. Always name the source outlet.
- Do NOT pad. If an item is weak, minor, or thinly sourced, put it in "also_noted", never in
  a main section. Better a short brief than a padded one.
- Never invent facts, numbers, quotes, or sources. If the material is thin, say less.
- Tag each item with the UN agencies most likely to care (0-3), for the reader's filter.
{STRUCTURE_NOTE}"""

REVIEW_SYSTEM = """You are a rigorous editor reviewing a UN media brief before human sign-off.
Check: (1) every item names a source; (2) nothing looks invented or exaggerated beyond the
inputs; (3) weak/minor items are in also_noted, not main sections; (4) neutral institutional
tone; (5) agency tags are plausible; (6) no padding.
Return ONLY JSON: {"confidence": <0-100>, "issues": ["specific, actionable fixes"]}.
Score honestly; 90+ means genuinely ready for a human reviewer."""

REVISE_SYSTEM = DRAFT_SYSTEM + "\nRevise the draft to resolve the listed issues. Return the full corrected JSON only."

TRANSLATE_SYSTEM = """You are a professional English-to-Mongolian (Cyrillic) translator.
Translate every human-readable string (insights, theme names, item text) into natural
Mongolian. Keep source names, URLs, agency codes and the JSON structure/keys unchanged.
Return ONLY the translated JSON with the same shape."""


# ---------- pipeline ----------

def draft(raw):
    user = "Here are this week's candidate articles as JSON:\n\n" + json.dumps(raw, ensure_ascii=False)
    last_err = None
    for attempt in range(2):
        system = DRAFT_SYSTEM
        if attempt == 1:
            system += "\n\nIMPORTANT: reply with the JSON object ONLY. No preamble, no explanation, no code fences."
        try:
            return parse_json(call(system, user))
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            print(f"  draft attempt {attempt + 1}: could not parse JSON, retrying...")
    raise last_err


def review_loop(report):
    threshold = CONFIG["review"]["confidence_threshold"]
    for i in range(1, CONFIG["review"]["max_iterations"] + 1):
        verdict = parse_json(call(REVIEW_SYSTEM, json.dumps(report, ensure_ascii=False)))
        score = int(verdict.get("confidence", 0))
        issues = verdict.get("issues", [])
        print(f"  review pass {i}: confidence={score}")
        report["confidence"] = score
        if score >= threshold or not issues:
            break
        fix = ("Draft JSON:\n" + json.dumps(report, ensure_ascii=False) +
               "\n\nIssues to fix:\n" + json.dumps(issues, ensure_ascii=False))
        report = parse_json(call(REVISE_SYSTEM, fix))
        report["confidence"] = score
    return report


def translate(report_en):
    mn = parse_json(call(TRANSLATE_SYSTEM, json.dumps(report_en, ensure_ascii=False)))
    # Merge EN + MN into one bilingual document the site can toggle.
    out = {
        "week_start": report_en["week_start"], "week_end": report_en["week_end"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "confidence": report_en.get("confidence", 0),
        "insights": [{"en": e, "mn": m} for e, m in
                     zip(report_en["insights"], mn.get("insights", report_en["insights"]))],
        "sections": [], "also_noted": [],
    }
    for s_en, s_mn in zip(report_en["sections"], mn.get("sections", report_en["sections"])):
        out["sections"].append({
            "theme": s_en["theme"], "theme_mn": s_mn.get("theme", s_en["theme"]),
            "items": [{"en": ie["text"], "mn": im.get("text", ie["text"]),
                       "source": ie["source"], "url": ie["url"], "agencies": ie.get("agencies", [])}
                      for ie, im in zip(s_en["items"], s_mn.get("items", s_en["items"]))],
        })
    for ie, im in zip(report_en.get("also_noted", []),
                      mn.get("also_noted", report_en.get("also_noted", []))):
        out["also_noted"].append({"en": ie["text"], "mn": im.get("text", ie["text"]),
                                  "source": ie["source"], "url": ie["url"],
                                  "agencies": ie.get("agencies", [])})
    return out


def main():
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    n = raw.get("count", 0)
    print(f"Generating report from {n} items...")
    if n == 0:
        print("No news collected this run; skipping report generation (no PR will open).")
        return

    # Trim to keep input manageable, cheaper, and reliable.
    cap = CONFIG.get("max_items_to_model", 120)
    items = raw.get("items", [])[:cap]
    for it in items:
        it["snippet"] = (it.get("snippet") or "")[:300]
    raw["items"] = items
    raw["count"] = len(items)
    print(f"  using {len(items)} items after trimming")

    report_en = draft(raw)
    report_en = review_loop(report_en)
    report = translate(report_en)

    out_path = ROOT / "reports" / f"{report['week_start']}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {out_path.relative_to(ROOT)} (confidence {report['confidence']})")


if __name__ == "__main__":
    main()
