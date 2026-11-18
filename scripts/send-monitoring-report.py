#!/usr/bin/env python3
"""Run the installed monitoring reporter with the versioned email layout."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LEGACY_REPORTER = Path(os.environ.get(
    "NOAHPAC_LEGACY_REPORTER", "/opt/scripts/quarterly-email-report.py"
))


def load_reporter():
    if not LEGACY_REPORTER.is_file():
        raise FileNotFoundError(f"Monitoring reporter not found: {LEGACY_REPORTER}")
    spec = importlib.util.spec_from_file_location("monitor_email_runtime", LEGACY_REPORTER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load monitoring reporter: {LEGACY_REPORTER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    reporter = load_reporter()
    sys.path.insert(0, str(REPO / "lib"))
    from monitor_email_layout import render_app_block

    reporter.render_app_block = render_app_block
    return reporter.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
