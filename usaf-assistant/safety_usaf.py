"""Pre-transmission and post-generation safety checks for the USAF Medical Assistant.

Dependency-free so it can be unit tested without the API stack. Extends the clinical assistant's
identifier patterns with military identifiers. Exact dates are not blocked (clinical timelines need
them) but are reported as warnings so the UI can suggest "[DATE]" or relative offsets.
"""
import re

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
    ("rank followed by a name", re.compile(
        r"\b(?:AB|Amn|A1C|SrA|SSgt|TSgt|MSgt|SMSgt|CMSgt|Sgt|2d\s?Lt|2Lt|1st\s?Lt|1Lt|Capt|Maj|Lt\s?Col|LtCol|Col|Brig\s?Gen|Maj\s?Gen|Lt\s?Gen|Gen|Mr|Mrs|Ms|Miss|Dr)\.?\s+(?-i:[A-Z][a-z]{2,})\b")),
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
