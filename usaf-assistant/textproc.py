"""Section-aware chunking for structured USAF publications (dependency-free).

Publications such as DAFMAN 48-123, the AFECD/AFOCD and the DoD/DAF PRP and arming manuals are organised
by chapter, numbered paragraph, table and AFSC entry. Chunking by those boundaries (instead of every N
characters) lets a citation say "DAFMAN 48-123, Table 6.1, p. 142", and keeps a table in one piece where it
reasonably fits.
"""
import re

MAX_CHARS = 3200
MAX_CHARS_TABLE = 6500
OVERLAP = 300
MIN_CHARS_BEFORE_PARAGRAPH_SPLIT = 1200

_CHAPTER = re.compile(r"^(?:CHAPTER|Chapter|SECTION|Section|ATTACHMENT|Attachment|APPENDIX|Appendix)\s+[A-Z0-9][\w.\-]*\b.{0,80}$")
_TABLE = re.compile(r"^(?:TABLE|Table|FIGURE|Figure)\s+[A-Z0-9][\w.\-]*\b.{0,100}$")
_PARAGRAPH = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){1,5})\.?\s+(?=[A-Z\"(])")
_AFSC = re.compile(r"^(?:AFSC\s+)?(\d[A-Z]\d[0-9XA-Z]\d|\d{2}[A-Z]\d[A-Z]?)\b[^\n]{0,80}$")
_NUMBERED_SECTION = re.compile(r"^\(\d{1,2}\)\s+[A-Z][A-Z ,/&()\-]{3,}")
_PAGE_NUMBER_ONLY = re.compile(r"^\s*(?:Page\s+)?\d{1,4}(?:\s+of\s+\d{1,4})?\s*$", re.I)


def classify_line(line):
    """Return (kind, label) for a heading-like line, else (None, None)."""
    s = line.strip()
    if not s or len(s) > 120:
        return None, None
    if _CHAPTER.match(s) or _NUMBERED_SECTION.match(s):
        return "chapter", s[:90]
    if _TABLE.match(s):
        return "table", s[:90]
    m = _PARAGRAPH.match(s)
    if m:
        return "paragraph", "para " + m.group(1)
    if _AFSC.match(s):
        return "afsc", s[:90]
    return None, None


def clean_page_text(text):
    return "\n".join(l.rstrip() for l in text.splitlines() if not _PAGE_NUMBER_ONLY.match(l))


def chunk_pages(pages):
    """pages: list of (page_number, text). Returns list of dicts {text, section, page_start, page_end}."""
    chunks = []
    cur = None  # {"lines": [], "section": str, "kind": str, "page_start": int, "page_end": int}

    def flush(carry=False):
        nonlocal cur
        if cur and "".join(cur["lines"]).strip():
            chunks.append({"text": "\n".join(cur["lines"]).strip(), "section": cur["section"],
                           "page_start": cur["page_start"], "page_end": cur["page_end"]})
        if carry and cur:
            tail = "\n".join(cur["lines"])[-OVERLAP:]
            cur = {"lines": [tail], "section": cur["section"].split(" (cont.)")[0] + " (cont.)",
                   "kind": cur["kind"], "page_start": cur["page_end"], "page_end": cur["page_end"]}
        else:
            cur = None

    def length():
        return sum(len(l) + 1 for l in cur["lines"]) if cur else 0

    for page_no, raw in pages:
        for line in clean_page_text(raw).splitlines():
            kind, label = classify_line(line)
            if cur is None:
                cur = {"lines": [], "section": label or "(front matter)", "kind": kind or "text",
                       "page_start": page_no, "page_end": page_no}
            elif kind in ("chapter", "table", "afsc") or (kind == "paragraph" and length() >= MIN_CHARS_BEFORE_PARAGRAPH_SPLIT):
                flush()
                cur = {"lines": [], "section": label, "kind": kind, "page_start": page_no, "page_end": page_no}
            limit = MAX_CHARS_TABLE if cur["kind"] == "table" else MAX_CHARS
            if length() + len(line) > limit:
                cur["page_end"] = page_no
                flush(carry=True)
                cur["page_start"] = page_no
            cur["lines"].append(line)
            cur["page_end"] = page_no
    flush()
    return chunks


def header_for(doc_short, section, page_start, page_end):
    pages = f"p. {page_start}" if page_start == page_end else f"pp. {page_start}-{page_end}"
    return f"[{doc_short} | {section} | {pages}]"
