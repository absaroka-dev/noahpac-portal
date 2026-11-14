"""Staged library updates: the daily staging step, the review helpers and the evaluation gate (no network, no API keys, no index).

Run: python3 -m unittest tests.test_usaf_library_update
"""
import datetime
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "usaf-assistant"
sys.path.insert(0, str(APP))

import check_library_currency as cur  # noqa: E402
import library_update as lu  # noqa: E402


def sha(b):
    return hashlib.sha256(b).hexdigest()


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lib, self.stage, self.incoming = self.tmp / "library", self.tmp / "staging", self.tmp / "in"
        for d in (self.lib, self.incoming):
            d.mkdir()
        self.old_lib = cur.LIBRARY_DIR
        cur.LIBRARY_DIR = self.lib
        (self.lib / "a.pdf").write_bytes(b"old bytes")
        self.manifest = {"a.pdf": {"short": "DOC A", "check_url": "https://x/a.pdf"}}

    def tearDown(self):
        cur.LIBRARY_DIR = self.old_lib

    def docs(self, status="update_available"):
        return {"a.pdf": {"short": "DOC A", "status": status, "message": "e-Publishing has a different file."}}

    def fps(self, content=b"new bytes", saved=True):
        f = self.incoming / "a.pdf"
        if saved:
            f.write_bytes(content)
        return {"https://x/a.pdf": {"status": 200, "sha256": sha(content), "last_modified": "Fri, 13 Nov 2026 18:08:13 GMT", **({"saved": str(f)} if saved else {})}}

    def test_changed_file_is_staged_with_meta(self):
        docs = self.docs()
        self.assertEqual(cur.stage_updates(self.manifest, docs, self.fps(), self.incoming, staging=self.stage, now="T"), ["a.pdf"])
        meta = json.loads((self.stage / "a.pdf" / "meta.json").read_text())
        self.assertEqual(meta["sha256"], sha(b"new bytes"))
        self.assertEqual(meta["loaded_sha256"], sha(b"old bytes"))
        self.assertEqual((self.stage / "a.pdf" / meta["staged_file"]).read_bytes(), b"new bytes")
        self.assertIn("staged", docs["a.pdf"])
        self.assertIn("review", docs["a.pdf"]["message"])
        self.assertEqual((self.lib / "a.pdf").read_bytes(), b"old bytes", "staging must never touch the library")

    def test_newer_download_replaces_the_older_staged_one(self):
        cur.stage_updates(self.manifest, self.docs(), self.fps(b"v2"), self.incoming, staging=self.stage)
        cur.stage_updates(self.manifest, self.docs(), self.fps(b"v3"), self.incoming, staging=self.stage)
        files = sorted(p.name for p in (self.stage / "a.pdf").iterdir())
        self.assertEqual(len(files), 2, files)  # meta.json + exactly one staged pdf
        self.assertEqual(json.loads((self.stage / "a.pdf" / "meta.json").read_text())["sha256"], sha(b"v3"))

    def test_staged_copy_is_removed_once_the_library_matches_again(self):
        cur.stage_updates(self.manifest, self.docs(), self.fps(), self.incoming, staging=self.stage)
        cur.stage_updates(self.manifest, self.docs("current"), self.fps(), self.incoming, staging=self.stage)
        self.assertFalse((self.stage / "a.pdf").exists())

    def test_hash_mismatch_is_not_staged(self):
        fps = self.fps()
        fps["https://x/a.pdf"]["sha256"] = "0" * 64
        docs = self.docs()
        self.assertEqual(cur.stage_updates(self.manifest, docs, fps, self.incoming, staging=self.stage), [])
        self.assertIn("stage_error", docs["a.pdf"])
        self.assertFalse((self.stage / "a.pdf").exists())

    def test_missing_download_is_reported_not_crashed(self):
        docs = self.docs()
        self.assertEqual(cur.stage_updates(self.manifest, docs, self.fps(saved=False), self.incoming, staging=self.stage), [])
        self.assertIn("stage_error", docs["a.pdf"])

    def test_metrics_list_staged_publications(self):
        docs = self.docs()
        cur.stage_updates(self.manifest, docs, self.fps(), self.incoming, staging=self.stage)
        text = cur.metrics_text(docs, 1.0)
        self.assertIn('usaf_library_doc_staged{doc="DOC A"} 1', text)
        self.assertNotIn("usaf_library_doc_staged{", cur.metrics_text(self.docs("current"), 1.0))


class ReviewHelperTests(unittest.TestCase):
    FILL = "\n".join(["The commander shall ensure that every member is briefed and records the briefing."] * 16)  # ~1300 chars over many lines: long enough that the chunker starts a new section at the next numbered paragraph
    OLD = [(1, "CHAPTER 1 GENERAL\n1.1. Purpose. " + FILL + "\n1.2. Scope. " + FILL + "\n1.3. Waivers. " + FILL + "\n")]
    NEW = [(1, "CHAPTER 1 GENERAL\n1.1. Purpose. " + FILL + "\n1.2. Scope. " + FILL + " It also applies to the Reserve.\n1.4. New paragraph. " + FILL + "\n")]

    def test_compare_sections_finds_added_removed_changed(self):
        old, new = lu.section_texts(self.OLD), lu.section_texts(self.NEW)
        c = lu.compare_sections(old, new)
        self.assertIn("para 1.3", c["removed"])
        self.assertIn("para 1.4", c["added"])
        self.assertIn("para 1.2", c["changed"])

    def test_diff_excerpt_shows_only_changed_lines(self):
        d = lu.diff_excerpt("a\nb\nc", "a\nB\nc")
        self.assertEqual(d, ["-b", "+B"])

    def test_diff_ignores_whitespace_and_page_numbers(self):
        self.assertEqual(lu.diff_excerpt("a  b\n12\nc", "a b\nPage 13 of 40\nc"), [])

    def test_find_dates_on_cover(self):
        pages = [(1, "BY ORDER OF THE SECRETARY\n13 November 2026\nCertified current 2 Jan 2026"), (9, "30 April 2020")]
        self.assertEqual(lu.find_dates(pages), ["13 November 2026", "2 Jan 2026"])

    def test_find_doc_by_short_name_and_file_name(self):
        m = {"a.pdf": {"short": "DAFI 31-117"}, "b.pdf": {"short": "DAFMAN 48-123"}, "_note": "x"}
        self.assertEqual(lu.find_doc(m, "dafi 31-117")[0], "a.pdf")
        self.assertEqual(lu.find_doc(m, "B.PDF")[0], "b.pdf")
        with self.assertRaises(lu.UpdateError):
            lu.find_doc(m, "nope")


class GateTests(unittest.TestCase):
    OLD_SECTIONS = {f"para {i}" for i in range(1, 31)}
    OK_PROBES = {"asked": 25, "found": 24}
    NO_GOLDEN = {"asked": 0, "failed": []}

    def check(self, new_info=None, new_sections=None, probes=None, golden=None):
        return lu.gate({"chunks": 100, "pages": 50}, new_info or {"chunks": 105, "pages": 52},
                       self.OLD_SECTIONS, new_sections if new_sections is not None else set(self.OLD_SECTIONS),
                       probes or self.OK_PROBES, golden or self.NO_GOLDEN)

    def test_healthy_candidate_passes(self):
        r = self.check()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["reasons"], [])

    def test_empty_or_shrunken_candidates_fail(self):
        self.assertFalse(self.check({"chunks": 0, "pages": 52})["ok"])
        self.assertFalse(self.check({"chunks": 40, "pages": 52})["ok"])
        self.assertFalse(self.check({"chunks": 105, "pages": 20})["ok"])

    def test_ingest_error_fails(self):
        self.assertFalse(self.check({"chunks": 105, "pages": 52, "error": "boom"})["ok"])

    def test_lost_sections_fail_or_warn(self):
        few = {f"para {i}" for i in range(1, 11)}
        self.assertFalse(self.check(new_sections=few)["ok"])  # 33% preserved
        most = {f"para {i}" for i in range(1, 24)}
        r = self.check(new_sections=most)  # 77% preserved
        self.assertTrue(r["ok"])
        self.assertTrue(r["warnings"])

    def test_retrieval_probes_decide(self):
        self.assertFalse(self.check(probes={"asked": 25, "found": 15})["ok"])
        r = self.check(probes={"asked": 25, "found": 21})
        self.assertTrue(r["ok"] and r["warnings"])
        self.assertTrue(self.check(probes={"asked": 2, "found": 2})["warnings"])  # too few probes is a warning, not a pass

    def test_any_failed_golden_question_fails(self):
        r = self.check(golden={"asked": 2, "failed": ["what is the rule?"]})
        self.assertFalse(r["ok"])
        self.assertIn("what is the rule?", " ".join(r["reasons"]))

    def test_probes_use_old_passages_whose_sections_survive(self):
        body = "x" * 300
        old = [{"section": f"para {i}", "page_start": i, "body": body} for i in range(1, 61)]
        old.append({"section": "(front matter)", "page_start": 0, "body": body})
        old.append({"section": "para 99", "page_start": 99, "body": body})  # gone from the new version
        probes = lu.make_probes(old, {f"para {i}" for i in range(1, 61)}, n=10)
        self.assertEqual(len(probes), 10)
        self.assertTrue(all(p["section"] != "para 99" and len(p["query"]) == 240 for p in probes))


class SwapTests(unittest.TestCase):
    """The swap with fake directories (no service, no index): success, automatic restore on any failure, backup pruning."""

    class Args:
        no_restart = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state, self.lib = self.tmp / "state", self.tmp / "state" / "library"
        self.saved = (lu.STATE_DIR, lu.LIBRARY_DIR, lu.BACKUP_DIR, lu.copy_atomic)
        lu.STATE_DIR, lu.LIBRARY_DIR, lu.BACKUP_DIR = self.state, self.lib, self.state / "backups"
        self.write(self.state, "old", "old v")
        self.cand = self.state / "candidate-1"
        self.write(self.cand, "new", "new v")

    def tearDown(self):
        lu.STATE_DIR, lu.LIBRARY_DIR, lu.BACKUP_DIR, lu.copy_atomic = self.saved

    @staticmethod
    def write(base, tag, version):
        (base / "library").mkdir(parents=True, exist_ok=True)
        (base / "library" / "a.pdf").write_text(f"{tag} pdf")
        (base / "library" / "manifest.json").write_text(json.dumps({"a.pdf": {"short": "DOC A", "version": version}}))
        (base / "ingest_report.json").write_text(json.dumps({"documents": [{"file": "a.pdf", "version": version}]}))
        (base / "index.stamp").write_text(tag)
        (base / "chroma_data").mkdir(exist_ok=True)
        (base / "chroma_data" / "idx.txt").write_text(f"{tag} index")

    def live(self):
        return ((self.lib / "a.pdf").read_text(), (self.state / "chroma_data" / "idx.txt").read_text(),
                json.loads((self.lib / "manifest.json").read_text())["a.pdf"]["version"], (self.state / "index.stamp").read_text())

    def test_swap_installs_everything_and_cleans_up(self):
        backup = lu.make_backup("a.pdf", "T1")
        lu.swap(self.cand, "a.pdf", {"version": "new v"}, backup, self.Args())
        self.assertEqual(self.live(), ("new pdf", "new index", "new v", "new"))
        self.assertEqual([p.name for p in self.state.glob("chroma_data.replaced-*")], [])
        self.assertEqual((backup / "chroma_data" / "idx.txt").read_text(), "old index", "the backup must keep the old index")

    def test_failure_in_the_middle_restores_the_old_library_and_index(self):
        backup = lu.make_backup("a.pdf", "T1")
        real, calls = lu.copy_atomic, []

        def flaky(src, dst):
            if Path(dst).name == "manifest.json" and not calls:
                calls.append(1)  # fail once (during the swap), not again during the restore
                raise OSError("disk full")
            real(src, dst)
        lu.copy_atomic = flaky
        with self.assertRaises(lu.UpdateError) as cm:
            lu.swap(self.cand, "a.pdf", {"version": "new v"}, backup, self.Args())
        lu.copy_atomic = real
        self.assertIn("restored", str(cm.exception))
        self.assertEqual(self.live(), ("old pdf", "old index", "old v", "old"))

    def test_wrong_version_after_the_swap_restores_the_backup(self):
        backup = lu.make_backup("a.pdf", "T1")
        with self.assertRaises(lu.UpdateError):
            lu.swap(self.cand, "a.pdf", {"version": "something else"}, backup, self.Args())
        self.assertEqual(self.live(), ("old pdf", "old index", "old v", "old"))

    def test_rollback_command_path_restores_from_backup(self):
        backup = lu.make_backup("a.pdf", "T1")
        lu.swap(self.cand, "a.pdf", {"version": "new v"}, backup, self.Args())
        lu.restore_backup(backup, "a.pdf", self.Args())
        self.assertEqual(self.live(), ("old pdf", "old index", "old v", "old"))

    def test_only_the_latest_backups_are_kept(self):
        for i in range(5):
            lu.make_backup("a.pdf", f"2026010{i}T000000")
        lu.prune_backups("a.pdf")
        self.assertEqual(len(list(lu.BACKUP_DIR.iterdir())), lu.KEEP_BACKUPS)
        self.assertTrue((lu.BACKUP_DIR / "20260104T000000-a.pdf").exists())


class EnvFileTests(unittest.TestCase):
    def test_export_lines_comments_and_quotes_are_understood(self):
        import os
        f = Path(tempfile.mkdtemp()) / "env"
        f.write_text("# comment\nexport TEST_KEY_ONE=abc\nTEST_KEY_TWO='def'\nexport TEST_KEY_THREE=\"ghi\"\n\nnot a pair\n")
        for k in ("TEST_KEY_ONE", "TEST_KEY_TWO", "TEST_KEY_THREE"):
            os.environ.pop(k, None)
        lu.load_env_file(f)
        self.assertEqual((os.environ["TEST_KEY_ONE"], os.environ["TEST_KEY_TWO"], os.environ["TEST_KEY_THREE"]), ("abc", "def", "ghi"))
        lu.load_env_file(Path("/nonexistent/env"))  # a missing file is not an error


class ManifestUpdateTests(unittest.TestCase):
    def test_new_entry_keeps_url_and_marks_verified(self):
        class A:
            version = None
            pub_date = None
            note = None
        e = lu.new_manifest_entry({"short": "DAFI 31-117", "check_url": "https://x", "pub_date": "2025-01-28", "version": "old"},
                                  {"remote_last_modified": "Fri, 13 Nov 2026 18:08:13 GMT"}, A(), datetime.date(2026, 11, 14))
        self.assertEqual(e["check_url"], "https://x")
        self.assertEqual(e["pub_date"], "2025-01-28")
        self.assertEqual(e["last_verified"], "2026-11-14")
        self.assertIn("edition date not confirmed", e["version"])

    def test_explicit_version_and_date_win_and_bad_dates_are_rejected(self):
        class A:
            version, pub_date, note = "DAFI 31-117, 13 Nov 2026", "2026-11-13", "reissued"
        e = lu.new_manifest_entry({"short": "DAFI 31-117"}, {}, A(), datetime.date(2026, 11, 14))
        self.assertEqual((e["version"], e["pub_date"], e["note"]), ("DAFI 31-117, 13 Nov 2026", "2026-11-13", "reissued"))
        A.pub_date = "13/11/2026"
        with self.assertRaises(ValueError):
            lu.new_manifest_entry({"short": "x"}, {}, A(), datetime.date(2026, 11, 14))


if __name__ == "__main__":
    unittest.main()
