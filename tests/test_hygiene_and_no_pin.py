"""Tests verifying removal of legacy pinning/singleton APIs, personal paths/IDs, and chat receipts."""

import os
import tempfile
import unittest
from pathlib import Path

from _helpers import sl


class TestHygieneAndNoPinAPIs(unittest.TestCase):
    def test_no_pinning_or_singleton_apis_on_module(self):
        forbidden_attrs = [
            "_deliver_pinned",
            "_update_pinned",
            "_update_pinned_locked",
            "_recover_unpinned",
            "_patch_pin_delivery",
            "_pin_scope",
            "_pin_display_line",
            "_map_record",
            "_pin_map",
            "_pin_receipt",
            "PIN_CHAT",
            "PIN_MAP",
            "PIN_SETTLE_DELAYS",
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(sl, attr),
                f"Legacy/pinning attribute {attr!r} must be removed from hermes-statusline",
            )

    def test_no_personal_paths_or_legacy_state_in_source(self):
        plugin_src = (Path(__file__).parents[1] / "__init__.py").read_text(encoding="utf-8")
        forbidden_strings = [
            "/home/",
            "/Users/",
            "runtime_status_pins.json",
            "runtime_status_pinned",
        ]
        for s in forbidden_strings:
            self.assertNotIn(
                s,
                plugin_src,
                f"Personal ID/path/token {s!r} found in plugin source code!",
            )

    def test_dynamic_hermes_home_resolution(self):
        original_env = os.environ.get("HERMES_HOME")
        try:
            with tempfile.TemporaryDirectory() as tmp1:
                os.environ["HERMES_HOME"] = tmp1
                self.assertEqual(sl.get_hermes_home(), Path(tmp1).resolve())

            with tempfile.TemporaryDirectory() as tmp2:
                os.environ["HERMES_HOME"] = tmp2
                self.assertEqual(sl.get_hermes_home(), Path(tmp2).resolve())
        finally:
            if original_env is not None:
                os.environ["HERMES_HOME"] = original_env
            else:
                os.environ.pop("HERMES_HOME", None)

    def test_no_chat_identifying_receipts_written(self):
        # Verify that loading caches or formatting statusline does not write chat/topic state receipts
        with tempfile.TemporaryDirectory() as tmp_home:
            original_env = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = tmp_home
            try:
                # Exercise module formatting
                sl.format_quota(None)
                sl.format_credit(None)
                sl.get_configured_timezone()

                # Ensure no state/runtime_status_footer/topics/ or similar chat directory exists
                topics_dir = Path(tmp_home) / "state" / "runtime_status_footer" / "topics"
                self.assertFalse(topics_dir.exists())
            finally:
                if original_env is not None:
                    os.environ["HERMES_HOME"] = original_env
                else:
                    os.environ.pop("HERMES_HOME", None)


if __name__ == "__main__":
    unittest.main()
