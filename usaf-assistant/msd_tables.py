"""Table-aware parser for the DAF Medical Standards Directory (MSD).

The MSD pages are borderless tables: one row per standard (ID such as C59), a description, then a grid of "X" marks
in narrow columns whose headers are printed rotated by 180 degrees. Plain text extraction reads the headers
backwards ("noitneteR") and loses which column each X belongs to, so "X X X X" would be meaningless. This parser uses
word positions: row bands from the thin separator rectangles, column names from the rotated header words, and each X
is assigned to the column under it.
"""
import re

ID_RE = re.compile(r"^[A-Z]\d{1,3}$")
FLAG_X_MIN, FLAG_X_MAX = 405.0, 592.0   # the nine narrow flag columns sit between these x positions on every standards page
STANDARD_COLUMNS = ["Retention", "DW", "Flying Class I/IA", "Flying Class II", "Flying Class III", "Air Traffic Controller (ATC)",
                    "Ground Based Operator (GBO)", "Operational Support Flying Duty", "Special Warfare Airmen (SWA)"]
SEP_MAX_HEIGHT = 1.2
SEP_MIN_WIDTH = 250
MARK_RE = re.compile(r"^[X^*†+]+$")


def _merge(values, tol=1.2):
    out = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def canonical_name(raw):
    """Map a (possibly partial, word-order-scrambled) rotated header to the standard column name."""
    t = set(re.findall(r"[a-z/]+", raw.lower()))
    if not t:
        return ""
    if "retention" in t:
        return "Retention"
    if t == {"dw"}:
        return "DW"
    if "i/ia" in t and ("class" in t or "flying" in t):
        return "Flying Class I/IA"
    if "iii" in t:
        return "Flying Class III"
    if "ii" in t:
        return "Flying Class II"
    if "atc" in t or len(t & {"air", "traffic", "controller"}) >= 2:
        return "Air Traffic Controller (ATC)"
    if "gbo" in t or len(t & {"ground", "based", "operator"}) >= 2:
        return "Ground Based Operator (GBO)"
    if t & {"operational", "support", "duty"}:
        return "Operational Support Flying Duty"
    if t & {"special", "warfare", "airmen", "swa"}:
        return "Special Warfare Airmen (SWA)"
    return raw.strip()


ANCHORS = {"noitneteR": 0, "WD": 1, "AI/I": 2, "II": 3, "III": 4}  # reversed header words that name the first five (unambiguous) columns


def header_columns(page, first_row_top):
    """[(x_center, column name)] for the nine flag columns.
    The columns are evenly spaced, but their position differs from section to section, so the page's own header is used:
    the first five columns (Retention, DW, Flying Class I/IA, II, III) are identified by their rotated header words, and
    the spacing between them places the other four. Returns [] when the anchors are not all found."""
    found = {}
    for w in page.extract_words(extra_attrs=["upright"], keep_blank_chars=False):
        if not w["upright"] and 90 < w["top"] < first_row_top and w["text"] in ANCHORS and (w["x0"] + w["x1"]) / 2 > 300:
            found.setdefault(ANCHORS[w["text"]], (w["x0"] + w["x1"]) / 2)
    if len(found) < 3 or 0 not in found:
        return []
    # straight line through the anchors that were readable (some pages overprint one or two of the header words)
    ks = sorted(found)
    mean_k, mean_x = sum(ks) / len(ks), sum(found[k] for k in ks) / len(ks)
    spacing = sum((k - mean_k) * (found[k] - mean_x) for k in ks) / sum((k - mean_k) ** 2 for k in ks)
    x0 = mean_x - spacing * mean_k
    if not 15 <= spacing <= 30 or max(abs(found[k] - (x0 + spacing * k)) for k in ks) > 4:
        return []
    return [(x0 + k * spacing, STANDARD_COLUMNS[k]) for k in range(len(STANDARD_COLUMNS))]


def _separators(page):
    tops = [r["top"] for r in page.rects if r["bottom"] - r["top"] <= SEP_MAX_HEIGHT and r["x1"] - r["x0"] >= SEP_MIN_WIDTH
            and r["top"] > 90]
    return _merge(tops, 1.5)


def join_lines(words, tol=3.5):
    """Words -> text in reading order: group into lines by vertical position, then left to right."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    return " ".join(x["text"] for _, ln in lines for x in sorted(ln, key=lambda w: w["x0"]))


def parse_page(page, fallback_names=None):
    """Return (rows, columns) for one MSD page; rows is empty if the page has no standards table.
    Each row: {id, text, marks:[(column name, mark)], comments}."""
    seps = _separators(page)
    words = page.extract_words(extra_attrs=["upright"], keep_blank_chars=False)
    id_words = [w for w in words if ID_RE.match(w["text"]) and w["x0"] < 70 and w["upright"]]
    if len(seps) < 2 or not id_words:
        return [], []
    cols = header_columns(page, min(w["top"] for w in id_words))
    names = [n for _, n in cols]
    if not cols:
        return [], cols  # unrecognised layout: the caller treats the page as plain text and reports it
    centers = [x for x, _ in cols]
    half = (centers[-1] - centers[0]) / (len(centers) - 1) / 2
    flag_left, flag_right = centers[0] - half, centers[-1] + half
    rows = []
    for a, b in zip(seps, seps[1:]):
        band = [w for w in words if a - 0.3 <= (w["top"] + w["bottom"]) / 2 < b + 0.3 and w["upright"]]
        idw = [w for w in band if ID_RE.match(w["text"]) and w["x0"] < 70]
        if not idw:
            continue
        desc, comments, marks = [], [], []
        for w in band:
            if w is idw[0]:
                continue
            xc = (w["x0"] + w["x1"]) / 2
            if xc < flag_left:
                desc.append(w)
            elif xc >= flag_right:
                comments.append(w)
            elif MARK_RE.match(w["text"]):
                i = min(range(len(centers)), key=lambda k: abs(centers[k] - xc))
                marks.append((i, w["text"]))
            else:
                desc.append(w)
        rows.append({"id": idw[0]["text"], "text": join_lines(desc),
                     "marks": [(names[i], m) for i, m in sorted(set(marks))], "comments": join_lines(comments)})
    return rows, cols


SECTION_RE = re.compile(r"^Section\s+([A-Z]):\s*(.+?)(?:\s+DAF Medical.*)?$")


def parse_pdf(pdf):
    """Walk the whole MSD. Returns (rows, other_pages): rows carry section letter/title and page number; other_pages is
    [(page_no, text)] for pages without a standards table (introduction, notes, SWA/weight sections written as prose)."""
    rows_out, other, unrecognised, section = [], [], [], ("", "")
    for pn, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""
        for line in text.splitlines()[:6]:
            m = SECTION_RE.match(line.strip())
            if m:
                section = (m.group(1), line.strip())
                break
        rows, cols = parse_page(page)
        if not rows and any(ID_RE.match(w) for w in text.split()) and "Disqualifying" in text:
            unrecognised.append(pn)
        if rows:
            for r in rows:
                r["page"], r["section"] = pn, section[1]
            rows_out.extend(rows)
        else:
            other.append((pn, text))
    return rows_out, other, unrecognised


def row_text(r):
    """Plain-language rendering of one standard: marks are written out as the column names they sit under."""
    cols = [f"{name}{'' if mark == 'X' else ' (' + mark + ')'}" for name, mark in r["marks"]]
    out = f"{r['id']}: {r['text']}".strip()
    out += "\nMSD columns marked X (standard applies): " + (", ".join(cols) if cols else "none marked on this line")
    if r["comments"]:
        out += "\nComments: " + r["comments"]
    return out
