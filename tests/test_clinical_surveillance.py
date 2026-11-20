import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, REPO / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


urgent = load("urgent_clinical_monitor", "scripts/monitor-urgent-clinical-feeds.py")
fingerprints = load("clinical_fingerprint_monitor", "scripts/monitor-source-fingerprints.py")
watcher = load("pubmed_watcher_under_test", "lib/pubmed_watcher.py")
events = load("review_events_under_test", "lib/review_events.py")
triage = load("triage_monitor_under_test", "triage/check_updates.py")
alert_filter = load("alert_filter_under_test", "lib/alert_filter.py")


class UrgentFeedParsingTests(unittest.TestCase):
    def test_parses_rss_items_with_stable_ids(self):
        raw = b"""<?xml version='1.0'?><rss><channel><item><title>Safety alert</title><link>/alert/1</link><guid>alert-1</guid></item></channel></rss>"""
        entries = urgent.rss_entries(raw, "https://www.fda.gov/feed.xml")
        self.assertEqual(entries, [{"id":"alert-1","title":"Safety alert","url":"https://www.fda.gov/alert/1"}])

    def test_parses_cdc_han_identifiers_and_deduplicates(self):
        raw = b'<a href="/han/2026/han00520.html">CDCHAN-00520 Example notice</a><a href="/han/2026/han00520.html">Duplicate CDCHAN-00520</a>'
        entries = urgent.han_entries(raw, "https://www.cdc.gov/han/")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "CDCHAN-00520")

    def test_prefers_canonical_han_url_when_link_text_disagrees(self):
        raw = b'<a href="/han/php/notices/han00533.html">CDCHAN-00532 Incorrect index text</a>'
        entries = urgent.han_entries(raw, "https://www.cdc.gov/han/site.html")
        self.assertEqual(entries[0]["id"], "CDCHAN-00533")

    def test_rejects_non_https_or_unmapped_channels(self):
        errors = urgent.validate({"schema_version":1,"channels":[{"id":"bad","type":"rss","url":"http://example.test/feed","tool_ids":[]}]})
        self.assertEqual(len(errors), 2)


class FingerprintMappingTests(unittest.TestCase):
    def test_sources_may_use_a_stable_fingerprint_mirror(self):
        source = {
            "url": "https://publisher.example/guideline",
            "fingerprint_url": "https://archive.example/guideline",
        }
        self.assertEqual(source.get("fingerprint_url", source["url"]), source["fingerprint_url"])

    def test_cdc_fingerprint_retries_403_without_custom_user_agent(self):
        url = "https://www.cdc.gov/steadi/protocol.pdf"
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request)
            if len(seen) == 1:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
            return io.BytesIO(b"pdf")

        with mock.patch.object(fingerprints.urllib.request, "urlopen", side_effect=fake_urlopen):
            response = fingerprints.open_source(url)
            self.assertEqual(response.read(), b"pdf")

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].get_header("User-agent"), fingerprints.USER_AGENT)
        self.assertIsNone(seen[1].get_header("User-agent"))

    def test_non_cdc_fingerprint_does_not_retry_403(self):
        url = "https://example.org/protocol.pdf"

        def blocked(request, timeout=None):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))

        with mock.patch.object(fingerprints.urllib.request, "urlopen", side_effect=blocked) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                fingerprints.open_source(url)
        self.assertEqual(urlopen.call_count, 1)

    def test_maps_changed_source_to_all_affected_tools(self):
        claims = {"claims":[
            {"tool_id":"a","source_ids":["source"]},
            {"tool_id":"b","source_ids":["source","other"]},
            {"tool_id":"c","source_ids":["other"]},
        ]}
        self.assertEqual(fingerprints.affected_tools("source", claims), ["a", "b"])

    def test_generates_bounded_text_diff(self):
        hunks, truncated = fingerprints.build_hunks("Heading\nDose 5 mg", "Heading\nDose 10 mg")
        self.assertFalse(truncated)
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0]["old_lines"][-1]["text"], "Dose 5 mg")
        self.assertEqual(hunks[0]["new_lines"][-1]["text"], "Dose 10 mg")
        self.assertTrue(hunks[0]["old_lines"][-1]["changed"])

    def test_document_fingerprints_ignore_markup_only_changes(self):
        old_text = fingerprints.normalized_text(b"<main><p>Dose 5 mg</p></main>", "text/html")
        new_text = fingerprints.normalized_text(
            b"<main class='redesign'><p> Dose   5 mg </p></main>", "text/html"
        )
        old = {"sha256": "old-raw", "text_sha256": fingerprints.text_sha256(old_text)}
        new = {"sha256": "new-raw", "text_sha256": fingerprints.text_sha256(new_text)}
        self.assertFalse(fingerprints.fingerprint_changed(old, new))

    def test_document_fingerprints_detect_clinical_text_changes(self):
        old = {"sha256": "old-raw", "text_sha256": fingerprints.text_sha256("Dose 5 mg")}
        new = {"sha256": "new-raw", "text_sha256": fingerprints.text_sha256("Dose 10 mg")}
        self.assertTrue(fingerprints.fingerprint_changed(old, new))

    def test_diff_represents_an_added_file(self):
        hunks, _ = fingerprints.build_hunks("", "New clinical content")
        self.assertEqual(hunks[0]["old_lines"], [])
        self.assertEqual(hunks[0]["new_lines"][0]["text"], "New clinical content")


class PubMedSurveillanceTests(unittest.TestCase):
    def test_transient_server_errors_retry_then_succeed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        failures = [
            urllib.error.HTTPError("https://example.test", 500, "error", {}, io.BytesIO()),
            urllib.error.HTTPError("https://example.test", 503, "error", {}, io.BytesIO()),
            response,
        ]
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=failures), \
             mock.patch.object(watcher.time, "sleep") as sleep, \
             mock.patch.object(watcher.random, "uniform", return_value=0.0):
            body = watcher._read_pubmed(urllib.request.Request("https://example.test"))
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    def test_rate_limit_honors_bounded_retry_after(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'ok'
        limited = urllib.error.HTTPError(
            "https://example.test", 429, "limited", {"Retry-After": "7"}, io.BytesIO()
        )
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=[limited, response]), \
             mock.patch.object(watcher.time, "sleep") as sleep:
            watcher._read_pubmed(urllib.request.Request("https://example.test"))
        sleep.assert_called_once_with(7.0)

    def test_permanent_client_error_is_not_retried(self):
        bad_request = urllib.error.HTTPError(
            "https://example.test", 400, "bad request", {}, io.BytesIO()
        )
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=bad_request) as urlopen, \
             mock.patch.object(watcher.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                watcher._read_pubmed(urllib.request.Request("https://example.test"))
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_sustained_outage_still_fails_closed(self):
        unavailable = urllib.error.HTTPError(
            "https://example.test", 503, "unavailable", {}, io.BytesIO()
        )
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=unavailable) as urlopen, \
             mock.patch.object(watcher.time, "sleep") as sleep, \
             mock.patch.object(watcher.random, "uniform", return_value=0.0):
            with self.assertRaises(urllib.error.HTTPError):
                watcher._read_pubmed(urllib.request.Request("https://example.test"))
        self.assertEqual(urlopen.call_count, watcher.PUBMED_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, watcher.PUBMED_MAX_ATTEMPTS - 1)

    def test_fetch_failure_raises_instead_of_reporting_clean_run(self):
        searches = [{"id":"one", "name":"One", "query":"query", "reldate":30}]
        with mock.patch.object(watcher, "esearch", side_effect=OSError("network unavailable")):
            with self.assertRaises(watcher.PubMedSearchError):
                watcher.run_searches(searches, {}, False)

    def test_finding_retains_exact_source_and_claim_route(self):
        searches = [{
            "id":"one", "name":"One", "query":"query", "reldate":30,
            "source_id":"pubmed", "claim_id":"calculators-screening",
        }]
        with mock.patch.object(watcher, "esearch", return_value=["123"]), \
             mock.patch.object(watcher, "esummary", return_value={"123":{
                 "title":"Study", "source":"Journal", "pubdate":"2026",
             }}), mock.patch.object(watcher.time, "sleep"):
            _, findings = watcher.run_searches(searches, {}, False)
        serialized = watcher.report_findings(findings)
        self.assertEqual(serialized[0]["source_id"], "pubmed")
        self.assertEqual(serialized[0]["claim_id"], "calculators-screening")


class TriageMonitorTests(unittest.TestCase):
    def test_tuple_findings_are_reported_before_state_is_saved(self):
        finding = {
            "pmid": "123", "title": "Validated triage update", "journal": "Journal",
            "pubdate": "2026", "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
        }
        calls = []

        with mock.patch.dict("os.environ", {"PUSHOVER_USER":"user", "PUSHOVER_TOKEN":"token"}), \
             mock.patch.object(triage.pw, "load_state", return_value={"adult_triage":["old"]}), \
             mock.patch.object(triage.pw, "run_searches", return_value=({"adult_triage":["old", "123"]}, [("Adult triage", finding)])), \
             mock.patch.object(triage.pw, "push_notify", side_effect=lambda *_args: calls.append("notify")), \
             mock.patch.object(triage.pw, "write_quarterly_result") as write_result, \
             mock.patch.object(triage.pw, "save_last_checked", side_effect=lambda *_args: calls.append("status")), \
             mock.patch.object(triage.pw, "save_state", side_effect=lambda *_args: calls.append("state")):
            self.assertEqual(triage.main(), 0)

        self.assertEqual(calls, ["notify", "status", "state"])
        reported = write_result.call_args.args[4][0]
        self.assertEqual(reported["search"], "Adult triage")
        self.assertEqual(reported["pmid"], "123")
        self.assertIn("Validated triage update", reported["detail"])

    def test_notification_failure_leaves_new_state_retryable(self):
        finding = {"pmid":"123", "title":"Update", "journal":"Journal", "pubdate":"2026"}

        with mock.patch.dict("os.environ", {"PUSHOVER_USER":"user", "PUSHOVER_TOKEN":"token"}), \
             mock.patch.object(triage.pw, "load_state", return_value={"adult_triage":["old"]}), \
             mock.patch.object(triage.pw, "run_searches", return_value=({"adult_triage":["old", "123"]}, [("Adult triage", finding)])), \
             mock.patch.object(triage.pw, "push_notify", side_effect=OSError("notification unavailable")), \
             mock.patch.object(triage.pw, "save_state") as save_state:
            with self.assertRaises(OSError):
                triage.main()

        save_state.assert_not_called()


class FocusedReviewEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "clinical-review.json").write_text(json.dumps({
            "sources": {"pubmed":{"title":"PubMed","url":"https://pubmed.ncbi.nlm.nih.gov/"},"other":{"title":"Other","url":"https://example.org/"}},
            "tools": [{"id":"tool","source_ids":["pubmed","other"]}],
        }))
        (root / "clinical-claims.json").write_text(json.dumps({"claims":[
            {"id":"literature","tool_id":"tool","source_ids":["pubmed"]},
            {"id":"other-claim","tool_id":"tool","source_ids":["other"]},
        ]}))
        events.REPO = root
        events.EVENT_DIR = root / "events"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pubmed_event_targets_only_pubmed_backed_claim(self):
        event = events.create_event("tool", "topic-monitor", [{"pmid":"123","title":"New paper"}])
        self.assertEqual(event["claim_ids"], ["literature"])
        self.assertEqual(event["source_ids"], ["pubmed"])
        self.assertEqual(event["evidence_scope"], "focused")

    def test_duplicate_evidence_is_deduplicated(self):
        first = events.create_event("tool", "topic-monitor", [{"pmid":"123"}])
        second = events.create_event("tool", "topic-monitor", [{"pmid":"123"}])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(events.list_events("tool")), 1)

    def test_exact_source_url_targets_its_claim(self):
        event = events.create_event(
            "tool", "topic-monitor",
            [{"url":"https://example.org/", "detail":"Page version changed"}],
        )
        self.assertEqual(event["source_ids"], ["other"])
        self.assertEqual(event["claim_ids"], ["other-claim"])

    def test_attaches_implementation_hunks_to_open_event(self):
        event = events.create_event("tool", "topic-monitor", [{"pmid":"123"}])
        updated = events.attach_implementation_hunks(event["id"], [{"id":"a" * 20}], {
            "base":"b" * 40, "head":"c" * 40,
        })
        self.assertEqual(updated["implementation_hunks"][0]["id"], "a" * 20)


class ReviewEvidencePersistenceTests(unittest.TestCase):
    def test_clean_monitor_run_preserves_pending_independent_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "last_checked.json"
            path.write_text(json.dumps({
                "date":"2026-10-16", "status":"review_required", "review_pending":True,
                "independent_review_pending":True, "primary_reviewer":"reviewer.one",
                "primary_reviewed_at":"2026-10-16T12:00:00+00:00", "review_claim_ids":["claim"],
            }))
            watcher.save_last_checked(Path(directory), "no_change")
            result = json.loads(path.read_text())
            self.assertEqual(result["status"], "review_required")
            self.assertTrue(result["independent_review_pending"])
            self.assertEqual(result["primary_reviewer"], "reviewer.one")
            self.assertEqual(result["review_claim_ids"], ["claim"])

class UrgentAlertFilterTests(unittest.TestCase):
    CONFIG = json.loads((REPO / "urgent-clinical-feeds.json").read_text())
    FILTERS = next(c for c in CONFIG["channels"] if c["id"] == "fda_medwatch")["filters"]

    def decide(self, title, path="/safety/medical-product-safety-information/x", description=""):
        return alert_filter.classify(
            {"title": title, "url": "https://www.fda.gov" + path, "description": description}, self.FILTERS)

    def test_device_food_veterinary_and_supplement_alerts_are_dropped(self):
        for title, path in (
            ("Early Alert: B. Braun Removes Streamline Bloodline Set", "/medical-devices/medical-device-recalls-and-early-alerts/x"),
            ("Heart Pump Controller Recall: Abiomed Removes Impella Controllers", "/medical-devices/x"),
            ("Wedgewood Issues Recall of Compounded Veterinary Medications", "/safety/recalls-market-withdrawals-safety-alerts/x"),
            ("FDA Advises Consumers Not to Buy or Serve Toddler Soft-Baked Bars", "/food/outbreaks/x"),
            ("Recall of 10 Day Forecast Capsules Due to Undeclared Sildenafil", "/safety/recalls-market-withdrawals-safety-alerts/x"),
            ("Medical Device Correction Update Luna G3 BPAP System", "/safety/recalls-market-withdrawals-safety-alerts/x"),
        ):
            with self.subTest(title=title):
                self.assertEqual(self.decide(title, path)["decision"], "drop")

    def test_lot_level_drug_recalls_are_logged_not_queued(self):
        verdict = self.decide("Eugia Issues Nationwide Recall of Vancomycin Hydrochloride for Injection",
                              "/safety/recalls-market-withdrawals-safety-alerts/eugia")
        self.assertEqual(verdict["decision"], "log")
        self.assertEqual(verdict["tool_ids"], [])

    def test_safety_communication_for_a_covered_drug_routes_only_to_tools_that_cover_it(self):
        verdict = self.decide("FDA Drug Safety Communication: Fluoroquinolone antibiotics and aortic aneurysm")
        self.assertEqual(verdict["decision"], "route")
        self.assertIn("abx", verdict["tool_ids"])
        self.assertNotIn("anticoag", verdict["tool_ids"])
        self.assertNotIn("steroids", verdict["tool_ids"])

    def test_description_text_is_searched_too(self):
        verdict = self.decide("MedWatch Alert: New warning", description="Andexxa reversal agent thromboembolic events")
        self.assertEqual(verdict["decision"], "route")
        self.assertEqual(verdict["tool_ids"], ["anticoag"])

    def test_unrelated_drug_alert_is_screened_not_queued(self):
        self.assertEqual(self.decide("MedWatch Alert: FDA Investigating Serious Risk of Malignancy Linked to Zolgensma Gene Therapy")["decision"], "screen")

    def test_glucagon_like_peptide_does_not_match_the_glucagon_antidote(self):
        verdict = self.decide("MedWatch Alert: FDA Requires Safety Labeling Changes for Glucagon-Like Peptide-1 Receptor Agonists (GLP-1 RAs)")
        self.assertEqual(verdict["decision"], "screen")
        self.assertEqual(self.decide("Glucagon emergency kit shortage")["tool_ids"], ["toxicology"])

    def test_channel_without_filters_routes_everything_as_before(self):
        verdict = alert_filter.classify({"title": "CDCHAN-00520", "url": "https://www.cdc.gov/han/x"}, None)
        self.assertEqual(verdict["decision"], "route")
        self.assertIsNone(verdict["tool_ids"])

    def test_every_watch_term_tool_belongs_to_the_channel(self):
        channel = next(c for c in self.CONFIG["channels"] if c["id"] == "fda_medwatch")
        self.assertTrue(set(self.FILTERS["watch_terms"]) <= set(channel["tool_ids"]))

    def test_pipeline_queues_only_applicable_alerts_and_logs_the_rest(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO / "lib"))
        import review_events as live_events
        feed = (b"<?xml version='1.0'?><rss><channel>"
                b"<item><title>Early Alert: Pump Infusion Sets</title><link>https://www.fda.gov/medical-devices/a</link><guid>a</guid></item>"
                b"<item><title>Drug Safety Communication: Fluoroquinolone antibiotics</title><link>https://www.fda.gov/safety/medical-product-safety-information/b</link><guid>b</guid></item>"
                b"<item><title>Zolgensma malignancy risk</title><link>https://www.fda.gov/safety/medical-product-safety-information/c</link><guid>c</guid></item>"
                b"</channel></rss>")
        config = {"schema_version": 1, "channels": [{
            "id": "fda", "title": "FDA", "type": "rss", "url": "https://example.test/rss",
            "tool_ids": ["abx", "anticoag", "steroids"], "filters": self.FILTERS | {"watch_terms": {"abx": ["fluoroquinolone"], "anticoag": ["andexxa"]}}}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps(config))
            state = root / "state.json"
            state.write_text(json.dumps({"channels": {"fda": {"seen": ["old"]}}}))
            sent, pending = [], []
            with mock.patch.object(urgent, "CONFIG", root / "config.json"), \
                 mock.patch.object(urgent, "STATE", state), mock.patch.object(urgent, "REPORT", root / "report.json"), \
                 mock.patch.object(urgent, "SCREENED_LOG", root / "screened.jsonl"), \
                 mock.patch.object(urgent, "fetch", return_value=(feed, "https://example.test/rss")), \
                 mock.patch.object(urgent, "mark_pending", side_effect=pending.append), \
                 mock.patch.object(urgent, "notify", side_effect=sent.append), \
                 mock.patch.object(urgent, "heartbeat"), \
                 mock.patch.object(live_events, "EVENT_DIR", root / "events"), \
                 mock.patch.object(_sys, "argv", ["monitor"]):
                self.assertEqual(urgent.main(), 0)
            created = [json.loads(path.read_text()) for path in (root / "events").glob("*.json")]
            self.assertEqual([event["tool_id"] for event in created], ["abx"])
            self.assertEqual(pending, [["abx"]])
            self.assertEqual([item["id"] for item in sent[0]], ["b"])
            logged = [json.loads(line) for line in (root / "screened.jsonl").read_text().splitlines()]
            self.assertEqual({row["id"]: row["decision"] for row in logged}, {"a": "drop", "c": "screen"})
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(len(report["findings"]), 1)
            self.assertEqual(len(report["screened"]), 2)

    def test_rss_description_is_captured_without_markup(self):
        raw = b"<?xml version='1.0'?><rss><channel><item><title>T</title><link>/a</link><guid>g</guid><description>&lt;p&gt;Safe &lt;b&gt;text&lt;/b&gt;&lt;/p&gt;</description></item></channel></rss>"
        self.assertEqual(urgent.rss_entries(raw, "https://www.fda.gov/")[0]["description"], "Safe text")


if __name__ == "__main__":
    unittest.main()
