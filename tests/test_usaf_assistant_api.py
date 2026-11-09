"""End-to-end API flow with fake Voyage/Anthropic clients and a throwaway Chroma index.
Needs the clinical-assistant venv (chromadb, flask); skipped elsewhere:
  /opt/noahpac/clinical-assistant/venv/bin/python3 -m unittest tests.test_usaf_assistant_api"""
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "usaf-assistant"

try:
    import chromadb  # noqa: F401
    import flask  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


class FakeVoyage:
    def embed(self, texts, model=None, input_type=None):
        return types.SimpleNamespace(embeddings=[[1.0, 0.0, 0.0] for _ in texts])


class FakeClaude:
    reply = "Per MSD, 5 Jun 2026, C60 the standard applies to Flying Class II [S1]. See Table 9.9 [S7]."

    class messages:
        @staticmethod
        def create(**kw):
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=FakeClaude.reply)],
                                         usage=types.SimpleNamespace(input_tokens=100, output_tokens=50))


@unittest.skipUnless(HAVE, "needs chromadb and flask")
class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["USAF_ASSISTANT_STATE_DIR"] = cls.tmp.name
        os.environ["VOYAGE_API_KEY"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "x"
        for m in ("runtime_paths", "api_server"):
            sys.modules.pop(m, None)
        sys.path.insert(0, str(APP))
        cls.api = importlib.import_module("api_server")
        cls.api.vo = FakeVoyage()
        cls.api.claude = FakeClaude()
        cls.api.collection.add(
            ids=["a", "b"], embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            documents=["[MSD | C60 | p. 13]\nC60: Abnormal pupils.\nMSD columns marked X (standard applies): Flying Class II",
                       "[DAFI 31-117 | para 2.1 | p. 7]\nArming requires training."],
            metadatas=[{"source_filename": "MSD.pdf", "doc": "MSD", "title": "Medical Standards Directory", "category": "standards", "section": "C60",
                        "cite": "MSD, 5 Jun 2026, C60", "page_start": 13, "page_end": 13, "pub_date": "2026-06-05", "date_basis": "manifest"},
                       {"source_filename": "d.pdf", "doc": "DAFI 31-117", "title": "", "category": "arming", "section": "para 2.1", "cite": "",
                        "page_start": 7, "page_end": 7, "pub_date": "2020-01-01", "date_basis": "manifest"}])
        cls.api.load_index(force=True)
        cls.client = cls.api.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def post(self, body, user="tester"):
        h = {"X-Remote-User": user} if user else {}
        return self.client.post("/api/ask", json=body, headers=h)

    def ok_body(self, **kw):
        b = {"mode": "standards", "question": "Abnormal pupils for a Class II flyer?", "identifiers_removed": True}
        b.update(kw)
        return b

    def test_requires_user_header(self):
        self.assertEqual(self.post(self.ok_body(), user=None).status_code, 401)

    def test_requires_confirmation_and_blocks_identifiers(self):
        self.assertEqual(self.post(self.ok_body(identifiers_removed=False)).status_code, 400)
        r = self.post(self.ok_body(question="SSgt Smith 123-45-6789 pupils"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("blocked", r.get_json()["error"])
        r = self.post(self.ok_body(conversation_history=[{"role": "user", "content": "DOB 01/02/1990"}]))
        self.assertEqual(r.status_code, 400)

    def test_unknown_mode(self):
        self.assertEqual(self.post(self.ok_body(mode="shell")).status_code, 400)

    def test_answer_has_validated_citations_and_flags_unverified_references(self):
        FakeClaude.reply = "Per MSD, 5 Jun 2026, C60 the standard applies to Flying Class II [S1]. See Table 9.9 [S7]."
        d = self.post(self.ok_body()).get_json()
        self.assertEqual([c["tag"] for c in d["citations"]], ["S1"])
        self.assertEqual(d["citations"][0]["cite"], "MSD, 5 Jun 2026, C60")
        self.assertNotIn("[S7]", d["answer"])
        self.assertTrue(any("table 9.9" in w.lower() for w in d["warnings"]))
        self.assertIn("Decision support", d["disclaimer"])

    def test_answer_without_citation_is_withheld_except_narsum(self):
        FakeClaude.reply = "It is probably fine."
        self.assertIn("withheld", self.post(self.ok_body()).get_json()["answer"])
        d = self.post(self.ok_body(mode="narsum")).get_json()
        self.assertEqual(d["answer"], "It is probably fine.")
        self.assertTrue(any("cites no publication" in w for w in d["warnings"]))

    def test_thinking_blocks_are_skipped(self):
        class Think:
            type = "thinking"
            thinking = "..."
        class Text:
            type = "text"
            text = "Per C60 [S1]."
        orig = FakeClaude.messages.create
        FakeClaude.messages.create = staticmethod(lambda **kw: types.SimpleNamespace(content=[Think(), Text()], usage=types.SimpleNamespace(input_tokens=1, output_tokens=1)))
        try:
            d = self.post(self.ok_body()).get_json()
        finally:
            FakeClaude.messages.create = orig
        self.assertEqual(d["answer"], "Per C60 [S1].")

    def test_identifier_in_model_output_is_withheld(self):
        FakeClaude.reply = "Contact SSgt Jones about C60 [S1]."
        self.assertIn("withheld", self.post(self.ok_body()).get_json()["answer"])

    def test_daily_cap(self):
        FakeClaude.reply = "Ok [S1]."
        self.api.DAILY_CALLS = 2
        try:
            codes = [self.post(self.ok_body(), user="capped").status_code for _ in range(3)]
        finally:
            self.api.DAILY_CALLS = 60
        self.assertEqual(codes, [200, 200, 429])

    def test_audit_log_has_no_question_text(self):
        FakeClaude.reply = "Ok [S1]."
        self.post(self.ok_body(question="Abnormal pupils unique-marker-xyz?"), user="audited")
        log = (Path(self.tmp.name) / "audit.jsonl").read_text()
        self.assertIn("audited", log)
        self.assertNotIn("unique-marker-xyz", log)


if __name__ == "__main__":
    unittest.main()
