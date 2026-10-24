"""Tests for gateway mixin wrappers, strict compatibility checks, idempotent reload, and safe unload."""

import inspect
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
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
            self.assertTrue(reason.startswith("compatible"))


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


class TestCompatibilityStrategyGuards(unittest.TestCase):
    def test_legacy_missing_session_finite_with_wrong_hash_rejected(self):
        stubs = _make_stub_gateway()
        stubs["gateway.session_finite"] = None
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("wrong hash gateway.run_turn content\n")
            tf_path = tf.name
        try:
            stubs["gateway.run_turn"].__file__ = tf_path
            with patch.dict(sys.modules, stubs):
                compatible, reason = sl.check_gateway_compatibility()
                self.assertFalse(compatible)
                self.assertIn("hash mismatch", reason)
                self.assertNotEqual(sl._active_compatibility_strategy, "legacy_pi_adapter")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_legacy_missing_session_finite_with_correct_pi_hash_accepted(self):
        stubs = _make_stub_gateway()
        stubs["gateway.session_finite"] = None
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("pi_run_turn")
            tf_path = tf.name
        try:
            stubs["gateway.run_turn"].__file__ = tf_path
            with patch.dict(sys.modules, stubs):
                with patch("pathlib.Path.read_bytes", return_value=b"pi_content"):
                    with patch("hashlib.sha256") as mock_sha:
                        mock_sha.return_value.hexdigest.return_value = sl.PI_RUN_TURN_SHA256
                        compatible, reason = sl.check_gateway_compatibility()
                        self.assertTrue(compatible)
                        self.assertIn("legacy_pi_adapter", reason)
                        self.assertEqual(sl._active_compatibility_strategy, "legacy_pi_adapter")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_modern_broken_session_finite_rejected_without_fallback(self):
        stubs = _make_stub_gateway()
        stubs["gateway.session_finite"].finite_turn_required = "not_callable"
        with patch.dict(sys.modules, stubs):
            with patch("hashlib.sha256") as mock_sha:
                mock_sha.return_value.hexdigest.return_value = sl.PI_RUN_TURN_SHA256
                compatible, reason = sl.check_gateway_compatibility()
                self.assertFalse(compatible)
                self.assertIn("finite_turn_required", reason)
                self.assertNotEqual(sl._active_compatibility_strategy, "legacy_pi_adapter")

    def test_modern_broken_session_finite_with_arguments_rejected(self):
        stubs = _make_stub_gateway()
        stubs["gateway.session_finite"].finite_turn_required = lambda required_arg: False
        with patch.dict(sys.modules, stubs):
            compatible, reason = sl.check_gateway_compatibility()
            self.assertFalse(compatible)
            self.assertIn("zero arguments", reason)
            self.assertNotEqual(sl._active_compatibility_strategy, "legacy_pi_adapter")


class TestReceiptAtomicityAndGenerations(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.tmp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.stubs = _make_stub_gateway()
        self.sys_modules_patch = patch.dict(sys.modules, self.stubs)
        self.sys_modules_patch.start()
        self.addCleanup(self.sys_modules_patch.stop)

    def test_receipt_io_failure_rolls_back_atomically(self):
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin
        orig_footer = mixin._hmwa_runtime_footer_line

        with patch("builtins.open", side_effect=OSError("Disk write error")):
            res = sl.register()
            self.assertEqual(res["status"], "incompatible")
            self.assertIn("receipt write failed", res["reason"])
            self.assertIs(mixin._hmwa_runtime_footer_line, orig_footer)
            self.assertIsNone(sl._active_registration_token)

    def test_unload_older_generation_does_not_delete_newer_receipt(self):
        res1 = sl.register()
        self.assertEqual(res1["status"], "ok")
        tok1 = sl._active_registration_token
        receipt_path = Path(res1["receipt"])
        self.assertTrue(receipt_path.exists())

        res2 = sl.register()
        self.assertEqual(res2["status"], "ok")
        tok2 = sl._active_registration_token
        self.assertIsNot(tok1, tok2)
        self.assertTrue(receipt_path.exists())

        status1 = sl.unpatch_gateway(tok1)
        self.assertFalse(status1["footer"])
        self.assertFalse(status1["persist"])
        self.assertTrue(receipt_path.exists())

        status2 = sl.unpatch_gateway(tok2)
        self.assertTrue(status2["footer"])
        self.assertTrue(status2["persist"])
        self.assertFalse(receipt_path.exists())


class TestFiniteTurnSemantics(unittest.TestCase):
    def test_modern_is_finite_turn_tri_state(self):
        stubs = _make_stub_gateway()
        with patch.dict(sys.modules, stubs):
            sl._active_compatibility_strategy = "session_finite_v1"

            stubs["gateway.session_finite"].finite_turn_required = lambda: None
            self.assertFalse(sl.is_finite_turn())

            stubs["gateway.session_finite"].finite_turn_required = lambda: False
            self.assertFalse(sl.is_finite_turn())

            stubs["gateway.session_finite"].finite_turn_required = lambda: True
            self.assertTrue(sl.is_finite_turn())

    def test_modern_is_finite_turn_exception_fails_closed(self):
        stubs = _make_stub_gateway()
        def broken():
            raise RuntimeError("context lost")

        stubs["gateway.session_finite"].finite_turn_required = broken
        with patch.dict(sys.modules, stubs):
            sl._active_compatibility_strategy = "session_finite_v1"
            self.assertTrue(sl.is_finite_turn())

    def test_legacy_pi_adapter_is_finite_turn_always_false(self):
        sl._active_compatibility_strategy = "legacy_pi_adapter"
        self.assertFalse(sl.is_finite_turn())


class TestLegacyPiPlatformGuard(unittest.TestCase):
    def setUp(self):
        def orig_footer(self, agent_result, source, turn_seconds):
            return "native_pi_line"

        self.stubs = _make_stub_gateway(footer_fn=orig_footer)
        self.sys_modules_patch = patch.dict(sys.modules, self.stubs)
        self.sys_modules_patch.start()
        self.addCleanup(self.sys_modules_patch.stop)

    def test_telegram_platform_renders_statusline_on_legacy_pi(self):
        sl.register()
        sl._active_compatibility_strategy = "legacy_pi_adapter"
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin

        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: None,
            _session_model_override=lambda k: None,
            _resolve_session_reasoning_config=lambda **kw: None,
        )
        source = SimpleNamespace(platform="telegram")
        result = {"model": "gpt-6.1-sol"}

        rendered = mixin._hmwa_runtime_footer_line(runner, result, source, 1.0)
        self.assertIn("stub_native_footer", rendered)
        sl.unregister()

    def test_non_telegram_platform_preserves_native_line_on_legacy_pi(self):
        sl.register()
        sl._active_compatibility_strategy = "legacy_pi_adapter"
        mixin = self.stubs["gateway.run_turn"].GatewayTurnMixin

        runner = SimpleNamespace(
            _session_key_for_source=lambda s: "sess:1",
            _peek_session_state=lambda k: None,
            _session_model_override=lambda k: None,
            _resolve_session_reasoning_config=lambda **kw: None,
        )
        source = SimpleNamespace(platform="slack")
        result = {"model": "gpt-6.1-sol"}

        rendered = mixin._hmwa_runtime_footer_line(runner, result, source, 1.0)
        self.assertEqual(rendered, "native_pi_line")
        sl.unregister()


if __name__ == "__main__":
    unittest.main()
