"""Filesystem locations for the Med Standards Assistant runtime state.

Production sets the environment variables from its systemd unit so mutable
artifacts (index, audit log, usage counters, corpus PDFs) live outside the
Nginx document root and outside git.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("USAF_ASSISTANT_STATE_DIR", APP_DIR / "state"))
CHROMA_DIR = Path(os.environ.get("USAF_ASSISTANT_CHROMA_DIR", STATE_DIR / "chroma_data"))
LIBRARY_DIR = Path(os.environ.get("USAF_ASSISTANT_LIBRARY_DIR", STATE_DIR / "library"))
AUDIT_LOG = Path(os.environ.get("USAF_ASSISTANT_AUDIT_LOG", STATE_DIR / "audit.jsonl"))
USAGE_FILE = Path(os.environ.get("USAF_ASSISTANT_USAGE_FILE", STATE_DIR / "usage.json"))
HEARTBEAT_FILE = Path(os.environ.get("USAF_ASSISTANT_HEARTBEAT", STATE_DIR / "heartbeat.json"))
