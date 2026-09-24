"""Audit-derived live-coverage gate for the real-time VWAP catalog.

Why this exists: upstream `vwap_instruments` lists every pair for which any
venue or pool exists (7,148 on 2026-09-24), but the VWAP engine only carries an
aggregate for pairs that actually trade. On that date 2,391 catalog tickers
returned "ticker not found" and 2,237 more had not traded for over five
minutes, mostly single-pool DEX pairs. Advertising them makes an agent pick a
pair that then fails or pays for an hours-old print.

`scripts/audit_instrument_quality.py` probes every ticker and writes
`src/data/vwap_live_coverage.json`. This module reads that file and lets the
catalog, search, list, and payment-preflight surfaces:

- drop tickers the engine does not know (`unavailable`), so they are never
  advertised and never reach a payment challenge;
- label pairs whose last trade is older than the audit's stale threshold as
  `low_activity`, and steer the recommendation to bid/ask when it exists.

The gate only ever removes symbols explicitly listed as unavailable. A missing
or disabled file means no filtering, so a stale audit can never hide a pair
that the engine does serve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from src.config import settings

PACKAGED_COVERAGE_PATH = Path(__file__).parent / "data" / "vwap_live_coverage.json"

STATUS_LIVE = "live"
STATUS_LOW_ACTIVITY = "low_activity"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNAUDITED = "unaudited"


def normalise_symbol(value: str) -> str:
    return str(value).replace("-", "").replace("/", "").replace("_", "").upper()


@dataclass(frozen=True)
class VwapCoverage:
    generated_at: str
    stale_seconds: float
    catalog_size: int
    live: frozenset[str]
    low_activity: dict[str, float]  # symbol -> last-trade age in seconds at audit time
    unavailable: frozenset[str]
    source: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def status(self, symbol: str) -> str:
        clean = normalise_symbol(symbol)
        if clean in self.unavailable:
            return STATUS_UNAVAILABLE
        if clean in self.low_activity:
            return STATUS_LOW_ACTIVITY
        if clean in self.live:
            return STATUS_LIVE
        return STATUS_UNAUDITED


def coverage_path() -> Path:
    configured = (getattr(settings.server, "vwap_coverage_path", "") or "").strip()
    return Path(configured) if configured else PACKAGED_COVERAGE_PATH


@lru_cache(maxsize=4)
def _load(path_str: str, mtime: float) -> VwapCoverage | None:
    try:
        payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    low = payload.get("low_activity") or {}
    if isinstance(low, list):  # tolerate a bare list
        low = {str(item): 0.0 for item in low}
    return VwapCoverage(
        generated_at=str(payload.get("generated_at") or ""),
        stale_seconds=float(payload.get("stale_seconds") or 0.0),
        catalog_size=int(payload.get("catalog_size") or 0),
        live=frozenset(normalise_symbol(s) for s in payload.get("live") or []),
        low_activity={normalise_symbol(k): float(v or 0.0) for k, v in dict(low).items()},
        unavailable=frozenset(normalise_symbol(s) for s in payload.get("unavailable") or []),
        source=str(payload.get("source") or ""),
        notes=tuple(str(n) for n in payload.get("notes") or []),
    )


def coverage() -> VwapCoverage | None:
    """Return the loaded gate, or None when disabled or no audit file exists."""
    if not getattr(settings.server, "vwap_coverage_gate_enabled", True):
        return None
    path = coverage_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _load(str(path), mtime)


def vwap_status(symbol: str) -> str:
    gate = coverage()
    return gate.status(symbol) if gate else STATUS_UNAUDITED


def last_trade_age_seconds(symbol: str) -> float | None:
    gate = coverage()
    if gate is None:
        return None
    return gate.low_activity.get(normalise_symbol(symbol))


def filter_vwap_instruments(tickers: Iterable[str]) -> list[str]:
    """Drop tickers the VWAP engine reported as not found; keep everything else."""
    gate = coverage()
    if gate is None or not gate.unavailable:
        return list(tickers)
    return [t for t in tickers if normalise_symbol(t) not in gate.unavailable]


def gate_metadata(*, excluded_count: int | None = None) -> dict[str, Any]:
    """Describe the gate for catalog metadata and the coverage route."""
    gate = coverage()
    if gate is None:
        return {
            "enabled": False,
            "reason": (
                "disabled by configuration"
                if not getattr(settings.server, "vwap_coverage_gate_enabled", True)
                else "no audit file; catalog is the raw upstream list"
            ),
        }
    payload: dict[str, Any] = {
        "enabled": True,
        "audited_at": gate.generated_at,
        "stale_seconds": gate.stale_seconds,
        "audited_catalog_size": gate.catalog_size,
        "live_symbols": len(gate.live),
        "low_activity_symbols": len(gate.low_activity),
        "unavailable_symbols_removed": len(gate.unavailable),
        "definitions": {
            "live": "Returned a fresh VWAP within stale_seconds at audit time.",
            "low_activity": (
                "VWAP engine serves the pair, but its last trade was older than "
                "stale_seconds at audit time; prefer bid/ask when available."
            ),
            "unavailable": (
                "Listed upstream but the VWAP engine reported 'ticker not found'; "
                "removed from discovery and rejected before any payment."
            ),
        },
    }
    if excluded_count is not None:
        payload["excluded_from_this_listing"] = excluded_count
    return payload
