#!/usr/bin/env python3
"""normalize_usage.py: Standalone local normalizer for hermes usage --json.

Consumes the official JSON output from `hermes usage --json` on stdin, normalizes
it into the local cache format used by hermes-statusline, and writes or prints it.

ZERO network egress: only consumes stdin.
Does NOT run cron, does NOT schedule background jobs, does NOT fetch credentials.

Usage:
    hermes usage --json | python3 scripts/normalize_usage.py
    hermes usage --json | python3 scripts/normalize_usage.py --write
    python3 scripts/normalize_usage.py < sample_usage.json --out /path/to/provider_usage_cache.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def parse_iso_or_epoch(val: Any) -> Optional[float]:
    """Parse an ISO 8601 string or numeric timestamp to an epoch float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None
    return None


def normalize_usage_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a hermes usage --json snapshot into the provider_usage_cache format."""
    if not isinstance(doc, dict):
        raise ValueError("Input JSON must be an object")

    provider = str(doc.get("provider") or "").strip().lower()
    if not provider:
        raise ValueError("Document missing required 'provider' field")

    fetched_raw = doc.get("fetched_at")
    if fetched_raw is None:
        raise ValueError("Document missing required 'fetched_at' field")
    fetched_at = parse_iso_or_epoch(fetched_raw)
    if fetched_at is None:
        raise ValueError(f"Invalid 'fetched_at' timestamp: {fetched_raw!r}")

    windows_input = doc.get("windows") or []
    if not isinstance(windows_input, list):
        windows_input = []

    normalized_windows: Dict[str, Dict[str, Any]] = {}

    for w in windows_input:
        if not isinstance(w, dict):
            continue
        label = str(w.get("label") or "").strip().lower()
        used = w.get("used_percent")
        if used is None or not isinstance(used, (int, float)):
            continue

        resets_at = w.get("resets_at")
        window_entry = {
            "used_percent": float(used),
            "resets_at": resets_at,
        }

        # Strict mapping to session or weekly; drop unrecognized windows without inventing assignments
        if "5-hour" in label or "session" in label or "5h" in label:
            normalized_windows["session"] = window_entry
        elif "week" in label or "weekly" in label or "7d" in label:
            normalized_windows["weekly"] = window_entry

    return {
        "provider": provider,
        "fetched_at": fetched_at,
        "windows": normalized_windows,
    }


def get_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return Path(env.strip()).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize `hermes usage --json` stdin into provider_usage_cache.json"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write directly to $HERMES_HOME/state/provider_usage_cache.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Explicit output path to write normalized cache JSON",
    )
    args = parser.parse_args(argv)

    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            print("Error: empty stdin received", file=sys.stderr)
            return 1
        data = json.loads(raw_input)
    except Exception as exc:
        print(f"Error parsing JSON from stdin: {exc}", file=sys.stderr)
        return 1

    try:
        normalized = normalize_usage_document(data)
    except Exception as exc:
        print(f"Error normalizing usage document: {exc}", file=sys.stderr)
        return 1

    formatted = json.dumps(normalized, indent=2)

    target_path = None
    if args.out:
        target_path = args.out
    elif args.write:
        target_path = get_hermes_home() / "state" / "provider_usage_cache.json"

    if target_path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_path.with_name(f"{target_path.name}.tmp.{os.getpid()}")
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(formatted)
            f.write("\n")
        temp_file.replace(target_path)
        print(f"Wrote normalized usage cache to {target_path}", file=sys.stderr)
    else:
        print(formatted)

    return 0


if __name__ == "__main__":
    sys.exit(main())
