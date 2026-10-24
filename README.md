# hermes-statusline

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-thomashuangth%2Fhermes--statusline-blue)](https://github.com/thomashuangth/hermes-statusline)

A lightweight, portable statusline plugin for [Hermes Agent (NousResearch/hermes-agent)](https://github.com/NousResearch/hermes-agent). Adds a clean, compact monospace status footer to final responses across messaging platforms (such as Telegram) without core edits, extra LLM tokens, or transcript history pollution.

Tested compatibility target: Hermes checkout `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (2026-10-22).

---

## Active Statusline Behavior

The plugin outputs a single compact, monospace line under the final response of each turn:

### Standard Turn (Codex / Subscription)
```
`gpt-6.1-sol · low · ctx 24% · 5h 18% (18:40) · 7j 39% (31/10 09:00)`
```
- **model**: Model served for this turn.
- **effort**: Effective reasoning effort (`none`, `low`, `medium`, `high`, or `default`).
- **ctx**: Context window token utilization (`ctx <percent>%`).
- **5h quota**: Consumed percentage of the 5-hour rolling session window with reset time in the configured timezone.
- **7j quota**: Consumed percentage of the weekly window with date/time reset in the configured timezone.
- **stale indication**: Marked with `~` if the cache is older than 30 minutes; dropped completely after 6 hours.

### Pay-As-You-Go Provider Turn (DeepSeek / Kie)
```
`deepseek-flash · low · ctx 6% · credit $16.06`
`deepseek-v4-1-flash · low · ctx 6% · credits 179.5`
```
On metered API providers, pay-as-you-go balances are displayed instead of subscription windows:
- Currency balances display with currency symbol: `credit $16.06`
- Unit-based credit balances display explicitly as credits: `credits 179.5`
- Provider isolation is strictly enforced: Codex quotas are never rendered on DeepSeek turns, and credit balances are never rendered on Codex turns.
- OpenAI API keys are strictly separated: `openai` does NOT map to `openai-codex`.

### Fallback Turn
```
`deepseek-flash · low · ctx 6% · credit $16.06 · fallback deepseek · (recovery unknown)`
`gpt-4o-mini · low · ctx 12% · fallback · (recovery 14:00)`
```
When fallback routing activates (`_fallback_activated` flag or requested/served model mismatch):
- The served model is shown as the primary model field.
- A fallback indicator (`fallback <provider>`) identifies the routing deviation.
- A deliberate user `/model` override is NOT marked as a fallback.
- A conservative recovery ETA is calculated based strictly on provider resets from exhausted quota windows or credential pools. If evidence is incomplete or windows are not strictly exhausted, it reports `(recovery unknown)`—never an invented time.

---

## Zero Transcript Pollution & Persistence Isolation

1. **In-Memory Display Only**: The statusline footer is generated solely for platform message delivery.
2. **Exact Same-Turn Suffix Stripping**: Before persistence to the session database or assistant transcript history, the plugin wraps `GatewayTurnMixin._hmwa_persist_turn_transcript` using a task-local `ContextVar` to strip ONLY the exact same-turn `\n\n` + footer suffix from the response. Bare occurrences inline are never stripped.
3. **Preserved Messages**: The original `agent_result` and `messages` objects are preserved completely unmodified. The LLM never sees its own statusline in subsequent turns, consuming **zero extra LLM tokens**.

---

## Installation

The CLI install syntax below uses the independently verified Hermes plugin commands:

### Recommended: Direct Plugin Install
```bash
# Auto-enable immediately:
hermes plugins install thomashuangth/hermes-statusline --enable

# Or using the full Git URL:
hermes plugins install https://github.com/thomashuangth/hermes-statusline --enable
```

### Headless / CI / Docker Automation
```bash
# Answer dependency consent non-interactively:
hermes plugins install thomashuangth/hermes-statusline --yes-deps --enable
```

### Install Disabled, Enable Later
```bash
hermes plugins install thomashuangth/hermes-statusline --no-enable
hermes plugins enable hermes-statusline
```

### Manual Installation
```bash
# Clone directly to your plugins directory:
git clone https://github.com/thomashuangth/hermes-statusline "${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-statusline"

# Enable plugin:
hermes plugins enable hermes-statusline
```

### Management
```bash
# Disable or remove:
hermes plugins disable hermes-statusline
hermes plugins remove hermes-statusline
```

---

## Configuration

Enable the runtime footer and configure fields in your Hermes configuration:

```bash
# Enable runtime footer for Telegram
hermes config set display.platforms.telegram.runtime_footer.enabled true

# Configure active fields (recommend model, effort, ctx, quota - no duplicate served_model)
hermes config set display.platforms.telegram.runtime_footer.fields '["model","effort","ctx","quota"]'
```

### Timezone
The default timezone is `UTC`. You can configure your local timezone by setting the environment variable:
```bash
export HERMES_STATUSLINE_TZ="Europe/Paris"
# or
export TZ="America/New_York"
```
Reset timestamps and recovery ETAs will automatically format according to the configured timezone, with full daylight saving time (DST) awareness via standard library `zoneinfo`.

---

## Local Quota Cache Architecture (Zero Network Path)

The plugin implements a **strictly zero-network path**: it contains no network calls, credentials reading, or outbound requests. Quotas and credit balances are optional and absent by default. They are read purely from local cache files written by your external cron jobs or background scripts:

### Quota Cache Schema (`$HERMES_HOME/state/provider_usage_cache.json`)
```json
{
  "provider": "openai-codex",
  "source": "usage_api",
  "fetched_at": 1729759200.0,
  "windows": {
    "session": {
      "label": "5-Hour Limit",
      "used_percent": 18.0,
      "resets_at": "2026-10-24T18:40:00+00:00"
    },
    "weekly": {
      "label": "Weekly Limit",
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

If cache files are absent, unparseable, or expired (> 6 hours), the quota field safely fails closed and is omitted from the footer. There is **no automatic network refresh** and no background cron task is installed automatically.

---

## Internal / Private Seam Compatibility Risks

This plugin interfaces in-memory with private gateway seams without modifying core files on disk:
- `GatewayTurnMixin._hmwa_runtime_footer_line(self, agent_result, source, _turn_seconds)`
- `GatewayTurnMixin._hmwa_persist_turn_transcript(self, **kwargs)`
- `gateway.session_finite.finite_turn_required()` (0 arguments)

### Catalog Admission Notice:
> [!IMPORTANT]
> Running `hermes plugins validate repo` **fails with "no core override"** by design, because this plugin wraps `GatewayTurnMixin` in memory. `hermes-statusline` is an unsupported private-seam extension and is **NOT catalog-admissible** for the official plugin catalog. Direct installation via Git URL (`hermes plugins install https://github.com/thomashuangth/hermes-statusline --enable`) or local directory clone is the supported distribution method. Do not submit this plugin to the public catalog index.

### Mitigations & Fail-Closed Design:
1. **Strict Positional & Coroutine Signature Checks**: `check_gateway_compatibility()` validates the exact positional parameter kinds of `_hmwa_runtime_footer_line` and confirms that `_hmwa_persist_turn_transcript` is a coroutine function accepting keyword arguments `response` and `agent_result`. It also verifies that `gateway.session_finite.finite_turn_required` is a 0-argument callable. If any interface deviates, the plugin **fails closed** (`{"status": "incompatible"}`) and message delivery proceeds normally without decoration.
2. **Atomic Registration & Rollback**: Registration snapshots exact pre-registration functions and active tokens; if either wrapper fails, pre-registration state is atomically restored without leaving half-patched wrappers.
3. **Hermes Lifecycle Wiring**: `register(ctx)` registers a restoration closure with `ctx.on_unload(callback)`.
4. **Ownership Token & Function Identity**: Unloading uses exact function identity and an owner token. An unload callback from an older generation cannot remove a newer generation, and foreign decorators wrapping the mixin (e.g., using `functools.wraps`) are never stripped.
5. **Runtime Exception Safety**: Exceptions after `original(...)` are caught and logged, returning the native output to guarantee message delivery is never disrupted.
6. **Zero-Network Recovery ETA**: Recovery ETA never calls `credential_pool.load_pool` or reads disk credentials. It calculates recovery exclusively from local quota cache or provably matching live in-memory pool objects.
7. **No Live Telegram Proof**: Testing has been performed against self-contained unit and integration stubs matching checkout `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. No live Telegram bot tokens or credentials were used.

---

## Running Tests

All tests use standard library `unittest` and run with complete filesystem isolation. Neither `$HOME` nor `$HERMES_HOME` is ever touched:

```bash
./scripts/run_tests.sh
```

To run with an alternate Python interpreter:
```bash
PYTHON=python3.11 ./scripts/run_tests.sh
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
