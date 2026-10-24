"""Independent review: unknown quota windows/freshness must not be invented."""
import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('normalizer_review_module', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'normalize_usage.py')
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)

class ReviewNormalizerSafety(unittest.TestCase):
    def test_unrecognized_window_is_not_assigned_to_session(self):
        result = normalizer.normalize_usage_document({'provider':'openai-codex','fetched_at':'2026-10-24T10:00:00+00:00','windows':[{'label':'Unrecognized provider-specific limit','used_percent':17}]})
        self.assertEqual(result['windows'], {})

    def test_missing_fetch_timestamp_is_rejected_not_refreshed(self):
        with self.assertRaises(ValueError):
            normalizer.normalize_usage_document({'provider':'openai-codex','windows':[{'label':'Session','used_percent':17}]})

    def test_invalid_fetch_timestamp_is_rejected_not_refreshed(self):
        with self.assertRaises(ValueError):
            normalizer.normalize_usage_document({'provider':'openai-codex','fetched_at':'invalid','windows':[{'label':'Session','used_percent':17}]})
