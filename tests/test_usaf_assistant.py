import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "usaf-assistant"
sys.path.insert(0, str(APP))

import modes  # noqa: E402
import check_library_currency as cur  # noqa: E402
import msd_tables  # noqa: E402
import refcheck  # noqa: E402
import retrieval  # noqa: E402
import safety_usaf  # noqa: E402
import textproc  # noqa: E402
import usage  # noqa: E402


class SafetyTest(unittest.TestCase):
    def test_blocks_identifiers(self):
        for text, label in [
            ("SSgt Smith has asthma", "rank followed by a name"),
            ("Capt. Jones was seen", "rank followed by a name"),
            ("member 123-45-6789 reports", "Social Security number"),
            ("SSN 123 45 6789", "Social Security number"),
            ("DoD ID: 1234567890", "DoD ID / EDIPI"),
            ("id 1234567890 seen", "ten-digit number (possible DoD ID)"),
            ("DOB 01/02/1990", "date of birth"),
            ("born on 02/03/1988", "date of birth"),
            ("call 555-123-4567", "phone number"),
            ("mail a.b@example.com", "email address"),
        ]:
            self.assertEqual(safety_usaf.detected_identifier(text), label, text)

    def test_allows_normal_clinical_text(self):
        for text in [
            "[MEMBER], 4N0X1, has mild persistent asthma controlled on one inhaler; deployment to the AOR is planned.",
            "Per MSD C60 and Table 6.1, the standard applies to Flying Class II.",
            "AFSC 1C1X1, 3 months ago, 12 weeks of treatment, FEV1 92 percent predicted",
            "Capt [NAME] reviewed the packet",
        ]:
            self.assertIsNone(safety_usaf.detected_identifier(text), text)

    def test_exact_dates_warn_but_do_not_block(self):
        text = "Seen on 03 Mar 2026 and again 2026-04-01."
        self.assertIsNone(safety_usaf.detected_identifier(text))
        self.assertTrue(safety_usaf.exact_date_warning(text))
        self.assertFalse(safety_usaf.exact_date_warning("seen 3 months ago"))


class RedactionTest(unittest.TestCase):
    def red(self, text):
        return safety_usaf.redact(text, terms_file="/nonexistent")

    def test_structured_identifiers_become_placeholders(self):
        clean, f = self.red("SSgt Smith (SSN 123-45-6789, DoD ID: 1234567890) call 555-123-4567 or a.b@x.com; 12 Oak Street; DOB 01/02/1990; MRN: AB1234")
        for token in ("Smith", "123-45-6789", "1234567890", "555-123-4567", "a.b@x.com", "Oak Street", "01/02/1990", "AB1234"):
            self.assertNotIn(token, clean)
        for ph in ("[NAME]", "[SSN]", "[DOD-ID]", "[PHONE]", "[EMAIL]", "[ADDRESS]", "[DOB]", "[MRN]"):
            self.assertIn(ph, clean)
        self.assertIsNone(safety_usaf.detected_identifier(clean))
        self.assertNotIn("Smith", str(f))  # findings carry counts only

    def test_names_after_labels_and_titles_but_not_ordinary_words(self):
        clean, _ = self.red("Name: Jane Public; named Mary Smith; Lt Col Jones; Maj Gen Brown; a Maj Gen decides; Flying Class II member Reports well")
        self.assertEqual(clean, "Name: [NAME]; named [NAME]; Lt Col [NAME]; Maj Gen [NAME]; a Maj Gen decides; Flying Class II member Reports well")

    def test_clinical_text_and_publication_citations_are_left_alone(self):
        text = "History of VTE, 4N0X1, per MSD, 5 Jun 2026, C60 and Table 5-2, seen 03 Mar 2026, ICD J45.40"
        self.assertEqual(self.red(text), (text, []))

    def test_idempotent_and_listed_terms(self):
        once, _ = self.red("SSgt Smith SSN 123-45-6789")
        self.assertEqual(self.red(once), (once, []))
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# bases and units\nMinot\nRamstein Air Base\n")
        clean, found = safety_usaf.redact("seen at Minot and at ramstein air base", terms_file=f.name)
        self.assertEqual(clean, "seen at [TERM] and at [TERM]")
        self.assertEqual(found[0]["count"], 2)


class ChunkingTest(unittest.TestCase):
    def test_sections_and_pages_are_kept(self):
        pages = [(1, "CHAPTER 6 MEDICAL STANDARDS\n6.1. General. text\n" + "filler\n" * 5),
                 (2, "Table 6.1. Asthma Standards\nrow a\nrow b\n")]
        chunks = textproc.chunk_pages(pages)
        self.assertEqual([c["section"] for c in chunks], ["CHAPTER 6 MEDICAL STANDARDS", "Table 6.1. Asthma Standards"])
        self.assertEqual((chunks[1]["page_start"], chunks[1]["page_end"]), (2, 2))

    def test_long_section_is_split_with_overlap_and_label(self):
        body = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        chunks = textproc.chunk_pages([(1, "Chapter 1 Big\n" + body)])
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(c["text"]) <= textproc.MAX_CHARS + 200 for c in chunks))
        self.assertIn("(cont.)", chunks[1]["section"])

    def test_numbered_form_sections_are_headings(self):
        chunks = textproc.chunk_pages([(1, "(4) HISTORY\ntext a\n(5) TARGETED PHYSICAL EXAM\ntext b\n")])
        self.assertEqual([c["section"] for c in chunks], ["(4) HISTORY", "(5) TARGETED PHYSICAL EXAM"])

    def test_page_numbers_alone_are_dropped(self):
        self.assertNotIn("42", textproc.clean_page_text("text\n  42  \nmore"))

    def test_header_format(self):
        self.assertEqual(textproc.header_for("MSD", "C60", 13, 13), "[MSD | C60 | p. 13]")
        self.assertEqual(textproc.header_for("X", "S", 1, 3), "[X | S | pp. 1-3]")


class RetrievalTest(unittest.TestCase):
    def test_bm25_finds_exact_codes_and_numbers(self):
        docs = ["MSD C60 abnormal pupils flying class", "hearing audiogram table 5-2 limits", "asthma persistent controller waiver"]
        b = retrieval.BM25(docs)
        self.assertEqual(b.top("C60", 3)[0][0], 0)
        self.assertEqual(b.top("table 5-2", 3)[0][0], 1)
        self.assertEqual(b.top("asthma", 3)[0][0], 2)

    def test_glossary_expansion_is_whole_word(self):
        g = {"pcs": "permanent change of station"}
        self.assertIn("permanent change of station", retrieval.expand_query("PCS waiver", g))
        self.assertEqual(retrieval.expand_query("pcsx waiver", g), "pcsx waiver")

    def test_rrf_prefers_items_ranked_by_several_lists(self):
        self.assertEqual(retrieval.rrf([["a", "b", "c"], ["b", "a", "d"], ["b", "x"]], top_n=2), ["b", "a"])

    def test_shipped_glossary_loads(self):
        g = retrieval.load_glossary(APP / "glossary.json")
        self.assertEqual(g["pcs"], "permanent change of station")
        self.assertNotIn("_note", g)


class RefCheckTest(unittest.TestCase):
    def test_unverified_references_are_reported(self):
        text = "Table 6.1 lists asthma limits. Paragraph 6.12.3 covers waivers."
        self.assertEqual(refcheck.unverified_refs("See Table 6.1 [S1].", text), [])
        self.assertEqual(refcheck.unverified_refs("See Table 6.1 and para 6.12.3 and Table 9-9.", "Table 6.1 is here. 6.12.3 applies."), ["table 9-9"])

    def test_tags(self):
        self.assertEqual(refcheck.used_tags("a [S2] b [S1][S2] c [S9]", 3), [2, 1])
        self.assertEqual(refcheck.invalid_tags("a [S2] [S9] [S0]", 3), [0, 9])


class UsageTest(unittest.TestCase):
    def test_daily_cap_and_audit_has_no_content(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "usage.json"
            self.assertTrue(usage.check_and_count(f, "u1", 2, 1000, now="2026-01-01")[0])
            self.assertTrue(usage.check_and_count(f, "u1", 2, 1000, now="2026-01-01")[0])
            self.assertFalse(usage.check_and_count(f, "u1", 2, 1000, now="2026-01-01")[0])
            self.assertTrue(usage.check_and_count(f, "u2", 2, 1000, now="2026-01-01")[0])
            usage.add_tokens(f, "u2", 5000, now="2026-01-01")
            self.assertFalse(usage.check_and_count(f, "u2", 5, 1000, now="2026-01-01")[0])
            self.assertTrue(usage.check_and_count(f, "u1", 2, 1000, now="2026-01-02")[0])
            log = Path(d) / "audit.jsonl"
            usage.audit(log, user="u1", mode="ask", outcome="answered", qlen=12, qh=usage.qhash("secret question"))
            rec = json.loads(log.read_text())
            self.assertNotIn("secret question", log.read_text())
            self.assertEqual(rec["qh"], usage.qhash("secret question"))


class MsdTableTest(unittest.TestCase):
    def test_canonical_column_names(self):
        self.assertEqual(msd_tables.canonical_name("Flying Class I/IA"), "Flying Class I/IA")
        self.assertEqual(msd_tables.canonical_name("Class III Flying"), "Flying Class III")
        self.assertEqual(msd_tables.canonical_name("Air Controller Traffic"), "Air Traffic Controller (ATC)")
        self.assertEqual(msd_tables.canonical_name("Special Airmen"), "Special Warfare Airmen (SWA)")
        self.assertEqual(msd_tables.canonical_name("DW"), "DW")

    def test_row_text_names_the_marked_columns(self):
        r = {"id": "C60", "text": "Abnormal pupils.", "marks": [("Flying Class II", "X"), ("Air Traffic Controller (ATC)", "X*")], "comments": "See AMWG"}
        out = msd_tables.row_text(r)
        self.assertIn("C60: Abnormal pupils.", out)
        self.assertIn("Flying Class II, Air Traffic Controller (ATC) (X*)", out)
        self.assertIn("Comments: See AMWG", out)

    def test_line_joining_follows_reading_order(self):
        w = lambda t, top, x: {"text": t, "top": top, "x0": x}
        self.assertEqual(msd_tables.join_lines([w("b", 20.0, 5), w("a2", 10.2, 30), w("a1", 10.0, 5)]), "a1 a2 b")


class CurrencyTest(unittest.TestCase):
    TODAY = __import__("datetime").date(2026, 11, 8)
    MAN = {"a.pdf": {"short": "A", "check_url": "https://x/a.pdf"},
           "b.pdf": {"short": "B", "last_verified": "2026-10-20", "verify_every_days": 30},
           "c.pdf": {"short": "C"},
           "d.pdf": {"short": "D", "last_verified": "2026-01-01", "verify_every_days": 90}}

    def test_url_entry_same_hash_is_current_and_different_hash_is_update_available(self):
        fp = {"https://x/a.pdf": {"status": 200, "sha256": "aa", "last_modified": "Mon"}}
        self.assertEqual(cur.evaluate(self.MAN, fp, {"a.pdf": "aa"}, self.TODAY)["a.pdf"]["status"], "current")
        self.assertEqual(cur.evaluate(self.MAN, fp, {"a.pdf": "bb"}, self.TODAY)["a.pdf"]["status"], "update_available")

    def test_unreachable_counts_failures_and_keeps_the_last_known_answer(self):
        prev = {"a.pdf": {"status": "current", "message": "ok", "failures": 0}}
        r = cur.evaluate(self.MAN, {"https://x/a.pdf": {"status": 403}}, {"a.pdf": "aa"}, self.TODAY, prev)["a.pdf"]
        self.assertEqual((r["status"], r["failures"], r["last_known"]["status"]), ("unreachable", 1, "current"))
        r2 = cur.evaluate(self.MAN, {}, {"a.pdf": "aa"}, self.TODAY, {"a.pdf": r})["a.pdf"]
        self.assertEqual(r2["failures"], 2)

    def test_date_based_entries(self):
        out = cur.evaluate(self.MAN, {}, {}, self.TODAY)
        self.assertEqual(out["b.pdf"]["status"], "verified")      # 2026-10-20 + 30 days = 2026-11-19
        self.assertEqual(out["b.pdf"]["due"], "2026-11-19")
        self.assertEqual(out["c.pdf"]["status"], "verify_due")    # never verified
        self.assertEqual(out["d.pdf"]["status"], "verify_due")    # 2026-01-01 + 90 days has passed

    def test_metrics(self):
        docs = cur.evaluate(self.MAN, {"https://x/a.pdf": {"status": 200, "sha256": "zz"}}, {"a.pdf": "aa"}, self.TODAY)
        t = cur.metrics_text(docs, 1000)
        self.assertIn("usaf_library_update_available 1", t)
        self.assertIn("usaf_library_verify_due 2", t)
        self.assertIn('usaf_library_doc_status{doc="A",status="update_available"} 1', t)


class ModesAndSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = (APP / "api_server.py").read_text()
        cls.ingest = (APP / "ingest_usaf.py").read_text()
        cls.unit = (REPO / "ops" / "systemd" / "usaf-assistant-api.service").read_text()
        cls.nginx = (REPO / "ops" / "nginx" / "noahpac-usaf-assistant.locations.conf").read_text()

    def test_every_mode_has_a_prompt_with_the_grounding_rules(self):
        for m in modes.MODES:
            p = modes.system_prompt(m)
            self.assertIn("ONLY from the excerpts", p)
            self.assertIn("Never invent", p)
        self.assertFalse(modes.valid_mode("rm -rf"))

    def test_api_is_loopback_only_and_embedded_chroma(self):
        self.assertIn('app.run(host="127.0.0.1", port=8768, debug=False)', self.api)
        for src in (self.api, self.ingest):
            self.assertIn("chromadb.PersistentClient", src)
            self.assertNotIn("chromadb.HttpClient", src)
            self.assertNotIn("trust_remote_code", src)

    def test_api_needs_the_authelia_user_header_and_checks_identifiers_both_ways(self):
        self.assertIn('request.headers.get("X-Remote-User")', self.api)
        self.assertIn("detected_identifier(combined)", self.api)
        self.assertIn("detected_identifier(raw)", self.api)
        self.assertIn('data.get("identifiers_removed") is not True', self.api)

    def test_port_does_not_collide_with_other_local_services(self):
        self.assertNotIn("8766", self.api)
        self.assertNotIn("8767", self.api)

    def test_currency_monitor_is_wired_up(self):
        unit = (REPO / "ops" / "systemd" / "usaf-assistant-currency.service").read_text()
        self.assertIn("ExecStart=/var/www/noahpac-portal/ops/usaf-assistant-currency.sh", unit)
        timer = (REPO / "ops" / "systemd" / "usaf-assistant-currency.timer").read_text()
        self.assertIn("OnCalendar=", timer)  # wall-clock, not boot-relative
        self.assertNotIn("OnBootSec", timer)

    def test_systemd_sandbox(self):
        for d in ("User=www-data", "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "CapabilityBoundingSet=",
                  "ReadWritePaths=/var/lib/noahpac-usaf-assistant"):
            self.assertIn(d, self.unit)

    def test_nginx_requires_auth_and_forwards_the_user(self):
        self.assertEqual(self.nginx.count("auth_request            /internal/authelia/authz;"), 3)  # page, ask, result/health
        self.assertIn("proxy_set_header X-Remote-User $usaf_user;", self.nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8768;", self.nginx)
        self.assertIn("limit_req zone=usaf_assistant_api", self.nginx)
        self.assertIn("location = /usaf-assistant/api/ask", self.nginx)  # only starting a job is rate limited at 10/min; polling is not
        self.assertIn("alias /var/www/noahpac-portal/usaf-assistant/web/;", self.nginx)  # only static files are served, never the Python or state


if __name__ == "__main__":
    unittest.main()
