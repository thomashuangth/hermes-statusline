"""Regression tests for independent review blocker findings (RED -> GREEN)."""

import asyncio
import functools
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from _helpers import sl


class TestBlockerRegressions(unittest.IsolatedAsyncioTestCase):
    # Finding 1: openai alias must NOT map to openai-codex
    def test_openai_alias_removed(self):
        self.assertNotIn("openai", sl.PROVIDER_ALIASES)
        self.assertFalse(sl.same_provider("openai", "openai-codex"))

    # Finding 1: _session_row_provider must read persisted route without trusting billing_provider
    def test_session_row_provider_persisted_route(self):
        class FakeDB:
            def get_session(self, sid):
                return {
                    "billing_provider": "openai-codex",  # Stale, must be ignored!
                    "model_config": json.dumps({
                        "gateway_runtime": {"provider": "deepseek"}
                    }),
                }

        class FakeStore:
            def __init__(self):
                self._db = FakeDB()
            def lookup_by_session_key(self, key):
                return SimpleNamespace(session_id="sess_123")
            def get_model_override(self, key):
                return None

        runner = SimpleNamespace(session_store=FakeStore())
        prov = sl._session_row_provider(runner, "telegram:123:456")
        self.assertEqual(prov, "deepseek")

    # Finding 2 & Review 2 Item 1: Finite turn must accept 0 args, return True/False, fail closed on missing/error
    def test_finite_turn_guard_returns_empty_and_records_context(self):
        fake_gw_finite = types.ModuleType("gateway.session_finite")
        fake_gw_finite.finite_turn_required = lambda: True

        with patch.dict(sys.modules, {"gateway.session_finite": fake_gw_finite}):
            self.assertTrue(sl.is_finite_turn())

        fake_gw_finite.finite_turn_required = lambda: False
        with patch.dict(sys.modules, {"gateway.session_finite": fake_gw_finite}):
            self.assertFalse(sl.is_finite_turn())

        # When module or attribute throws/missing, must fail closed (True)
        with patch.dict(sys.modules, {"gateway.session_finite": None}):
            self.assertTrue(sl.is_finite_turn())

    # Finding 3: Strict signature check requires persist to be a coroutine function
    def test_strict_signature_rejects_non_coroutine_persist(self):
        def sync_persist(self, **kwargs):
            return "sync"

        fake_module = types.ModuleType("gateway.run_turn")
        fake_module.GatewayTurnMixin = type(
            "GatewayTurnMixin",
            (),
            {
                "_hmwa_runtime_footer_line": lambda self, agent_result, source, turn_seconds: "",
                "_hmwa_persist_turn_transcript": sync_persist,  # NOT a coroutine!
            },
        )
        fake_pkg = types.ModuleType("gateway")
        fake_pkg.run_turn = fake_module
        fake_rf = types.ModuleType("gateway.runtime_footer")
        fake_rf.format_runtime_footer = lambda **kw: ""
        fake_rf.resolve_footer_config = lambda *a: {}
        fake_pkg.runtime_footer = fake_rf
        fake_sf = types.ModuleType("gateway.session_finite")
        fake_sf.finite_turn_required = lambda: False
        fake_pkg.session_finite = fake_sf

        with patch.dict(sys.modules, {
            "gateway": fake_pkg,
            "gateway.run_turn": fake_module,
            "gateway.runtime_footer": fake_rf,
            "gateway.session_finite": fake_sf,
        }):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("coroutine", reason.lower())

    # Finding 4: Old generation unload must be no-op after new registration; foreign wrapper safe
    def test_unload_identity_and_foreign_wrapper_guard(self):
        fake_gw = types.ModuleType("gateway")
        fake_rt = types.ModuleType("gateway.run_turn")
        fake_rf = types.ModuleType("gateway.runtime_footer")
        fake_sf = types.ModuleType("gateway.session_finite")
        fake_sf.finite_turn_required = lambda: False
        fake_gw.run_turn = fake_rt
        fake_gw.runtime_footer = fake_rf
        fake_gw.session_finite = fake_sf
        fake_rf.format_runtime_footer = lambda **kw: ""
        fake_rf.resolve_footer_config = lambda *a: {}

        def orig_footer(self, agent_result, source, turn_seconds):
            return "orig"

        async def orig_persist(self, *, event=None, source=None, session_entry=None, session_key=None,
                               agent_result=None, agent_messages=None, prepared=None, response=None,
                               agent_failed_early=None, hidden_reasoning_incomplete=None,
                               is_context_overflow_failure=None):
            return "persisted"

        mixin = type("GatewayTurnMixin", (), {
            "_hmwa_runtime_footer_line": orig_footer,
            "_hmwa_persist_turn_transcript": orig_persist,
        })
        fake_rt.GatewayTurnMixin = mixin

        with patch.dict(sys.modules, {
            "gateway": fake_gw,
            "gateway.run_turn": fake_rt,
            "gateway.runtime_footer": fake_rf,
            "gateway.session_finite": fake_sf,
        }):
            # Generation 1
            unreg_callbacks = []
            ctx1 = SimpleNamespace(on_unload=lambda cb: unreg_callbacks.append(cb))
            res1 = sl.register(ctx1)
            self.assertEqual(res1["status"], "ok")
            gen1_footer = mixin._hmwa_runtime_footer_line

            # Generation 2
            ctx2 = SimpleNamespace(on_unload=lambda cb: unreg_callbacks.append(cb))
            res2 = sl.register(ctx2)
            self.assertEqual(res2["status"], "ok")
            gen2_footer = mixin._hmwa_runtime_footer_line

            # Running generation 1 unload callback must NOT unpatch generation 2!
            unreg_callbacks[0]()
            self.assertIs(mixin._hmwa_runtime_footer_line, gen2_footer)

            # Foreign wrapper using functools.wraps
            @functools.wraps(mixin._hmwa_runtime_footer_line)
            def foreign_wrapper(*args, **kwargs):
                return "foreign"
            mixin._hmwa_runtime_footer_line = foreign_wrapper

            # Running generation 2 unload must NOT remove the foreign wrapper
            unreg_callbacks[1]()
            self.assertIs(mixin._hmwa_runtime_footer_line, foreign_wrapper)

    # Review 2 Item 2: Atomic rollback failure injection
    def test_atomic_rollback_on_persist_patch_failure(self):
        fake_gw = types.ModuleType("gateway")
        fake_rt = types.ModuleType("gateway.run_turn")
        fake_rf = types.ModuleType("gateway.runtime_footer")
        fake_sf = types.ModuleType("gateway.session_finite")
        fake_sf.finite_turn_required = lambda: False
        fake_gw.run_turn = fake_rt
        fake_gw.runtime_footer = fake_rf
        fake_gw.session_finite = fake_sf
        fake_rf.format_runtime_footer = lambda **kw: ""
        fake_rf.resolve_footer_config = lambda *a: {}

        def orig_footer(self, agent_result, source, turn_seconds):
            return "orig_footer_val"

        async def orig_persist(self, *, response=None, agent_result=None, **kwargs):
            return "persisted"

        mixin = type("GatewayTurnMixin", (), {
            "_hmwa_runtime_footer_line": orig_footer,
            "_hmwa_persist_turn_transcript": orig_persist,
        })
        fake_rt.GatewayTurnMixin = mixin

        with patch.dict(sys.modules, {
            "gateway": fake_gw,
            "gateway.run_turn": fake_rt,
            "gateway.runtime_footer": fake_rf,
            "gateway.session_finite": fake_sf,
        }):
            # First register cleanly to establish Generation 1
            res1 = sl.register()
            self.assertEqual(res1["status"], "ok")
            gen1_footer = mixin._hmwa_runtime_footer_line
            gen1_persist = mixin._hmwa_persist_turn_transcript
            gen1_token = sl._active_registration_token

            # Now simulate a failure during the second registration's persist patch
            with patch.object(sl, "patch_footer_persistence", return_value="injected_failure"):
                res2 = sl.register()
                self.assertEqual(res2["status"], "incompatible")
                self.assertIn("persist patch failed", res2["reason"])

                # Exact pre-registration state must be rolled back to Generation 1!
                self.assertIs(mixin._hmwa_runtime_footer_line, gen1_footer)
                self.assertIs(mixin._hmwa_persist_turn_transcript, gen1_persist)
                self.assertIs(sl._active_registration_token, gen1_token)

            # Cleanup
            sl.unregister()

    # Review 2 Item 3: Fallback recovery ETA uses primary runtime / config, never served provider
    def test_fallback_primary_resolution_never_uses_served_provider(self):
        agent = SimpleNamespace(
            _primary_runtime={"provider": "openai-codex", "model": "gpt-5.2"},
            _fallback_activated=True,
        )
        # Served provider is deepseek, primary is openai-codex
        cache = {
            "provider": "openai-codex",
            "source": "usage_api",
            "fetched_at": 1000.0,
            "windows": {
                "session": {"used_percent": 100.0, "resets_at": "1970-01-01T00:20:00+00:00"},
                "weekly": {"used_percent": 20.0, "resets_at": "1970-01-01T00:20:00+00:00"},
            },
        }
        # Calling estimate_recovery with primary="openai-codex" (from agent._primary_runtime)
        eta = sl.estimate_recovery(cache, primary="openai-codex", now=1000.0, agent=agent)
        self.assertIn("recovery", eta)
        self.assertNotIn("unknown", eta)

        # If served provider was erroneously passed ("deepseek"), it would fail closed as unknown
        eta_wrong = sl.estimate_recovery(cache, primary="deepseek", now=1000.0, agent=agent)
        self.assertEqual(eta_wrong, "(recovery unknown)")

    # Review 2 Item 7: Concurrent tasks with ContextVar footer isolation
    async def test_concurrent_tasks_contextvar_isolation(self):
        async def mock_persist(runner, **kwargs):
            return kwargs.get("response")

        async def worker(task_id: int):
            self_obj = SimpleNamespace(id=f"runner_{task_id}")
            result_obj = {"id": f"res_{task_id}"}
            footer = f"`model_{task_id} · low`"

            sl._set_turn_footer_context(id(self_obj), id(result_obj), footer)

            # Simulate async context switch
            await asyncio.sleep(0.001)

            # Persist without footer
            response_text = f"Agent answer {task_id}\n\n{footer}"
            kwargs = {"response": response_text, "agent_result": result_obj}
            cleaned = await sl.persist_without_footer(mock_persist, self_obj, kwargs, footer)
            self.assertEqual(cleaned, f"Agent answer {task_id}")

        await asyncio.gather(*(worker(i) for i in range(10)))

    # Finding 5: persist_without_footer must NOT remove bare occurrence without \n\n
    async def test_persist_without_footer_does_not_strip_bare_occurrence(self):
        seen = []
        async def fake_persist(runner, **kwargs):
            seen.append(kwargs)
            return "ok"

        footer = "`model · low`"
        # Body contains the bare footer inline, but not as a "\n\n" suffix
        body = "Here is the code `model · low` in a sentence."
        kwargs = {"response": body}

        await sl.persist_without_footer(fake_persist, None, kwargs, footer)
        self.assertEqual(seen[0]["response"], body)

    # Finding 7: Deliberate model override must NOT be marked as fallback
    def test_deliberate_model_override_not_marked_fallback(self):
        # Global default is codex, but turn requested deepseek and served deepseek
        line = sl.decorate_fallback_statusline(
            "`deepseek · low · ctx 5%`",
            fallback_active=False,
            fallback_marker="",
            recovery="",
        )
        self.assertNotIn("fallback", line)

    # Finding 8: Non-currency credit balances must display "credits"
    def test_credits_label_for_non_currency_balance(self):
        cache = {
            "fetched_at": 1000.0,
            "credits": {
                "kie": {"balance": 179.51, "unit_type": "credits", "fetched_at": 1000.0}
            },
        }
        text = sl.format_credit(cache, now=1000.0, provider="kie")
        self.assertEqual(text, "credits 179.5")
        self.assertNotEqual(text, "credit 179.5")


if __name__ == "__main__":
    unittest.main()
