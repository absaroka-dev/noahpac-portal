"""Per-user daily call/token caps and a content-free audit log (dependency-free)."""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


def _today():
    return time.strftime("%Y-%m-%d")


def check_and_count(path, user, max_calls, max_tokens, now=None):
    """Reserve one call for `user`. Returns (allowed, calls_used_after, tokens_used)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    day = now or _today()
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = {d: v for d, v in data.items() if d == day}  # only today is kept
        u = data.setdefault(day, {}).setdefault(user, {"calls": 0, "tokens": 0})
        if u["calls"] >= max_calls or u["tokens"] >= max_tokens:
            return False, u["calls"], u["tokens"]
        u["calls"] += 1
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True, u["calls"], u["tokens"]


def add_tokens(path, user, tokens, now=None):
    path = Path(path)
    day = now or _today()
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        u = data.get(day, {}).get(user)
        if u is None:
            return
        u["tokens"] += int(tokens)
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def audit(path, **fields):
    """One JSON line per request. Never the question or answer text: only a short hash and the length."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    rec.update(fields)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode())
    finally:
        os.close(fd)


def qhash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
