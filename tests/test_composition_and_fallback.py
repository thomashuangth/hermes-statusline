"""Tests for statusline composition, fallback detection, and conservative recovery ETA."""

import unittest
from types import SimpleNamespace

from _helpers import sl


class TestComposition(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0  # 2024-10-24T08:40:00Z
        self.quota_cache = {
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

    def test_active_compact_single_line_shape(self):
        line = sl.compose_line(
            fields=["model", "effort", "ctx", "quota", "served_model"],
            native_render=lambda n: "gpt-6.1-sol" if n == "model" else "",
            effort_report={"enabled": True, "effort": "low"},
            context_tokens=24_000,
            context_length=100_000,
            quota_cache=self.quota_cache,
            quota_provider="openai-codex",
            now=self.now,
        )
        expected = "`gpt-6.1-sol · low · ctx 24% · 5h 18% (18:40) · 7j 39% (31/10 09:00)`"
        self.assertEqual(line, expected)
        self.assertEqual(len(line.splitlines()), 1)

    def test_native_line_untouched_when_no_extras(self):
        line = sl.compose_line(
            fields=["model", "context_pct"],
            native_render=lambda n: {"model": "gpt-4o", "context_pct": "12%"}.get(n, ""),
            effort_report=None,
            context_tokens=0,
            context_length=None,
        )
        self.assertEqual(line, "gpt-4o · 12%")
        self.assertFalse(line.startswith("`"))

    def test_backticks_in_native_fields_are_escaped(self):
        line = sl.compose_line(
            fields=["model", "ctx"],
            native_render=lambda n: "`custom-model`" if n == "model" else "",
            effort_report=None,
            context_tokens=10_000,
            context_length=100_000,
        )
        self.assertEqual(line, "`ˋcustom-modelˋ · ctx 10%`")


class TestConservativeRecoveryETA(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0  # Reference epoch (2024-10-24 08:40:00 UTC)

    def _quota_cache(self, session_used=100, weekly_used=20, session_reset="2024-10-24T10:40:00+00:00"):
        return {
            "source": "usage_api",
            "provider": "openai-codex",
            "fetched_at": self.now,
            "windows": {
                "session": {
                    "used_percent": session_used,
                    "resets_at": session_reset,
                },
                "weekly": {
                    "used_percent": weekly_used,
                    "resets_at": "2024-10-31T08:40:00+00:00",
                },
            },
        }

    def test_exhausted_window_calculates_recovery_eta(self):
        cache = self._quota_cache(session_used=100, weekly_used=10, session_reset="2024-10-24T10:40:00+00:00")
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now)
        self.assertEqual(eta, "(recovery 10:40)")

    def test_multiple_exhausted_windows_take_latest_reset(self):
        cache = {
            "source": "usage_api",
            "provider": "openai-codex",
            "fetched_at": self.now,
            "windows": {
                "session": {"used_percent": 100, "resets_at": "2024-10-24T10:40:00+00:00"},
                "weekly": {"used_percent": 100, "resets_at": "2024-10-25T14:00:00+00:00"},
            },
        }
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now)
        self.assertEqual(eta, "(recovery 25/10 14:00)")

    def test_non_exhausted_windows_never_invent_eta(self):
        # 43% and 7% used means neither window is exhausted -> cannot invent recovery ETA
        cache = self._quota_cache(session_used=43, weekly_used=7)
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now)
        self.assertEqual(eta, "(recovery unknown)")

    def test_stale_cache_returns_unknown(self):
        cache = self._quota_cache(session_used=100)
        cache["fetched_at"] = self.now - (sl.FRESH_SECONDS + 1)
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now)
        self.assertEqual(eta, "(recovery unknown)")

    def test_foreign_provider_returns_unknown(self):
        cache = self._quota_cache(session_used=100)
        cache["provider"] = "deepseek"
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now)
        self.assertEqual(eta, "(recovery unknown)")

    def test_credential_pool_declared_reset(self):
        pool_reset = self.now + 600.0  # +10 minutes -> 08:50 UTC
        pool = SimpleNamespace(
            entries=lambda: [
                SimpleNamespace(last_status="exhausted", last_error_reset_at=pool_reset)
            ],
            next_available_at=lambda **kw: pool_reset,
        )
        eta = sl.estimate_recovery(None, primary="openai-codex", now=self.now, pool=pool)
        self.assertEqual(eta, "(recovery 08:50)")

    def test_credential_pool_mismatch_returns_unknown(self):
        pool_reset = self.now + 600.0
        # If next_available_at diverges from entry reset, it's inconsistent -> unknown
        pool = SimpleNamespace(
            entries=lambda: [
                SimpleNamespace(last_status="exhausted", last_error_reset_at=pool_reset)
            ],
            next_available_at=lambda **kw: pool_reset + 300,
        )
        eta = sl.estimate_recovery(None, primary="openai-codex", now=self.now, pool=pool)
        self.assertEqual(eta, "(recovery unknown)")

    def test_french_recovery_style(self):
        cache = self._quota_cache(session_used=100, session_reset="2024-10-24T10:40:00+00:00")
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=self.now, recovery_style="fr")
        self.assertEqual(eta, "(reprise estimée 10:40)")

        unknown = sl.estimate_recovery(self._quota_cache(session_used=50), primary="openai-codex", now=self.now, recovery_style="fr")
        self.assertEqual(unknown, "(reprise estimée inconnue)")


class TestFallbackDecoration(unittest.TestCase):
    def test_fallback_statusline_decoration(self):
        base_line = "`deepseek-flash · low · ctx 6% · credit $16.06`"
        decorated = sl.decorate_fallback_statusline(
            base_line,
            fallback_active=True,
            fallback_marker="fallback deepseek",
            recovery="(recovery unknown)",
        )
        self.assertEqual(
            decorated,
            "`deepseek-flash · low · ctx 6% · credit $16.06 · fallback deepseek · (recovery unknown)`",
        )

    def test_non_fallback_stays_unchanged(self):
        base_line = "`gpt-6.1-sol · low · ctx 24%`"
        decorated = sl.decorate_fallback_statusline(
            base_line,
            fallback_active=False,
            fallback_marker="",
            recovery="",
        )
        self.assertEqual(decorated, base_line)


if __name__ == "__main__":
    unittest.main()
