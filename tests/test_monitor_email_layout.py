#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import monitor_email_layout as layout


class MonitoringEmailLayoutTests(unittest.TestCase):
    def setUp(self):
        sys.modules["monitor_email_runtime"] = SimpleNamespace(
            render_findings=lambda findings: "<div>Finding cards</div>" if findings else ""
        )

    def tearDown(self):
        sys.modules.pop("monitor_email_runtime", None)

    def test_action_header_uses_email_safe_tables_and_nonwrapping_badge(self):
        block = layout.render_app_block({
            "status":"changed",
            "app_url":"https://noahpac.com/vaccines/",
            "app_name":"ACIP/AAP/ACOG Vaccine Schedules",
            "ran_at":"2026-11-18T12:09",
            "findings":[{"pmid":"42683684"}],
        })
        self.assertGreaterEqual(block.count('role="presentation"'), 2)
        self.assertNotIn("display:flex", block)
        self.assertIn("white-space:nowrap", block)
        self.assertIn("REVIEW NEEDED", block)
        self.assertIn("1 finding", block)
        self.assertIn("Nov 18&nbsp;·&nbsp;12:09", block)

    def test_long_status_is_shortened_and_escaped(self):
        block = layout.render_app_block({
            "status":"held_for_review",
            "app_url":"https://noahpac.com/calculators/",
            "app_name":"Clinical Calculators & Scores",
            "ran_at":None,
            "findings":[],
        })
        self.assertIn("HELD FOR REVIEW", block)
        self.assertIn("Clinical Calculators &amp; Scores", block)
        self.assertIn("No findings", block)
        self.assertIn("Run time unavailable", block)


if __name__ == "__main__":
    unittest.main()
