"""Bounded execution for ready RWA sourcing jobs."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from src.rwa_adapters import RWAAdapterRegistry, RWA_ADAPTER_REGISTRY
from src.rwa_pricing import calculate_block_vwap
from src.rwa_realtime_quality import evaluate_realtime_quality
from src.rwa_security import public_probe_error_message
from src.rwa_sourcing import build_sourcing_jobs


_SETTING_ATTRIBUTE_BY_ENV = {
    "RWA_PROBE_MAX_CONCURRENCY": "rwa_probe_max_concurrency",
    "RWA_PROBE_CALL_TIMEOUT_SECONDS": "rwa_probe_call_timeout_seconds",
    "RWA_PROBE_TOTAL_TIMEOUT_SECONDS": "rwa_probe_total_timeout_seconds",
}


def _raw_config_value(name: str, default: int | float) -> Any:
    if name in os.environ:
        return os.environ[name]
    try:
        from src.config import settings

        attribute = _SETTING_ATTRIBUTE_BY_ENV.get(name)
        return getattr(settings.server, attribute, default) if attribute else default
    except (AttributeError, ImportError):
        return default


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_raw_config_value(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(_raw_config_value(name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(minimum, min(value, maximum))


_PROBE_SEMAPHORE = asyncio.Semaphore(
    _bounded_int_env("RWA_PROBE_MAX_CONCURRENCY", 2, 1, 8)
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _symbol_match_keys(value: Any) -> set[str]:
    raw = str(value or "").strip().upper().replace("-", "/").replace(" ", "")
    if not raw:
        return set()
    keys = {raw}
    if "/" in raw:
        base, quote = raw.split("/", 1)
        if base:
            keys.add(base)
            if quote in {"USD", "USDC", "USDT", "USDT0"}:
                keys.update({f"{base}/USD", f"{base}USD", f"{base}/USDC", f"{base}USDC"})
    else:
        for quote in ("USDT0", "USDC", "USDT", "USD"):
            if raw.endswith(quote) and len(raw) > len(quote):
                base = raw[: -len(quote)]
                keys.update({base, f"{base}/USD", f"{base}USD", f"{base}/USDC", f"{base}USDC"})
                break
    return keys


@lru_cache(maxsize=2)
def _cached_sourcing_jobs(include_completed_targets: bool) -> tuple[dict[str, Any], ...]:
    """Build the static sourcing plan once so probes only perform cheap filtering."""
    result = build_sourcing_jobs(include_completed_targets=include_completed_targets)
    return tuple(result["jobs"])


def warm_sourcing_job_cache() -> None:
    """Precompute both operator probe plans before the service becomes ready."""
    _cached_sourcing_jobs(False)
    _cached_sourcing_jobs(True)


def _filter_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = _cached_sourcing_jobs(bool(payload.get("include_completed_targets")))
    venue_filter = {str(item).strip().lower() for item in payload.get("venues", []) if str(item).strip()}
    symbol_filter = set().union(*(_symbol_match_keys(item) for item in payload.get("symbols", [])))
    job_id_filter = {str(item).strip() for item in payload.get("job_ids", []) if str(item).strip()}
    filtered: list[dict[str, Any]] = []
    for job in jobs:
        if job["status"] != "ready_to_probe":
            continue
        if venue_filter and str(job["venue"]).lower() not in venue_filter:
            continue
        job_symbol_keys = _symbol_match_keys(job.get("symbol")) | _symbol_match_keys(job.get("asset_id"))
        if symbol_filter and not (job_symbol_keys & symbol_filter):
            continue
        if job_id_filter and str(job["job_id"]) not in job_id_filter:
            continue
        filtered.append(job)
    return filtered


async def _probe_sourcing_jobs_inner(
    payload: dict[str, Any],
    *,
    registry: RWAAdapterRegistry = RWA_ADAPTER_REGISTRY,
    store: Any | None = None,
    persistence_timeout_seconds: float = 5.0,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Execute bounded ready-to-probe sourcing jobs with quality checks."""
    limit = max(1, min(int(payload.get("limit") or 5), 10))
    include_order_book = bool(payload.get("include_order_book", True))
    persist = bool(payload.get("persist"))
    block_size_usd = float(payload.get("block_size_usd") or 10_000)
    side = str(payload.get("side") or "buy").lower()
    jobs: list[dict[str, Any]] = []
    filtered_jobs = _filter_jobs(payload)
    for job in filtered_jobs:
        try:
            registry.get(str(job["venue"]))
        except KeyError:
            continue
        jobs.append(job)
        if len(jobs) >= limit:
            break
    now = _utc_now_iso()
    results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    for job in jobs:
        try:
            adapter = registry.get(str(job["venue"]))
            call_timeout = _bounded_float_env(
                "RWA_PROBE_CALL_TIMEOUT_SECONDS",
                10.0,
                0.1,
                30.0,
            )
            bidask = await asyncio.wait_for(
                adapter.fetch_bidask(str(job["symbol"])), timeout=call_timeout
            )
            bidask["timestamp"] = bidask.get("timestamp") or now
            observations.append(bidask)
            result: dict[str, Any] = {
                "job": job,
                "status": "ok",
                "bidask": bidask,
            }
            if include_order_book:
                order_book = await asyncio.wait_for(
                    adapter.fetch_order_book(
                        str(job["symbol"]),
                        side=side,
                        depth=max(1, min(int(payload.get("depth") or 100), 200)),
                    ),
                    timeout=call_timeout,
                )
                order_book["timestamp"] = order_book.get("timestamp") or now
                observations.append(order_book)
                result["order_book"] = order_book
                result["block_vwap"] = calculate_block_vwap(
                    {
                        **order_book,
                        "block_size_usd": block_size_usd,
                    }
                )
            results.append(result)
        except NotImplementedError:
            results.append(
                {
                    "job": job,
                    "status": "not_implemented",
                    "message": "The selected adapter operation is not implemented.",
                }
            )
        except Exception as exc:  # pragma: no cover - exercised through adapter-specific tests.
            results.append(
                {
                    "job": job,
                    "status": "error",
                    "error_code": "RWA_SOURCE_PROBE_ERROR",
                    "message": public_probe_error_message(exc),
                }
            )

    quality = (
        evaluate_realtime_quality({"now": now, "observations": observations})
        if observations
        else {"aggregate_status": "no_observations", "observations": []}
    )

    stored_records: list[dict[str, Any]] = []
    if persist and store is not None:
        quality_by_key = {
            (row["symbol"], row["venue"], row["source_type"]): row
            for row in quality.get("observations", [])
            if isinstance(row, dict)
        }
        pending_records: list[dict[str, Any]] = []
        for observation in observations:
            key = (
                str(observation.get("symbol") or "").upper(),
                str(observation.get("venue") or "").lower(),
                str(observation.get("source_type") or ""),
            )
            pending_records.append(
                {
                    "raw_payload": observation,
                    "normalized_observation": observation,
                    "realtime_quality": quality_by_key.get(key, {}),
                    "metadata": {"product": "rwa_sourcing_probe"},
                }
            )
        if pending_records:
            remaining = (
                deadline_monotonic - time.monotonic()
                if deadline_monotonic is not None
                else persistence_timeout_seconds
            )
            if remaining <= 0.02:
                raise TimeoutError("RWA probe has no time remaining for persistence")
            lock_timeout = max(0.01, min((remaining - 0.01) / 2, 5.0))
            persistence_task = asyncio.create_task(
                asyncio.to_thread(
                    store.store_observations_batch,
                    pending_records,
                    lock_timeout_seconds=lock_timeout,
                    deadline_monotonic=deadline_monotonic,
                    ingestion_source="sourcing_probe",
                )
            )
            try:
                stored_records = await asyncio.shield(persistence_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(persistence_task)
                except Exception:
                    pass
                raise
            except sqlite3.Error as exc:
                raise ValueError("RWA evidence persistence is temporarily unavailable") from exc

    return {
        "summary": {
            "requested_limit": limit,
            "jobs_selected": len(jobs),
            "jobs_succeeded": len([item for item in results if item["status"] == "ok"]),
            "observations": len(observations),
            "persisted": len(stored_records),
        },
        "quality": quality,
        "results": results,
        "stored_observations": stored_records,
    }


async def probe_sourcing_jobs(
    payload: dict[str, Any],
    *,
    registry: RWAAdapterRegistry = RWA_ADAPTER_REGISTRY,
    store: Any | None = None,
) -> dict[str, Any]:
    """Run one bounded probe under global concurrency and wall-clock ceilings."""
    total_timeout = _bounded_float_env(
        "RWA_PROBE_TOTAL_TIMEOUT_SECONDS",
        30.0,
        0.1,
        60.0,
    )
    deadline = time.monotonic() + total_timeout
    async with asyncio.timeout(total_timeout):
        async with _PROBE_SEMAPHORE:
            return await _probe_sourcing_jobs_inner(
                payload,
                registry=registry,
                store=store,
                persistence_timeout_seconds=total_timeout,
                deadline_monotonic=deadline,
            )
