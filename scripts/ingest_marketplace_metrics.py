#!/usr/bin/env python3
"""Collect authorized marketplace metrics and ingest secret-free snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


SECRET_KEY_RE = re.compile(r"secret|token|authorization|password|cookie|api[_-]?key", re.I)
PLATFORM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def configured_feeds() -> dict[str, str]:
    feeds = {
        str(key): str(value).strip()
        for key, value in _json_object(
            os.getenv("MARKETPLACE_METRICS_FEEDS_JSON", ""),
            label="MARKETPLACE_METRICS_FEEDS_JSON",
        ).items()
        if str(value).strip()
    }
    for platform_id, env_name in (
        ("pay_sh", "PAY_SH_METRICS_API_URL"),
        ("smithery", "SMITHERY_METRICS_API_URL"),
    ):
        if value := os.getenv(env_name, "").strip():
            feeds.setdefault(platform_id, value)
    invalid = [key for key in feeds if not PLATFORM_ID_RE.fullmatch(key)]
    if invalid:
        raise ValueError(f"Invalid platform ids: {', '.join(sorted(invalid))}")
    return feeds


def normalize_metrics_payload(payload: Any) -> dict[str, Any]:
    """Keep a bounded, secret-free metrics object suitable for the command center."""
    if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
        payload = payload["metrics"]
    if not isinstance(payload, dict):
        raise ValueError("Marketplace response must be an object or contain a metrics object")

    normalized: dict[str, Any] = {}
    for raw_key, value in list(payload.items())[:100]:
        key = str(raw_key)[:80]
        if SECRET_KEY_RE.search(key):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            normalized[key] = value
        elif isinstance(value, str):
            normalized[key] = value[:500]
        elif isinstance(value, dict):
            normalized[key] = {
                str(child_key)[:80]: child_value
                for child_key, child_value in list(value.items())[:25]
                if not SECRET_KEY_RE.search(str(child_key))
                and (child_value is None or isinstance(child_value, (bool, int, float, str)))
            }
        elif isinstance(value, list):
            normalized[key] = [
                item if not isinstance(item, str) else item[:500]
                for item in value[:25]
                if item is None or isinstance(item, (bool, int, float, str))
            ]
    if not normalized:
        raise ValueError("Marketplace response contained no safe metric fields")
    normalized["metric_scope"] = "performance"
    return normalized


def smithery_metrics(payload: Any, *, start: datetime, end: datetime) -> dict[str, Any]:
    """Reduce Smithery runtime logs to counts without retaining invocation payloads."""
    if not isinstance(payload, dict):
        raise ValueError("Smithery logs response must be an object")
    invocations = payload.get("invocations")
    if not isinstance(invocations, list):
        invocations = []
    tool_calls: dict[str, int] = {}
    successful = 0
    failed = 0
    for item in invocations[:1000]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("toolName") or item.get("tool_name") or "unknown")[:80]
        tool_calls[tool] = tool_calls.get(tool, 0) + 1
        status = str(item.get("status") or "").lower()
        if status in {"success", "succeeded", "ok", "completed"}:
            successful += 1
        elif status in {"error", "failed", "failure"}:
            failed += 1
    total = payload.get("total")
    return normalize_metrics_payload(
        {
            "runtime_invocations_total": int(total) if isinstance(total, (int, float)) else len(invocations),
            "successful_invocations_in_page": successful,
            "failed_invocations_in_page": failed,
            "tool_calls_in_page": tool_calls,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "source": "smithery_runtime_logs_api",
        }
    )


def load_input(path: str) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("platforms"), dict):
        payload = payload["platforms"]
    if not isinstance(payload, dict):
        raise ValueError("Input file must map platform ids to metrics objects")
    return {
        str(platform_id): normalize_metrics_payload(metrics)
        for platform_id, metrics in payload.items()
        if PLATFORM_ID_RE.fullmatch(str(platform_id))
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--service-url",
        default=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8402"),
    )
    parser.add_argument("--token", default=os.getenv("OBSERVABILITY_DASHBOARD_TOKEN", ""))
    parser.add_argument("--input", help="Offline JSON export mapping platform ids to metrics")
    parser.add_argument("--smithery-qualified-name", default=os.getenv("SMITHERY_QUALIFIED_NAME", ""))
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    token_envs = {
        str(key): str(value)
        for key, value in _json_object(
            os.getenv("MARKETPLACE_METRICS_TOKEN_ENVS_JSON", ""),
            label="MARKETPLACE_METRICS_TOKEN_ENVS_JSON",
        ).items()
    }
    snapshots: dict[str, tuple[dict[str, Any], str | None]] = {}
    if args.input:
        snapshots.update(
            (platform_id, (metrics, None))
            for platform_id, metrics in load_input(args.input).items()
        )

    feeds = configured_feeds()
    failures: list[dict[str, str]] = []
    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        if args.smithery_qualified_name and "smithery" not in feeds:
            smithery_token = os.getenv("SMITHERY_API_KEY", "").strip()
            if not smithery_token:
                failures.append({"platform_id": "smithery", "error": "MissingSmitheryApiKey"})
            else:
                end = datetime.now(UTC)
                start = end - timedelta(hours=max(1, args.window_hours))
                source_url = (
                    "https://api.smithery.ai/servers/"
                    f"{quote(args.smithery_qualified_name, safe='')}/logs"
                )
                try:
                    response = client.get(
                        source_url,
                        headers={"Authorization": f"Bearer {smithery_token}"},
                        params={"from": start.isoformat(), "to": end.isoformat(), "limit": 1000},
                    )
                    response.raise_for_status()
                    snapshots["smithery"] = (
                        smithery_metrics(response.json(), start=start, end=end),
                        source_url,
                    )
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    failures.append({"platform_id": "smithery", "error": type(exc).__name__})

        for platform_id, source_url in feeds.items():
            headers: dict[str, str] = {"Accept": "application/json"}
            token_env = token_envs.get(platform_id)
            if token_env and (feed_token := os.getenv(token_env, "").strip()):
                headers["Authorization"] = f"Bearer {feed_token}"
            try:
                response = client.get(source_url, headers=headers)
                response.raise_for_status()
                snapshots[platform_id] = (normalize_metrics_payload(response.json()), source_url)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                failures.append({"platform_id": platform_id, "error": type(exc).__name__})

        results = []
        for platform_id, (metrics, source_url) in snapshots.items():
            result = {
                "platform_id": platform_id,
                "metric_keys": sorted(metrics),
                "source_configured": bool(source_url),
            }
            if not args.dry_run:
                headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
                try:
                    response = client.post(
                        f"{args.service_url.rstrip('/')}/internal/observability/marketplace-metrics",
                        headers=headers,
                        json={
                            "platform_id": platform_id,
                            "source_url": source_url,
                            "metrics": metrics,
                            "status": "ok",
                        },
                    )
                    response.raise_for_status()
                    result["ingested"] = True
                except httpx.HTTPError as exc:
                    result["ingested"] = False
                    failures.append({"platform_id": platform_id, "error": type(exc).__name__})
            results.append(result)

    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "configured_feeds": sorted(feeds),
                "results": results,
                "failures": failures,
            },
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
