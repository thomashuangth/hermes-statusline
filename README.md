# hermes-statusline (v1.0.1)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-thomashuangth%2Fhermes--statusline-blue)](https://github.com/thomashuangth/hermes-statusline)

A lightweight, portable statusline plugin for [Hermes Agent (NousResearch/hermes-agent)](https://github.com/NousResearch/hermes-agent). Adds a clean, compact monospace status footer to final responses across messaging platforms (such as Telegram) without core edits, extra LLM tokens, or transcript history pollution.

### Supported Hermes Core Targets
- **Modern Core**: commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (Strategy: `session_finite_v1`)
- **Raspberry Pi Core**: commit `4f9b741a3a0284ecce31dc7e4cffde49173ec764` (Strategy: `legacy_pi_adapter`)

---

## Active Statusline Behavior

The plugin outputs a single compact, monospace line under the final response of each turn:

### Standard Turn (Codex / Subscription)
```
`gpt-6.1-sol · low (configured) · ctx 24% · 5h 18% (18:40) · 7j 39% (31/10 09:00)`
```
- **model**: Model served for this turn.
- **effort**: Effective reasoning effort labeled `(configured)` (e.g. `low (configured)`, `medium (configured)`, `none (configured)`). Never claims provider confirmation. Absent or empty effort is omitted (avoids invented default).
- **ctx**: Context window token utilization (`ctx <percent>%`). When token counts are missing from provider results, the field is omitted rather than inventing values.
- **5h quota**: Consumed percentage of the 5-hour rolling session window with reset time in the configured timezone.
- **7j quota**: Consumed percentage of the weekly window with date/time reset in the configured timezone.
- **stale indication**: Marked with `~` if the cache is older than 30 minutes; dropped completely after 6 hours.

### Pay-As-You-Go Provider Turn (DeepSeek / Kie)
```
`deepseek-flash · low (configured) · ctx 6% · credit $16.06`
`deepseek-v4-1-flash · low (configured) · ctx 6% · credits 179.5`
```
On metered API providers, pay-as-you-go balances are displayed instead of subscription windows:
- Currency balances display with currency symbol: `credit $16.06`
- Unit-based credit balances display explicitly as credits: `credits 179.5`
- Provider isolation is strictly enforced: Codex quotas are never rendered on DeepSeek turns, and credit balances are never rendered on Codex turns.
- OpenAI API keys are strictly separated: `openai` does NOT map to `openai-codex`.

### Fallback Turn
```
`deepseek-flash · low (configured) · ctx 6% · credit $16.06 · fallback deepseek · (recovery unknown)`
`gpt-4o-mini · low (configured) · ctx 12% · fallback · (recovery 14:00)`
```
When fallback routing activates (`_fallback_activated` flag or requested/served model mismatch):
- The served model is shown as the primary model field.
- A fallback indicator (`fallback <provider>`) identifies the routing deviation.
- A deliberate user `/model` override is NOT marked as a fallback.
- A conservative recovery ETA is calculated based strictly on provider resets from exhausted quota windows or credential pools. If evidence is incomplete or windows are not strictly exhausted, it reports `(recovery unknown)`—never an invented time.

---

## Dual-Core Compatibility & Fail-Closed Seams

`hermes-statusline` incorporates safe, explicit interface strategies justified by actual source analysis of both official commits:

### 1. Modern Core (`session_finite_v1`)
- Targets Hermes commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e`.
- Uses `gateway.session_finite.finite_turn_required()`.
- Respects the tri-state ContextVar (`True`, `False`, or `None`): NEVER substitutes unconditional `False`.
- Turns only suppress statuslines when `finite_turn_required() is True`.
- Any exception evaluating `is_finite_turn()` fails closed (returns `True` to prevent leakage).
- Missing `gateway.session_finite` or broken imports on modern core fail closed immediately and **NEVER fall back** to the legacy adapter.

### 2. Raspberry Pi Legacy Core (`legacy_pi_adapter`)
- Targets Hermes Pi commit `4f9b741a3a0284ecce31dc7e4cffde49173ec764`.
- **Exact-source cryptographic guard**: The legacy adapter activates **ONLY** when:
  1. Module `gateway.session_finite` is genuinely absent (`importlib.util.find_spec("gateway.session_finite") is None`), **AND**
  2. The on-disk `gateway/run_turn.py` file hash matches the official pinned Pi core commit SHA256 (`dc0829d1d8c7d521502f847442ecb3386b7671268047372e22f6da263417be66`).
  If the hash does not match, registration fails closed (`{"status": "incompatible"}`).
- **Platform Scope Guard**: On the legacy Pi core, Telegram is the proven supported messaging platform. If the turn source is not Telegram (or an unknown surface), the plugin preserves exact old native behavior.
- Does not invent artificial ContextVars pretending to be core finite state.

---

## Zero Transcript Pollution & Persistence Isolation

1. **In-Memory Display Only**: The statusline footer is generated solely for platform message delivery.
2. **Exact Same-Turn Suffix Stripping**: Before persistence to the session database or assistant transcript history, the plugin wraps `GatewayTurnMixin._hmwa_persist_turn_transcript` using a task-local `ContextVar` to strip ONLY the exact same-turn `\n\n` + footer suffix from the response. Bare occurrences inline are never stripped.
3. **Preserved Messages**: The original `agent_result` and `messages` objects are preserved completely unmodified. The LLM never sees its own statusline in subsequent turns, consuming **zero extra LLM tokens**.

---

## Gateway Hot Reload & Activation

### Why `hermes plugins enable` is a No-Op When Already Enabled
In the two pinned Hermes Agent cores (`hermes_cli/plugins.py`), running `hermes plugins enable hermes-statusline` on a plugin that is already listed in `plugins.enabled` in `config.yaml` immediately prints:
```text
Plugin 'hermes-statusline' is already enabled.
```
and returns without signaling the running gateway daemon. **It does not reload plugin code or refresh in-memory handlers.**

### Official Reload Architecture: Gateway Control Socket
To reload plugins in a live, running Hermes gateway without restarting or interrupting active sessions, use the official control socket API (`gateway/control_socket.py`):

```python
from gateway.control_socket import reload_gateway_plugins
from hermes_constants import get_hermes_home
from pathlib import Path

# Exact core signature:
# reload_gateway_plugins(home: Path, *, profile_home: Optional[Path] = None, timeout: float = 30.0) -> Optional[dict[str, Any]]
home = Path(get_hermes_home())
result = reload_gateway_plugins(home)
# Returns {"reloaded": True, "plugins": [...], ...} when live reload succeeds.
```
> [!NOTE]
> Do not reset the session or restart the gateway to apply this plugin. The control-socket request performs forced plugin discovery; this plugin restores/re-wraps its own in-memory methods via its unload callback. Generic native-handler removal has separate Hermes lifecycle limitations: this is not a promise that every plugin handler can be unwired live.
>
> This plugin wraps **private core interfaces**, not a public statusline SDK hook. Compatibility is deliberately constrained and must be revalidated after core upgrades. Repository CI is not catalog admission; a catalog validator may reject these private wrappers.

### Human Activation & Receipt Verification Procedure
1. Verify the plugin is installed and enabled in configuration:
   ```bash
   hermes plugins list
   ```
2. After updating the installed plugin directory, trigger reload **as the gateway's OS user and profile**, using the installation-bound runtime rather than system Python. The following preserves the bootstrap emitted by the two pinned cores and changes only its final entry point:
   ```bash
   python3 - <<'PY'
   import json, subprocess
   argv = json.loads(subprocess.check_output(["hermes", "--print-runtime-command"], text=True))
   index = argv.index("-c") + 1
   entry = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
   if not argv[index].endswith(entry):
       raise SystemExit("Unsupported launcher shape: stop and inspect this installation.")
   lines = [
       "from pathlib import Path",
       "import json",
       "from hermes_constants import get_hermes_home",
       "from gateway.control_socket import reload_gateway_plugins",
       "home = Path(get_hermes_home())",
       "result = reload_gateway_plugins(home, profile_home=home)",
       "print(json.dumps(result, indent=2))",
       "if not isinstance(result, dict) or result.get('reloaded') is not True:",
       "    raise SystemExit('No successful live reload acknowledgement.')",
   ]
   argv[index] = argv[index][:-len(entry)] + "exec(" + repr("\n".join(lines)) + ")"
   subprocess.run(argv, check=True)
   PY
   ```
3. Read `gateway.pid` as JSON and obtain its `pid`; inspect **only** the matching `$HERMES_HOME/state/hermes_statusline/loaded-<pid>.json`. Compare `plugin_source_sha256` with the installed `__init__.py`, `version` with `1.0.1`, and `core_identity.run_turn_sha256` with the actual imported source. A receipt from a CLI/test PID or an old process is not evidence for the live gateway. Confirm a new timestamp after reload; then observe an ordinary final response in the intended Telegram conversation.
   **Receipt fields:**
   - `pid`: Process ID of the running Hermes gateway.
   - `plugin_source_sha256`: SHA256 of `__init__.py` verifying exact code running.
   - `version`: Plugin version (`1.0.1`).
   - `compatibility_strategy`: `session_finite_v1` (modern core) or `legacy_pi_adapter` (Raspberry Pi).
   - `interfaces`: Verified private seams.
   - `core_identity`: Actual `run_turn_path`, `run_turn_sha256`, and verified `core_commit`.
   - `registered_at`: ISO 8601 UTC timestamp.

If receipt creation fails (e.g., read-only disk), registration atomically rolls back both wrappers and fails closed.

---

## Configuration

Enable the runtime footer and configure fields in your Hermes configuration:

```bash
# Enable runtime footer for Telegram
hermes config set display.platforms.telegram.runtime_footer.enabled true

# Configure active fields (model, effort, ctx, quota)
hermes config set display.platforms.telegram.runtime_footer.fields '["model","effort","ctx","quota"]'
```

### Timezone
The default timezone is `UTC`. Configure your local timezone:
```bash
export HERMES_STATUSLINE_TZ="Europe/Paris"
# or
export TZ="America/New_York"
```

---

## Zero-Network Quota & Cache Normalization

The plugin maintains a strict **zero-network path**: it contains no network calls, credentials reading, or outbound requests. Quotas and credit balances are optional, absent by default, and read solely from local cache files:

### Quota Cache Schema (`$HERMES_HOME/state/provider_usage_cache.json`)
```json
{
  "provider": "openai-codex",
  "fetched_at": 1729759200.0,
  "windows": {
    "session": {
      "used_percent": 18.0,
      "resets_at": "2026-10-24T18:40:00+00:00"
    },
    "weekly": {
      "used_percent": 39.0,
      "resets_at": "2026-10-31T09:00:00+00:00"
    }
  }
}
```

### Credit Cache Schema (`$HERMES_HOME/state/provider_credit_cache.json`)
```json
{
  "fetched_at": 1729759200.0,
  "credits": {
    "deepseek": {
      "balance": 16.06,
      "currency": "USD",
      "fetched_at": 1729759200.0
    },
    "kie": {
      "balance": 179.51,
      "unit_type": "credits",
      "fetched_at": 1729759200.0
    }
  }
}
```

### Optional Normalizer Script (`scripts/normalize_usage.py`)
To populate `provider_usage_cache.json` without custom code, use the included standalone normalizer:
```bash
# Read official hermes usage --json from stdin and update cache atomically:
hermes usage --json | python3 scripts/normalize_usage.py --write
```
- **Opt-in only**: Must be executed by the operator; no automated cron or credential fetches are installed.
- Consumes stdin only, never synthesizes quota values, and performs zero network egress.

---

## Running Tests

All tests enforce complete sandbox isolation. Tests must set temporary `HOME` and `HERMES_HOME` before Python launches and before imports, with `HERMES_DISABLE_LAZY_INSTALLS=1`:

```bash
# Run unit tests:
./scripts/run_tests.sh

# Run real-core integration tests against an explicit core checkout:
HERMES_TEST_CORE=/path/to/extracted/core ./scripts/run_tests.sh
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
