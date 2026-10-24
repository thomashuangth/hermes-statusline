"""Real-core integration tests for hermes-statusline.

Must be executed with HERMES_TEST_CORE pointing to an extracted Hermes core archive.
Runs strictly in an isolated subprocess per core (no cross-core pollution).
Tests real imported GatewayTurnMixin and real persistence methods from the core.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


class TestRealGatewayCoreIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core_env = os.environ.get("HERMES_TEST_CORE")
        if not core_env:
            raise unittest.SkipTest("HERMES_TEST_CORE environment variable not set")

        cls.core_dir = Path(core_env).resolve()
        if not cls.core_dir.is_dir():
            raise FileNotFoundError(f"HERMES_TEST_CORE directory not found: {cls.core_dir}")

        # Temporary HOME & HERMES_HOME isolation
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.isolated_home = Path(cls.tmp_dir.name) / "home"
        cls.isolated_hermes = Path(cls.tmp_dir.name) / "hermes_home"
        cls.isolated_home.mkdir(parents=True, exist_ok=True)
        cls.isolated_hermes.mkdir(parents=True, exist_ok=True)

        os.environ["HOME"] = str(cls.isolated_home)
        os.environ["HERMES_HOME"] = str(cls.isolated_hermes)
        os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

        # Prepend core and repo to sys.path
        repo_dir = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo_dir))
        sys.path.insert(0, str(cls.core_dir))

        # Pre-populate agent.turn_scripted_prelude for Pi core to avoid Python 3.11+ Generator syntax error
        if (cls.core_dir / "agent" / "turn_scripted_prelude.py").exists():
            if "agent.turn_scripted_prelude" not in sys.modules:
                prelude_mod = types.ModuleType("agent.turn_scripted_prelude")
                prelude_mod.Prelude = None
                prelude_mod.play_prelude = lambda *a, **k: None
                prelude_mod.run_scripted_prelude = lambda *a, **k: None
                sys.modules["agent.turn_scripted_prelude"] = prelude_mod

        import gateway.run
        run_mod = gateway.run
        if not Path(run_mod.__file__).resolve().is_relative_to(cls.core_dir):
            raise AssertionError(f"gateway.run came from another checkout: {run_mod.__file__}")

        # Only the config input is replaced; all rendering and persistence APIs are real.
        from unittest.mock import patch
        cls.config_patch = patch.object(run_mod, "_load_gateway_config", autospec=True, return_value={
            "display": {
                "platforms": {
                    "telegram": {
                        "runtime_footer": {
                            "enabled": True,
                            "fields": ["model", "effort", "ctx", "quota"],
                        }
                    }
                }
            }
        })
        cls.config_patch.start()
        cls.addClassCleanup(cls.config_patch.stop)

        import __init__ as statusline_mod
        from gateway import run_turn
        cls.sl = statusline_mod
        cls.run_turn = run_turn

    @classmethod
    def tearDownClass(cls):
        cls.tmp_dir.cleanup()

    def test_01_core_archive_source_identity(self):
        """Assert that imported gateway.run_turn lives inside HERMES_TEST_CORE and matches pinned hash."""
        run_turn_path = Path(self.run_turn.__file__).resolve()
        self.assertTrue(
            run_turn_path.is_relative_to(self.core_dir),
            f"run_turn source ({run_turn_path}) is outside HERMES_TEST_CORE ({self.core_dir})",
        )

        h = hashlib.sha256(run_turn_path.read_bytes()).hexdigest()
        is_pi = (self.core_dir / "gateway" / "session_finite.py").exists() is False
        if is_pi:
            self.assertEqual(h, self.sl.PI_RUN_TURN_SHA256, "Pi core run_turn.py sha256 mismatch")
        else:
            self.assertEqual(h, self.sl.ORIG_RUN_TURN_SHA256, "Modern core run_turn.py sha256 mismatch")

    def test_02_compatibility_check_and_strategy(self):
        """Check seam verification and correct strategy selection for this core."""
        compatible, reason = self.sl.check_gateway_compatibility()
        self.assertTrue(compatible, f"Seam compatibility failed: {reason}")

        has_finite = (self.core_dir / "gateway" / "session_finite.py").exists()
        expected_strat = "session_finite_v1" if has_finite else "legacy_pi_adapter"
        self.assertEqual(self.sl._active_compatibility_strategy, expected_strat)

    def test_03_registration_and_receipt_receipt_contents(self):
        """Register plugin, verify return dict, loaded-<pid>.json receipt file and contents."""
        res = self.sl.register()
        self.assertEqual(res["status"], "ok")
        self.assertIn(res["footer"], ("patched", "repatched"))
        self.assertIn(res["persist"], ("patched", "repatched"))
        self.assertEqual(res["pid"], os.getpid())
        self.assertEqual(res["version"], "1.0.1")

        has_finite = (self.core_dir / "gateway" / "session_finite.py").exists()
        expected_strat = "session_finite_v1" if has_finite else "legacy_pi_adapter"
        expected_commit = self.sl.ORIG_CORE_COMMIT if has_finite else self.sl.PI_CORE_COMMIT
        expected_hash = self.sl.ORIG_RUN_TURN_SHA256 if has_finite else self.sl.PI_RUN_TURN_SHA256

        self.assertEqual(res["strategy"], expected_strat)
        self.assertEqual(res["core_identity"]["core_commit"], expected_commit)
        self.assertEqual(res["core_identity"]["run_turn_sha256"], expected_hash)

        # Receipt file on disk
        receipt_path = Path(res["receipt"])
        self.assertTrue(receipt_path.is_file(), f"Receipt file not found at {receipt_path}")
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["version"], "1.0.1")
        self.assertEqual(data["compatibility_strategy"], expected_strat)
        self.assertEqual(data["core_identity"]["core_commit"], expected_commit)
        self.assertEqual(data["core_identity"]["run_turn_sha256"], expected_hash)
        self.assertEqual(data["plugin_source_sha256"], hashlib.sha256(Path(self.sl.__file__).read_bytes()).hexdigest())

    def test_04_real_gateway_turn_mixin_footer_execution(self):
        """Exercise patched GatewayTurnMixin._hmwa_runtime_footer_line on a real core instance."""
        from gateway.platforms.base import Platform

        class FakeStore:
            def __init__(self):
                self.appended = []
            async def append_to_transcript(self, sid, entry, **kw):
                self.appended.append(entry)
            async def update_session(self, key, **kw):
                pass

        class RealGatewayHarness(self.run_turn.GatewayTurnMixin):
            def __init__(self):
                self.async_session_store = FakeStore()
                self._session_db = None
            def _session_key_for_source(self, s):
                return "sess:real_core_test"
            def _peek_session_state(self, k):
                return None
            def _session_model_override(self, k):
                return None
            def _resolve_session_reasoning_config(self, **kw):
                return {"enabled": True, "effort": "low"}
            def _hmwa_user_transcript_entry(self, e, p, ts):
                return {"role": "user", "content": "hello"}
            async def _refresh_agent_cache_message_count(self, k, s):
                pass

        harness = RealGatewayHarness()
        source_tg = types.SimpleNamespace(platform=Platform.TELEGRAM)
        agent_result = {
            "model": "gpt-6.1-sol",
            "last_prompt_tokens": 12000,
            "context_length": 128000,
        }

        # 1. Telegram platform renders statusline with (configured) effort
        rendered = harness._hmwa_runtime_footer_line(agent_result, source_tg, 1.5)
        self.assertIn("gpt-6.1-sol", rendered)
        self.assertIn("low (configured)", rendered)
        self.assertIn("ctx 9%", rendered)

        has_finite = (self.core_dir / "gateway" / "session_finite.py").exists()
        if not has_finite:
            # On legacy Pi core: non-telegram platform preserves native behavior
            source_slack = types.SimpleNamespace(platform="slack")
            native_rendered = harness._hmwa_runtime_footer_line(agent_result, source_slack, 1.5)
            # Native line does not have custom statusline markers like (configured)
            self.assertNotIn("(configured)", native_rendered)
        else:
            # On modern core: test finite turn scope suppression
            from gateway.session_finite import finite_turn_scope
            with finite_turn_scope(True):
                finite_rendered = harness._hmwa_runtime_footer_line(agent_result, source_tg, 1.5)
                self.assertEqual(finite_rendered, "", "Finite turn must suppress statusline footer")

            with finite_turn_scope(False):
                normal_rendered = harness._hmwa_runtime_footer_line(agent_result, source_tg, 1.5)
                self.assertIn("gpt-6.1-sol", normal_rendered)

    def test_05_concurrent_task_local_finite_scope_isolation(self):
        """Verify concurrent task-local finite scope has zero cross-talk (modern core)."""
        has_finite = (self.core_dir / "gateway" / "session_finite.py").exists()
        if not has_finite:
            return  # Pi core has no finite gateway turns

        from gateway.platforms.base import Platform
        from gateway.session_finite import finite_turn_scope

        class ConcurrentHarness(self.run_turn.GatewayTurnMixin):
            def _session_key_for_source(self, s): return "sess:concurrent"
            def _peek_session_state(self, k): return None
            def _session_model_override(self, k): return None
            def _resolve_session_reasoning_config(self, **kw): return None

        harness = ConcurrentHarness()
        source = types.SimpleNamespace(platform=Platform.TELEGRAM)
        agent_result = {"model": "gpt-6.1-sol"}

        async def run_finite():
            with finite_turn_scope(True):
                await asyncio.sleep(0.01)
                return harness._hmwa_runtime_footer_line(agent_result, source, 1.0)

        async def run_interactive():
            with finite_turn_scope(False):
                await asyncio.sleep(0.01)
                return harness._hmwa_runtime_footer_line(agent_result, source, 1.0)

        async def run_concurrent():
            res_finite, res_interactive = await asyncio.gather(run_finite(), run_interactive())
            return res_finite, res_interactive

        res_fin, res_inter = asyncio.run(run_concurrent())
        self.assertEqual(res_fin, "")
        self.assertIn("gpt-6.1-sol", res_inter)

    def test_06_real_gateway_persistence_stripping(self):
        """Verify real GatewayTurnMixin._hmwa_persist_turn_transcript strips footer suffix."""
        from gateway.platforms.base import Platform

        class FakeStore:
            def __init__(self):
                self.appended = []
            async def append_to_transcript(self, sid, entry, **kw):
                self.appended.append(entry)
            async def update_session(self, key, **kw):
                pass

        class RealGatewayHarness(self.run_turn.GatewayTurnMixin):
            def __init__(self):
                self.async_session_store = FakeStore()
                self._session_db = None
            def _session_key_for_source(self, s):
                return "sess:persist_test"
            def _peek_session_state(self, k):
                return None
            def _session_model_override(self, k):
                return None
            def _resolve_session_reasoning_config(self, **kw):
                return {"enabled": True, "effort": "low"}
            def _hmwa_user_transcript_entry(self, e, p, ts):
                return {"role": "user", "content": "user query"}
            async def _refresh_agent_cache_message_count(self, k, s):
                pass

        async def run_persist():
            harness = RealGatewayHarness()
            source = types.SimpleNamespace(platform=Platform.TELEGRAM)
            agent_result = {"model": "gpt-6.1-sol", "last_prompt_tokens": 1000}

            # 1. Render footer
            footer = harness._hmwa_runtime_footer_line(agent_result, source, 1.0)
            self.assertTrue(bool(footer))

            # 2. Persist turn with full body containing footer
            full_response = f"Clean model reply\n\n{footer}"
            event = types.SimpleNamespace(message_id=None, internal=False)
            session_entry = types.SimpleNamespace(session_id="sid:persist_test", session_key="sess:persist_test")
            prepared = types.SimpleNamespace(history=[{"role": "user"}])

            await harness._hmwa_persist_turn_transcript(
                event=event,
                source=source,
                session_entry=session_entry,
                session_key="sess:persist_test",
                agent_result=agent_result,
                agent_messages=[],
                prepared=prepared,
                response=full_response,
                agent_failed_early=False,
                hidden_reasoning_incomplete=False,
                is_context_overflow_failure=False,
            )

            # 3. Assert assistant row persisted without statusline footer
            assistant_rows = [r for r in harness.async_session_store.appended if r.get("role") == "assistant"]
            self.assertEqual(len(assistant_rows), 1)
            self.assertEqual(assistant_rows[0]["content"], "Clean model reply")

        asyncio.run(run_persist())

    def test_07_unpatch_and_receipt_cleanup(self):
        """Unregister restores original methods and cleans up receipt."""
        unreg = self.sl.unregister()
        self.assertEqual(unreg["status"], "unregistered")
        self.assertTrue(unreg["restored"]["footer"])
        self.assertTrue(unreg["restored"]["persist"])

        pid = os.getpid()
        receipt_file = self.isolated_hermes / "state" / "hermes_statusline" / f"loaded-{pid}.json"
        self.assertFalse(receipt_file.exists(), "Receipt file must be unlinked on unregister")


if __name__ == "__main__":
    unittest.main()
