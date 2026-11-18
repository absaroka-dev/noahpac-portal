"""Email-client-safe presentation helpers for consolidated monitor reports."""

from __future__ import annotations

import html
from datetime import date


STATUS_LABEL = {
    "changed": "REVIEW NEEDED",
    "held_for_review": "HELD FOR REVIEW",
    "no_change": "No change",
    "refreshed": "Refreshed",
    "error": "ERROR",
    "missing": "Did not report",
}

STATUS_COLOR = {
    "changed": "#b45309",
    "held_for_review": "#b45309",
    "no_change": "#166534",
    "refreshed": "#166534",
    "error": "#991b1b",
    "missing": "#991b1b",
}

STATUS_BG = {
    "changed": "#fef3c7",
    "held_for_review": "#fef3c7",
    "no_change": "#f0fdf4",
    "refreshed": "#f0fdf4",
    "error": "#fef2f2",
    "missing": "#fef2f2",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_run_time(value: object) -> str:
    """Keep ISO timestamps legible without letting email clients split the date."""
    raw = str(value or "").strip()
    if not raw or raw == "—":
        return "Run time unavailable"
    day, separator, clock = raw.partition("T")
    if separator:
        clock = clock[:5]
        try:
            parsed_day = date.fromisoformat(day)
            day = f"{parsed_day.strftime('%b')} {parsed_day.day}"
        except ValueError:
            pass
        return f"{esc(day)}&nbsp;·&nbsp;{esc(clock)}"
    return esc(raw)


def render_app_block(result: dict) -> str:
    """Render a monitor result with table layout supported by major email clients."""
    status = result["status"]
    color = STATUS_COLOR.get(status, "#6b7280")
    background = STATUS_BG.get(status, "#f9fafb")
    label = STATUS_LABEL.get(status, status)
    findings = result.get("findings") or []
    count = len(findings)
    count_text = f'{count} finding{"s" if count != 1 else ""}' if count else "No findings"

    # Imported lazily to keep this module independently testable. The legacy
    # report generator still owns finding-card rendering and email delivery.
    from monitor_email_runtime import render_findings  # type: ignore

    findings_html = render_findings(findings)
    badge = (
        f'<span style="display:inline-block;padding:5px 10px;border-radius:999px;'
        f'font-size:11px;line-height:1.2;font-weight:700;letter-spacing:.02em;'
        f'white-space:nowrap;background:{color};color:#fff">{esc(label)}</span>'
    )

    return (
        '<div style="margin-bottom:16px;border-radius:6px;border:1px solid #e5e7eb;overflow:hidden">'
        f'<div style="padding:14px 16px;background:{background}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse">'
        '<tr><td style="padding:0 0 8px">'
        f'<a href="{esc(result["app_url"])}" style="font-weight:700;font-size:16px;line-height:1.35;'
        f'color:#111827;text-decoration:none">{esc(result["app_name"])}</a>'
        '</td></tr>'
        '<tr><td style="padding:0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse"><tr>'
        f'<td valign="middle" style="font-size:12px;line-height:1.35;color:#6b7280;'
        f'padding:0 10px 0 0;white-space:nowrap">'
        f'{esc(count_text)}&nbsp;&nbsp;·&nbsp;&nbsp;{format_run_time(result.get("ran_at"))}</td>'
        f'<td align="right" valign="middle" style="width:1%;padding:0;white-space:nowrap">{badge}</td>'
        '</tr></table>'
        '</td></tr></table>'
        '</div>'
        f'{("<div style=\"padding:12px 16px\">" + findings_html + "</div>") if findings_html else ""}'
        '</div>'
    )
