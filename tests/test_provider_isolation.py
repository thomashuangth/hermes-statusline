"""Tests for provider isolation, alias handling, and fail-closed routing."""

import unittest
from types import SimpleNamespace

from _helpers import sl


class TestProviderMatching(unittest.TestCase):
    def test_same_provider_matches_valid_providers(self):
        self.assertTrue(sl.same_provider("openai-codex", "openai-codex"))
        self.assertTrue(sl.same_provider("deepseek", "deepseek"))
        self.assertTrue(sl.same_provider("kie", "kie"))

    def test_same_provider_alias_support(self):
        self.assertTrue(sl.same_provider("codex", "openai-codex"))
        self.assertFalse(sl.same_provider("openai", "openai-codex"))  # OpenAI API != Codex subscription
        self.assertTrue(sl.same_provider("chatgpt", "openai-codex"))
        self.assertTrue(sl.same_provider("deep-seek", "deepseek"))
        self.assertTrue(sl.same_provider("kie-flash", "kie"))

    def test_same_provider_fails_closed_on_empty_or_none(self):
        # A missing provider on either side must NEVER match
        self.assertFalse(sl.same_provider(None, "openai-codex"))
        self.assertFalse(sl.same_provider("", "openai-codex"))
        self.assertFalse(sl.same_provider("openai-codex", None))
        self.assertFalse(sl.same_provider("openai-codex", ""))
        self.assertFalse(sl.same_provider(None, None))
        self.assertFalse(sl.same_provider("", ""))

    def test_provider_mismatch_fails_closed(self):
        self.assertFalse(sl.same_provider("deepseek", "openai-codex"))
        self.assertFalse(sl.same_provider("kie", "openai-codex"))
        self.assertFalse(sl.same_provider("deepseek", "kie"))


class TestProviderDisplayIsolation(unittest.TestCase):
    def setUp(self):
        self.now = 1729759200.0
        self.quota_cache = {
            "provider": "openai-codex",
            "source": "usage_api",
            "fetched_at": self.now,
            "windows": {
                "session": {"used_percent": 20.0, "resets_at": "2026-10-24T18:40:00+00:00"},
                "weekly": {"used_percent": 40.0, "resets_at": "2026-10-31T09:00:00+00:00"},
            },
        }
        self.credit_cache = {
            "fetched_at": self.now,
            "credits": {
                "deepseek": {"balance": 15.00, "currency": "USD", "fetched_at": self.now},
                "kie": {"balance": 150.0, "unit_type": "credits", "fetched_at": self.now},
            },
        }

    def test_codex_turn_never_renders_credit(self):
        # Even when credit cache is passed, a Codex turn must only show windows and never credit
        line = sl.compose_line(
            fields=["model", "effort", "ctx", "quota"],
            native_render=lambda n: "gpt-6.1-sol" if n == "model" else "",
            effort_report={"effort": "low"},
            context_tokens=10_000,
            context_length=100_000,
            quota_cache=self.quota_cache,
            credit_cache=self.credit_cache,
            quota_provider="openai-codex",
            now=self.now,
        )
        self.assertIn("5h 20%", line)
        self.assertIn("7j 40%", line)
        self.assertNotIn("credit", line)

    def test_deepseek_turn_never_renders_codex_quota(self):
        # Even when quota cache is present, a DeepSeek turn must never show Codex windows
        line = sl.compose_line(
            fields=["model", "effort", "ctx", "quota"],
            native_render=lambda n: "deepseek-flash" if n == "model" else "",
            effort_report={"effort": "low"},
            context_tokens=5_000,
            context_length=100_000,
            quota_cache=self.quota_cache,
            credit_cache=self.credit_cache,
            quota_provider="deepseek",
            now=self.now,
        )
        self.assertIn("credit $15.00", line)
        self.assertNotIn("5h", line)
        self.assertNotIn("7j", line)

    def test_unknown_provider_renders_neither_quota_nor_credit(self):
        line = sl.compose_line(
            fields=["model", "effort", "ctx", "quota"],
            native_render=lambda n: "mystery-model" if n == "model" else "",
            effort_report={"effort": "low"},
            context_tokens=5_000,
            context_length=100_000,
            quota_cache=self.quota_cache,
            credit_cache=self.credit_cache,
            quota_provider=None,
            now=self.now,
        )
        self.assertEqual(line, "`mystery-model · low · ctx 5%`")
        self.assertNotIn("5h", line)
        self.assertNotIn("credit", line)


class TestTurnProviderResolution(unittest.TestCase):
    def setUp(self):
        self.config = {
            "model": {
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "aliases": {"codex": "openai-codex/gpt-6.1-sol"},
            },
            "providers": {
                "deepseek": {"models": ["deepseek-chat", "deepseek-coder"]},
            },
        }
        self.source = SimpleNamespace(platform="telegram", chat_id="12345", thread_id="67890")

    def test_agent_result_direct_provider(self):
        runner = SimpleNamespace()
        result = {"model": "custom-model", "provider": "custom-provider"}
        trace = {}
        prov = sl.resolve_turn_provider(runner, result, self.source, self.config, trace=trace)
        self.assertEqual(prov, "custom-provider")
        self.assertEqual(trace.get("provider_source"), "agent_result.provider")

    def test_live_agent_provider(self):
        agent = SimpleNamespace(provider="live-provider")
        state = SimpleNamespace(turn=SimpleNamespace(agent=agent))
        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: state,
            _session_model_override=lambda k: None,
        )
        result = {"model": "unknown-model"}
        trace = {}
        prov = sl.resolve_turn_provider(runner, result, self.source, self.config, trace=trace)
        self.assertEqual(prov, "live-provider")
        self.assertEqual(trace.get("provider_source"), "live_agent")

    def test_session_override_provider(self):
        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: None,
            _session_model_override=lambda k: {"provider": "override-provider"},
        )
        result = {"model": "unknown-model"}
        trace = {}
        prov = sl.resolve_turn_provider(runner, result, self.source, self.config, trace=trace)
        self.assertEqual(prov, "override-provider")
        self.assertEqual(trace.get("provider_source"), "memory_override")

    def test_config_table_provider_lookup(self):
        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: None,
            _session_model_override=lambda k: None,
        )
        result = {"model": "deepseek-chat"}
        trace = {}
        prov = sl.resolve_turn_provider(runner, result, self.source, self.config, trace=trace)
        self.assertEqual(prov, "deepseek")
        self.assertEqual(trace.get("provider_source"), "model_provider_table")

    def test_unrecognized_model_without_credit_returns_none(self):
        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: None,
            _session_model_override=lambda k: None,
        )
        result = {"model": "completely-unknown"}
        trace = {}
        prov = sl.resolve_turn_provider(runner, result, self.source, self.config, trace=trace)
        self.assertIsNone(prov)
        self.assertEqual(trace.get("provider_source"), "none")


if __name__ == "__main__":
    unittest.main()
