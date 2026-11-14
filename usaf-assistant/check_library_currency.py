#!/usr/bin/env python3
"""Is every loaded publication still the current one?  (Med Standards Assistant library monitor)

Two kinds of check, chosen per publication in the library manifest (`library/manifest.json`):
  * `check_url`  - a public e-Publishing PDF. The file is fetched (through a real browser, see fetch-epub.mjs: the site refuses scripts)
                   and its SHA-256 compared with the loaded copy. Same bytes = current; different bytes = a new version or a
                   corrected re-issue is published, review it and reload the library.
  * no URL       - publications that sit behind CAC (Medical Standards Directory, classification directories, waiver guide) cannot be
                   fetched. The manifest records `last_verified` (the date a person last confirmed it is the newest) and
                   `verify_every_days`; when that date passes, the publication is reported as "verification due".
Results go to STATE_DIR/currency.json (shown on the page through /api/library) and to a Prometheus textfile (Grafana alerts).

  check_library_currency.py                run all checks
  check_library_currency.py --offline      only the date-based checks (no browser)
  check_library_currency.py --mark-verified FILE_OR_SHORT [...]   record that a person has just confirmed it is current
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runtime_paths import LIBRARY_DIR, STATE_DIR  # noqa: E402

CURRENCY_FILE = STATE_DIR / "currency.json"
STAGING_DIR = STATE_DIR / "staging"  # changed publications downloaded for review (library_update.py reads them)
TEXTFILE = Path(os.environ.get("USAF_CURRENCY_TEXTFILE", "/var/lib/node_exporter/textfile_collector/usaf_library_currency.prom"))
DEFAULT_VERIFY_DAYS = 90


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def evaluate(manifest, fingerprints, local_hashes, today, previous=None):
    """Pure decision logic. fingerprints: {url: {status, sha256, last_modified, len, error?}}; local_hashes: {file: sha256}.
    previous: the earlier currency.json documents (to count consecutive failures). Returns {file: {...}}."""
    previous = previous or {}
    out = {}
    for fname, m in manifest.items():
        if fname.startswith("_") or not isinstance(m, dict):
            continue
        rec = {"short": m.get("short") or fname, "mode": "url" if m.get("check_url") else "date"}
        url = m.get("check_url")
        if url:
            fp = fingerprints.get(url) or {}
            lm = fp.get("last_modified")
            if fp.get("status") == 200 and fp.get("sha256"):
                rec["failures"] = 0
                rec["remote_last_modified"] = lm
                if local_hashes.get(fname) == fp["sha256"]:
                    rec.update(status="current", message="Same file as the one on e-Publishing" + (f" (last modified there {lm})." if lm else "."))
                else:
                    rec.update(status="update_available",
                               message="e-Publishing has a different file" + (f" (last modified {lm})" if lm else "") +
                                       " than the loaded copy. Review it and reload the library.")
            else:
                fails = (previous.get(fname) or {}).get("failures", 0) + 1
                rec.update(status="unreachable", failures=fails,
                           message="Could not check e-Publishing this time" + (f" ({fp.get('error') or 'HTTP ' + str(fp.get('status'))})." if fp else "."))
                # keep the last real answer visible if there is one
                prev = previous.get(fname) or {}
                if prev.get("status") in ("current", "update_available"):
                    rec["last_known"] = {"status": prev["status"], "message": prev.get("message")}
        else:
            verified = parse_date(m.get("last_verified"))
            days = int(m.get("verify_every_days") or DEFAULT_VERIFY_DAYS)
            if verified is None:
                rec.update(status="verify_due", message="Never verified: confirm this is the newest version, then record it.", due=None)
            else:
                due = verified + datetime.timedelta(days=days)
                rec["due"] = due.isoformat()
                if today >= due:
                    rec.update(status="verify_due", message=f"Last confirmed current on {verified.isoformat()}; check again (every {days} days) on the official site and record it.")
                else:
                    rec.update(status="verified", message=f"Last confirmed current on {verified.isoformat()}; next check due {due.isoformat()}.")
        out[fname] = rec
    return out


def stage_updates(manifest, docs, fingerprints, incoming, staging=None, now=None):
    """Keep the downloaded file of every publication that differs from the loaded copy, and forget staged files that are no longer
    different (the library was updated, or e-Publishing went back). Staged: STAGING/<library file>/<sha12>-<name> + meta.json.
    Never touches the library or the index. Sets docs[f]["staged"] (or ["stage_error"]) and returns the list of staged file names."""
    staging = Path(staging or STAGING_DIR)
    now = now or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    staged = []
    for fname, d in docs.items():
        m = manifest.get(fname) or {}
        folder = staging / fname
        if d.get("status") != "update_available":
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)  # no longer different from e-Publishing
            continue
        fp = fingerprints.get(m.get("check_url")) or {}
        saved = fp.get("saved")
        if not saved or not Path(saved).is_file():
            d["stage_error"] = "download did not return a file"
            continue
        if sha256_file(saved) != fp.get("sha256"):
            d["stage_error"] = "downloaded file does not match the fingerprint"
            continue
        target = folder / f"{fp['sha256'][:12]}-{fname}"
        if not target.is_file():
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)  # an older staged version is replaced
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(saved, target)
        meta = {"file": fname, "short": d["short"], "url": m.get("check_url"), "sha256": fp["sha256"], "size": target.stat().st_size,
                "remote_last_modified": fp.get("last_modified"), "fetched_at": now, "loaded_sha256": sha256_file(Path(LIBRARY_DIR) / fname) if (Path(LIBRARY_DIR) / fname).is_file() else None,
                "staged_file": target.name}
        write_atomic(folder / "meta.json", json.dumps(meta, indent=1) + "\n", 0o644)
        d["staged"] = {"sha256": fp["sha256"], "size": meta["size"], "fetched_at": now, "remote_last_modified": fp.get("last_modified")}
        d["message"] += f' A copy is staged: run ops/usaf-assistant-update.sh review "{d["short"]}".'
        staged.append(fname)
    return staged


def write_atomic(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def metrics_text(docs, now):
    n = lambda s: sum(1 for d in docs.values() if d["status"] == s)
    lines = ["# HELP usaf_library_documents Publications in the Med Standards Assistant library", "# TYPE usaf_library_documents gauge", f"usaf_library_documents {len(docs)}",
             "# HELP usaf_library_update_available Publications whose e-Publishing file differs from the loaded copy", "# TYPE usaf_library_update_available gauge", f"usaf_library_update_available {n('update_available')}",
             "# HELP usaf_library_verify_due Publications past their manual verification date", "# TYPE usaf_library_verify_due gauge", f"usaf_library_verify_due {n('verify_due')}",
             "# HELP usaf_library_check_failures Highest count of consecutive failed e-Publishing checks", "# TYPE usaf_library_check_failures gauge",
             f"usaf_library_check_failures {max([d.get('failures', 0) for d in docs.values()] or [0])}",
             "# HELP usaf_library_currency_last_success_timestamp_seconds When the currency check last completed", "# TYPE usaf_library_currency_last_success_timestamp_seconds gauge",
             f"usaf_library_currency_last_success_timestamp_seconds {int(now)}",
             "# HELP usaf_library_doc_status Per publication status (1 = current state)", "# TYPE usaf_library_doc_status gauge"]
    for f, d in sorted(docs.items()):
        lines.append(f'usaf_library_doc_status{{doc="{d["short"].replace(chr(34), "")}",status="{d["status"]}"}} 1')
    lines += ["# HELP usaf_library_doc_staged 1 for a publication whose newer e-Publishing file is downloaded and waiting for review", "# TYPE usaf_library_doc_staged gauge"]
    for f, d in sorted(docs.items()):
        if d.get("staged"):
            lines.append(f'usaf_library_doc_staged{{doc="{d["short"].replace(chr(34), "")}"}} 1')
    return "\n".join(lines) + "\n"


def fetch_fingerprints(urls, save_dir=None):
    """Fingerprint every URL in one browser session; with save_dir the bytes of each PDF are also written there (rec["saved"])."""
    if not urls:
        return {}
    cmd = ["node", str(HERE / "fetch-epub.mjs")] + (["--save", str(save_dir)] if save_dir else []) + list(urls)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        data = json.loads(res.stdout)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return {u: {"error": f"browser helper failed: {type(e).__name__}"} for u in urls}
    return {r["url"]: r for r in data if "url" in r}


def mark_verified(names, today):
    p = LIBRARY_DIR / "manifest.json"
    m = json.loads(p.read_text())
    hit = []
    for fname, e in m.items():
        if isinstance(e, dict) and (fname in names or e.get("short") in names):
            e["last_verified"] = today.isoformat()
            hit.append(e.get("short") or fname)
    if not hit:
        sys.exit("no manifest entry matches: " + ", ".join(names))
    write_atomic(p, json.dumps(m, indent=1) + "\n", 0o640)
    try:
        import pwd
        os.chown(p, 0, pwd.getpwnam("www-data").pw_gid)
    except (ImportError, KeyError, PermissionError):
        pass
    print("recorded as verified today:", ", ".join(hit))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--mark-verified", nargs="+")
    ap.add_argument("--today")
    args = ap.parse_args()
    today = parse_date(args.today) or datetime.date.today()
    if args.mark_verified:
        return mark_verified(set(args.mark_verified), today)
    try:
        manifest = json.loads((LIBRARY_DIR / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read the library manifest: {e}")
    try:
        previous = json.loads(CURRENCY_FILE.read_text()).get("documents", {})
    except (OSError, ValueError):
        previous = {}
    urls = [] if args.offline else sorted({m["check_url"] for m in manifest.values() if isinstance(m, dict) and m.get("check_url")})
    incoming = None
    if urls:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(STAGING_DIR, 0o750)
        incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=STAGING_DIR))
    try:
        fingerprints = fetch_fingerprints(urls, incoming)
        local = {f: sha256_file(LIBRARY_DIR / f) for f, m in manifest.items() if isinstance(m, dict) and m.get("check_url") and (LIBRARY_DIR / f).is_file()}
        docs = evaluate(manifest, fingerprints, local, today, previous)
        if args.offline:  # keep the last browser-based answers for URL entries
            for f, d in docs.items():
                if d["mode"] == "url" and f in previous:
                    docs[f] = previous[f]
        elif urls:
            # a failed browser run (every URL unreachable) must not discard staged files: only act on real answers
            real = {f for f, d in docs.items() if d.get("status") in ("current", "update_available")}
            stage_updates(manifest, {f: d for f, d in docs.items() if f in real}, fingerprints, incoming)
            for f, d in docs.items():
                if f not in real and (previous.get(f) or {}).get("staged"):
                    d["staged"] = previous[f]["staged"]  # keep showing the staged copy while the check is failing
    finally:
        if incoming:
            shutil.rmtree(incoming, ignore_errors=True)
    now = time.time()
    write_atomic(CURRENCY_FILE, json.dumps({"checked": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S"), "documents": docs}, indent=1), 0o644)
    write_atomic(TEXTFILE, metrics_text(docs, now))
    for f, d in sorted(docs.items()):
        print(f"{d['status']:17} {d['short']}: {d['message']}")


if __name__ == "__main__":
    main()
