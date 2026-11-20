"""Applicability filter for urgent official alerts.

The FDA MedWatch feed carries every recall and correction, most of them about
devices, food or veterinary products that none of the clinical tools can be
affected by. This module decides, from the feed item alone, whether an alert
can touch any tool's content:

  drop   - not applicable (device, food, veterinary, undeclared supplement, ...)
  log    - lot-level recall paths: recorded in the screened log, never queued
  screen - passed the drops but names nothing any tool covers: logged, never queued
  route  - names a drug, class or vaccine a tool covers: queued for those tools only

Nothing is deleted: every non-routed item is appended to the screened log so the
decision can be audited, and the rules live in urgent-clinical-feeds.json.
"""

from __future__ import annotations

import re
import urllib.parse


def _path(url: str) -> str:
    return urllib.parse.urlparse(url or "").path.lower()


def _term_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term.strip().lower()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?:s|es)?(?![a-z0-9]|-like)", re.I)


def classify(entry: dict, filters: dict | None) -> dict:
    """Return {"decision", "reason", "tool_ids", "matched_terms"} for one feed entry."""
    if not filters:
        return {"decision": "route", "reason": "no filters configured", "tool_ids": None, "matched_terms": []}
    title = entry.get("title", "") or ""
    text = f"{title} {entry.get('description', '') or ''}"
    path = _path(entry.get("url", ""))

    for fragment in filters.get("drop_url_paths", []):
        if fragment.lower() in path:
            return {"decision": "drop", "reason": f"url path {fragment}", "tool_ids": [], "matched_terms": []}
    for pattern in filters.get("drop_title_patterns", []):
        if re.search(pattern, title, re.I):
            return {"decision": "drop", "reason": f"title matches {pattern}", "tool_ids": [], "matched_terms": []}
    for fragment in filters.get("log_only_url_paths", []):
        if fragment.lower() in path:
            return {"decision": "log", "reason": f"lot-level recall path {fragment}", "tool_ids": [], "matched_terms": []}

    tool_ids, matched = [], []
    for tool_id, terms in sorted(filters.get("watch_terms", {}).items()):
        hits = [term for term in terms if _term_pattern(term).search(text)]
        if hits:
            tool_ids.append(tool_id)
            matched.extend(h for h in hits if h not in matched)
    if tool_ids:
        return {"decision": "route", "reason": "names covered content", "tool_ids": tool_ids, "matched_terms": matched}
    return {"decision": "screen", "reason": "names nothing any tool covers", "tool_ids": [], "matched_terms": []}
