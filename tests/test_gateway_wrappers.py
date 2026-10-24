"""Tests for gateway mixin wrappers, strict compatibility checks, idempotent reload, and safe unload."""

import inspect
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from _helpers import sl


async def _default_persist(self, *, response=None, agent_result=None, **kwargs):
    return "persisted"


def _default_footer(self, agent_result, source, turn_seconds):
    return "native"


def _make_stub_gateway(footer_fn=_default_footer, persist_fn=_default_persist, omit_footer=False, omit_persist=False):
    gateway_pkg = types.ModuleType("gateway")
    run_turn_mod = types.ModuleType("gateway.run_turn")
    runtime_footer_mod = types.ModuleType("gateway.runtime_footer")
    run_mod = types.ModuleType("gateway.run")
    session_finite_mod = types.ModuleType("gateway.session_finite")

    gateway_pkg.run_turn = run_turn_mod
    gateway_pkg.runtime_footer = runtime_footer_mod
    gateway_pkg.run = run_mod
    gateway_pkg.session_finite = session_finite_mod

    session_finite_mod.finite_turn_required = lambda: False

    runtime_footer_mod.format_runtime_footer = lambda **kw: "stub_native_footer"
    runtime_footer_mod.resolve_footer_config = lambda cfg, key: {
        "enabled": True,
        "fields": ["model", "effort", "ctx", "quota"],
    }

    run_mod._load_gateway_config = lambda: {
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
    }
    run_mod._platform_config_key = lambda p: "telegram"

    attrs = {}
    if not omit_footer and footer_fn is not None:
        attrs["_hmwa_runtime_footer_line"] = footer_fn
    if not omit_persist and persist_fn is not None:
        attrs["_hmwa_persist_turn_transcript"] = persist_fn
    run_turn_mod.GatewayTurnMixin = type("GatewayTurnMixin", (), attrs)

    return {
        "gateway": gateway_pkg,
        "gateway.run_turn": run_turn_mod,
        "gateway.runtime_footer": runtime_footer_mod,
        "gateway.run": run_mod,
        "gateway.session_finite": session_finite_mod,
    }


class TestGatewayCompatibility(unittest.TestCase):
    def test_missing_gateway_module_fails_closed(self):
        with patch.dict(sys.modules, {"gateway": None, "gateway.run_turn": None}):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("cannot import gateway.run_turn", reason)

    def test_missing_mixin_fails_closed(self):
        stubs = _make_stub_gateway()
        # Remove GatewayTurnMixin
        delattr(stubs["gateway.run_turn"], "GatewayTurnMixin")
        with patch.dict(sys.modules, stubs):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("GatewayTurnMixin class missing", reason)

    def test_missing_footer_method_fails_closed(self):
        stubs = _make_stub_gateway(omit_footer=True)
        with patch.dict(sys.modules, stubs):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("_hmwa_runtime_footer_line missing", reason)

    def test_incompatible_footer_signature_fails_closed(self):
        def bad_sig(self, result):  # Only 2 parameters
            return ""

        stubs = _make_stub_gateway(footer_fn=bad_sig)
        with patch.dict(sys.modules, stubs):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("signature mismatch", reason)

    def test_valid_signature_passes(self):
        stubs = _make_stub_gateway()
        with patch.dict(sys.modules, stubs):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertTrue(compatible)
            self.assertEqual(reason, "compatible")


class TestGatewayPatchingAndUnload(unittest.TestCase):
    def setUp(self):
        def orig_footer(self, agent_result, source, turn_seconds):
            return "original_footer"

        async def orig_persist(self, *, response=None, agent_result=None, **kwargs):
            return "original_persisted"

        self.orig_footer = orig_footer
        self.orig_persist = orig_persist

        self.stubs = _make_stub_gateway(
            footer_fn=orig_footer, persist_fn=orig_persist
        )
        self.sys_modules_patch = patch.dict(sys.modules, self.stubs)
        self.sys_modules_patch.start()

    def tearDown(self):
        self.sys_modules_patch.stop()

    def test_patch_and_idempotent_reload(self):
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin

        # Initial registration
        res1 = sl.register()
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res1["footer"], "patched")
        self.assertEqual(res1["persist"], "patched")

        patched_footer = mixin._hmwa_runtime_footer_line
        self.assertTrue(getattr(patched_footer, sl._WRAPPER_PATCH_MARKER, False))
        self.assertIs(getattr(patched_footer, "__wrapped__"), self.orig_footer)

        # Second registration (hot reload)
        res2 = sl.register()
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["footer"], "repatched")
        self.assertEqual(res2["persist"], "repatched")

        # __wrapped__ must still point to original, not double-wrapped!
        repatched_footer = mixin._hmwa_runtime_footer_line
        self.assertIs(getattr(repatched_footer, "__wrapped__"), self.orig_footer)

        sl.unregister()

    def test_safe_unload_restores_originals(self):
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin
        sl.register()

        # Unregister / unload
        unreg = sl.unregister()
        self.assertEqual(unreg["status"], "unregistered")
        self.assertTrue(unreg["restored"]["footer"])
        self.assertTrue(unreg["restored"]["persist"])

        # Mixin methods must be restored to original functions
        self.assertIs(mixin._hmwa_runtime_footer_line, self.orig_footer)
        self.assertIs(mixin._hmwa_persist_turn_transcript, self.orig_persist)

    def test_unload_ownership_safe_when_not_patched(self):
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin
        # Calling unregister when methods are not patched by us does not alter them
        res = sl.unpatch_gateway()
        self.assertFalse(res["footer"])
        self.assertFalse(res["persist"])
        self.assertIs(mixin._hmwa_runtime_footer_line, self.orig_footer)


class TestOptionsDisabled(unittest.TestCase):
    def test_disabled_option_leaves_native_footer_untouched(self):
        def orig_footer(self, agent_result, source, turn_seconds):
            return "native_footer_text"

        stubs = _make_stub_gateway(footer_fn=orig_footer)
        mixin = stubs["gateway.run_turn"].GatewayTurnMixin

        # Set config to enabled=False
        stubs["gateway.run"]._load_gateway_config = lambda: {
            "display": {
                "platforms": {
                    "telegram": {
                        "runtime_footer": {
                            "enabled": False,
                            "fields": ["model", "effort", "ctx"],
                        }
                    }
                }
            }
        }
        stubs["gateway.runtime_footer"].resolve_footer_config = lambda cfg, key: {
            "enabled": False,
            "fields": ["model", "effort", "ctx"],
        }

        with patch.dict(sys.modules, stubs):
            sl.register()

            runner = SimpleNamespace(
                _session_key_for_source=lambda s: "sess:1",
                _peek_session_state=lambda k: None,
                _session_model_override=lambda k: None,
                _resolve_session_reasoning_config=lambda **kw: None,
            )
            source = SimpleNamespace(platform="telegram")
            result = {"model": "gpt-6.1-sol"}

            rendered = mixin._hmwa_runtime_footer_line(runner, result, source, 1.0)
            self.assertEqual(rendered, "native_footer_text")

            sl.unregister()


if __name__ == "__main__":
    unittest.main()
