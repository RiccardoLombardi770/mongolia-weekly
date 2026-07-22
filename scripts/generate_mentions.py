"""
generate_mentions.py — turn raw mentions into the "In the Media" record.

Pipeline:
  1. MATCH     — deterministic narrowing (match_sources.py): an explicit link
                 is accepted as certain; otherwise the top three candidate
                 publications are carried forward.
  2. CLASSIFY  — Claude reads each article and returns tone, mention type,
                 prominence, agencies involved, a one-sentence summary, and —
                 for the inferred cases only — which candidate publication is
                 being cited, with its own confidence and reasoning.
  3. REVIEW    — a critic pass scores the batch 0-100; below threshold it is
                 revised, up to max_iterations.
  4. STYLE     — the UN Editorial Manual linter runs over the English.
  5. TRANSLATE — Mongolian, using the office glossary verbatim.
  6. SAVE      — mentions/<week_start>.json

Needs ANTHROPIC_API_KEY.
"""

import json
import os
import pathlib
import re
import sys
import datetime as dt

import yaml
from anthropic import Anthropic

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sources_loader as SL          # noqa: E402
import match_sources as MS           # noqa: E402
from un_style import lint_object, STYLE_PROMPT   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
MM = CONFIG.get("media_monitoring", {})
RAW = ROOT / "data" / "raw_mentions.json"

client = Anthropic()
MODEL = CONFIG["model"]
MAXTOK = CONFIG["max_output_tokens"]

TONES = MM.get("tone_labels", ["Supportive", "Neutral", "Critical"])
TYPES = MM.get("mention_types", [])
PROMINENCE = MM.get("prominence_levels", [])


def call(system, user):
    msg = client.messages.create(model=MODEL, max_tokens=MAXTOK, system=system,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in msg.content if b.type == "text")


def parse_json(text):
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model reply. First 400 chars:\n" + text[:400])
    return json.loads(cleaned[start:end + 1])


CLASSIFY_SYSTEM = f"""You analyse how the media in Mongolia and abroad refer to the
United Nations, for the UN Resident Coordinator's Office. You are describing coverage,
not judging journalists, and your output is published on a public page: stay descriptive
and professional throughout.

For every article you receive, return:
- summary: one or two sentences, in English, saying what the article is about and how the
  United Nations appears in it.
- tone: exactly one of {json.dumps(TONES)}. Judge the tone TOWARDS the United Nations
  only, not the tone of the article overall. A grim article about winter losses that
  reports our figures neutrally is "Neutral", not "Critical". Reserve "Critical" for
  explicit criticism, doubt cast on our work, or a negative framing of our role.
- tone_note: one short clause explaining the tone judgement, in neutral language
  ("reports our figures without comment", "questions the pace of the response").
- mention_type: exactly one of {json.dumps(TYPES)}.
- prominence: exactly one of {json.dumps(PROMINENCE)} — where in the article we appear.
- agencies: which United Nations entities are actually named (0-3, use the acronyms).
- officials: any United Nations official named in the article, as written.
- source_choice: ONLY for articles where candidate publications are supplied. Give the
  1-based number of the candidate the article is citing, or null if none of them fits.
  Do not force a choice; "null" is the correct answer whenever the article's figures,
  quotes and dates do not clearly line up with a candidate.
- source_confidence: 0-100, how sure you are of that choice.
- source_reason: one clause naming the concrete overlap you relied on (a shared figure,
  a shared quotation, a matching date), or why none fits.

Rules:
- Never invent a citation link. An article saying "according to the United Nations" with
  no matching publication in the candidate list is source_choice: null.
- Never speculate about a journalist's motives.
{STYLE_PROMPT}

Return ONLY valid JSON:
{{"mentions": [{{"id": "<the id given>", "summary": "...", "tone": "...",
  "tone_note": "...", "mention_type": "...", "prominence": "...",
  "agencies": [], "officials": [], "source_choice": null,
  "source_confidence": 0, "source_reason": "..."}}]}}"""

REVIEW_SYSTEM = f"""You are reviewing a media-monitoring batch before it goes to a human
at a United Nations office. Check: (1) every tone label is justified by the article and is
one of {json.dumps(TONES)}; (2) no source attribution is asserted without concrete overlap;
(3) summaries are factual, contain nothing not in the article, and name no motives;
(4) the register is neutral and publishable on a public page; (5) UN Editorial Manual
style is respected, including UN short-form country names.
Return ONLY JSON: {{"confidence": <0-100>, "issues": ["specific, actionable fixes"]}}.
90+ means genuinely ready for a human reviewer."""

REVISE_SYSTEM = CLASSIFY_SYSTEM + \
    "\nRevise your analysis to resolve the listed issues. Return the full corrected JSON only."


def translate_system(glossary):
    gloss = ""
    if glossary:
        pairs = "\n".join(f"  {k} = {v}" for k, v in list(glossary.items())[:80])
        gloss = ("\nUse this office glossary verbatim; do not re-translate these terms:\n"
                 + pairs)
    return ("You are a professional English-to-Mongolian (Cyrillic) translator working for "
            "a United Nations office. Translate every value of the keys 'summary' and "
            "'tone_note' and 'overview' into natural, formal Mongolian. Leave every other "
            "key, all URLs, outlet names, dates, agency acronyms and the JSON structure "
            "exactly as they are. Return ONLY the translated JSON, same shape." + gloss)


def payload_for_model(mentions):
    """Trim each mention to what the model needs, and number the candidates."""
    out = []
    for m in mentions:
        src = m.get("un_source", {})
        entry = {
            "id": m["id"],
            "outlet": m.get("outlet", ""),
            "language": m.get("language", ""),
            "published": m.get("published", ""),
            "title": m.get("title", ""),
            "url": m.get("url", ""),
            "article_text": (m.get("body") or m.get("snippet") or "")[:3500],
        }
        if src.get("method") != "explicit_link" and src.get("candidates"):
            entry["candidate_publications"] = [
                {"n": i, "title": c["title"], "date": c["date"], "agency": c["agency"],
                 "url": c["url"], "why_shortlisted": "; ".join(c.get("evidence", []))}
                for i, c in enumerate(src["candidates"], 1)]
        else:
            entry["candidate_publications"] = []
            if src.get("method") == "explicit_link":
                entry["note"] = ("The article links directly to a United Nations page; "
                                 "the source is already established, set source_choice to null.")
        out.append(entry)
    return out


def classify(mentions):
    user = ("Analyse these articles. Return one object per article, keeping the ids.\n\n"
            + json.dumps(payload_for_model(mentions), ensure_ascii=False))
    for attempt in range(2):
        system = CLASSIFY_SYSTEM
        if attempt:
            system += "\n\nIMPORTANT: reply with the JSON object ONLY. No preamble, no fences."
        try:
            return parse_json(call(system, user))
        except (ValueError, json.JSONDecodeError):
            print(f"  classify attempt {attempt + 1}: unparseable JSON, retrying...")
    raise RuntimeError("Could not obtain valid JSON from the classification step.")


def review_loop(analysis, mentions):
    threshold = CONFIG["review"]["confidence_threshold"]
    score = 0
    for i in range(1, CONFIG["review"]["max_iterations"] + 1):
        verdict = parse_json(call(REVIEW_SYSTEM, json.dumps(analysis, ensure_ascii=False)))
        score = int(verdict.get("confidence", 0))
        issues = verdict.get("issues", [])
        print(f"  review pass {i}: confidence={score}")
        if score >= threshold or not issues:
            break
        fix = ("Your analysis:\n" + json.dumps(analysis, ensure_ascii=False)
               + "\n\nThe articles:\n"
               + json.dumps(payload_for_model(mentions), ensure_ascii=False)
               + "\n\nIssues to fix:\n" + json.dumps(issues, ensure_ascii=False))
        analysis = parse_json(call(REVISE_SYSTEM, fix))
    return analysis, score


def merge(mentions, analysis):
    """Combine the deterministic match with the model's judgement."""
    by_id = {a.get("id"): a for a in analysis.get("mentions", [])}
    threshold = int(MM.get("match_min_confidence", 60))
    out = []
    for m in mentions:
        a = by_id.get(m["id"], {})
        src = dict(m.get("un_source", {}))

        if src.get("method") == "explicit_link":
            pass                                        # already certain
        else:
            choice, conf = a.get("source_choice"), int(a.get("source_confidence") or 0)
            cands = src.get("candidates", [])
            if choice and isinstance(choice, int) and 1 <= choice <= len(cands) and conf >= threshold:
                c = cands[choice - 1]
                # Blend: the model's confidence, tempered by the deterministic score.
                blended = int(round(0.6 * conf + 0.4 * c["score"]))
                src.update({"matched": blended >= threshold,
                            "method": "inferred" if blended >= threshold else "none",
                            "confidence": blended, "title": c["title"], "url": c["url"],
                            "date": c["date"], "agency": c["agency"],
                            "reason": a.get("source_reason", ""),
                            "evidence": c.get("evidence", [])})
            else:
                src.update({"matched": False, "method": "none",
                            "confidence": conf,
                            "reason": a.get("source_reason", "no candidate matched"),
                            "title": "", "url": "", "date": "", "agency": ""})

        out.append({
            "id": m["id"],
            "outlet": m.get("outlet", ""),
            "outlet_domain": m.get("outlet_domain", ""),
            "known_outlet": m.get("known_outlet", False),
            "language": m.get("language", ""),
            "published": m.get("published", ""),
            "title": m.get("title", ""),
            "url": m.get("url", ""),
            "summary": {"en": a.get("summary", ""), "mn": ""},
            "tone": a.get("tone", "Neutral"),
            "tone_note": {"en": a.get("tone_note", ""), "mn": ""},
            "mention_type": a.get("mention_type", ""),
            "prominence": a.get("prominence", ""),
            "agencies": a.get("agencies", []) or [],
            "officials": a.get("officials", []) or [],
            "un_source": src,
        })
    return out


def translate(doc, glossary):
    try:
        mn = parse_json(call(translate_system(glossary), json.dumps(doc, ensure_ascii=False)))
    except Exception as e:
        print(f"  translation skipped ({e}); the page will show English in both tabs")
        return doc
    by_id = {m.get("id"): m for m in mn.get("mentions", [])}
    for m in doc["mentions"]:
        t = by_id.get(m["id"], {})
        m["summary"]["mn"] = (t.get("summary") or {}).get("en") or t.get("summary") or m["summary"]["en"]
        m["tone_note"]["mn"] = (t.get("tone_note") or {}).get("en") or t.get("tone_note") or m["tone_note"]["en"]
    ov = mn.get("overview")
    if isinstance(ov, dict):
        doc["overview"]["mn"] = ov.get("en") or ov.get("mn") or doc["overview"]["en"]
    elif isinstance(ov, str):
        doc["overview"]["mn"] = ov
    return doc


def overview(mentions):
    if not mentions:
        return "No mentions of the United Nations were recorded in the monitored outlets this week."
    tones = {t: sum(1 for m in mentions if m["tone"] == t) for t in TONES}
    outlets = sorted({m["outlet"] for m in mentions})
    matched = sum(1 for m in mentions if m["un_source"].get("matched"))
    parts = [f"{len(mentions)} mention{'s' if len(mentions) != 1 else ''} recorded across "
             f"{len(outlets)} outlet{'s' if len(outlets) != 1 else ''}"]
    parts.append(", ".join(f"{v} {k.lower()}" for k, v in tones.items() if v))
    parts.append(f"the underlying United Nations publication was identified for "
                 f"{matched} of them")
    return "; ".join(parts) + "."


def main():
    if not RAW.exists():
        print("No data/raw_mentions.json; run scripts/collect_mentions.py first.")
        return
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    mentions = raw.get("mentions", [])
    issues = list(raw.get("issues", []))

    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)

    for i, m in enumerate(mentions, 1):
        m["id"] = f"m{i:03d}"

    doc = {"week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
           "confidence": 0, "overview": {"en": "", "mn": ""},
           "mentions": [], "issues": issues, "style_flags": []}

    if not mentions:
        doc["overview"]["en"] = overview([])
        doc["overview"]["mn"] = doc["overview"]["en"]
        doc["confidence"] = 100
    else:
        print(f"Matching {len(mentions)} mention(s) against the publication index...")
        MS.match_all(mentions)
        explicit = sum(1 for m in mentions if m["un_source"].get("method") == "explicit_link")
        print(f"  {explicit} matched by direct link, {len(mentions) - explicit} to infer")

        analysis = classify(mentions)
        analysis, score = review_loop(analysis, mentions)
        doc["mentions"] = merge(mentions, analysis)
        doc["confidence"] = score
        doc["overview"]["en"] = overview(doc["mentions"])

    # House style, then translation.
    data, wb_issues = SL.load_workbook_data()
    doc["issues"] += wb_issues
    if CONFIG.get("style", {}).get("linter", True):
        doc, changes, flags = lint_object(
            doc, protected=SL.protected_names(data),
            flag_terms=CONFIG.get("style", {}).get("flag_only_terms", []))
        doc["style_flags"] = [{"term": t, "context": c} for t, c in flags]
        if changes:
            print(f"  style linter applied {len(changes)} correction(s)")

    doc = translate(doc, SL.glossary(data))

    out = ROOT / "mentions" / f"{doc['week_start']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {out.relative_to(ROOT)} — {len(doc['mentions'])} mention(s), "
          f"confidence {doc['confidence']}")


if __name__ == "__main__":
    main()
