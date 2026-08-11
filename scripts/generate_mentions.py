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

Steps 2, 3 and 5 run in batches of media_monitoring.classify_batch_size
articles: one reply per batch stays inside max_output_tokens, and a batch that
fails anyway costs that batch only — the page is still written.
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

TONES = MM.get("tone_labels", ["Supportive", "Neutral", "Critical"])
TYPES = MM.get("mention_types", [])
PROMINENCE = MM.get("prominence_levels", [])

# How many articles go into one classify/review call. A weekly batch of 60
# articles asked for a single reply of well over max_output_tokens, so the JSON
# came back truncated and unparseable — every week since the first one. Small
# batches keep each reply comfortably inside the limit, and a batch that still
# fails costs us that batch only, not the whole page.
BATCH_SIZE = int(MM.get("classify_batch_size", 12))

UNANALYSED_NOTE = ("This article was collected but could not be analysed automatically "
                   "this week; it is listed here for reference.")

# US dollars per million tokens, as published by Anthropic. Cached input is
# charged at 1.25x on the write and 0.1x on the read. These are here only to
# print an estimate at the end of a run — they are not used for anything else,
# so a stale price makes the estimate wrong, never the report.
PRICES = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}
USAGE = {}          # model -> {input, output, cache_write, cache_read}


def record_usage(model, usage):
    u = USAGE.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
    u["input"] += usage.input_tokens or 0
    u["output"] += usage.output_tokens or 0
    u["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    u["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0


def report_usage():
    if not USAGE:
        return
    print("\nToken usage this run:")
    total = 0.0
    unpriced = []
    for model, u in USAGE.items():
        print(f"  {model}")
        print(f"    input {u['input']:,} | cache write {u['cache_write']:,} | "
              f"cache read {u['cache_read']:,} | output {u['output']:,}")
        price = PRICES.get(model)
        if not price:
            unpriced.append(model)
            continue
        pin, pout = price
        cost = ((u["input"] + 1.25 * u["cache_write"] + 0.1 * u["cache_read"]) * pin
                + u["output"] * pout) / 1_000_000
        total += cost
        print(f"    estimated cost: ${cost:.4f}")
    if unpriced:
        print(f"  (no price on file for {', '.join(unpriced)}; excluded from the total)")
    print(f"  ESTIMATED TOTAL: ${total:.4f}")


def call(system, user, model=None):
    # A plain string system prompt is cached whole (ephemeral). The review loop
    # reuses the same CLASSIFY/REVIEW system prompts within a run, so a cache hit
    # skips most of that input on the repeat calls. Pass a pre-built list of
    # blocks (see revise_system()) to control the cache split.
    system_param = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if isinstance(system, str) else system
    )
    msg = client.messages.create(
        model=model or MODEL, max_tokens=MAXTOK,
        system=system_param, messages=[{"role": "user", "content": user}],
    )
    record_usage(model or MODEL, msg.usage)
    text = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "max_tokens":
        # The reply was cut off mid-JSON; say so plainly rather than letting it
        # surface later as an inscrutable "Expecting ',' delimiter".
        print(f"  ! reply hit max_output_tokens ({MAXTOK}) and was truncated")
    return text


def parse_json(text):
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in model reply. First 400 chars:\n" + text[:400])
    return json.loads(cleaned[start:end + 1])


JSON_ONLY = "\n\nIMPORTANT: reply with the JSON object ONLY. No preamble, no fences."


def with_suffix(system, extra):
    """Append an instruction to a system prompt, string or block list alike."""
    if isinstance(system, str):
        return system + extra
    return list(system) + [{"type": "text", "text": extra}]


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
            sys_prompt, prompt = with_suffix(system, JSON_ONLY), user
        text = call(sys_prompt, prompt, model=model)
        try:
            return parse_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err, last_text = e, text
            print(f"  {label} attempt {attempt + 1}: could not parse JSON, retrying...")
    raise last_err


CLASSIFY_SYSTEM = f"""You analyse how the media in Mongolia and abroad refer to the
United Nations, for the UN Resident Coordinator's Office. You are describing coverage,
not judging journalists, and your output is published on a public page: stay descriptive
and professional throughout.

For every article you receive, return:
- summary: one or two sentences, in English, saying what the article is about and how the
  United Nations appears in it.
- tone: exactly one of {json.dumps(TONES)}. Judge the tone TOWARDS the United Nations
  only, not the tone of the article overall, and not the subject matter.

  "Neutral" is the default and will be the correct answer for most articles. Routine
  reporting — an appointment, an event, a launch, an agreement, a statement quoted as
  said, our figures cited as figures — is "Neutral", however welcome the underlying news
  is. Move away from "Neutral" only when the article's own words evaluate us.

  "Supportive" requires explicit approving language ABOUT the United Nations or its work,
  written by the outlet: praise, endorsement, credit for a result, or a favourable
  judgement of our role. None of the following is enough on its own, and each is a
  common mistake:
    - the absence of criticism;
    - a positive or hopeful subject (education, clean energy, gender equality);
    - the article appearing on a United Nations website, or the United Nations being the
      author of the statement being reported — the publisher is not the tone;
    - approving words spoken by an official we quote, rather than written by the outlet;
    - the mere fact that a partnership, milestone or anniversary is being reported.
  If you cannot point to specific approving words in the article text, the answer is
  "Neutral".

  "Critical" requires explicit criticism, doubt cast on our work, or a negative framing
  of our role. A grim article about winter losses that reports our figures without
  comment is "Neutral", not "Critical".
- tone_note: one short clause explaining the tone judgement, in neutral language. For
  "Supportive" and "Critical" it must quote the words from the article that carry the
  judgement ("calls the programme 'a turning point for herders'", "questions 'the slow
  pace' of the response"). For "Neutral", say what the article does instead ("reports
  our figures without comment", "announces the appointment"). Never justify a tone by
  what the article does NOT say.
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
at a United Nations office. Check: (1) every tone label is one of {json.dumps(TONES)} and
is earned — "Neutral" is the expected answer for routine coverage, and a "Supportive" or
"Critical" label is only justified when its tone_note quotes evaluative words from the
article itself. Flag any "Supportive" resting on the absence of criticism, on a positive
subject, on the piece appearing on a United Nations site, or on wording that merely
restates the label; (2) no source attribution is asserted without concrete overlap;
(3) summaries are factual, contain nothing not in the article, and name no motives;
(4) the register is neutral and publishable on a public page; (5) UN Editorial Manual
style is respected, including UN short-form country names.
Return ONLY JSON: {{"confidence": <0-100>, "issues": ["specific, actionable fixes"]}}.
90+ means genuinely ready for a human reviewer."""

REVISE_SUFFIX = "\nRevise your analysis to resolve the listed issues. Return the full corrected JSON only."


def revise_system():
    # Two blocks so the CLASSIFY_SYSTEM portion re-hits the cache entry written by
    # classify()'s call, instead of caching "CLASSIFY_SYSTEM + suffix" as a separate blob.
    return [
        {"type": "text", "text": CLASSIFY_SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": REVISE_SUFFIX},
    ]


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
    return call_json(CLASSIFY_SYSTEM, user, label="classify")


def review_loop(analysis, mentions, tag=""):
    """Critique and revise one batch. A failed pass keeps the draft as it is.

    The review verdict is small, but a revision returns the whole analysis
    again — so this runs per batch, like classify, and never raises: a batch
    that reviews badly is still better than no page at all.
    """
    threshold = CONFIG["review"]["confidence_threshold"]
    score = 0
    log = []
    for i in range(1, CONFIG["review"]["max_iterations"] + 1):
        try:
            verdict = call_json(REVIEW_SYSTEM, json.dumps(analysis, ensure_ascii=False),
                                label=f"review{tag}")
        except Exception as e:
            print(f"  ! review pass {i} failed ({type(e).__name__}); keeping the draft as it is")
            break
        score = int(verdict.get("confidence", 0))
        issues = verdict.get("issues", [])
        print(f"  review pass {i}: confidence={score}")
        for issue in issues:
            print(f"    - {issue}")
        log.append({"pass": i, "confidence": score, "issues": issues})
        if score >= threshold or not issues:
            break
        fix = ("Your analysis:\n" + json.dumps(analysis, ensure_ascii=False)
               + "\n\nThe articles:\n"
               + json.dumps(payload_for_model(mentions), ensure_ascii=False)
               + "\n\nIssues to fix:\n" + json.dumps(issues, ensure_ascii=False))
        try:
            analysis = call_json(revise_system(), fix, label=f"revise{tag}")
        except Exception as e:
            print(f"  ! revision failed ({type(e).__name__}); keeping the previous draft")
            break
    return analysis, score, log


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def analyse(mentions):
    """Classify and review the week's articles in small batches.

    Returns the combined analysis, the lowest batch confidence (the honest
    number to show a reviewer), the review log, and a note for any batch that
    had to be dropped.
    """
    batches = list(chunked(mentions, BATCH_SIZE))
    combined, logs, scores, issues = [], [], [], []
    for n, batch in enumerate(batches, 1):
        tag = f" {n}/{len(batches)}"
        print(f"Batch{tag} — {len(batch)} article(s)")
        try:
            analysis = classify(batch)
        except Exception as e:
            print(f"  ! batch{tag} could not be classified ({type(e).__name__}: {e}); skipped")
            issues.append(
                f"{len(batch)} article(s) could not be analysed this week "
                f"({type(e).__name__}); they appear on the page without tone or summary. "
                f"Re-run scripts/generate_mentions.py to try again.")
            continue
        analysis, score, log = review_loop(analysis, batch, tag=tag)
        combined += analysis.get("mentions", []) or []
        if score:                        # 0 means the critic never got to speak
            scores.append(score)
        logs += [dict(entry, batch=n) for entry in log]
    return {"mentions": combined}, (min(scores) if scores else 0), logs, issues


def merge(mentions, analysis):
    """Combine the deterministic match with the model's judgement."""
    by_id = {a.get("id"): a for a in analysis.get("mentions", [])}
    threshold = int(MM.get("match_min_confidence", 60))
    out = []
    for m in mentions:
        a = by_id.get(m["id"])
        unanalysed = a is None          # its batch failed; list it, don't lose it
        a = a or {}
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
            "summary": {"en": a.get("summary") or (UNANALYSED_NOTE if unanalysed else ""),
                        "mn": ""},
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
    # Only what needs translating — not review_log/issues/style_flags/un_source,
    # which are pure prose or structured evidence the translator doesn't touch
    # and which would needlessly inflate the prompt. Batched for the same reason
    # as the analysis: Mongolian Cyrillic is token-hungry, and one reply covering
    # sixty summaries does not fit in max_output_tokens.
    system = translate_system(glossary)
    done, failed = [], 0
    first = True
    for batch in chunked(doc["mentions"], BATCH_SIZE):
        payload = {
            "mentions": [{"id": m["id"], "summary": m["summary"], "tone_note": m["tone_note"]}
                         for m in batch],
        }
        if first:                        # the overview rides along with batch one
            payload["overview"] = doc["overview"]
        try:
            mn = call_json(system, json.dumps(payload, ensure_ascii=False),
                           model=TRANSLATE_MODEL, label="translate")
        except Exception as e:
            print(f"  translation batch failed ({type(e).__name__}: {e})")
            failed += 1
            first = False
            continue
        done.append(mn)
        first = False

    if failed:
        print(f"  {failed} translation batch(es) failed; those entries will show English "
              f"in the Mongolian tab this week")
        doc.setdefault("issues", []).append(
            "Part of the Mongolian translation failed this week ({} batch(es)); those "
            "entries currently show English text in the MN tab. Re-run "
            "scripts/generate_mentions.py, or translate them by hand.".format(failed))
    if not done:
        return doc

    mn = {"mentions": [m for d in done for m in d.get("mentions", []) or []],
          "overview": next((d.get("overview") for d in done if d.get("overview")), None)}

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
           "mentions": [], "issues": issues, "style_flags": [], "review_log": []}

    if not mentions:
        doc["overview"]["en"] = overview([])
        doc["overview"]["mn"] = doc["overview"]["en"]
        doc["confidence"] = 100
    else:
        print(f"Matching {len(mentions)} mention(s) against the publication index...")
        MS.match_all(mentions)
        explicit = sum(1 for m in mentions if m["un_source"].get("method") == "explicit_link")
        print(f"  {explicit} matched by direct link, {len(mentions) - explicit} to infer")

        analysis, score, log, analysis_issues = analyse(mentions)
        doc["mentions"] = merge(mentions, analysis)
        doc["confidence"] = score
        doc["review_log"] = log
        doc["issues"] += analysis_issues
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
    report_usage()


if __name__ == "__main__":
    main()
