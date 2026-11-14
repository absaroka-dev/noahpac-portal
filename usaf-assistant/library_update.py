#!/usr/bin/env python3
"""Staged, gated updates of the Med Standards Assistant library.

The daily currency check (check_library_currency.py) downloads a changed e-Publishing file into STATE_DIR/staging/ and raises an
alert, but never touches the library or the index. A person looks at the change and approves it here:

  library_update.py list                          staged publications and what is loaded
  library_update.py review "DAFI 31-117"          what changed (pages, sections, text diff, dates on the cover)
  library_update.py apply  "DAFI 31-117" [--version "..."] [--pub-date YYYY-MM-DD] [--dry-run] [--yes]
  library_update.py rollback "DAFI 31-117"        put back the library, manifest and index from the last backup

apply never edits the live index in place. It (1) backs up the library file, manifest, report and index, (2) builds a complete
candidate library + index in a separate directory with the normal ingest, (3) evaluates the candidate (structure against the old
version, self-retrieval probes taken from the old index, optional golden questions in eval/golden.json) and stops if the gate
fails, (4) only then stops the API, swaps the index and the file in, starts the API and checks it, and (5) restores the backup
automatically if anything after the swap starts goes wrong. Publications are public documents; nothing here sends document text
anywhere except the embedding calls ingest_usaf.py already makes.
"""
import argparse
import datetime
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runtime_paths import LIBRARY_DIR, STATE_DIR  # noqa: E402
import retrieval  # noqa: E402
import textproc  # noqa: E402

STAGING_DIR = STATE_DIR / "staging"
BACKUP_DIR = STATE_DIR / "backups"
LOCK_FILE = STATE_DIR / "update.lock"
UPDATE_LOG = STATE_DIR / "updates.log"
GOLDEN_FILE = HERE / "eval" / "golden.json"
ENV_FILE = os.environ.get("USAF_UPDATE_ENV_FILE", "/etc/usaf-assistant-api.env")
PYTHON = os.environ.get("USAF_UPDATE_PYTHON", "/opt/noahpac/clinical-assistant/venv/bin/python3")
SERVICE = os.environ.get("USAF_UPDATE_SERVICE", "usaf-assistant-api")
RUN_AS = os.environ.get("USAF_UPDATE_RUN_AS", "www-data")
COLLECTION = "usaf_docs"
EMBED_MODEL = "voyage-3"
KEEP_BACKUPS = 3
PROBES = 25
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
_DATE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\.?,?\s+(\d{{4}})\b", re.I)


class UpdateError(Exception):
    pass


# --------------------------------------------------------------------------- manifest, staging


def load_manifest():
    return json.loads((LIBRARY_DIR / "manifest.json").read_text())


def find_doc(manifest, name):
    """(library file name, manifest entry) for a file name or short name (case-insensitive, exact)."""
    hits = [(f, e) for f, e in manifest.items()
            if isinstance(e, dict) and (f.lower() == name.lower() or str(e.get("short", "")).lower() == name.lower())]
    if not hits:
        raise UpdateError(f'no publication named "{name}" in the manifest')
    if len(hits) > 1:
        raise UpdateError(f'"{name}" matches several publications; use the file name')
    return hits[0]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def staged_for(fname):
    """(staged PDF path, meta). Raises unless the staged file is intact."""
    folder = STAGING_DIR / fname
    try:
        meta = json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        raise UpdateError(f"nothing is staged for {fname} (the daily check stages a file when e-Publishing differs from the loaded copy)")
    path = folder / meta.get("staged_file", "")
    if not path.is_file() or sha256_file(path) != meta.get("sha256"):
        raise UpdateError(f"the staged file for {fname} is missing or does not match its recorded hash; run the currency check again")
    return path, meta


# --------------------------------------------------------------------------- reading publications


def pdf_pages(path):
    r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise UpdateError(f"pdftotext failed on {Path(path).name}: {r.stderr.strip()[:200]}")
    return [(i + 1, t) for i, t in enumerate(r.stdout.split("\f")) if t.strip()]


def base_section(label):
    return label.split(" (cont.)")[0]


def section_texts(pages):
    """{section label: text} using the same section-aware chunker the index uses."""
    out = {}
    for c in textproc.chunk_pages(pages):
        out.setdefault(base_section(c["section"]), []).append(c["text"])
    return {k: "\n".join(v) for k, v in out.items()}


def norm_lines(text):
    lines = (re.sub(r"\s+", " ", l).strip() for l in text.splitlines())
    return [l for l in lines if l and not re.fullmatch(r"(?:page\s+)?\d{1,4}(?:\s+of\s+\d{1,4})?", l, re.I)]


def norm_text(text):
    return " ".join(norm_lines(text)).lower()


def compare_sections(old, new):
    """Sections added, removed and changed between two {label: text} maps."""
    o, n = set(old), set(new)
    common = sorted(o & n)
    changed = [k for k in common if norm_text(old[k]) != norm_text(new[k])]
    return {"added": sorted(n - o), "removed": sorted(o - n), "changed": changed, "same": len(common) - len(changed)}


def diff_excerpt(old_text, new_text, max_lines=60):
    out = []
    for line in difflib.unified_diff(norm_lines(old_text), norm_lines(new_text), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        out.append(line[:160])
        if len(out) >= max_lines:
            out.append("... (diff cut off)")
            break
    return out


def find_dates(pages, first=3):
    seen = []
    for number, text in pages:
        if number > first:
            continue
        for m in _DATE.finditer(text):
            d = f"{int(m.group(1))} {m.group(2).title()} {m.group(3)}"
            if d not in seen:
                seen.append(d)
    return seen[:8]


# --------------------------------------------------------------------------- the gate (pure)


def gate(old_info, new_info, old_sections, new_sections, probes, golden):
    """Decide whether a candidate may go live. Pure function of the measurements (see tests).
    old_info/new_info: {"chunks": int, "pages": int|None, "error": str|None}; *_sections: sets of section labels;
    probes: {"asked": int, "found": int}; golden: {"asked": int, "failed": [question,...]}."""
    reasons, warnings = [], []
    if new_info.get("error"):
        reasons.append(f"ingest reported an error for this file: {new_info['error']}")
    nc, oc = new_info.get("chunks") or 0, old_info.get("chunks") or 0
    if nc == 0:
        reasons.append("the new file produced no text chunks (scanned or damaged PDF?)")
    elif oc and nc < 0.5 * oc:
        reasons.append(f"the new file has far fewer chunks than the loaded one ({nc} vs {oc})")
    elif oc and nc > 3 * oc:
        warnings.append(f"the new file has many more chunks than the loaded one ({nc} vs {oc})")
    npg, opg = new_info.get("pages"), old_info.get("pages")
    if npg and opg and npg < 0.5 * opg:
        reasons.append(f"the new file has far fewer pages ({npg} vs {opg})")
    preserved = None
    if len(old_sections) >= 10:
        preserved = len(old_sections & new_sections) / len(old_sections)
        if preserved < 0.6:
            reasons.append(f"only {preserved:.0%} of the old sections still exist in the new file")
        elif preserved < 0.85:
            warnings.append(f"{preserved:.0%} of the old sections still exist in the new file")
    rate = None
    if probes["asked"] < 5:
        warnings.append(f"only {probes['asked']} retrieval probes could be built; the retrieval check is weak")
    else:
        rate = probes["found"] / probes["asked"]
        if rate < 0.8:
            reasons.append(f"retrieval check failed: {probes['found']} of {probes['asked']} passages from the old version were found in the new index")
        elif rate < 0.9:
            warnings.append(f"retrieval check: {probes['found']} of {probes['asked']} passages found")
    for q in golden.get("failed", []):
        reasons.append(f'golden question not answered by the new index: "{q}"')
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings,
            "numbers": {"chunks": [oc, nc], "pages": [opg, npg], "sections_preserved": preserved, "probe_rate": rate,
                        "golden": [golden.get("asked", 0) - len(golden.get("failed", [])), golden.get("asked", 0)]}}


def make_probes(old_chunks, new_sections, n=PROBES):
    """Evenly spread passages of the OLD version whose section still exists in the new one: (query text, expected section)."""
    cands = [c for c in old_chunks if base_section(c["section"]) in new_sections and len(c["body"]) >= 200 and c["section"] != "(front matter)"]
    cands.sort(key=lambda c: (c.get("page_start") or 0, c["section"]))
    step = max(1, len(cands) // n)
    return [{"query": " ".join(c["body"].split())[:240], "section": base_section(c["section"])} for c in cands[::step][:n]]


# --------------------------------------------------------------------------- index access (needs chromadb/voyageai: venv only)


def load_env_file(path):
    """KEY=VALUE lines of the service env file into this process (values are never printed)."""
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


def load_chunks(chroma_path, fname):
    import chromadb
    col = chromadb.PersistentClient(path=str(chroma_path)).get_collection(COLLECTION)
    data = col.get(where={"source_filename": fname}, include=["documents", "metadatas"])
    out = []
    for doc, meta in zip(data["documents"], data["metadatas"]):
        out.append({"section": meta.get("section", ""), "page_start": meta.get("page_start"), "body": doc.split("\n", 1)[-1]})
    return out


class Searcher:
    """Hybrid retrieval over one Chroma directory, mirroring api_server.retrieve (vector + BM25, reciprocal-rank fusion)."""

    def __init__(self, chroma_path, embed):
        import chromadb
        self.col = chromadb.PersistentClient(path=str(chroma_path)).get_collection(COLLECTION)
        data = self.col.get(include=["documents", "metadatas"])
        self.ids, self.meta = data["ids"], dict(zip(data["ids"], data["metadatas"]))
        self.bm25 = retrieval.BM25(data["documents"]) if data["documents"] else None
        self.embed = embed

    def top(self, query, k=5):
        vec = self.col.query(query_embeddings=[self.embed(query)], n_results=10)["ids"][0]
        rankings = [vec]
        if self.bm25:
            rankings.append([self.ids[i] for i, _ in self.bm25.top(query, 10)])
        return [self.meta[i] for i in retrieval.rrf(rankings, top_n=k)]


def voyage_embedder():
    import voyageai
    vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    return lambda q: vo.embed([q], model=EMBED_MODEL, input_type="query").embeddings[0]


def run_probes(searcher, fname, probes):
    found = 0
    for p in probes:
        hits = searcher.top(p["query"], 5)
        if any(h.get("source_filename") == fname and base_section(h.get("section", "")) == p["section"] for h in hits):
            found += 1
    return {"asked": len(probes), "found": found}


def run_golden(searcher, short):
    try:
        items = [g for g in json.loads(GOLDEN_FILE.read_text()).get("questions", []) if g.get("doc") == short]
    except (OSError, ValueError):
        items = []
    failed = []
    for g in items:
        hits = searcher.top(g["question"], 5)
        if not any(h.get("doc") == short and g["section_contains"].lower() in h.get("section", "").lower() for h in hits):
            failed.append(g["question"])
    return {"asked": len(items), "failed": failed}


# --------------------------------------------------------------------------- filesystem helpers


def _chown(path, recursive=True):
    if os.geteuid() != 0:
        return
    try:
        import pwd
        u = pwd.getpwnam(RUN_AS)
    except (ImportError, KeyError):
        return
    paths = [Path(path)] + (list(Path(path).rglob("*")) if recursive and Path(path).is_dir() else [])
    for p in paths:
        try:
            os.chown(p, u.pw_uid, u.pw_gid)
        except OSError:
            pass


def copy_atomic(src, dst):
    """Copy to dst atomically, keeping the mode and owner of the file it replaces (or www-data for a new file)."""
    tmp = Path(str(dst) + ".new")
    shutil.copyfile(src, tmp)
    if Path(dst).exists():
        st = os.stat(dst)
        os.chmod(tmp, st.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
    else:
        shutil.copymode(src, tmp)
        _chown(tmp, recursive=False)
    os.replace(tmp, dst)


def service(action, args):
    if args.no_restart:
        return
    r = subprocess.run(["systemctl", action, SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError(f"systemctl {action} {SERVICE} failed: {r.stderr.strip()[:200]}")


def wait_active(args, seconds=40):
    if args.no_restart:
        return
    end = time.time() + seconds
    while time.time() < end:
        if subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0:
            time.sleep(3)  # still active after a few seconds: not crash-looping
            if subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0:
                return
        time.sleep(1)
    raise UpdateError(f"{SERVICE} did not come back active")


@contextmanager
def lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise UpdateError("another library update is running")
    try:
        yield
    finally:
        f.close()


def log_event(**kw):
    kw["time"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(UPDATE_LOG, "a") as f:
        f.write(json.dumps(kw) + "\n")
    _chown(UPDATE_LOG, recursive=False)


def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


# --------------------------------------------------------------------------- backup, candidate, swap, restore


def make_backup(fname, ts):
    b = BACKUP_DIR / f"{ts}-{safe(fname)}"
    b.mkdir(parents=True)
    for rel, src in ((f"library/{fname}", LIBRARY_DIR / fname), ("library/manifest.json", LIBRARY_DIR / "manifest.json"),
                     ("ingest_report.json", STATE_DIR / "ingest_report.json"), ("index.stamp", STATE_DIR / "index.stamp")):
        if src.exists():
            (b / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, b / rel)
    shutil.copytree(STATE_DIR / "chroma_data", b / "chroma_data")
    (b / "WHAT").write_text(f"{fname}\n")
    return b


def prune_backups(fname):
    mine = sorted(BACKUP_DIR.glob(f"*-{safe(fname)}"))
    for old in mine[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


def restore_backup(backup, fname, args):
    """Library file, manifest, report, stamp and index back from a backup directory."""
    service("stop", args)
    live = STATE_DIR / "chroma_data"
    if live.exists():
        shutil.rmtree(live)
    shutil.copytree(backup / "chroma_data", live)
    _chown(live)
    for rel, dst in ((f"library/{fname}", LIBRARY_DIR / fname), ("library/manifest.json", LIBRARY_DIR / "manifest.json"),
                     ("ingest_report.json", STATE_DIR / "ingest_report.json"), ("index.stamp", STATE_DIR / "index.stamp")):
        if (backup / rel).exists():
            copy_atomic(backup / rel, dst)
    service("start", args)
    wait_active(args)


def build_candidate(fname, staged_pdf, new_entry, cand, args):
    (cand / "library").mkdir(parents=True)
    for p in LIBRARY_DIR.iterdir():
        if p.is_file() and p.name not in (fname, "manifest.json"):
            shutil.copy2(p, cand / "library" / p.name)
    shutil.copy2(staged_pdf, cand / "library" / fname)
    manifest = load_manifest()
    manifest[fname] = new_entry
    (cand / "library" / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    _chown(cand)
    env = {"USAF_ASSISTANT_STATE_DIR": str(cand), "USAF_ASSISTANT_LIBRARY_DIR": str(cand / "library")}
    if os.geteuid() == 0 and RUN_AS and not args.run_as_self:
        script = f"set -a; source '{ENV_FILE}'; set +a; export USAF_ASSISTANT_STATE_DIR='{cand}' USAF_ASSISTANT_LIBRARY_DIR='{cand / 'library'}'; cd '{HERE}'; exec '{PYTHON}' ingest_usaf.py"
        cmd = ["runuser", "-u", RUN_AS, "--", "bash", "-c", script]
        child_env = None
    else:
        load_env_file(ENV_FILE)
        cmd = [PYTHON, str(HERE / "ingest_usaf.py")]
        child_env = dict(os.environ, **env)
    r = subprocess.run(cmd, capture_output=True, text=True, env=child_env, cwd=str(HERE))
    (cand / "ingest.log").write_text(r.stdout + "\n" + r.stderr)
    if r.returncode != 0:
        raise UpdateError("the candidate ingest failed (see " + str(cand / "ingest.log") + "): " + (r.stderr or r.stdout).strip().splitlines()[-1][:200])


def report_entry(report_path, fname):
    try:
        for d in json.loads(Path(report_path).read_text()).get("documents", []):
            if d.get("file") == fname:
                return d
    except (OSError, ValueError):
        pass
    return {}


# --------------------------------------------------------------------------- commands


def cmd_list(args):
    manifest = load_manifest()
    try:
        cur = json.loads((STATE_DIR / "currency.json").read_text()).get("documents", {})
    except (OSError, ValueError):
        cur = {}
    print(f"{'publication':44} {'status':17} staged")
    for f, e in sorted(manifest.items(), key=lambda kv: str(kv[1].get("short") if isinstance(kv[1], dict) else kv[0])):
        if not isinstance(e, dict):
            continue
        d = cur.get(f, {})
        staged = "-"
        try:
            meta = json.loads((STATE_DIR / "staging" / f / "meta.json").read_text())
            staged = f"yes ({meta.get('fetched_at')}, e-Publishing modified {meta.get('remote_last_modified')})"
        except (OSError, ValueError):
            pass
        print(f"{e.get('short', f)[:43]:44} {d.get('status', '?'):17} {staged}")


def review_data(fname):
    path, meta = staged_for(fname)
    old_pages, new_pages = pdf_pages(LIBRARY_DIR / fname), pdf_pages(path)
    old_s, new_s = section_texts(old_pages), section_texts(new_pages)
    cmp = compare_sections(old_s, new_s)
    old_all, new_all = "\n".join(t for _, t in old_pages), "\n".join(t for _, t in new_pages)
    return {"meta": meta, "old_pages": old_pages, "new_pages": new_pages, "cmp": cmp, "old_all": old_all, "new_all": new_all}


def print_review(entry, d):
    meta, cmp = d["meta"], d["cmp"]
    print(f"\n=== {entry.get('short')}: loaded copy vs the staged e-Publishing file ===")
    print(f"loaded : {entry.get('version')}  ({len(d['old_pages'])} pages)")
    print(f"staged : e-Publishing modified {meta.get('remote_last_modified')}, fetched {meta.get('fetched_at')}  ({len(d['new_pages'])} pages, {meta.get('size')} bytes)")
    print(f"dates on the first pages of the staged file: {', '.join(find_dates(d['new_pages'])) or 'none found'}")
    print(f"dates on the first pages of the loaded file: {', '.join(find_dates(d['old_pages'])) or 'none found'}")
    print(f"words: {len(d['old_all'].split())} -> {len(d['new_all'].split())}")
    print(f"sections: {cmp['same']} unchanged, {len(cmp['changed'])} changed, {len(cmp['added'])} added, {len(cmp['removed'])} removed")
    for label, key in (("added", "added"), ("removed", "removed"), ("changed", "changed")):
        if cmp[key]:
            print(f"  {label}: " + "; ".join(cmp[key][:14]) + (f"; ... (+{len(cmp[key]) - 14} more)" if len(cmp[key]) > 14 else ""))
    print("text changes (- loaded / + staged):")
    for line in diff_excerpt(d["old_all"], d["new_all"]) or ["  (no text differences found; only the file bytes differ)"]:
        print("  " + line)


def cmd_review(args):
    fname, entry = find_doc(load_manifest(), args.doc)
    d = review_data(fname)
    print_review(entry, d)
    dates = find_dates(d["new_pages"])
    print("\nTo load it:  sudo ops/usaf-assistant-update.sh apply " + f'"{entry.get("short")}" --version "{entry.get("short")}, <edition date from the cover>" --pub-date YYYY-MM-DD')
    if dates:
        print("(dates seen on the cover pages: " + ", ".join(dates) + ")")


def new_manifest_entry(entry, meta, args, today):
    e = dict(entry)
    remote = (meta.get("remote_last_modified") or "").strip()
    e["version"] = args.version or f"{entry.get('short')}, replaced from e-Publishing on {today.isoformat()} (file modified {remote or 'date unknown'}); edition date not confirmed"
    if args.pub_date:
        datetime.date.fromisoformat(args.pub_date)
        e["pub_date"] = args.pub_date
    if args.note is not None:
        e["note"] = args.note
    e["last_verified"] = today.isoformat()
    return e


def cmd_apply(args):
    if not args.dry_run and not args.no_restart and os.geteuid() != 0:
        raise UpdateError("run apply as root (sudo ops/usaf-assistant-update.sh apply ...); --dry-run works without it")
    manifest = load_manifest()
    fname, entry = find_doc(manifest, args.doc)
    staged_pdf, meta = staged_for(fname)
    today = datetime.date.today()
    d = review_data(fname)
    print_review(entry, d)
    new_entry = new_manifest_entry(entry, meta, args, today)
    print(f"\nmanifest version will read: {new_entry['version']}")
    if not args.pub_date:
        print(f"  (publication date stays {new_entry.get('pub_date')}; pass --pub-date YYYY-MM-DD if the cover shows a newer edition date)")
    if not args.version:
        print("  (no --version given: the page will say the edition date is not confirmed; pass --version with the cover edition to fix that)")
    with lock():
        ts = time.strftime("%Y%m%dT%H%M%S")
        cand = STATE_DIR / f"candidate-{ts}"
        backup = None
        swapping = False
        try:
            backup = make_backup(fname, ts)
            print(f"\nbackup: {backup}")
            print("building the candidate index (a full ingest into a separate directory; the live index is not touched) ...")
            build_candidate(fname, staged_pdf, new_entry, cand, args)
            load_env_file(ENV_FILE)
            old_chunks, new_chunks = load_chunks(backup / "chroma_data", fname), load_chunks(cand / "chroma_data", fname)
            old_sec, new_sec = {base_section(c["section"]) for c in old_chunks}, {base_section(c["section"]) for c in new_chunks}
            searcher = Searcher(cand / "chroma_data", voyage_embedder())
            probes = run_probes(searcher, fname, make_probes(old_chunks, new_sec))
            golden = run_golden(searcher, entry.get("short"))
            old_info = report_entry(STATE_DIR / "ingest_report.json", fname)
            new_info = report_entry(cand / "ingest_report.json", fname)
            result = gate(old_info, new_info, old_sec, new_sec, probes, golden)
            print("\n=== evaluation of the candidate ===")
            print(json.dumps(result["numbers"]))
            for w in result["warnings"]:
                print("  warning:", w)
            for r in result["reasons"]:
                print("  FAIL   :", r)
            if not result["ok"] and not args.force:
                print("\nThe gate failed; nothing was changed. Read the review above, or use --force only if you are sure.")
                log_event(action="apply", doc=fname, outcome="gate_failed", reasons=result["reasons"])
                return 2
            if args.dry_run:
                print("\n--dry-run: stopping here; nothing was changed." + ("" if result["ok"] else " (the gate failed)"))
                return 0
            if not args.yes and input('\nType "yes" to load the staged file into the live library: ').strip().lower() != "yes":
                print("cancelled; nothing was changed.")
                return 1
            swapping = True
            swap(cand, fname, new_entry, backup, args)
        except UpdateError as e:
            log_event(action="apply", doc=fname, outcome="error", error=str(e))
            print("ERROR:", e, file=sys.stderr)
            return 1
        finally:
            if not args.keep_candidate:
                shutil.rmtree(cand, ignore_errors=True)
            if backup and not swapping:
                shutil.rmtree(backup, ignore_errors=True)  # nothing was swapped: dry runs and refused candidates must not use up the kept backups
    log_event(action="apply", doc=fname, outcome="ok", version=new_entry["version"], sha256=meta["sha256"], backup=str(backup), forced=bool(args.force))
    shutil.rmtree(STAGING_DIR / fname, ignore_errors=True)
    prune_backups(fname)
    print(f"\nDone: {entry.get('short')} is loaded. Backup kept at {backup} (roll back with: rollback \"{entry.get('short')}\").")
    refresh_currency(args)
    return 0


def swap(cand, fname, new_entry, backup, args):
    live = STATE_DIR / "chroma_data"
    moved = STATE_DIR / f"chroma_data.replaced-{backup.name}"
    service("stop", args)
    try:
        os.rename(live, moved)
        os.rename(cand / "chroma_data", live)
        _chown(live)
        copy_atomic(cand / "library" / fname, LIBRARY_DIR / fname)
        copy_atomic(cand / "library" / "manifest.json", LIBRARY_DIR / "manifest.json")
        copy_atomic(cand / "ingest_report.json", STATE_DIR / "ingest_report.json")
        copy_atomic(cand / "index.stamp", STATE_DIR / "index.stamp")
        service("start", args)
        wait_active(args)
        if report_entry(STATE_DIR / "ingest_report.json", fname).get("version") != new_entry["version"]:
            raise UpdateError("the live report does not show the new version after the swap")
    except Exception as e:
        print(f"swap failed ({e}); restoring the backup ...", file=sys.stderr)
        try:
            shutil.rmtree(moved, ignore_errors=True)
            restore_backup(backup, fname, args)
        except Exception as e2:
            raise UpdateError(f"swap failed ({e}) AND the automatic restore failed ({e2}); restore by hand from {backup}")
        raise UpdateError(f"swap failed ({e}); the previous library and index were restored")
    shutil.rmtree(moved, ignore_errors=True)


def refresh_currency(args):
    """Re-run the currency check (with this process's state/library paths, never hard-coded ones) so the alert clears at once."""
    if args.no_restart:
        print("rehearsal (--no-restart): not re-running the currency check")
        return
    env = dict(os.environ, HOME=os.environ.get("HOME", "/root"), PLAYWRIGHT_TEST_DIR=os.environ.get("PLAYWRIGHT_TEST_DIR", "/var/www/noahpac-portal/tests"),
               USAF_ASSISTANT_STATE_DIR=str(STATE_DIR), USAF_ASSISTANT_LIBRARY_DIR=str(LIBRARY_DIR))
    r = subprocess.run(["python3", str(HERE / "check_library_currency.py")],
                       capture_output=True, text=True, env=env, cwd=str(HERE))
    _chown(STATE_DIR / "currency.json", recursive=False)
    print("currency check re-run" + ("" if r.returncode == 0 else f" (failed: {r.stderr.strip()[-120:]}; the daily run will fix it)"))


def cmd_rollback(args):
    if os.geteuid() != 0 and not args.no_restart:
        raise UpdateError("run rollback as root")
    fname, entry = find_doc(load_manifest(), args.doc)
    mine = sorted(BACKUP_DIR.glob(f"*-{safe(fname)}"))
    if not mine:
        raise UpdateError(f"no backup of {fname} found in {BACKUP_DIR}")
    backup = mine[-1]
    print(f"restoring {entry.get('short')} from {backup} (library file, manifest, report and index) ...")
    if not args.yes and input('Type "yes" to continue: ').strip().lower() != "yes":
        print("cancelled.")
        return 1
    with lock():
        restore_backup(backup, fname, args)
    log_event(action="rollback", doc=fname, outcome="ok", backup=str(backup))
    refresh_currency(args)
    print("restored.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("review", "apply", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("doc")
        if name in ("apply", "rollback"):
            p.add_argument("--yes", action="store_true")
            p.add_argument("--no-restart", action="store_true", help="rehearsal on a copy of the state: do not stop/start the service")
        if name == "apply":
            p.add_argument("--version")
            p.add_argument("--pub-date")
            p.add_argument("--note")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--force", action="store_true", help="load even though the gate failed")
            p.add_argument("--keep-candidate", action="store_true")
            p.add_argument("--run-as-self", action="store_true", help="run the candidate ingest as the current user (rehearsal)")
    args = ap.parse_args(argv)
    try:
        return {"list": cmd_list, "review": cmd_review, "apply": cmd_apply, "rollback": cmd_rollback}[args.cmd](args) or 0
    except UpdateError as e:
        print("ERROR:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
