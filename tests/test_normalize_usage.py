"""Tests for scripts/normalize_usage.py using real usage_snapshot_document serializer fixtures."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Import normalize functions
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from normalize_usage import normalize_usage_document, parse_iso_or_epoch, main as normalize_main


class TestNormalizeUsage(unittest.TestCase):
    def setUp(self):
        # Real schema fixture matching core usage_snapshot_document serializer
        self.sample_snapshot = {
            "provider": "openai-codex",
            "source": "api",
            "title": "Codex Tier 5",
            "plan": "team",
            "fetched_at": "2026-10-24T10:00:00+00:00",
            "windows": [
                {
                    "label": "5-hour session",
                    "used_percent": 18.0,
                    "resets_at": "2026-10-24T18:40:00+00:00",
                    "detail": "18% used",
                },
                {
                    "label": "Weekly cap",
                    "used_percent": 39.0,
                    "resets_at": "2026-10-31T09:00:00+00:00",
                    "detail": "39% used",
                },
            ],
            "details": ["session window: 18%", "weekly cap: 39%"],
            "unavailable_reason": None,
        }

    def test_normalize_usage_document_structure(self):
        res = normalize_usage_document(self.sample_snapshot)
        self.assertEqual(res["provider"], "openai-codex")
        self.assertIsInstance(res["fetched_at"], float)
        self.assertIn("session", res["windows"])
        self.assertIn("weekly", res["windows"])

        session = res["windows"]["session"]
        self.assertEqual(session["used_percent"], 18.0)
        self.assertEqual(session["resets_at"], "2026-10-24T18:40:00+00:00")

        weekly = res["windows"]["weekly"]
        self.assertEqual(weekly["used_percent"], 39.0)
        self.assertEqual(weekly["resets_at"], "2026-10-31T09:00:00+00:00")

    def test_missing_provider_raises_value_error(self):
        bad_doc = {"windows": []}
        with self.assertRaises(ValueError):
            normalize_usage_document(bad_doc)

    def test_parse_iso_or_epoch(self):
        self.assertIsNone(parse_iso_or_epoch(None))
        self.assertEqual(parse_iso_or_epoch(123456.78), 123456.78)
        self.assertIsInstance(parse_iso_or_epoch("2026-10-24T10:00:00+00:00"), float)
        self.assertIsNone(parse_iso_or_epoch("not-a-date"))

    def test_cli_stdin_to_stdout(self):
        input_data = json.dumps(self.sample_snapshot)
        with patch("sys.stdin", io.StringIO(input_data)):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = normalize_main(argv=[])
                self.assertEqual(ret, 0)
                output = json.loads(mock_stdout.getvalue())
                self.assertEqual(output["provider"], "openai-codex")
                self.assertEqual(output["windows"]["session"]["used_percent"], 18.0)

    def test_cli_write_file(self):
        input_data = json.dumps(self.sample_snapshot)
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            out_path = Path(tf.name)
        try:
            with patch("sys.stdin", io.StringIO(input_data)):
                ret = normalize_main(argv=["--out", str(out_path)])
                self.assertEqual(ret, 0)
                written = json.loads(out_path.read_text(encoding="utf-8"))
                self.assertEqual(written["provider"], "openai-codex")
                self.assertIn("session", written["windows"])
        finally:
            out_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
