"""Test helpers: loads repo/__init__.py into hermes_statusline module namespace."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "__init__.py"

spec = importlib.util.spec_from_file_location("hermes_statusline", PLUGIN_FILE)
assert spec and spec.loader
sl = importlib.util.module_from_spec(spec)
sys.modules["hermes_statusline"] = sl
spec.loader.exec_module(sl)
