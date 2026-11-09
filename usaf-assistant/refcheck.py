"""Verification of paragraph/table references the model writes (dependency-free).

The model is told to cite only excerpt tags like [S2], but it may also quote "para 6.12.3" or "Table 5-2".
Every such reference is checked against the text of the excerpts actually retrieved; unverifiable ones
are reported so the page can flag them instead of presenting an invented citation as fact.
"""
import re

_REF = re.compile(
    r"\b(?:(?:Table|Figure|Attachment|Chapter|Section|Appendix)\s+[A-Z0-9][\w.\-]*"
    r"|(?:para(?:graph)?s?\.?)\s+\d+(?:\.\d+)+"
    r"|\d{1,2}(?:\.\d{1,3}){2,5})", re.I)
_TAG = re.compile(r"\[S(\d{1,2})\]")


def _norm(s):
    return re.sub(r"[\s]+", " ", s.lower()).strip(" .,;:")


def extract_refs(answer):
    refs = []
    for m in _REF.finditer(answer):
        r = _norm(m.group(0))
        r = re.sub(r"^paras?\.?\s*|^paragraphs?\s*", "", r)
        if r and r not in refs:
            refs.append(r)
    return refs


def unverified_refs(answer, excerpt_text):
    hay = _norm(excerpt_text)
    bad = []
    for r in extract_refs(answer):
        if r not in hay:
            bad.append(r)
    return bad


def used_tags(answer, n_excerpts):
    seen = []
    for m in _TAG.finditer(answer):
        i = int(m.group(1))
        if 1 <= i <= n_excerpts and i not in seen:
            seen.append(i)
    return seen


def invalid_tags(answer, n_excerpts):
    return sorted({int(m.group(1)) for m in _TAG.finditer(answer) if not 1 <= int(m.group(1)) <= n_excerpts})
