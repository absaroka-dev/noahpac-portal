#!/usr/bin/env python3
"""api_server.py -- local Flask API for the Med Standards Assistant (NARSUM drafting, standards check, waiver packets).

POST /api/ask {mode, question, conversation_history, identifiers_removed}
Hybrid retrieval (Voyage vectors + BM25, fused) over the section-aware index built by ingest_usaf.py, then Claude answers
ONLY from the numbered excerpts. Every citation is validated against what was really retrieved, quoted paragraph/table
numbers are checked against the excerpt text, input and output are scanned for identifiers, and each request is counted
against a per-user daily cap and written to a content-free audit log.

Runs on 127.0.0.1:8768 behind nginx + Authelia. The user name comes from the X-Remote-User header that nginx sets
from the Authelia session; a request without it is refused (so calling the port directly gets nowhere).
"""
import datetime
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import anthropic
import chromadb
import voyageai
from flask import Flask, jsonify, request

import modes as modes_mod
import refcheck
import retrieval
import usage
from runtime_paths import AUDIT_LOG, CHROMA_DIR, HEARTBEAT_FILE, LIBRARY_DIR, STATE_DIR, USAGE_FILE
from safety_usaf import detected_identifier, exact_date_warning

EMBED_MODEL = "voyage-3"
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "claude-sonnet-5")
TOP_K = int(os.environ.get("USAF_TOP_K", "10"))
MAX_QUESTION_CHARS = int(os.environ.get("USAF_MAX_QUESTION_CHARS", "6000"))
MAX_HISTORY_TURNS = 8
MAX_RETRIEVAL_DISTANCE = float(os.environ.get("MAX_RETRIEVAL_DISTANCE", "0.95"))
MIN_BM25_FOR_EXACT = float(os.environ.get("USAF_MIN_BM25", "8.0"))
DAILY_CALLS = int(os.environ.get("USAF_DAILY_CALLS", "60"))
DAILY_TOKENS = int(os.environ.get("USAF_DAILY_TOKENS", "300000"))
STALE_DAYS = int(os.environ.get("USAF_STALE_DAYS", "540"))
GLOSSARY = retrieval.load_glossary(Path(__file__).resolve().parent / "glossary.json")
USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
DISCLAIMER = ("Decision support and drafting aid only. The listed publications, the responsible flight surgeon/medical authority and "
              "the waiver authority govern. Check e-Publishing for the current version before relying on any citation.")

app = Flask(__name__)
vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"]) if os.environ.get("VOYAGE_API_KEY") else None
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]) if os.environ.get("ANTHROPIC_API_KEY") else None
chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
collection = chroma.get_or_create_collection(name="usaf_docs")

_index = {"stamp": None, "ids": [], "bm25": None, "meta": {}, "docs": {}}


def stamp_value():
    try:
        return (STATE_DIR / "index.stamp").read_text()
    except OSError:
        return "none"


def load_index(force=False):
    """(Re)build the in-memory BM25 index when the ingest stamp changes."""
    st = stamp_value()
    if not force and _index["stamp"] == st:
        return
    data = collection.get(include=["documents", "metadatas"])
    _index["ids"] = data["ids"]
    _index["docs"] = dict(zip(data["ids"], data["documents"]))
    _index["meta"] = dict(zip(data["ids"], data["metadatas"]))
    _index["bm25"] = retrieval.BM25(data["documents"]) if data["documents"] else None
    _index["stamp"] = st


def retrieve(question, preferred):
    q = retrieval.expand_query(question, GLOSSARY)
    emb = vo.embed([q], model=EMBED_MODEL, input_type="query").embeddings[0]
    rankings, best_dist = [], None
    r = collection.query(query_embeddings=[emb], n_results=TOP_K, include=["distances"])
    rankings.append(r["ids"][0])
    if r["distances"][0]:
        best_dist = min(r["distances"][0])
    if preferred:
        try:
            rp = collection.query(query_embeddings=[emb], n_results=TOP_K, where={"category": {"$in": preferred}}, include=["distances"])
            rankings.append(rp["ids"][0])
        except Exception:
            pass
    best_bm25 = 0.0
    if _index["bm25"]:
        hits = _index["bm25"].top(q, TOP_K)
        rankings.append([_index["ids"][i] for i, _ in hits])
        best_bm25 = hits[0][1] if hits else 0.0
    fused = retrieval.rrf(rankings, top_n=TOP_K)
    return fused, best_dist, best_bm25


def pages_text(meta):
    a, b = meta.get("page_start"), meta.get("page_end")
    return f"p. {a}" if a == b else f"pp. {a}-{b}"


def stale_warning(meta):
    d = meta.get("pub_date")
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except (TypeError, ValueError):
        return f"{meta.get('doc')}: publication date unknown; confirm the version on e-Publishing."
    if age > STALE_DAYS:
        return f"{meta.get('doc')} is dated {d}; check e-Publishing for a newer version."
    return None


def reply(payload, status=200):
    return payload, status


def error(msg, status):
    return {"error": msg}, status


# Cloudflare cuts any request whose first byte takes more than about 5 s (seen on both of this host's zones), and an answer takes
# 10-40 s. So POST /api/ask only starts a job and returns at once; the page polls GET /api/result/<id>.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 1800
MAX_JOBS_PER_USER = 2
SLOTS = threading.BoundedSemaphore(int(os.environ.get("USAF_MAX_PARALLEL", "4")))


def _run_job(job_id, user, data):
    try:
        with SLOTS:
            payload, status = ask_impl(user, data)
    except Exception as e:  # never leave a job pending forever
        app.logger.exception("job failed")
        payload, status = {"error": f"Unexpected server error ({type(e).__name__}). Try again."}, 500
    with JOBS_LOCK:
        JOBS[job_id].update(done=True, status=status, payload=payload)


@app.route("/api/ask", methods=["POST"])
def ask():
    user = (request.headers.get("X-Remote-User") or "").strip()
    if not USER_RE.match(user):
        return jsonify({"error": "Sign in required."}), 401
    data = request.get_json(force=True, silent=True) or {}
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, j in JOBS.items() if now - j["created"] > JOB_TTL]:
            del JOBS[k]
        if sum(1 for j in JOBS.values() if j["user"] == user and not j["done"]) >= MAX_JOBS_PER_USER:
            return jsonify({"error": "Two questions are already being answered for you. Wait for one to finish."}), 429
        job_id = secrets.token_urlsafe(16)
        JOBS[job_id] = {"user": user, "created": now, "done": False}
    threading.Thread(target=_run_job, args=(job_id, user, data), daemon=True).start()
    return jsonify({"job": job_id}), 202


@app.route("/api/result/<job_id>")
def result(job_id):
    user = (request.headers.get("X-Remote-User") or "").strip()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j["user"] != user or not USER_RE.match(user):
            return jsonify({"error": "Unknown or expired request."}), 404
        if not j["done"]:
            return jsonify({"pending": True}), 200
        out = {"http_status": j["status"], "result": j["payload"]}
    return jsonify(out), 200


def ask_impl(user, data):
    if not vo or not claude:
        return error("Server not configured -- missing API key(s).", 503)

    mode = data.get("mode") or "ask"
    question = (data.get("question") or "").strip()
    history = data.get("conversation_history") or []
    if not modes_mod.valid_mode(mode):
        return error("Unknown mode.", 400)
    if not question:
        return error("question is required", 400)
    if len(question) > MAX_QUESTION_CHARS:
        return error("question too long", 400)
    if data.get("identifiers_removed") is not True:
        return error("Confirm that names, SSN, DoD ID, birth dates and other identifiers have been removed before submitting.", 400)
    turns = [t for t in history[-MAX_HISTORY_TURNS:] if isinstance(t, dict) and t.get("role") in ("user", "assistant") and t.get("content")]
    combined = "\n".join([question] + [str(t["content"]) for t in turns])
    ident = detected_identifier(combined)
    if ident:
        usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="blocked_identifier", kind=ident, qlen=len(question), qh=usage.qhash(question))
        return error(f"Submission blocked before external processing: possible {ident} detected. Replace it with a placeholder like [MEMBER] and try again.", 400)

    load_index()
    if not _index["ids"]:
        return reply({"answer": "No publications are indexed yet.", "citations": [], "unverified_references": [], "warnings": [], "disclaimer": DISCLAIMER, "mode": mode})

    ok, calls, tokens_used = usage.check_and_count(USAGE_FILE, user, DAILY_CALLS, DAILY_TOKENS)
    if not ok:
        usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="daily_cap", qlen=len(question))
        return error("Daily usage limit reached for your account. Try again tomorrow.", 429)

    try:
        prior = next((t["content"] for t in reversed(turns) if t["role"] == "user"), "")
        ids, best_dist, best_bm25 = retrieve((prior[:1500] + "\n" + question) if prior else question, modes_mod.MODES[mode]["categories"])
    except Exception as e:
        return error(f"retrieval failed: {e}", 502)
    exact_hit = best_bm25 >= MIN_BM25_FOR_EXACT
    vec_hit = best_dist is not None and best_dist <= MAX_RETRIEVAL_DISTANCE
    if not ids or not (vec_hit or exact_hit):
        usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="no_support", qlen=len(question), qh=usage.qhash(question), best_dist=best_dist, best_bm25=round(best_bm25, 2))
        return reply({"answer": "The loaded publications do not contain sufficiently close support for this question, so I cannot give a document-grounded answer. Try naming the publication, table, AFSC or condition more specifically.",
                      "citations": [], "unverified_references": [], "warnings": [], "disclaimer": DISCLAIMER, "mode": mode})

    excerpts = [(i + 1, _index["docs"][cid], _index["meta"][cid]) for i, cid in enumerate(ids)]
    context = "\n\n---\n\n".join(f"[S{n}]\n{text}" for n, text, _ in excerpts)
    messages = [{"role": t["role"], "content": t["content"]} for t in turns]
    messages.append({"role": "user", "content": f"Excerpts:\n\n{context}\n\n---\n\nUser message:\n{question}"})
    try:
        resp = claude.messages.create(model=ANSWER_MODEL, max_tokens=modes_mod.MODES[mode]["max_tokens"],
                                      system=modes_mod.system_prompt(mode), messages=messages)
    except Exception as e:
        return error(f"Claude API call failed: {e}", 502)
    toks = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
    usage.add_tokens(USAGE_FILE, user, toks)
    raw = "".join(b.text for b in resp.content if isinstance(getattr(b, "text", None), str)).strip()  # thinking blocks have no text
    if not raw:
        return error("The model returned no text. Try again.", 502)

    leaked = detected_identifier(raw)
    if leaked:
        usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="withheld_output_identifier", kind=leaked, qlen=len(question), tokens=toks)
        return reply({"answer": "The generated text contained something that looks like an identifier, so it was withheld. Try again with placeholders.",
                      "citations": [], "unverified_references": [], "warnings": [], "disclaimer": DISCLAIMER, "mode": mode})

    n = len(excerpts)
    bad_tags = refcheck.invalid_tags(raw, n)
    answer = re.sub(r"\[S(\d{1,2})\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", raw)
    tags = refcheck.used_tags(answer, n)
    warnings = []
    if not tags:
        if mode != "narsum":
            usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="withheld_no_citation", qlen=len(question), tokens=toks)
            return reply({"answer": "The generated response did not cite the retrieved publications, so it has been withheld.",
                          "citations": [], "unverified_references": [], "warnings": [], "disclaimer": DISCLAIMER, "mode": mode})
        warnings.append("This draft cites no publication. Add the governing standard before using it.")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        warnings.append("The answer was cut off at the length limit. Ask a narrower question, or reply 'continue'.")
    if bad_tags:
        warnings.append("The model referred to excerpts that do not exist; those tags were removed.")
    all_excerpt_text = "\n".join(t for _, t, _ in excerpts)
    unverified = refcheck.unverified_refs(answer, all_excerpt_text)
    citations, seen_docs = [], set()
    for tag in tags:
        _, _, meta = excerpts[tag - 1]
        citations.append({"tag": f"S{tag}", "cite": meta.get("cite") or None, "doc": meta.get("doc"), "title": meta.get("title") or None, "section": meta.get("section"),
                          "pages": pages_text(meta), "pub_date": meta.get("pub_date"), "date_basis": meta.get("date_basis")})
        if meta.get("doc") not in seen_docs:
            seen_docs.add(meta.get("doc"))
            w = stale_warning(meta)
            if w:
                warnings.append(w)
    if unverified:
        warnings.append("Some paragraph/table numbers in the text were not found in the retrieved excerpts; verify them in the publication: " + ", ".join(unverified))
    if exact_date_warning(combined):
        warnings.append("Your text contains exact dates. Prefer [DATE] or relative terms (for example '3 months ago') when you can.")

    usage.audit(AUDIT_LOG, user=user, mode=mode, outcome="answered", qlen=len(question), qh=usage.qhash(question), docs=sorted(seen_docs),
                cited=len(tags), unverified=len(unverified), tokens=toks, best_dist=best_dist, best_bm25=round(best_bm25, 2))
    return reply({"answer": answer, "citations": citations, "unverified_references": unverified, "warnings": warnings,
                  "disclaimer": DISCLAIMER, "mode": mode, "calls_today": calls, "calls_limit": DAILY_CALLS})


@app.route("/api/library")
def library():
    """What the assistant is working from: every loaded publication with its version/date, so the user can check it against e-Publishing."""
    user = (request.headers.get("X-Remote-User") or "").strip()
    if not USER_RE.match(user):
        return jsonify({"error": "Sign in required."}), 401
    try:
        rep = json.loads((STATE_DIR / "ingest_report.json").read_text())
    except (OSError, ValueError):
        rep = {}
    try:
        manifest = json.loads((LIBRARY_DIR / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    try:
        currency = json.loads((STATE_DIR / "currency.json").read_text())
    except (OSError, ValueError):
        currency = {}
    cdocs = currency.get("documents", {})
    docs = []
    for d in rep.get("documents", []):
        if d.get("error"):
            docs.append({"short": d.get("file"), "error": d["error"]})
            continue
        m = manifest.get(d.get("file"), {})
        age = None
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(d.get("pub_date"))).days
        except (TypeError, ValueError):
            pass
        c = cdocs.get(d.get("file")) or {}
        if c.get("status") == "current":
            age = None  # compared with the official file today, so the generic "old date" warning would only mislead
        docs.append({"currency": {"status": c.get("status"), "message": c.get("message")} if c else None, "short": m.get("short") or d.get("short"), "title": m.get("title") or d.get("title"), "version": m.get("version") or d.get("version"),
                     "pub_date": d.get("pub_date"), "date_basis": d.get("date_basis"), "category": m.get("category") or d.get("category"),
                     "file": d.get("file"), "chunks": d.get("chunks"), "pages": d.get("pages"), "msd_rows": d.get("msd_rows"),
                     "note": m.get("note") or d.get("note"), "older_than_limit": age is not None and age > STALE_DAYS,
                     "changed_since_index": bool(m) and m.get("pub_date") != d.get("pub_date")})
    return jsonify({"built": rep.get("built"), "currency_checked": currency.get("checked"), "stale_days": STALE_DAYS, "documents": docs})


@app.route("/api/modes")
def list_modes():
    return jsonify({k: v["label"] for k, v in modes_mod.MODES.items()})


@app.route("/health")
def health():
    load_index()
    try:
        rep = json.loads((STATE_DIR / "ingest_report.json").read_text())
    except (OSError, ValueError):
        rep = {}
    return jsonify({"ok": True, "chunks": collection.count(), "built": rep.get("built"), "documents": len(rep.get("documents", []))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8768, debug=False)
