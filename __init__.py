"""hermes-statusline: Portable statusline footer plugin for Hermes Agent.

Compatibility Target: Hermes checkout bf0203a794a07aac3ff764972ab9087baa4e4f4e (2026-10-22).

Provides a compact monospace final-turn statusline footer for Hermes agent
responses without core edits, extra LLM tokens, or transcript history pollution.

Active format (compact single-line monospace):
    `model · applied effort · ctx used% · 5h used% (reset) · 7j used% (reset)`
Or on pay-as-you-go providers (e.g. DeepSeek, Kie):
    `model · applied effort · ctx used% · credit $16.06`
    `model · applied effort · ctx used% · credits 179.5`

Fallback behavior:
    Shows served model, effort, ctx, optional remaining credit, fallback indicator,
    and a conservative recovery ETA or unknown.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-statusline"
VERSION = "1.0.0"
FORMAT_VERSION = "compact-mono-active-v1"

NATIVE_FIELDS = ("model", "served_model", "context_pct", "latency", "cwd")
EXTRA_FIELDS = ("effort", "ctx", "quota")
_SEP = " · "

# Freshness bounds:
# <= FRESH_SECONDS (30 min): fresh.
# > FRESH_SECONDS and <= MAX_AGE_SECONDS (6 hours): stale, marked with '~'.
# > MAX_AGE_SECONDS or < -60s: expired/invalid, omitted entirely.
FRESH_SECONDS = 30 * 60
MAX_AGE_SECONDS = 6 * 3600

USAGE_CACHE_FILENAME = "provider_usage_cache.json"
CREDIT_CACHE_FILENAME = "provider_credit_cache.json"

# Strict provider aliases: OpenAI API is NOT Codex subscription.
# 'openai' is explicitly omitted so OpenAI API keys never match Codex subscription quotas.
PROVIDER_ALIASES: Dict[str, str] = {
    "codex": "openai-codex",
    "chatgpt": "openai-codex",
    "kie-flash": "kie",
    "deep-seek": "deepseek",
}

# Task-local ContextVar holding the footer line for the current turn:
# Tuple: (runner_id, agent_result_id, rendered_footer_line)
_turn_footer_context: ContextVar[Optional[Tuple[int, int, str]]] = ContextVar(
    "hermes_statusline_turn_footer", default=None
)

# Marker attribute and active tokens for wrapper idempotency and ownership safety
_WRAPPER_PATCH_MARKER = "_hermes_statusline_patched"
_active_registration_token: Optional[object] = None
_active_footer_wrapper: Optional[Callable] = None
_active_persist_wrapper: Optional[Callable] = None


# ============================================================================
# Dynamic Environment & Timezone Resolution
# ============================================================================


def get_hermes_home() -> Path:
    """Return the dynamically resolved Hermes home directory.

    Evaluated dynamically on each access; never freezes at import time.
    """
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return Path(env.strip()).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def get_configured_timezone(tz_name: Optional[str] = None) -> Any:
    """Resolve the configured timezone (default UTC).

    Priority:
    1. Explicit tz_name parameter
    2. Environment variable HERMES_STATUSLINE_TZ
    3. Environment variable TZ
    4. Fallback: UTC
    """
    candidate = tz_name or os.environ.get("HERMES_STATUSLINE_TZ") or os.environ.get("TZ") or "UTC"
    candidate = str(candidate).strip()
    if not candidate or candidate.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(candidate)
    except Exception:
        logger.debug("Failed to resolve timezone %r, falling back to UTC", candidate)
        return timezone.utc


# ============================================================================
# Provider Normalization & Isolation (Fail Closed)
# ============================================================================


def normalize_provider(value: Any) -> str:
    """Return normalized canonical provider name in lowercase, or empty string."""
    name = str(value or "").strip().lower()
    if not name:
        return ""
    return PROVIDER_ALIASES.get(name, name)


def same_provider(turn_provider: Any, cache_provider: Any) -> bool:
    """Strict provider match check.

    Returns True ONLY when both sides are non-empty and normalize to the exact
    same provider. A missing, None, or empty provider on either side is never a
    match (fail closed to prevent provider mixing).
    """
    left = normalize_provider(turn_provider)
    right = normalize_provider(cache_provider)
    return bool(left) and bool(right) and left == right


# ============================================================================
# Local Cache Loaders (Safe Local Cache First, Zero Network Path)
# ============================================================================


def load_quota_cache(path: Optional[Path] = None) -> Optional[dict]:
    """Load local provider usage quota cache file. Returns None if unreadable."""
    target = path or (get_hermes_home() / "state" / USAGE_CACHE_FILENAME)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_credit_cache(path: Optional[Path] = None) -> Optional[dict]:
    """Load local provider credit cache file. Returns None if unreadable."""
    target = path or (get_hermes_home() / "state" / CREDIT_CACHE_FILENAME)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ============================================================================
# Quota & Credit Formatting
# ============================================================================


def _used_percent(window: Any) -> Optional[int]:
    """Extract and clamp consumed used percent (0-100) from quota window dict."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    try:
        val = float(used)
        if not math.isfinite(val):
            return None
        return max(0, min(100, int(round(val))))
    except (TypeError, ValueError):
        return None


def _format_reset_label(raw: Any, *, with_date: bool, tz: Optional[Any] = None) -> str:
    """Format reset timestamp in the configured timezone."""
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(str(raw))
    except Exception:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    target_tz = tz or timezone.utc
    try:
        local = moment.astimezone(target_tz)
    except Exception:
        local = moment.astimezone(timezone.utc)
    return local.strftime("%d/%m %H:%M") if with_date else local.strftime("%H:%M")


def format_quota(
    cache: Optional[dict],
    *,
    now: Optional[float] = None,
    provider: Any = None,
    tz: Optional[Any] = None,
) -> str:
    """Format active compact quota consumption:

    `5h used% (reset) · 7j used% (reset)`
    Prefixed with '~' if stale (> 30 min) or refresh fallback.
    Returns empty string if expired (> 6 h), missing, or provider mismatch.
    """
    if not isinstance(cache, dict):
        return ""
    if not same_provider(provider, cache.get("provider")):
        return ""
    fetched = cache.get("fetched_at")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
        return ""
    current_time = time.time() if now is None else now
    age = current_time - float(fetched)
    if age > MAX_AGE_SECONDS or age < -60:
        return ""

    windows = cache.get("windows")
    if not isinstance(windows, dict):
        return ""

    session = windows.get("session")
    weekly = windows.get("weekly")
    parts: List[str] = []

    session_used = _used_percent(session)
    if session_used is not None:
        reset = _format_reset_label((session or {}).get("resets_at"), with_date=False, tz=tz)
        parts.append(f"5h {session_used}%" + (f" ({reset})" if reset else ""))

    weekly_used = _used_percent(weekly)
    if weekly_used is not None:
        reset = _format_reset_label((weekly or {}).get("resets_at"), with_date=True, tz=tz)
        parts.append(f"7j {weekly_used}%" + (f" ({reset})" if reset else ""))

    if not parts:
        return ""

    is_stale = (age > FRESH_SECONDS) or bool(cache.get("turn_refresh_fallback"))
    prefix = "~" if is_stale else ""
    return _SEP.join(prefix + part for part in parts)


def _credit_entry(cache: Optional[dict], provider: Any) -> Tuple[Optional[str], Optional[dict]]:
    """Look up (key, entry) for provider in credit cache dict."""
    if not isinstance(cache, dict):
        return None, None
    credits_map = cache.get("credits")
    if not isinstance(credits_map, dict):
        return None, None
    wanted = normalize_provider(provider)
    if not wanted:
        return None, None
    for name, entry in credits_map.items():
        if isinstance(entry, dict) and normalize_provider(name) == wanted:
            return name, entry
    return None, None


def format_credit(
    cache: Optional[dict],
    *,
    now: Optional[float] = None,
    provider: Any = None,
    refreshed: Optional[bool] = None,
) -> str:
    """Format remaining credit for pay-as-you-go providers:

    Currency balance: `credit $16.06`
    Unit balance: `credits 179.5`
    Prefixed with '~' if stale (> 30 min) unless refreshed is True for this turn.
    Returns empty string if expired (> 6 h), missing, or provider mismatch.
    """
    _, entry = _credit_entry(cache, provider)
    if entry is None:
        return ""
    balance = entry.get("balance")
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        return ""
    try:
        val = float(balance)
        if not math.isfinite(val):
            return ""
    except (TypeError, ValueError):
        return ""

    fetched = entry.get("fetched_at")
    if not isinstance(fetched, (int, float)) and isinstance(cache, dict):
        fetched = cache.get("fetched_at")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
        return ""

    current_time = time.time() if now is None else now
    age = current_time - float(fetched)
    if age > MAX_AGE_SECONDS or age < -60:
        return ""

    currency = str(entry.get("currency") or "").strip().upper()
    if currency:
        symbol = "$" if currency == "USD" else currency + " "
        formatted_val = f"credit {symbol}{val:.2f}"
    else:
        formatted_val = f"credits {val:.1f}"

    if refreshed is True:
        return formatted_val
    prefix = "~" if age > FRESH_SECONDS else ""
    return f"{prefix}{formatted_val}"


# ============================================================================
# Effort & Context Formatters
# ============================================================================


def format_effort(reasoning_config: Any) -> str:
    """Format applied reasoning effort: 'none', 'low', 'medium', 'high', or 'default'."""
    if reasoning_config is None:
        return "default"
    if not isinstance(reasoning_config, dict):
        return ""
    if reasoning_config.get("enabled") is False:
        return "none"
    effort = str(reasoning_config.get("effort") or "").strip()
    return effort or "default"


def format_ctx(context_tokens: Any, context_length: Any) -> str:
    """Format context usage percentage: `ctx 24%` (clamped 0-100%)."""
    if context_tokens is None or context_length is None:
        return ""
    try:
        length = int(context_length)
        tokens = int(context_tokens)
    except (TypeError, ValueError):
        return ""
    if length <= 0 or tokens < 0:
        return ""
    percent = max(0, min(100, round(tokens / length * 100)))
    return f"ctx {percent}%"


# ============================================================================
# Conservative Recovery ETA (Never Invented, Mathematical Lower Bound)
# ============================================================================


def estimate_recovery(
    cache: Optional[dict],
    *,
    primary: Any,
    now: float,
    agent: Any = None,
    pool: Any = None,
    tz: Optional[Any] = None,
    trace: Optional[dict] = None,
    recovery_style: str = "en",
) -> str:
    """Compute conservative recovery ETA from exhausted quota windows or pools.

    Returns '(recovery HH:MM)' or '(recovery DD/MM HH:MM)' or '(recovery unknown)'.
    If recovery_style == 'fr', returns '(reprise estimée ...)'.
    Never invents an ETA. Returns unknown if evidence is missing, foreign,
    inconsistent, or if any window is not strictly exhausted.
    """
    target_tz = tz or timezone.utc
    is_fr = recovery_style.lower().startswith("fr")
    unknown_label = "(reprise estimée inconnue)" if is_fr else "(recovery unknown)"
    prefix_label = "(reprise estimée " if is_fr else "(recovery "

    if trace is None:
        trace = {}
    trace.update({"eta_source": None, "eta_epoch": None})

    # Gateway monotonic retry cooldown inspection
    if agent is not None:
        rt = getattr(agent, "_primary_runtime", None) or {}
        if not same_provider(primary, rt.get("provider")):
            return unknown_label
        rejected = getattr(agent, "_entitlement_rejected_models", None) or ()
        if (rt.get("provider"), rt.get("model")) in rejected:
            return unknown_label
        until = getattr(agent, "_rate_limited_until", 0)
        if isinstance(until, (int, float)) and math.isfinite(until):
            delay = max(0.0, until - time.monotonic())
            trace["gateway_retry_not_before_epoch"] = now + delay if delay else None

    def _render_eta(eta: float, source_name: str) -> str:
        cooldown_epoch = trace.get("gateway_retry_not_before_epoch") or 0.0
        if cooldown_epoch > eta + 1.0:
            return unknown_label
        try:
            local = datetime.fromtimestamp(eta, target_tz)
            curr = datetime.fromtimestamp(now, target_tz)
        except Exception:
            return unknown_label

        if local.date() == curr.date():
            fmt = "%H:%M"
        elif local.year == curr.year:
            fmt = "%d/%m %H:%M"
        else:
            fmt = "%d/%m/%Y %H:%M"

        trace.update({"eta_source": source_name, "eta_epoch": eta})
        return f"{prefix_label}{local.strftime(fmt)})"

    # 1. Primary credential pool: live in-memory pool only (never load credentials/files)
    if pool is None and agent is not None and hasattr(agent, "_credential_pool"):
        cand_pool = getattr(agent, "_credential_pool", None)
        if cand_pool is not None:
            try:
                from agent.credential_pool import credential_pool_matches_provider
                rt = getattr(agent, "_primary_runtime", None) or {}
                if credential_pool_matches_provider(cand_pool, primary, base_url=rt.get("base_url")):
                    pool = cand_pool
            except Exception:
                pass

    if pool is not None:
        try:
            next_at = pool.next_available_at(
                model=(getattr(agent, "_primary_runtime", None) or {}).get("model") or None
            )
            trace["primary_pool_next_available_at"] = next_at
            entries = pool.entries()
            if not entries:
                return unknown_label
            resets = []
            for entry in entries:
                reset = getattr(entry, "last_error_reset_at", None)
                if (
                    getattr(entry, "last_status", None) != "exhausted"
                    or isinstance(reset, bool)
                    or not isinstance(reset, (int, float))
                    or not math.isfinite(reset)
                    or reset <= now
                ):
                    return unknown_label
                resets.append(reset)
            min_reset = min(resets)
            if not isinstance(next_at, (int, float)) or abs(next_at - min_reset) > 1.0:
                return unknown_label
            return _render_eta(min_reset, "primary_pool_provider_declared_reset")
        except Exception:
            trace["primary_pool_read_failed"] = True
            return unknown_label

    # 2. Local Usage API Quota Cache
    if not isinstance(cache, dict) or cache.get("source") != "usage_api":
        return unknown_label
    if not same_provider(primary, cache.get("provider")):
        return unknown_label

    fetched = cache.get("fetched_at")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
        return unknown_label
    if not (-60 <= now - fetched <= FRESH_SECONDS):
        return unknown_label

    windows = cache.get("windows")
    if not isinstance(windows, dict):
        return unknown_label

    resets = []
    blocked = []
    for name in ("session", "weekly"):
        w = windows.get(name)
        if not isinstance(w, dict):
            return unknown_label
        used = w.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            return unknown_label
        if not (0 <= used <= 100):
            return unknown_label
        if used < 100:
            continue
        try:
            reset_dt = datetime.fromisoformat(str(w.get("resets_at")))
            if reset_dt.tzinfo is None:
                return unknown_label
            epoch = reset_dt.timestamp()
            if not math.isfinite(epoch) or epoch <= now:
                return unknown_label
        except (ValueError, TypeError, OverflowError):
            return unknown_label
        resets.append(epoch)
        blocked.append(name)

    if not resets:
        return unknown_label

    max_reset = max(resets)
    trace.update({"blocking_windows": blocked, "eta_cache_fetched_at": fetched})
    return _render_eta(max_reset, "primary_usage_api_exhausted_windows")


# ============================================================================
# Provider Resolution for Current Turn (Topic-Safe, Fail Closed)
# ============================================================================


def _clean_provider(val: Any) -> Optional[str]:
    txt = str(val or "").strip()
    return txt or None


def _session_row_provider(runner: Any, session_key: str) -> Optional[str]:
    """Provider from the session's persisted route, without trusting billing_provider.

    Inspects runner.session_store.lookup_by_session_key(session_key) and
    runner.session_store._db.get_session(session_id)['model_config']['gateway_runtime']['provider'].
    Never trusts billing_provider, which may be stale from initial billing.
    """
    if not session_key:
        return None
    store = getattr(runner, "session_store", None)
    if store is None:
        return None
    try:
        getter_override = getattr(store, "get_model_override", None)
        if callable(getter_override):
            override = getter_override(session_key)
            if isinstance(override, dict):
                val = _clean_provider(override.get("provider"))
                if val:
                    return val
    except Exception:
        pass
    try:
        lookup = getattr(store, "lookup_by_session_key", None)
        entry = lookup(session_key) if callable(lookup) else None
        session_id = getattr(entry, "session_id", None)
        getter_db = getattr(store, "_db_for_key", None)
        db = getter_db(session_key) if callable(getter_db) else None
        if db is None:
            db = getattr(store, "_db", None)
        if db is None or not session_id:
            return None
        row = db.get_session(session_id) or {}
        raw = row.get("model_config")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            runtime = raw.get("gateway_runtime")
            if isinstance(runtime, dict):
                return _clean_provider(runtime.get("provider"))
    except Exception:
        pass
    return None


def build_model_provider_map(config: Any) -> Dict[str, str]:
    """Derive model -> provider mapping from configuration."""
    mapping: Dict[str, str] = {}
    if not isinstance(config, dict):
        return mapping

    def _put(model_name: Any, prov: Any) -> None:
        m = str(model_name or "").strip().lower()
        p = normalize_provider(prov)
        if m and p:
            mapping.setdefault(m, p)

    for section_name in ("default", "model"):
        sec = config.get(section_name)
        if isinstance(sec, dict):
            _put(sec.get("model"), sec.get("provider"))
            for alias, target in (sec.get("aliases") or {}).items():
                if isinstance(target, str) and "/" in target:
                    prov_part, _, mod_part = target.partition("/")
                    _put(mod_part, prov_part)
                    _put(alias, prov_part)
                elif isinstance(target, dict):
                    _put(target.get("model") or alias, target.get("provider"))

    for alias, target in (config.get("model_aliases") or {}).items():
        if isinstance(target, dict):
            _put(target.get("model") or alias, target.get("provider"))

    for prov_name, cfg in (config.get("providers") or {}).items():
        if isinstance(cfg, dict):
            for m in cfg.get("models") or []:
                _put(m, prov_name)

    for entry in config.get("fallback_providers") or []:
        if isinstance(entry, dict):
            _put(entry.get("model"), entry.get("provider"))

    return mapping


def provider_for_model(model: Any, config: Any) -> Optional[str]:
    """Look up provider for a given model from the configuration table."""
    return build_model_provider_map(config).get(str(model or "").strip().lower())


def infer_provider_from_model(model: Any, credit_cache: Optional[dict]) -> Optional[str]:
    """Infer provider from model name ONLY if it has an existing credit entry.

    Fails closed: never invents balances or codex quota.
    """
    name = str(model or "").strip().lower()
    if not name or not isinstance(credit_cache, dict):
        return None
    credits_map = credit_cache.get("credits")
    if not isinstance(credits_map, dict):
        return None
    known = {normalize_provider(k) for k in credits_map.keys()}
    for token in (name, *name.replace(".", "-").split("-")):
        cand = normalize_provider(token)
        if cand and cand in known:
            return cand
    return None


def resolve_turn_provider(
    runner: Any,
    agent_result: Any,
    source: Any,
    config: Any = None,
    *,
    trace: Optional[dict] = None,
) -> Optional[str]:
    """Resolve the provider actually serving this turn in this topic.

    Priority:
    1. agent_result.provider
    2. Live topic agent provider
    3. Session memory model override
    4. Persisted session route (model_config.gateway_runtime.provider)
    5. Config model->provider table
    6. Model name gated by credit cache
    7. Parent channel / chat override
    8. None (fail closed: never defaults to foreign provider)
    """
    def _done(val: Optional[str], branch: str) -> Optional[str]:
        if trace is not None:
            trace["provider_source"] = branch
        return normalize_provider(val) or None

    if isinstance(agent_result, dict):
        direct = agent_result.get("provider")
        if direct and str(direct).strip():
            return _done(str(direct).strip(), "agent_result.provider")

    session_key = None
    try:
        session_key = runner._session_key_for_source(source)
    except Exception:
        pass

    if session_key:
        try:
            state = runner._peek_session_state(session_key)
            live = getattr(getattr(getattr(state, "turn", None), "agent", None), "provider", None)
            if live and str(live).strip():
                return _done(str(live).strip(), "live_agent")
        except Exception:
            pass
        try:
            override = runner._session_model_override(session_key)
            if isinstance(override, dict):
                val = override.get("provider")
                if val and str(val).strip():
                    return _done(str(val).strip(), "memory_override")
        except Exception:
            pass
        try:
            persisted = _session_row_provider(runner, session_key)
            if persisted:
                return _done(persisted, "persisted_route")
        except Exception:
            pass

    served_model = agent_result.get("model") if isinstance(agent_result, dict) else None
    if served_model:
        inferred = provider_for_model(served_model, config)
        if inferred:
            return _done(inferred, "model_provider_table")
        gated = infer_provider_from_model(served_model, load_credit_cache())
        if gated:
            return _done(gated, "model_name_gated_by_credit")

    return _done(None, "none")


def configured_primary_provider(config: Any) -> str:
    """Return configured primary provider (default.provider or model.provider)."""
    if not isinstance(config, dict):
        return ""
    # In Hermes checkout bf0203a794a07aac3ff764972ab9087baa4e4f4e, config uses 'default'
    def_cfg = config.get("default")
    if isinstance(def_cfg, dict) and def_cfg.get("provider"):
        return normalize_provider(def_cfg.get("provider"))
    m_cfg = config.get("model")
    if isinstance(m_cfg, dict) and m_cfg.get("provider"):
        return normalize_provider(m_cfg.get("provider"))
    return ""


# ============================================================================
# Statusline Composition & Decoration
# ============================================================================


def compose_line(
    *,
    fields: Any,
    native_render: Callable[[str], str],
    effort_report: Any,
    context_tokens: Any,
    context_length: Any,
    quota_cache: Optional[dict] = None,
    credit_cache: Optional[dict] = None,
    now: Optional[float] = None,
    quota_provider: Any = None,
    credit_refreshed: Optional[bool] = None,
    tz: Optional[Any] = None,
) -> str:
    """Compose the compact monospace final-turn statusline.

    Native fields are delegated to native_render callback.
    Added extra fields ('effort', 'ctx', 'quota') are evaluated and formatted.
    Output is wrapped in monospace inline-code backticks (`...`).
    If no extra fields are configured, returns native rendering unmodified.
    """
    fields_list = list(fields or ())
    has_extras = any(f in EXTRA_FIELDS for f in fields_list)

    def _render_quota_or_credit() -> str:
        q_text = format_quota(quota_cache, now=now, provider=quota_provider, tz=tz)
        if q_text:
            return q_text
        c_text = format_credit(
            credit_cache, now=now, provider=quota_provider, refreshed=credit_refreshed
        )
        return c_text

    renderers = {
        "effort": lambda: format_effort(effort_report),
        "ctx": lambda: format_ctx(context_tokens, context_length),
        "quota": _render_quota_or_credit,
    }

    parts: List[str] = []
    for field in fields_list:
        name = str(field)
        if name in renderers:
            val = renderers[name]()
        elif name in NATIVE_FIELDS:
            val = native_render(name)
        else:
            continue
        if val and str(val).strip():
            parts.append(str(val).strip())

    if not parts:
        return ""

    joined = _SEP.join(parts)
    if not has_extras:
        return joined

    safe_text = joined.replace("`", "ˋ")
    return f"`{safe_text}`"


def decorate_fallback_statusline(
    line: str,
    *,
    fallback_active: bool,
    fallback_marker: str,
    recovery: str = "",
) -> str:
    """Decorate statusline with fallback indicator and conservative recovery ETA."""
    if not line or not fallback_active:
        return line

    content = line[1:-1] if line.startswith("`") and line.endswith("`") else line
    parts = [p.strip() for p in content.split(_SEP) if p.strip()]

    marker_str = fallback_marker.strip() if fallback_marker else "fallback"
    if marker_str and not any(marker_str in p for p in parts):
        parts.append(marker_str)

    if recovery and recovery.strip():
        parts.append(recovery.strip())

    new_content = _SEP.join(parts).replace("`", "ˋ")
    return f"`{new_content}`"


# ============================================================================
# Persistence Isolation (No Transcript Footers, Exact Same-Turn Suffix Only)
# ============================================================================


async def persist_without_footer(
    original_persist: Callable,
    runner: Any,
    kwargs: Dict[str, Any],
    footer_line: str,
) -> Any:
    """Exact same-turn suffix stripping before transcript persistence.

    Strips ONLY exact '\n\n' + footer for the same turn. Never bare occurrence.
    Preserves original agent_result and messages dicts unmodified.
    Ensures zero extra LLM tokens or persisted statusline artifacts in session history.
    """
    response = kwargs.get("response")
    if footer_line and isinstance(response, str):
        suffix = "\n\n" + footer_line
        if response.endswith(suffix):
            kwargs = {**kwargs, "response": response[:-len(suffix)]}
    return await original_persist(runner, **kwargs)


# ============================================================================
# Gateway Mixin In-Memory Patching (Idempotent Hot Reload & Safe Unload)
# ============================================================================


def is_finite_turn() -> bool:
    """Check if the turn requires finite turn behavior (zero-argument call).

    Fails closed (returns True) if the gateway module is unavailable or throws.
    """
    try:
        from gateway.session_finite import finite_turn_required
        return finite_turn_required() is True
    except Exception as exc:
        logger.debug("Failed finite_turn_required check: %s; failing closed", exc)
        return True


def _get_shared_context_var() -> ContextVar:
    """Return ContextVar shared on GatewayTurnMixin across reload generations."""
    try:
        from gateway import run_turn
        mixin = getattr(run_turn, "GatewayTurnMixin", None)
        if mixin is not None:
            ctx = getattr(mixin, "_hermes_statusline_turn_footer_context", None)
            if ctx is None:
                ctx = _turn_footer_context
                mixin._hermes_statusline_turn_footer_context = ctx
            return ctx
    except Exception:
        pass
    return _turn_footer_context


def _set_turn_footer_context(runner_id: int, result_id: int, line: str) -> None:
    ctx = _get_shared_context_var()
    ctx.set((runner_id, result_id, line))


def check_gateway_compatibility() -> Tuple[bool, str]:
    """Strict signature and compatibility checks for GatewayTurnMixin private seams.

    Fails closed if internal interfaces are missing or unsupported.
    """
    try:
        from gateway import run_turn
    except Exception as exc:
        return False, f"cannot import gateway.run_turn: {exc}"

    mixin = getattr(run_turn, "GatewayTurnMixin", None)
    if not isinstance(mixin, type):
        return False, "GatewayTurnMixin class missing in gateway.run_turn"

    # 1. Check exact positional signature of _hmwa_runtime_footer_line:
    # (self, agent_result, source, _turn_seconds/turn_seconds)
    footer_method = getattr(mixin, "_hmwa_runtime_footer_line", None)
    if not callable(footer_method):
        return False, "GatewayTurnMixin._hmwa_runtime_footer_line missing or not callable"

    try:
        sig = inspect.signature(footer_method)
        params = list(sig.parameters.values())
        if len(params) != 4:
            return False, f"GatewayTurnMixin._hmwa_runtime_footer_line signature mismatch: expected 4 parameters, got {len(params)} ({list(sig.parameters.keys())})"
        if params[0].name != "self" or params[0].kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return False, f"parameter 0 must be positional 'self', got {params[0].name}"
        if params[1].name != "agent_result" or params[1].kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return False, f"parameter 1 must be positional 'agent_result', got {params[1].name}"
        if params[2].name != "source" or params[2].kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return False, f"parameter 2 must be positional 'source', got {params[2].name}"
        if params[3].name not in ("_turn_seconds", "turn_seconds") or params[3].kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return False, f"parameter 3 must be positional '_turn_seconds' or 'turn_seconds', got {params[3].name}"
    except Exception as exc:
        return False, f"failed to inspect footer signature: {exc}"

    # 2. Check _hmwa_persist_turn_transcript is coroutine and accepts response & agent_result keywords
    persist_method = getattr(mixin, "_hmwa_persist_turn_transcript", None)
    if not callable(persist_method):
        return False, "GatewayTurnMixin._hmwa_persist_turn_transcript missing or not callable"

    if not inspect.iscoroutinefunction(persist_method):
        return False, "GatewayTurnMixin._hmwa_persist_turn_transcript must be a coroutine function"

    try:
        sig_persist = inspect.signature(persist_method)
        params_p = sig_persist.parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params_p.values())
        for kw in ("response", "agent_result"):
            if kw in params_p:
                if params_p[kw].kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                    return False, f"GatewayTurnMixin._hmwa_persist_turn_transcript parameter '{kw}' must be keywordable"
            elif not has_varkw:
                return False, f"GatewayTurnMixin._hmwa_persist_turn_transcript must accept '{kw}' keyword argument"
    except Exception as exc:
        return False, f"failed to inspect persist signature: {exc}"

    # 3. Check gateway.runtime_footer helpers exist
    try:
        from gateway import runtime_footer
        if not callable(getattr(runtime_footer, "format_runtime_footer", None)):
            return False, "gateway.runtime_footer.format_runtime_footer missing or not callable"
        if not callable(getattr(runtime_footer, "resolve_footer_config", None)):
            return False, "gateway.runtime_footer.resolve_footer_config missing or not callable"
    except Exception as exc:
        return False, f"cannot import gateway.runtime_footer: {exc}"

    # 4. Check gateway.session_finite.finite_turn_required is a zero-arg callable
    try:
        from gateway import session_finite
        fn = getattr(session_finite, "finite_turn_required", None)
        if not callable(fn):
            return False, "gateway.session_finite.finite_turn_required missing or not callable"
        sig_finite = inspect.signature(fn)
        req_finite = [
            p for p in sig_finite.parameters.values()
            if p.default == inspect.Parameter.empty and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        if len(req_finite) != 0:
            return False, f"gateway.session_finite.finite_turn_required must accept zero arguments, got {list(sig_finite.parameters.keys())}"
    except Exception as exc:
        return False, f"cannot import gateway.session_finite: {exc}"

    return True, "compatible"


def patch_footer_line(token: object) -> str:
    """Envelop GatewayTurnMixin._hmwa_runtime_footer_line with idempotent wrapper."""
    global _active_footer_wrapper
    try:
        from gateway import run_turn
        from gateway.runtime_footer import format_runtime_footer, resolve_footer_config
    except Exception as exc:
        return f"import_error: {exc}"

    mixin = getattr(run_turn, "GatewayTurnMixin", None)
    if not mixin:
        return "missing_mixin"

    current = getattr(mixin, "_hmwa_runtime_footer_line", None)
    if not callable(current):
        return "missing_footer_method"

    already = getattr(current, _WRAPPER_PATCH_MARKER, False)
    original = getattr(current, "__wrapped__", current) if already else current
    if not callable(original):
        return "missing_original_behind_wrapper"

    def patched_footer(self, agent_result, source, turn_seconds, *args, **kwargs):
        # 1. Finite turn guard: fail closed / return empty immediately
        if is_finite_turn():
            _set_turn_footer_context(id(self), id(agent_result), "")
            return ""

        line = original(self, agent_result, source, turn_seconds, *args, **kwargs)

        try:
            from gateway.run import _load_gateway_config, _platform_config_key
            config = _load_gateway_config()
            platform_key = _platform_config_key(getattr(source, "platform", ""))
            resolved = resolve_footer_config(config, platform_key)
            fields = list(resolved.get("fields") or [])
            enabled = bool(resolved.get("enabled", True))
        except Exception:
            config = None
            fields = []
            enabled = False

        if not enabled:
            _set_turn_footer_context(id(self), id(agent_result), line)
            return line

        has_extras = any(f in EXTRA_FIELDS for f in fields)
        if not has_extras:
            _set_turn_footer_context(id(self), id(agent_result), line)
            return line

        try:
            # Live agent inspection
            live_agent = None
            try:
                state = self._peek_session_state(self._session_key_for_source(source))
                live_agent = getattr(getattr(state, "turn", None), "agent", None)
            except Exception:
                pass

            # Effort resolution: prefer applied live agent.reasoning_config if reliably present
            effort_report = None
            if live_agent is not None:
                rc = getattr(live_agent, "reasoning_config", None)
                if isinstance(rc, dict) and ("effort" in rc or "enabled" in rc):
                    effort_report = rc

            if effort_report is None:
                try:
                    resolver = getattr(self, "_resolve_session_reasoning_config", None)
                    if callable(resolver):
                        effort_report = resolver(
                            source=source, model=agent_result.get("model") or ""
                        )
                except Exception:
                    pass

            tz = get_configured_timezone()
            provider = resolve_turn_provider(self, agent_result, source, config)

            context_tokens = agent_result.get("last_prompt_tokens", 0) if isinstance(agent_result, dict) else 0
            context_length = agent_result.get("context_length") if isinstance(agent_result, dict) else None
            requested_model = agent_result.get("requested_model") if isinstance(agent_result, dict) else None
            served_model = agent_result.get("served_model") if isinstance(agent_result, dict) else None

            # Fallback detection: requires _fallback_activated or explicit model mismatch.
            # Provider mismatch to global default is NOT fallback (deliberate /model override).
            fallback_activated = bool(getattr(live_agent, "_fallback_activated", False))
            model_mismatch = bool(served_model and requested_model and served_model != requested_model)
            fallback_active = fallback_activated or model_mismatch

            effective_model = served_model if (fallback_active and served_model) else (agent_result.get("model") if isinstance(agent_result, dict) else None)

            def native_render(field_name: str) -> str:
                try:
                    return format_runtime_footer(
                        model=effective_model,
                        context_tokens=context_tokens,
                        context_length=context_length,
                        turn_seconds=turn_seconds,
                        requested_model=requested_model,
                        served_model=served_model,
                        fields=[field_name],
                    )
                except Exception:
                    return ""

            quota_cache = load_quota_cache()
            credit_cache = load_credit_cache()
            now = time.time()

            status_line = compose_line(
                fields=fields,
                native_render=native_render,
                effort_report=effort_report,
                context_tokens=context_tokens,
                context_length=context_length,
                quota_cache=quota_cache,
                credit_cache=credit_cache,
                now=now,
                quota_provider=provider,
                tz=tz,
            )

            if fallback_active and status_line:
                indicator = f"fallback {provider}" if provider else "fallback"
                primary_prov = None
                if live_agent is not None:
                    rt = getattr(live_agent, "_primary_runtime", None)
                    if isinstance(rt, dict) and rt.get("provider"):
                        primary_prov = normalize_provider(rt.get("provider"))
                if not primary_prov:
                    primary_prov = configured_primary_provider(config)

                recovery = estimate_recovery(
                    quota_cache,
                    primary=primary_prov,
                    now=now,
                    agent=live_agent,
                    tz=tz,
                )
                status_line = decorate_fallback_statusline(
                    status_line,
                    fallback_active=True,
                    fallback_marker=indicator,
                    recovery=recovery,
                )

            final_line = status_line or line
            _set_turn_footer_context(id(self), id(agent_result), final_line)
            return final_line
        except Exception as exc:
            logger.debug("hermes-statusline post-original exception: %s; keeping native output", exc)
            _set_turn_footer_context(id(self), id(agent_result), line)
            return line

    setattr(patched_footer, _WRAPPER_PATCH_MARKER, True)
    setattr(patched_footer, "_owner_token", token)
    patched_footer.__wrapped__ = original
    _active_footer_wrapper = patched_footer
    mixin._hmwa_runtime_footer_line = patched_footer
    return "repatched" if already else "patched"


def patch_footer_persistence(token: object) -> str:
    """Envelop GatewayTurnMixin._hmwa_persist_turn_transcript for same-turn suffix stripping."""
    global _active_persist_wrapper
    try:
        from gateway import run_turn
    except Exception as exc:
        return f"import_error: {exc}"

    mixin = getattr(run_turn, "GatewayTurnMixin", None)
    if not mixin:
        return "missing_mixin"

    current = getattr(mixin, "_hmwa_persist_turn_transcript", None)
    if not callable(current):
        return "missing_persist_method"

    already = getattr(current, _WRAPPER_PATCH_MARKER, False)
    original = getattr(current, "__wrapped__", current) if already else current
    if not callable(original):
        return "missing_original_behind_wrapper"

    async def patched_persist(self, **kwargs):
        ctx = _get_shared_context_var()
        ctx_val = ctx.get()
        footer = ""
        if ctx_val and ctx_val[:2] == (id(self), id(kwargs.get("agent_result"))):
            footer = ctx_val[2]
        return await persist_without_footer(original, self, kwargs, footer)

    setattr(patched_persist, _WRAPPER_PATCH_MARKER, True)
    setattr(patched_persist, "_owner_token", token)
    patched_persist.__wrapped__ = original
    _active_persist_wrapper = patched_persist
    mixin._hmwa_persist_turn_transcript = patched_persist
    return "repatched" if already else "patched"


def unpatch_gateway(token: Optional[object] = None) -> Dict[str, bool]:
    """Ownership-safe unpatching / unloading of all gateway wrappers.

    If a token is provided, only unpatches if the token matches the active registration.
    Safely respects exact function identity and does not clobber foreign wrappers.
    """
    global _active_registration_token, _active_footer_wrapper, _active_persist_wrapper
    status = {"footer": False, "persist": False}

    if token is not None and token is not _active_registration_token:
        # Outdated generation unload; no-op
        return status

    try:
        from gateway import run_turn
        mixin = getattr(run_turn, "GatewayTurnMixin", None)
        if mixin:
            footer = getattr(mixin, "_hmwa_runtime_footer_line", None)
            if footer is _active_footer_wrapper and getattr(footer, "_owner_token", None) is _active_registration_token:
                orig = getattr(footer, "__wrapped__", None)
                if orig:
                    mixin._hmwa_runtime_footer_line = orig
                    status["footer"] = True

            persist = getattr(mixin, "_hmwa_persist_turn_transcript", None)
            if persist is _active_persist_wrapper and getattr(persist, "_owner_token", None) is _active_registration_token:
                orig = getattr(persist, "__wrapped__", None)
                if orig:
                    mixin._hmwa_persist_turn_transcript = orig
                    status["persist"] = True
    except Exception as exc:
        logger.debug("Failed during unpatch_gateway: %s", exc)

    if status["footer"] or status["persist"]:
        _active_registration_token = None
        _active_footer_wrapper = None
        _active_persist_wrapper = None
    return status


# ============================================================================
# Plugin Lifecycle (register / unregister)
# ============================================================================


def register(ctx: Any = None) -> Dict[str, Any]:
    """Hermes plugin entry point. Strict signature checks fail closed.

    Atomic registration: rolls back if either wrapper fails.
    Wires ctx.on_unload callback with ownership token.
    """
    global _active_registration_token
    compatible, reason = check_gateway_compatibility()
    if not compatible:
        logger.warning(
            "hermes-statusline: gateway interfaces incompatible (%s); failing closed.",
            reason,
        )
        return {"status": "incompatible", "reason": reason}

    from gateway import run_turn
    mixin = getattr(run_turn, "GatewayTurnMixin", None)
    if mixin is None:
        return {"status": "incompatible", "reason": "GatewayTurnMixin missing"}

    # Snapshot pre-registration state for exact atomic rollback
    prev_footer = getattr(mixin, "_hmwa_runtime_footer_line", None)
    prev_persist = getattr(mixin, "_hmwa_persist_turn_transcript", None)
    prev_token = _active_registration_token
    prev_footer_wrapper = _active_footer_wrapper
    prev_persist_wrapper = _active_persist_wrapper

    token = object()

    def _rollback():
        global _active_registration_token, _active_footer_wrapper, _active_persist_wrapper
        if prev_footer is not None:
            mixin._hmwa_runtime_footer_line = prev_footer
        if prev_persist is not None:
            mixin._hmwa_persist_turn_transcript = prev_persist
        _active_registration_token = prev_token
        _active_footer_wrapper = prev_footer_wrapper
        _active_persist_wrapper = prev_persist_wrapper

    footer_status = patch_footer_line(token)
    if footer_status not in ("patched", "repatched"):
        _rollback()
        return {"status": "incompatible", "reason": f"footer patch failed: {footer_status}"}

    persist_status = patch_footer_persistence(token)
    if persist_status not in ("patched", "repatched"):
        _rollback()
        return {"status": "incompatible", "reason": f"persist patch failed: {persist_status}"}

    _active_registration_token = token

    # Wire Hermes ctx.on_unload hook
    if ctx is not None and hasattr(ctx, "on_unload") and callable(ctx.on_unload):
        ctx.on_unload(lambda: unpatch_gateway(token))

    logger.info("hermes-statusline: registered (footer=%s, persist=%s)", footer_status, persist_status)
    return {
        "status": "ok",
        "footer": footer_status,
        "persist": persist_status,
        "version": VERSION,
    }


def unregister(ctx: Any = None) -> Dict[str, Any]:
    """Hermes plugin unload hook. Cleanly restores original gateway methods."""
    res = unpatch_gateway(_active_registration_token)
    logger.info("hermes-statusline: unregistered (footer=%s, persist=%s)", res["footer"], res["persist"])
    return {"status": "unregistered", "restored": res}
