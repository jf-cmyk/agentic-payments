"""Safe marketplace-side performance collection.

Listing reachability is handled separately by ``marketplace_health``.  This
module collects reviewed performance inputs without retaining request bodies,
logs, or credentials.  Automatic authenticated collection is intentionally
limited to Smithery's fixed official API host; generic feeds are public-only.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

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


def configured_public_feeds(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    feeds = {
        str(key): str(value).strip()
        for key, value in _json_object(
            env.get("MARKETPLACE_METRICS_FEEDS_JSON", ""),
            label="MARKETPLACE_METRICS_FEEDS_JSON",
        ).items()
        if str(value).strip()
    }
    if value := env.get("PAY_SH_METRICS_API_URL", "").strip():
        feeds.setdefault("pay_sh", value)
    if value := env.get("SMITHERY_METRICS_API_URL", "").strip():
        feeds.setdefault("smithery", value)
    invalid_ids = [key for key in feeds if not PLATFORM_ID_RE.fullmatch(key)]
    if invalid_ids:
        raise ValueError(f"Invalid platform ids: {', '.join(sorted(invalid_ids))}")
    invalid_urls = [
        key
        for key, value in feeds.items()
        if urlsplit(value).scheme != "https" or not urlsplit(value).hostname
    ]
    if invalid_urls:
        raise ValueError(
            "Automatic marketplace feeds must use HTTPS: "
            + ", ".join(sorted(invalid_urls))
        )
    return feeds


def performance_collection_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(
        configured_public_feeds(env)
        or env.get("SMITHERY_QUALIFIED_NAME", "").strip()
    )


def normalize_metrics_payload(payload: Any) -> dict[str, Any]:
    """Keep a bounded, secret-free metrics object suitable for observability."""
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
    """Reduce Smithery runtime logs to aggregate counts only."""
    if not isinstance(payload, dict):
        raise ValueError("Smithery logs response must be an object")
    invocations = payload.get("invocations")
    if not isinstance(invocations, list):
        invocations = []
    tool_calls: dict[str, int] = {}
    request_methods: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    successful = 0
    failed = 0
    for item in invocations[:100]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("toolName") or item.get("tool_name") or "unknown")[:80]
        tool_calls[tool] = tool_calls.get(tool, 0) + 1
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        method = str(request.get("method") or "unknown").upper()[:20]
        request_methods[method] = request_methods.get(method, 0) + 1
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        outcome = str(response.get("outcome") or item.get("status") or "unknown").lower()[:40]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        raw_status = response.get("status")
        status_code = int(raw_status) if isinstance(raw_status, (int, float)) else None
        has_exceptions = bool(item.get("exceptions"))
        if outcome in {"ok", "success", "succeeded", "completed"} or (
            status_code is not None and 200 <= status_code < 400 and not has_exceptions
        ):
            successful += 1
        elif outcome in {"error", "failed", "failure"} or has_exceptions or (
            status_code is not None and status_code >= 400
        ):
            failed += 1
    total = payload.get("total")
    return normalize_metrics_payload(
        {
            "runtime_invocations_total": int(total) if isinstance(total, (int, float)) else len(invocations),
            "runtime_invocations_sampled": min(len(invocations), 100),
            "successful_invocations_in_page": successful,
            "failed_invocations_in_page": failed,
            "tool_calls_in_page": tool_calls,
            "request_methods_in_page": request_methods,
            "outcomes_in_page": outcomes,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "source": "smithery_runtime_logs_api",
        }
    )


async def collect_marketplace_performance(
    *,
    timeout_seconds: float = 20.0,
    window_hours: int = 24,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch configured performance inputs and return secret-free snapshots."""
    env = os.environ if environ is None else environ
    feeds = configured_public_feeds(env)
    snapshots: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    timeout = min(60.0, max(1.0, float(timeout_seconds)))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={"User-Agent": "Blocksize-Marketplace-Metrics/1.0"},
    ) as client:
        qualified_name = env.get("SMITHERY_QUALIFIED_NAME", "").strip()
        if qualified_name and "smithery" not in feeds:
            smithery_token = env.get("SMITHERY_API_KEY", "").strip()
            if not smithery_token:
                failures.append({"platform_id": "smithery", "error": "MissingSmitheryApiKey"})
            else:
                end = datetime.now(UTC)
                start = end - timedelta(hours=max(1, int(window_hours)))
                source_url = (
                    "https://api.smithery.ai/servers/"
                    f"{quote(qualified_name, safe='')}/logs"
                )
                try:
                    response = await client.get(
                        source_url,
                        headers={"Authorization": f"Bearer {smithery_token}"},
                        params={"from": start.isoformat(), "to": end.isoformat(), "limit": 100},
                    )
                    response.raise_for_status()
                    snapshots.append(
                        {
                            "platform_id": "smithery",
                            "source_url": source_url,
                            "status": "ok",
                            "metrics": smithery_metrics(response.json(), start=start, end=end),
                        }
                    )
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    failures.append({"platform_id": "smithery", "error": type(exc).__name__})

        for platform_id, source_url in feeds.items():
            try:
                response = await client.get(source_url, headers={"Accept": "application/json"})
                response.raise_for_status()
                snapshots.append(
                    {
                        "platform_id": platform_id,
                        "source_url": source_url,
                        "status": "ok",
                        "metrics": normalize_metrics_payload(response.json()),
                    }
                )
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                failures.append({"platform_id": platform_id, "error": type(exc).__name__})
    return snapshots, failures
