"""Bounded execution for ready RWA sourcing jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.rwa_adapters import RWAAdapterRegistry, RWA_ADAPTER_REGISTRY
from src.rwa_pricing import calculate_block_vwap
from src.rwa_realtime_quality import evaluate_realtime_quality
from src.rwa_sourcing import build_sourcing_jobs


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


def _filter_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = build_sourcing_jobs(include_completed_targets=bool(payload.get("include_completed_targets")))["jobs"]
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


async def probe_sourcing_jobs(
    payload: dict[str, Any],
    *,
    registry: RWAAdapterRegistry = RWA_ADAPTER_REGISTRY,
    store: Any | None = None,
) -> dict[str, Any]:
    """Execute bounded ready-to-probe sourcing jobs with quality checks."""
    limit = max(1, min(int(payload.get("limit") or 5), 10))
    include_order_book = bool(payload.get("include_order_book", True))
    persist = bool(payload.get("persist"))
    block_size_usd = float(payload.get("block_size_usd") or 10_000)
    side = str(payload.get("side") or "buy").lower()
    jobs: list[dict[str, Any]] = []
    for job in _filter_jobs(payload):
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
            bidask = await adapter.fetch_bidask(str(job["symbol"]))
            bidask["timestamp"] = bidask.get("timestamp") or now
            observations.append(bidask)
            result: dict[str, Any] = {
                "job": job,
                "status": "ok",
                "bidask": bidask,
            }
            if include_order_book:
                order_book = await adapter.fetch_order_book(
                    str(job["symbol"]),
                    side=side,
                    depth=int(payload.get("depth") or 100),
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
        except NotImplementedError as exc:
            results.append({"job": job, "status": "not_implemented", "message": str(exc)})
        except Exception as exc:  # pragma: no cover - exercised through adapter-specific tests.
            results.append(
                {
                    "job": job,
                    "status": "error",
                    "error_code": "RWA_SOURCE_PROBE_ERROR",
                    "message": str(exc),
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
        for observation in observations:
            key = (
                str(observation.get("symbol") or "").upper(),
                str(observation.get("venue") or "").lower(),
                str(observation.get("source_type") or ""),
            )
            stored_records.append(
                store.store_observation(
                    {
                        "raw_payload": observation,
                        "normalized_observation": observation,
                        "realtime_quality": quality_by_key.get(key, {}),
                        "metadata": {"product": "rwa_sourcing_probe"},
                    }
                )
            )

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
