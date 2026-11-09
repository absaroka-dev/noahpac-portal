"""Pre-transmission and post-generation safety checks for the Med Standards Assistant.

Dependency-free so it can be unit tested without the API stack. Extends the clinical assistant's
identifier patterns with military identifiers. Exact dates are not blocked (clinical timelines need
them) but are reported as warnings so the UI can suggest "[DATE]" or relative offsets.
"""
import re

_RANKS = (r"(?:AB|Amn|A1C|SrA|SSgt|TSgt|MSgt|SMSgt|CMSgt|Sgt|2d\s?Lt|2Lt|1st\s?Lt|1Lt|Capt|Maj\s?Gen|Brig\s?Gen|Lt\s?Gen|Lt\s?Col|LtCol|Maj|Col|Gen|"
          r"Mr|Mrs|Ms|Miss|Dr)")  # longest alternatives first: "Maj Gen" must not read as the rank "Maj" followed by the name "Gen"
_CAP = r"(?-i:(?!Gen\b|Col\b)[A-Z][a-z]{2,})"

IDENTIFIER_PATTERNS = [
    ("email address", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("phone number", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("Social Security number", re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")),
    ("nine-digit number (possible SSN)", re.compile(r"(?<![\d.])\d{9}(?![\d.])")),
    ("DoD ID / EDIPI", re.compile(r"\b(?:DoD\s*ID|EDIPI|DODID)\s*(?:number|no\.?|#)?\s*[:#-]?\s*\d{6,10}\b", re.I)),
    ("ten-digit number (possible DoD ID)", re.compile(r"(?<![\d.])\d{10}(?![\d.])")),
    ("medical record number", re.compile(r"\b(?:MRN|medical record(?: number)?|patient ID)\s*[:#-]?\s*[A-Z0-9-]{4,}\b", re.I)),
    ("date of birth", re.compile(r"\b(?:DOB|date of birth|born(?: on)?)\s*[:#-]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.I)),
    ("date of birth", re.compile(r"\b(?:DOB|date of birth)\s*[:#-]?\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b", re.I)),
    ("street address", re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct)\b", re.I)),
    # rank + capitalised surname, e.g. "SSgt Smith", "Capt. John Doe", "Lt Col Jones"
    ("rank followed by a name", re.compile(r"\b" + _RANKS + r"\.?\s+" + _CAP + r"\b")),
]

_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_PATTERNS = [
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}\s+" + _MONTHS + r"\.?,?\s+\d{2,4}\b", re.I),
    re.compile(r"\b" + _MONTHS + r"\.?\s+\d{1,2},?\s+\d{4}\b", re.I),
]


def detected_identifier(text):
    """Return a label for the first identifier-like pattern found, else None."""
    for label, pattern in IDENTIFIER_PATTERNS:
        if pattern.search(text):
            return label
    return None


def exact_date_warning(text):
    """True when the text holds an exact calendar date (re-identification risk, not a block)."""
    return any(p.search(text) for p in DATE_PATTERNS)


# ---------------------------------------------------------------------------------------------------------------------------------------------
# Redaction: replace identifiers with placeholders instead of refusing the whole message. Best effort only: rules find structured identifiers
# (SSN, DoD ID, phone, email, address, labelled birth dates and record numbers, names after a rank or a label); a bare name, a unit or a rare
# detail cannot be found by rules, so the page keeps its "I removed identifiers" confirmation and detected_identifier() stays as the final gate.
# Exact calendar dates are NOT replaced (clinical timelines need them); they only raise a warning.
# ---------------------------------------------------------------------------------------------------------------------------------------------
import os
from collections import Counter

_NAME_AFTER_RANK = re.compile(r"\b(" + _RANKS + r")\.?\s+" + _CAP + r"(?:\s+(?-i:[A-Z]\.))?(?:\s+" + _CAP + r")?\b")
_NAME_LABELLED = re.compile(r"\b((?:patient\s+)?name\s*[:\-]\s*|named\s+|(?:member|patient|airman|pt)\s*:\s*)" + _CAP + r"(?:\s+(?-i:[A-Z]\.))?(?:\s+" + _CAP + r")?", re.I)
# (label, placeholder, pattern, template); template uses \g<1> to keep a leading rank or label
_REDACTIONS = [
    ("email address", "[EMAIL]", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), None),
    ("Social Security number", "[SSN]", re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b"), None),
    ("DoD ID / EDIPI", "[DOD-ID]", re.compile(r"\b(?:DoD\s*ID|EDIPI|DODID)\s*(?:number|no\.?|#)?\s*[:#-]?\s*\d{6,10}\b", re.I), None),
    ("medical record number", "[MRN]", re.compile(r"\b(?:MRN|medical record(?: number)?|patient ID)\s*[:#-]?\s*[A-Z0-9-]{4,}\b", re.I), None),
    ("date of birth", "[DOB]", re.compile(r"\b(?:DOB|date of birth|born(?: on)?)\s*[:#-]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.I), None),
    ("date of birth", "[DOB]", re.compile(r"\b(?:DOB|date of birth)\s*[:#-]?\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b", re.I), None),
    ("phone number", "[PHONE]", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"), None),
    ("street address", "[ADDRESS]", re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct)\b", re.I), None),
    ("name after a rank or title", "[NAME]", _NAME_AFTER_RANK, r"\1 [NAME]"),
    ("name after a label", "[NAME]", _NAME_LABELLED, r"\1[NAME]"),
    ("nine-digit number", "[SSN]", re.compile(r"(?<![\d.])\d{9}(?![\d.])"), None),
    ("ten-digit number", "[DOD-ID]", re.compile(r"(?<![\d.])\d{10}(?![\d.])"), None),
]
TERMS_FILE = os.environ.get("USAF_REDACT_TERMS", "/etc/usaf-assistant/redact-terms.txt")


def _load_terms(path):
    """Owner-supplied words to always replace (names of people, bases, units), one per line; blank lines and # comments are ignored."""
    try:
        with open(path, encoding="utf-8") as f:
            words = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return None
    words = sorted({w for w in words if len(w) >= 3}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I) if words else None


def redact(text, terms_file=None):
    """Return (clean_text, findings). findings: [{"label", "placeholder", "count"}] (counts only, never the matched text)."""
    found = Counter()
    meta = {}
    out = text
    for label, placeholder, pattern, template in _REDACTIONS:
        out, n = pattern.subn(template or placeholder, out)
        if n:
            found[(label, placeholder)] += n
    terms = _load_terms(terms_file or TERMS_FILE)
    if terms:
        out, n = terms.subn("[TERM]", out)
        if n:
            found[("listed name, base or unit", "[TERM]")] += n
    merged = {}
    for (label, placeholder), n in found.items():
        m = merged.setdefault(placeholder, {"label": label, "placeholder": placeholder, "count": 0})
        m["count"] += n
    merged.get("[NAME]", {}).update(label="name")
    return out, list(merged.values())
