#!/usr/bin/env python3
"""Builds the Med Standards Assistant index from the PDFs in LIBRARY_DIR.

Section-aware chunking (textproc.py), per-chunk metadata (document, section, page range, publication date),
Voyage embeddings, embedded Chroma collection `usaf_docs`. Full rebuild each run; the old index is replaced
only after every embedding succeeded, so a failed run never leaves an empty index.

library/manifest.json (optional but recommended) maps file names to citation details:
  {"DAFMAN48-123.pdf": {"short": "DAFMAN 48-123", "title": "Medical Examinations and Standards",
                        "pub_date": "2024-01-01", "category": "standards", "url": "https://static.e-publishing.af.mil/..."}}
Categories: standards, classification, waiver, prp, arming, other.
"""
import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import anthropic
import chromadb
import pdfplumber
import voyageai
from pdf2image import convert_from_path

import msd_tables
import textproc
from runtime_paths import CHROMA_DIR, HEARTBEAT_FILE, LIBRARY_DIR, STATE_DIR

COLLECTION = "usaf_docs"
EMBED_MODEL = "voyage-3"
EMBED_BATCH_SIZE = int(os.environ.get("USAF_EMBED_BATCH", "8"))
EMBED_BATCH_DELAY_SEC = float(os.environ.get("USAF_EMBED_DELAY", "30"))


OCR_MODEL = "claude-haiku-4-5-20251001"
OCR_PROMPT = ("Transcribe all the text visible in this image verbatim, preserving structure (headings, numbered "
              "paragraphs, table rows and columns) as plain text. Do not summarize, comment, or add anything not in the image.")


def ocr_page(client, img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    msg = client.messages.create(model=OCR_MODEL, max_tokens=4096, messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": OCR_PROMPT}]}])
    return msg.content[0].text.strip()


def document_date(pdf_path):
    """Best-available date, labelled with its basis (a file timestamp is never presented as a publication date)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            raw = str((pdf.metadata or {}).get("ModDate") or (pdf.metadata or {}).get("CreationDate") or "")
        m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "PDF metadata"
    except Exception:
        pass
    return time.strftime("%Y-%m-%d", time.localtime(pdf_path.stat().st_mtime)), "file modified"


def load_manifest():
    p = LIBRARY_DIR / "manifest.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def extract_pages(pdf_path, anthropic_client):
    """[(1-based page number, text)], OCR (Claude vision) for pages with no text layer."""
    pages, needs_ocr = {}, []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text()
            if t and t.strip():
                pages[i + 1] = t
            else:
                needs_ocr.append(i)
    if needs_ocr and anthropic_client:
        images = convert_from_path(str(pdf_path), dpi=200)
        for i in needs_ocr:
            if i < len(images):
                try:
                    t = ocr_page(anthropic_client, images[i])
                except Exception as e:  # one bad page must not drop the document
                    print(f"  WARNING: OCR failed on page {i + 1}: {e}", file=sys.stderr)
                    continue
                if t:
                    pages[i + 1] = t
    return sorted(pages.items()), len(needs_ocr)


def fmt_cite_date(iso):
    try:
        y, mo, d = (int(x) for x in iso.split("-"))
        return f"{d} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][mo - 1]} {y}"
    except (ValueError, IndexError, AttributeError):
        return iso or "date unknown"


def build_chunks(pdf_path, m, aclient):
    """Chunks for one PDF. The Medical Standards Directory (manifest parser "msd") is read as a table: one chunk per
    standard line, with the X-marked columns written out in words. Every other page, and every other document, goes
    through the section-aware text chunker."""
    info = {}
    chunks = []
    if m.get("parser") == "msd":
        with pdfplumber.open(pdf_path) as pdf:
            rows, other, unrec = msd_tables.parse_pdf(pdf)
        d = fmt_cite_date(m.get("pub_date", ""))
        for r in rows:
            chunks.append({"text": msd_tables.row_text(r), "section": f"{r['id']} ({r['section'].split(' DAF ')[0]})",
                           "page_start": r["page"], "page_end": r["page"], "cite": f"MSD, {d}, {r['id']}"})
        text_pages = [(pn, t) for pn, t in other if t and t.strip()]
        info.update({"msd_rows": len(rows), "msd_unrecognised_layout_pages": unrec, "text_pages": len(text_pages)})
        if not rows:
            raise RuntimeError("no standards rows recognised; the MSD page layout may have changed")
        chunks.extend(textproc.chunk_pages(text_pages))
        return chunks, info
    pages, ocr_pages = extract_pages(pdf_path, aclient)
    info.update({"pages": len(pages), "ocr_pages": ocr_pages})
    return textproc.chunk_pages(pages), info


def embed_all(vo, texts):
    """Adaptive batching (same approach as the clinical assistant: split a rate-limited batch)."""
    done = {}
    queue = [(i, min(i + EMBED_BATCH_SIZE, len(texts))) for i in range(0, len(texts), EMBED_BATCH_SIZE)]
    first = True
    while queue:
        start, end = queue.pop(0)
        if not first:
            time.sleep(EMBED_BATCH_DELAY_SEC)
        first = False
        try:
            res = vo.embed(texts[start:end], model=EMBED_MODEL, input_type="document")
            for off, emb in enumerate(res.embeddings):
                done[start + off] = emb
            print(f"  embedded {len(done)}/{len(texts)}")
        except voyageai.error.RateLimitError:
            if end - start == 1:
                raise RuntimeError(f"chunk {start} rate limited even alone; refusing to build an incomplete index")
            mid = (start + end) // 2
            queue.insert(0, (mid, end))
            queue.insert(0, (start, mid))
    return [done[i] for i in range(len(texts))]


def main():
    dry = "--dry-run" in sys.argv
    if not dry and not os.environ.get("VOYAGE_API_KEY"):
        sys.exit("ERROR: VOYAGE_API_KEY not set")
    ak = os.environ.get("ANTHROPIC_API_KEY")
    aclient = anthropic.Anthropic(api_key=ak) if ak else None
    manifest = load_manifest()
    pdfs = sorted(LIBRARY_DIR.glob("*.pdf")) if LIBRARY_DIR.exists() else []
    if not pdfs:
        sys.exit(f"No PDFs in {LIBRARY_DIR}; nothing to index.")

    ids, texts, metas, report = [], [], [], []
    for pdf_path in pdfs:
        m = manifest.get(pdf_path.name, {})
        print(f"Extracting {pdf_path.name} ...")
        try:
            chunks, info = build_chunks(pdf_path, m, aclient)
        except Exception as e:
            print(f"  WARNING: skipping {pdf_path.name}: {e}", file=sys.stderr)
            report.append({"file": pdf_path.name, "error": str(e)})
            continue
        date, basis = (m.get("pub_date"), "manifest") if m.get("pub_date") else document_date(pdf_path)
        short = m.get("short") or pdf_path.stem
        for i, c in enumerate(chunks):
            hdr = textproc.header_for(short, c["section"], c["page_start"], c["page_end"])
            ids.append(f"{pdf_path.name}:{i}")
            texts.append(hdr + "\n" + c["text"])
            metas.append({"source_filename": pdf_path.name, "doc": short, "title": m.get("title", ""),
                          "category": m.get("category", "other"), "section": c["section"], "cite": c.get("cite") or "",
                          "page_start": c["page_start"], "page_end": c["page_end"],
                          "pub_date": date, "date_basis": basis, "chunk_index": i})
        info.update({"file": pdf_path.name, "short": short, "chunks": len(chunks), "in_manifest": bool(m), "pub_date": date, "date_basis": basis})
        report.append(info)
        print(f"  {info}" + ("" if m else "  (not in manifest.json)"))

    if not texts:
        sys.exit("No text extracted from any document; index left unchanged.")
    if dry:
        print(f"DRY RUN: {len(texts)} chunks from {len(report)} documents; nothing embedded or written.")
        print(json.dumps(report, indent=1))
        return
    vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    print(f"Embedding {len(texts)} chunks ...")
    embeddings = embed_all(vo, texts)

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_or_create_collection(name=COLLECTION)
    old = col.get()
    if old["ids"]:
        col.delete(ids=old["ids"])
    for s in range(0, len(ids), 500):
        col.add(ids=ids[s:s + 500], embeddings=embeddings[s:s + 500], documents=texts[s:s + 500], metadatas=metas[s:s + 500])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "ingest_report.json").write_text(json.dumps({"built": time.strftime("%Y-%m-%dT%H:%M:%S"), "documents": report}, indent=1))
    (STATE_DIR / "index.stamp").write_text(str(time.time()))
    print(f"Indexed {len(ids)} chunks from {len(report)} documents.")


if __name__ == "__main__":
    main()
