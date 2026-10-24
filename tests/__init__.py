"""hermes-statusline test suite package."""

import importlib.util
import sys
from pathlib import Path

# Load repo/__init__.py into sys.modules as 'hermes_statusline'
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "__init__.py"

spec = importlib.util.spec_from_file_location("hermes_statusline", PLUGIN_FILE)
assert spec and spec.loader
plugin_mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_statusline"] = plugin_mod
spec.loader.exec_module(plugin_mod)
