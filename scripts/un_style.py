"""
un_style.py — deterministic UN Editorial Manual linter.

The prompts tell Claude the rules; this file enforces the ones a machine can
enforce, so the house style does not drift between one Monday and the next.

It does three things:
  1. rewrites country names into the UN *short* form used in running text
     ("Russian Federation", not "Russia"; "Viet Nam", not "Vietnam");
  2. applies UN spelling and date conventions (programme, labour, per cent,
     "14 July 2026");
  3. FLAGS — never silently rewrites — politically sensitive names, so the
     office decides case by case. Flags surface in the pull request body.

Text inside URLs, inside quoted speech, and inside protected names (outlet
names such as "Voice of America" or "Russia Today") is left untouched.

Use:
    from un_style import lint
    fixed, changes, flags = lint(text, protected=["Russia Today"])
"""

import re

# ---------------------------------------------------------------------------
# 1. Country names — UN short form for running text.
#    Left side = what a newspaper is likely to write. Right side = UN usage.
#    The UN Editorial Manual uses the SHORT form in running text; the formal
#    long form ("the People's Republic of China") is reserved for treaty-style
#    or protocol contexts, so it is deliberately NOT applied here.
# ---------------------------------------------------------------------------
COUNTRY_FORMS = {
    # Directly relevant to Mongolia coverage
    r"Russia": "Russian Federation",
    r"South Korea": "Republic of Korea",
    r"North Korea": "Democratic People's Republic of Korea",
    r"Vietnam": "Viet Nam",
    r"Turkey": "Türkiye",
    r"Kyrgyzstan": "Kyrgyzstan",
    r"Laos": "Lao People's Democratic Republic",
    r"Burma": "Myanmar",
    r"Iran": "Iran (Islamic Republic of)",
    r"Syria": "Syrian Arab Republic",
    r"Moldova": "Republic of Moldova",
    r"Macedonia": "North Macedonia",
    r"Czech Republic": "Czechia",
    r"Holland": "Netherlands (Kingdom of the)",
    r"Ivory Coast": "Côte d'Ivoire",
    r"Cape Verde": "Cabo Verde",
    r"Swaziland": "Eswatini",
    r"Tanzania": "United Republic of Tanzania",
    r"Venezuela": "Venezuela (Bolivarian Republic of)",
    r"Bolivia": "Bolivia (Plurinational State of)",
    r"Micronesia": "Micronesia (Federated States of)",
    # Abbreviations that should be spelled out
    r"\bU\.S\.A?\.": "United States",
    r"\bUSA\b": "United States",
    r"\bthe US\b": "the United States",
    r"\bU\.K\.": "United Kingdom",
    r"\bthe UK\b": "the United Kingdom",
    r"\bUAE\b": "United Arab Emirates",
    r"\bDPRK\b": "Democratic People's Republic of Korea",
    r"\bROK\b": "Republic of Korea",
}

# UN short forms that take the definite article in running text.
# "Russia signed" -> "The Russian Federation signed", not "Russian Federation signed".
TAKES_THE = {
    "Russian Federation", "Republic of Korea",
    "Democratic People's Republic of Korea", "United States", "United Kingdom",
    "United Arab Emirates", "Lao People's Democratic Republic",
    "Syrian Arab Republic", "United Republic of Tanzania",
    "Netherlands (Kingdom of the)", "Republic of Moldova",
}

# Phrases that contain a country word but must never be rewritten.
BUILTIN_PROTECTED = [
    "Russia Today", "RT Russia", "Voice of America", "Radio Free Asia",
    "Russian Direct Investment Fund", "Bank of Russia", "Gazprom Russia",
    "Korea Herald", "Korea Times", "Korean Peninsula", "Vietnam News Agency",
    "South Korean", "North Korean", "Russian", "Iranian", "Syrian", "Bolivian",
    "Venezuelan", "Tanzanian", "Czech", "Turkish", "Vietnamese", "Laotian",
    "Turkey vulture",
]

# ---------------------------------------------------------------------------
# 2. Spelling and usage
# ---------------------------------------------------------------------------
SPELLING = {
    r"\bprogram\b": "programme",
    r"\bPrograms\b": "Programmes",
    r"\bprograms\b": "programmes",
    r"\blabor\b": "labour",
    r"\bLabor\b": "Labour",
    r"\bcenter\b": "centre",
    r"\bCenter\b": "Centre",
    r"\bcenters\b": "centres",
    r"\bdefense\b": "defence",
    r"\bDefense\b": "Defence",
    r"\bfavorable\b": "favourable",
    r"\bunfavorable\b": "unfavourable",
    r"\bneighboring\b": "neighbouring",
    r"\bneighbor\b": "neighbour",
    r"\bneighbors\b": "neighbours",
    r"\bbehavior\b": "behaviour",
    r"\bharbor\b": "harbour",
    r"\bpercent\b": "per cent",
    r"\bPercent\b": "Per cent",
    r"\bpercentage points\b": "percentage points",
    r"\bfulfill\b": "fulfil",
    r"\bfulfillment\b": "fulfilment",
    r"\benrollment\b": "enrolment",
    r"\binstallment\b": "instalment",
    r"\bthe UN\b": "the United Nations",
    r"\bThe UN\b": "The United Nations",
    r"\bU\.N\.": "United Nations",
    r"\bSecretary General\b": "Secretary-General",
    r"\bResident Coordinator's office\b": "Resident Coordinator's Office",
    r"\bNGOs?\b": lambda m: m.group(0),  # left as-is, listed for documentation
}

# "computer program" and "program of study" keep the American form when the
# sense is software; guard the obvious software case.
SOFTWARE_GUARD = re.compile(r"\b(computer|software|training)\s+programme\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# "July 14, 2026" -> "14 July 2026"
US_DATE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})\b")
# "14/07/2026" or "14-07-2026" -> "14 July 2026"
NUM_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")

URL_RE = re.compile(r"https?://\S+|www\.\S+")
QUOTE_RE = re.compile(r"[\u201c\"]([^\u201d\"]{10,400})[\u201d\"]")


def _mask(text, patterns):
    """Replace matches with placeholders so later rules cannot touch them."""
    store = []

    def repl(m):
        store.append(m.group(0))
        return f"\x00{len(store) - 1}\x00"

    for p in patterns:
        text = p.sub(repl, text)
    return text, store


def _unmask(text, store):
    for i, original in enumerate(store):
        text = text.replace(f"\x00{i}\x00", original)
    return text


def _protected_patterns(protected):
    names = sorted(set(BUILTIN_PROTECTED + list(protected or [])), key=len, reverse=True)
    out = []
    for n in names:
        if n.strip():
            out.append(re.compile(r"\b" + re.escape(n) + r"\b"))
    return out


def _fix_dates(text, changes):
    def us(m):
        new = f"{int(m.group(2))} {m.group(1)} {m.group(3)}"
        changes.append((m.group(0), new, "date format"))
        return new

    def num(m):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        name = [k for k, v in MONTHS.items() if v == mo][0]
        new = f"{d} {name} {y}"
        changes.append((m.group(0), new, "date format"))
        return new

    text = US_DATE.sub(us, text)
    text = NUM_DATE.sub(num, text)
    return text


def lint(text, protected=None, flag_terms=None, fix_countries=True):
    """Return (fixed_text, changes, flags).

    changes: list of (before, after, rule) — what was rewritten.
    flags:   list of (term, snippet) — sensitive names needing a human call.
    """
    if not text or not isinstance(text, str):
        return text, [], []

    changes, flags = [], []

    # Sensitive terms: flag, never rewrite.
    for term in (flag_terms or []):
        for m in re.finditer(r"\b" + re.escape(term) + r"\w*\b", text, re.I):
            start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
            flags.append((term, "…" + text[start:end].strip() + "…"))

    # Mask URLs, direct quotations, and protected names.
    masked, store = _mask(text, [URL_RE, QUOTE_RE] + _protected_patterns(protected))

    if fix_countries:
        for pattern, replacement in COUNTRY_FORMS.items():
            rx = pattern if pattern.startswith(r"\b") or "\\" in pattern[:2] \
                else r"\b" + pattern + r"\b"
            try:
                compiled = re.compile(rx)
            except re.error:
                continue
            def sub(m, r=replacement, whole=masked):
                if m.group(0) == r:
                    return m.group(0)
                new = r
                if r in TAKES_THE:
                    before = whole[max(0, m.start() - 12):m.start()]
                    has_article = re.search(r"\b(the|The|a|an|of|in|to|from|with)\s+$", before) \
                        and re.search(r"\b(the|The)\s+$", before)
                    if not has_article and not m.group(0).lower().startswith("the "):
                        sentence_start = (m.start() == 0 or
                                          re.search(r"[.!?]\s+$|^\s*$", before))
                        new = ("The " if sentence_start else "the ") + r
                changes.append((m.group(0), new, "UN country name"))
                return new
            masked = compiled.sub(sub, masked)

    for pattern, replacement in SPELLING.items():
        if callable(replacement):
            continue
        def sub2(m, r=replacement):
            if m.group(0) == r:
                return m.group(0)
            changes.append((m.group(0), r, "UN spelling"))
            return r
        masked = re.sub(pattern, sub2, masked)

    masked = SOFTWARE_GUARD.sub(lambda m: m.group(1) + " program", masked)
    masked = _fix_dates(masked, changes)

    return _unmask(masked, store), changes, flags


def lint_object(obj, protected=None, flag_terms=None, keys=("en", "text", "summary")):
    """Walk a nested dict/list and lint every English narrative string.

    Only the keys listed are touched, so URLs, outlet names, Mongolian text
    and JSON structure are never rewritten.
    """
    changes, flags = [], []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in keys and isinstance(v, str):
                    fixed, c, f = lint(v, protected, flag_terms)
                    node[k] = fixed
                    changes += c
                    flags += f
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    continue
                walk(v)

    walk(obj)
    return obj, changes, flags


# The rules the model is told to follow up front. Imported by the prompts so
# there is exactly one copy of the house style in the repository.
STYLE_PROMPT = """
UN EDITORIAL MANUAL STYLE — follow strictly:
- Countries: use the UN SHORT form in running text — "Russian Federation" (not
  "Russia"), "Republic of Korea", "Democratic People's Republic of Korea",
  "Viet Nam" (two words), "Türkiye", "United States", "United Kingdom",
  "Czechia", "Iran (Islamic Republic of)". Never abbreviate to US/UK/PRC.
  Do not use the formal long form ("the People's Republic of China") in
  running text; "China" is the correct UN short form.
- Adjectives keep their normal form: "Russian coal", "Chinese imports".
- Spelling: -ize endings (organize, recognize) but British forms elsewhere —
  programme, labour, centre, defence, favourable, neighbouring. "per cent" is
  two words. "Secretary-General" is hyphenated.
- Dates: "14 July 2026". Never "July 14, 2026" or "14/07/2026".
- Numbers: spell out one to nine; use figures from 10 up. Use "1.2 million".
- Titles: capitalize a title when it precedes a name ("Resident Coordinator
  Jane Doe"), lower-case when it stands alone ("the resident coordinator said").
- Refer to "the United Nations", never "the UN", in narrative text; agency
  acronyms (UNICEF, UNDP, WHO) are fine on their own.
- Register: measured, factual, institutional. No adjectives of praise or alarm,
  no rhetorical questions, no editorialising.
- Politically sensitive names (Taiwan, Kosovo, Palestine, Western Sahara,
  Crimea): use the standard United Nations designation, and if in any doubt
  describe the fact without adopting a contested label. These are flagged for
  human review, so do not improvise.
"""
