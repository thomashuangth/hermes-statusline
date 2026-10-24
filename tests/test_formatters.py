"""Tests for formatters: effort, context, quota, credit, staleness, and timezone."""

import time
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from _helpers import sl


class TestEffortFormatter(unittest.TestCase):
    def test_effort_values(self):
        self.assertEqual(sl.format_effort(None), "")
        self.assertEqual(sl.format_effort({}), "")
        self.assertEqual(sl.format_effort({"enabled": False, "effort": "high"}), "none (configured)")
        self.assertEqual(sl.format_effort({"enabled": True, "effort": "low"}), "low (configured)")
        self.assertEqual(sl.format_effort({"enabled": True, "effort": "medium"}), "medium (configured)")
        self.assertEqual(sl.format_effort({"enabled": True, "effort": "high"}), "high (configured)")
        self.assertEqual(sl.format_effort({"enabled": True}), "")
        self.assertEqual(sl.format_effort("invalid"), "")


class TestContextFormatter(unittest.TestCase):
    def test_context_percentage(self):
        self.assertEqual(sl.format_ctx(24_000, 100_000), "ctx 24%")
        self.assertEqual(sl.format_ctx(42_000, 100_000), "ctx 42%")
        self.assertEqual(sl.format_ctx(0, 100_000), "ctx 0%")
        self.assertEqual(sl.format_ctx(150_000, 100_000), "ctx 100%")  # Clamped to 100%

    def test_invalid_context_inputs(self):
        self.assertEqual(sl.format_ctx(None, 100_000), "")
        self.assertEqual(sl.format_ctx(10_000, None), "")
        self.assertEqual(sl.format_ctx(0, 0), "")
        self.assertEqual(sl.format_ctx(-10, 100_000), "")
        self.assertEqual(sl.format_ctx("bad", "input"), "")


class TestQuotaConsumedSemantics(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0  # Reference epoch
        self.cache = {
            "provider": "openai-codex",
            "source": "usage_api",
            "fetched_at": self.now,
            "windows": {
                "session": {
                    "label": "5-Hour Limit",
                    "used_percent": 18.0,
                    "resets_at": "2026-10-24T18:40:00+00:00",
                },
                "weekly": {
                    "label": "Weekly Limit",
                    "used_percent": 39.0,
                    "resets_at": "2026-10-31T09:00:00+00:00",
                },
            },
        }

    def test_consumed_percentage_is_used_not_remaining(self):
        # 18% used must render as 18%, and 39% used must render as 39%
        output = sl.format_quota(self.cache, now=self.now, provider="openai-codex")
        self.assertIn("5h 18%", output)
        self.assertIn("7j 39%", output)
        self.assertNotIn("82%", output)
        self.assertNotIn("61%", output)

    def test_resets_formatted_in_utc_by_default(self):
        output = sl.format_quota(self.cache, now=self.now, provider="openai-codex")
        self.assertEqual(output, "5h 18% (18:40) · 7j 39% (31/10 09:00)")

    def test_resets_formatted_in_custom_timezone(self):
        paris_tz = ZoneInfo("Europe/Paris")
        # In Paris on Oct 24, UTC+2 (CEST): 18:40 UTC -> 20:40; Oct 31 09:00 UTC -> 10:00 (CET, UTC+1 after change)
        output = sl.format_quota(self.cache, now=self.now, provider="openai-codex", tz=paris_tz)
        self.assertIn("5h 18% (20:40)", output)

    def test_missing_resets_still_shows_used_percent(self):
        cache_no_resets = {
            "provider": "openai-codex",
            "source": "usage_api",
            "fetched_at": self.now,
            "windows": {
                "session": {"used_percent": 25.0},
                "weekly": {"used_percent": 50.0},
            },
        }
        output = sl.format_quota(cache_no_resets, now=self.now, provider="openai-codex")
        self.assertEqual(output, "5h 25% · 7j 50%")


class TestQuotaFreshnessAndStaleness(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0
        self.base_cache = {
            "provider": "openai-codex",
            "source": "usage_api",
            "fetched_at": self.now,
            "windows": {
                "session": {"used_percent": 10.0, "resets_at": "2026-10-24T18:40:00+00:00"},
                "weekly": {"used_percent": 20.0, "resets_at": "2026-10-31T09:00:00+00:00"},
            },
        }

    def test_fresh_quota_has_no_stale_marker(self):
        # 15 minutes old <= 30 minutes
        cache = {**self.base_cache, "fetched_at": self.now - 15 * 60}
        output = sl.format_quota(cache, now=self.now, provider="openai-codex")
        self.assertTrue(output.startswith("5h 10%"))
        self.assertNotIn("~", output)

    def test_stale_quota_prefixed_with_tilde(self):
        # 45 minutes old > 30 minutes and <= 6 hours
        cache = {**self.base_cache, "fetched_at": self.now - 45 * 60}
        output = sl.format_quota(cache, now=self.now, provider="openai-codex")
        self.assertTrue(output.startswith("~5h 10%"))
        self.assertIn("~7j 20%", output)

    def test_turn_refresh_fallback_marks_stale_even_if_recent(self):
        # Fresh timestamp, but network refresh failed/fallback
        cache = {**self.base_cache, "turn_refresh_fallback": True}
        output = sl.format_quota(cache, now=self.now, provider="openai-codex")
        self.assertTrue(output.startswith("~5h 10%"))

    def test_expired_quota_dropped_after_6_hours(self):
        # 7 hours old > 6 hours
        cache = {**self.base_cache, "fetched_at": self.now - 7 * 3600}
        output = sl.format_quota(cache, now=self.now, provider="openai-codex")
        self.assertEqual(output, "")

    def test_future_timestamp_rejected(self):
        # More than 60s in the future
        cache = {**self.base_cache, "fetched_at": self.now + 120}
        output = sl.format_quota(cache, now=self.now, provider="openai-codex")
        self.assertEqual(output, "")

    def test_invalid_or_missing_quota_cache_renders_nothing(self):
        self.assertEqual(sl.format_quota(None, provider="openai-codex"), "")
        self.assertEqual(sl.format_quota({}, provider="openai-codex"), "")
        self.assertEqual(sl.format_quota({"provider": "openai-codex"}, provider="openai-codex"), "")
        self.assertEqual(
            sl.format_quota({"provider": "openai-codex", "fetched_at": "bad"}, provider="openai-codex"), ""
        )


class TestCreditFormatting(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0
        self.cache = {
            "fetched_at": self.now,
            "credits": {
                "deepseek": {
                    "balance": 16.06,
                    "currency": "USD",
                    "fetched_at": self.now,
                },
                "kie": {
                    "balance": 179.51,
                    "unit_type": "credits",
                    "fetched_at": self.now,
                },
            },
        }

    def test_deepseek_currency_format(self):
        output = sl.format_credit(self.cache, now=self.now, provider="deepseek")
        self.assertEqual(output, "credit $16.06")

    def test_kie_unit_format(self):
        output = sl.format_credit(self.cache, now=self.now, provider="kie")
        self.assertEqual(output, "credits 179.5")

    def test_stale_credit_marked_with_tilde(self):
        stale_cache = {
            "fetched_at": self.now - 45 * 60,
            "credits": {
                "deepseek": {
                    "balance": 12.50,
                    "currency": "USD",
                    "fetched_at": self.now - 45 * 60,
                }
            },
        }
        output = sl.format_credit(stale_cache, now=self.now, provider="deepseek")
        self.assertEqual(output, "~credit $12.50")

    def test_refreshed_credit_clears_stale_mark(self):
        stale_cache = {
            "fetched_at": self.now - 45 * 60,
            "credits": {
                "deepseek": {
                    "balance": 12.50,
                    "currency": "USD",
                    "fetched_at": self.now - 45 * 60,
                }
            },
        }
        output = sl.format_credit(stale_cache, now=self.now, provider="deepseek", refreshed=True)
        self.assertEqual(output, "credit $12.50")

    def test_expired_credit_dropped_after_6_hours(self):
        expired_cache = {
            "fetched_at": self.now - 7 * 3600,
            "credits": {
                "deepseek": {
                    "balance": 12.50,
                    "currency": "USD",
                    "fetched_at": self.now - 7 * 3600,
                }
            },
        }
        output = sl.format_credit(expired_cache, now=self.now, provider="deepseek")
        self.assertEqual(output, "")

    def test_unparseable_balance_renders_nothing(self):
        bad_cache = {
            "fetched_at": self.now,
            "credits": {
                "deepseek": {
                    "balance": "not-a-number",
                    "currency": "USD",
                    "fetched_at": self.now,
                }
            },
        }
        self.assertEqual(sl.format_credit(bad_cache, now=self.now, provider="deepseek"), "")


class TestTimezoneResolution(unittest.TestCase):
    def test_default_is_utc(self):
        tz = sl.get_configured_timezone(None)
        self.assertEqual(tz, timezone.utc)

    def test_valid_timezone_string(self):
        tz = sl.get_configured_timezone("Europe/Paris")
        self.assertEqual(str(tz), "Europe/Paris")

    def test_invalid_timezone_falls_back_to_utc(self):
        tz = sl.get_configured_timezone("Invalid/NonExistent_TZ")
        self.assertEqual(tz, timezone.utc)


if __name__ == "__main__":
    unittest.main()
