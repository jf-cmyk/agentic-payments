#!/usr/bin/env python3
"""Audit every Blocksize instrument for real, fresh, non-zero live data.

Read-only against the upstream API. Enumerates the instrument catalog per
service (vwap, bidask, fx, metal, state), probes each symbol once with the
matching ``BlocksizeClient`` call under bounded concurrency, classifies the
result, and writes:

  docs/gtm/instrument_quality_audit_<YYYY-MM-DD>.csv
  docs/gtm/instrument_quality_audit_<YYYY-MM-DD>.md

Classifications:

  ok                     positive price values and an upstream timestamp
                         younger than ``--stale-seconds``
  zero_or_missing_values price fields are zero/None, or the upstream payload
                         carried no timestamp (the client substitutes "now"
                         in that case, which would otherwise mask staleness)
  stale                  upstream timestamp older than ``--stale-seconds``
  error                  exception (class + truncated message) or timeout

Never prints or writes the API key. Idempotent: re-running on the same day
overwrites that day's report.

Usage:
  .venv/bin/python scripts/audit_instrument_quality.py
  .venv/bin/python scripts/audit_instrument_quality.py --limit 5
  .venv/bin/python scripts/audit_instrument_quality.py --services vwap,bidask
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient  # noqa: E402
from src import vwap_coverage  # noqa: E402
from src.blocksize_stream_cache import (  # noqa: E402
    BlocksizeStreamCache,
    _normalize as _normalize_ws_symbol,
)
try:
    from src.config import settings  # noqa: E402
except Exception:  # noqa: BLE001 - a missing key raises at import time
    settings = None  # type: ignore[assignment]

SERVICES = ("vwap", "bidask", "fx", "metal", "state")
CLASSIFICATIONS = ("ok", "zero_or_missing_values", "stale", "error")
ASSET_CLASS_LABELS = {
    ("vwap", "crypto"): "crypto VWAP",
    ("bidask", "crypto"): "crypto bid/ask",
    ("bidask", "equity"): "equity bid/ask",
    ("fx", "fx"): "FX",
    ("metal", "metal"): "metals",
    ("state", "crypto"): "state (websocket aggregate, pool fallback)",
}
TIMESTAMP_KEYS = ("ts", "timestamp", "block_time", "time")
ERROR_MESSAGE_MAX = 160

# Per-task capture of raw RPC results so staleness can be judged from the
# upstream payload rather than from the client's parsed (and defaulted) time.
_raw_results: contextvars.ContextVar[list[tuple[str, Any]] | None] = contextvars.ContextVar(
    "raw_results", default=None
)


class AuditClient(BlocksizeClient):
    """BlocksizeClient that records raw RPC results and caches state catalog."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state_instruments_cache: list[dict[str, Any]] | None = None

    async def _rpc_call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        result = await super()._rpc_call(method, params)
        bucket = _raw_results.get()
        if bucket is not None:
            bucket.append((method, result))
        return result

    async def list_state_instruments(self) -> list[dict[str, Any]]:
        # get_state_price() re-lists the catalog for every symbol; cache it so a
        # full state audit does not hammer the catalog endpoint.
        if self._state_instruments_cache is None:
            self._state_instruments_cache = await super().list_state_instruments()
        return self._state_instruments_cache


@dataclass
class ProbeResult:
    service: str
    asset_class: str
    symbol: str
    classification: str
    primary_value: float | None = None
    bid: float | None = None
    ask: float | None = None
    age_seconds: float | None = None
    timestamp_utc: str = ""
    timestamp_source: str = ""  # upstream | synthesized | none
    error_type: str = ""
    error_message: str = ""
    latency_ms: int = 0
    rpc_calls: int = 0
    notes: str = ""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Catalog:
    service: str
    symbols: list[str]
    asset_class_of: dict[str, str] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _truncate(text: str, limit: int = ERROR_MESSAGE_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _upstream_timestamp_present(raw_results: list[tuple[str, Any]]) -> bool:
    """True if any recorded price payload carried a timestamp-like field."""

    def walk(node: Any, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(node, dict):
            if any(node.get(key) not in (None, "", 0) for key in TIMESTAMP_KEYS):
                return True
            return any(walk(value, depth + 1) for value in node.values())
        if isinstance(node, list):
            return any(walk(item, depth + 1) for item in node[:500])
        return False

    for method, result in raw_results:
        if method.endswith("_instruments"):
            continue
        if walk(result):
            return True
    return False


def _classify(
    *,
    values_ok: bool,
    timestamp: datetime | None,
    upstream_ts: bool,
    stale_seconds: float,
    now: datetime,
) -> tuple[str, float | None, str, str]:
    """Return (classification, age_seconds, timestamp_source, note)."""
    notes: list[str] = []
    if timestamp is None:
        return "zero_or_missing_values", None, "none", "no timestamp on model"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (now - timestamp).total_seconds()
    if not upstream_ts:
        notes.append("upstream payload had no timestamp; client substituted now()")
        return "zero_or_missing_values", None, "synthesized", "; ".join(notes)
    if not values_ok:
        notes.append("price fields zero or missing")
        return "zero_or_missing_values", age, "upstream", "; ".join(notes)
    if age > stale_seconds:
        return "stale", age, "upstream", f"age {age:.0f}s > {stale_seconds:.0f}s"
    if age < -stale_seconds:
        return "stale", age, "upstream", f"timestamp {abs(age):.0f}s in the future"
    return "ok", age, "upstream", ""


# ---------------------------------------------------------------------------
# Catalog enumeration
# ---------------------------------------------------------------------------


async def _enumerate(client: AuditClient, service: str) -> Catalog:
    try:
        if service == "vwap":
            symbols = await client.list_vwap_instruments()
            return Catalog(service, sorted(set(symbols)), {s: "crypto" for s in symbols})
        if service == "bidask":
            entries = await client._list_bidask_entries()
            asset_class_of: dict[str, str] = {}
            for entry in entries:
                ticker = entry["ticker"]
                if client._is_fx_entry(entry):
                    asset_class_of[ticker] = "fx"
                elif client._is_metal_entry(entry):
                    asset_class_of[ticker] = "metal"
                elif client._is_equity_like_entry(entry):
                    asset_class_of[ticker] = "equity"
                else:
                    asset_class_of[ticker] = "crypto"
            return Catalog(service, sorted(asset_class_of), asset_class_of)
        if service == "fx":
            symbols = await client.list_fx_instruments()
            return Catalog(service, sorted(set(symbols)), {s: "fx" for s in symbols})
        if service == "metal":
            symbols = await client.list_metal_instruments()
            return Catalog(service, sorted(set(symbols)), {s: "metal" for s in symbols})
        if service == "state":
            instruments = await client.list_state_instruments()
            symbols = sorted(
                {
                    str(item.get("symbol")).upper()
                    for item in instruments
                    if isinstance(item, dict) and item.get("symbol")
                }
            )
            return Catalog(service, symbols, {s: "crypto" for s in symbols})
    except Exception as exc:  # noqa: BLE001 - report, do not crash the audit
        return Catalog(service, [], {}, error=f"{type(exc).__name__}: {_truncate(str(exc))}")
    raise ValueError(f"unknown service {service}")


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def _extract(service: str, model: Any) -> tuple[bool, float | None, float | None, float | None, str]:
    """Return (values_ok, primary, bid, ask, note) for a parsed client model."""
    if service == "vwap":
        primary = model.vwap
        return _positive(primary), primary, None, None, ""
    if service == "bidask":
        bid, ask = model.bid, model.ask
        note = ""
        if _positive(bid) and _positive(ask) and ask < bid:
            note = "crossed market (ask < bid)"
        return _positive(bid) and _positive(ask), getattr(model, "mid", None), bid, ask, note
    if service == "fx":
        bid, ask, mid = model.bid, model.ask, model.mid
        ok = (_positive(bid) and _positive(ask)) or _positive(mid)
        note = "" if (_positive(bid) and _positive(ask)) else "bid/ask missing, mid only"
        return ok, mid, bid, ask, note if ok else ""
    if service == "metal":
        return _positive(model.price), model.price, None, None, ""
    if service == "state":
        return _positive(model.price), model.price, None, None, ""
    raise ValueError(service)


async def _probe(
    client: AuditClient,
    service: str,
    asset_class: str,
    symbol: str,
    *,
    call: Callable[[str], Awaitable[Any]],
    timeout: float,
    stale_seconds: float,
    semaphore: asyncio.Semaphore,
) -> ProbeResult:
    async with semaphore:
        token = _raw_results.set([])
        started = time.perf_counter()
        try:
            model = await asyncio.wait_for(call(symbol), timeout=timeout)
            raw = _raw_results.get() or []
            latency = int((time.perf_counter() - started) * 1000)
            values_ok, primary, bid, ask, note = _extract(service, model)
            if service == "state":
                methods = {method for method, _ in raw}
                source_note = (
                    "source=state_subscribe" if "state_subscribe" in methods else "source=state_pool_http"
                )
                note = f"{source_note}; {note}" if note else source_note
            classification, age, ts_source, cnote = _classify(
                values_ok=values_ok,
                timestamp=getattr(model, "timestamp", None),
                upstream_ts=_upstream_timestamp_present(raw),
                stale_seconds=stale_seconds,
                now=datetime.now(timezone.utc),
            )
            ts = getattr(model, "timestamp", None)
            return ProbeResult(
                service=service,
                asset_class=asset_class,
                symbol=symbol,
                classification=classification,
                primary_value=primary,
                bid=bid,
                ask=ask,
                age_seconds=round(age, 1) if age is not None else None,
                timestamp_utc=ts.isoformat() if ts else "",
                timestamp_source=ts_source,
                latency_ms=latency,
                rpc_calls=len(raw),
                notes="; ".join(n for n in (note, cnote) if n),
            )
        except asyncio.TimeoutError:
            return ProbeResult(
                service, asset_class, symbol, "error",
                error_type="TimeoutError",
                error_message=f"no response within {timeout:.0f}s",
                latency_ms=int((time.perf_counter() - started) * 1000),
                rpc_calls=len(_raw_results.get() or []),
            )
        except BlocksizeAPIError as exc:
            return ProbeResult(
                service, asset_class, symbol, "error",
                error_type=f"BlocksizeAPIError[{exc.code}]",
                # Upstream puts the useful detail ("ticker X not found") in `data`.
                error_message=_truncate(
                    f"{exc.message} {exc.data}" if exc.data not in (None, "") else exc.message
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
                rpc_calls=len(_raw_results.get() or []),
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                service, asset_class, symbol, "error",
                error_type=type(exc).__name__,
                error_message=_truncate(str(exc) or repr(exc)),
                latency_ms=int((time.perf_counter() - started) * 1000),
                rpc_calls=len(_raw_results.get() or []),
            )
        finally:
            _raw_results.reset(token)


def _call_for(
    client: AuditClient,
    service: str,
    state_cache: BlocksizeStreamCache | None = None,
) -> Callable[[str], Awaitable[Any]]:
    if service == "state":
        return _state_call(client, state_cache)
    return {
        "vwap": client.get_vwap_latest,
        "bidask": client.get_bidask_snapshot,
        "fx": client.get_fx_rate,
        "metal": client.get_metal_price,
    }[service]


def _state_call(
    client: AuditClient, state_cache: BlocksizeStreamCache | None
) -> Callable[[str], Awaitable[Any]]:
    """Probe state the way production serves it: websocket aggregate first, pool HTTP second.

    `/v1/state/{pair}` reads the `state_subscribe` websocket cache first and only
    falls back to `state_instruments` + `state_pool` over HTTP. The HTTP path
    reports the pool's last on-chain `block_time`, which is days old for quiet
    pools, so auditing it alone misreports live symbols as stale.
    """

    async def call(symbol: str) -> Any:
        if state_cache is not None:
            try:
                model = await state_cache.get_state_price(symbol)
            except BlocksizeAPIError:
                model = None
            if model is not None:
                bucket = _raw_results.get()
                item = state_cache._state.get(_normalize_ws_symbol(symbol))
                if bucket is not None and item is not None:
                    bucket.append(("state_subscribe", item.payload))
                return model
        return await client.get_state_price(symbol)

    return call


async def _start_state_cache(
    client: AuditClient, symbols: list[str], *, wait_seconds: float, log: Callable[..., None]
) -> BlocksizeStreamCache | None:
    """Subscribe to state_subscribe for every catalog symbol and wait for the first snapshot."""
    cache = BlocksizeStreamCache(
        rest_client=client,
        enabled=True,
        state_tickers=symbols,
        state_mode="configured",
        max_state_tickers=max(len(symbols), 1),
        ttl_seconds=3600,
    )
    try:
        await cache.start()
        deadline = time.perf_counter() + wait_seconds
        while time.perf_counter() < deadline:
            if cache._ready.is_set() and cache._state:
                break
            await asyncio.sleep(0.5)
        else:
            log("[state] websocket snapshot did not arrive; falling back to state_pool HTTP only")
            await cache.stop()
            return None
        # Give streaming updates a moment so ages reflect the live feed, not the snapshot.
        await asyncio.sleep(3)
        log(f"[state] websocket cache ready with {len(cache._state)} aggregate rows")
        return cache
    except Exception as exc:  # noqa: BLE001
        log(f"[state] websocket cache unavailable ({type(exc).__name__}); using state_pool HTTP only")
        await cache.stop()
        return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _verdict(total: int, ok: int) -> tuple[str, str]:
    """Map an ok-rate to a public-claim verdict."""
    if total == 0:
        return "NO DATA", "catalog empty or unreachable; do not claim"
    rate = ok / total
    if rate >= 0.95 and ok >= 3:
        return "SAFE", f"{ok}/{total} ok ({rate:.0%}); claim is safe with the non-ok list excluded"
    if rate >= 0.50:
        return "ALLOWLIST ONLY", f"{ok}/{total} ok ({rate:.0%}); only claim for the audited ok symbols"
    if ok > 0:
        return "NOT SAFE", f"{ok}/{total} ok ({rate:.0%}); coverage too thin to claim publicly"
    return "NOT SAFE", f"0/{total} ok; no symbol produced fresh non-zero data"


def write_csv(path: Path, results: list[ProbeResult]) -> None:
    fields = list(ProbeResult.__dataclass_fields__)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in sorted(results, key=lambda r: (r.service, r.asset_class, r.symbol)):
            writer.writerow(result.as_row())


def write_markdown(
    path: Path,
    *,
    results: list[ProbeResult],
    catalogs: dict[str, Catalog],
    args: argparse.Namespace,
    started_at: datetime,
    finished_at: datetime,
    csv_path: Path,
) -> str:
    by_service: dict[str, Counter[str]] = defaultdict(Counter)
    by_class: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for result in results:
        by_service[result.service][result.classification] += 1
        by_class[(result.service, result.asset_class)][result.classification] += 1

    lines: list[str] = []
    lines.append("# Blocksize Instrument Quality Audit")
    lines.append("")
    lines.append(f"Audit date: {args.report_date} (local); run started {started_at.isoformat(timespec='seconds')} UTC")
    lines.append("")
    lines.append("Methodology:")
    lines.append("")
    lines.append(
        "- Enumerated every instrument per service through `BlocksizeClient` "
        "(`list_vwap_instruments`, `bidask_instruments` catalog split by the client's "
        "fx/metal/equity heuristics, `list_fx_instruments`, `list_metal_instruments`, "
        "`list_state_instruments`)."
    )
    lines.append(
        "- Probed each symbol once with the matching client call "
        "(`get_vwap_latest`, `get_bidask_snapshot`, `get_fx_rate`, `get_metal_price`)."
    )
    lines.append(
        "- State was probed the way `/v1/state/{pair}` serves it: the `state_subscribe` "
        "websocket aggregate first, then the `state_instruments` + `state_pool` HTTP "
        "fallback. Each state row's `notes` column records which source answered. The "
        "HTTP path reports the pool's last on-chain `block_time`, which is legitimately "
        "old for quiet pools, so it is not evidence of a stale live feed."
        if not getattr(args, "state_http_only", False)
        else "- State was probed through the `state_instruments` + `state_pool` HTTP fallback only "
        "(`--state-http-only`); ages reflect the pool's last on-chain `block_time`, not the "
        "live websocket aggregate."
    )
    lines.append(
        f"- Concurrency {args.concurrency}, per-call timeout {args.timeout:.0f}s, "
        f"stale threshold {args.stale_seconds:.0f}s."
    )
    lines.append(
        "- Freshness is judged from the raw upstream payload. When the payload carries no "
        "timestamp the client substitutes the current time, so those rows are classified as "
        "`zero_or_missing_values` (timestamp_source = synthesized) rather than trusted as fresh."
    )
    lines.append(
        "- Read-only: no writes to the upstream API, no product code changed. "
        "No secrets or authentication material were printed or written."
    )
    if args.limit:
        lines.append(f"- DRY RUN: limited to the first {args.limit} symbols per service.")
    if args.services != list(SERVICES):
        lines.append(f"- Services filtered to: {', '.join(args.services)}.")
    lines.append(f"- Started {started_at.isoformat(timespec='seconds')}, finished "
                 f"{finished_at.isoformat(timespec='seconds')} "
                 f"({(finished_at - started_at).total_seconds():.0f}s).")
    try:
        evidence_path = csv_path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        evidence_path = csv_path
    lines.append(f"- Per-symbol evidence: `{evidence_path}`.")
    lines.append("")

    lines.append("## Catalog sizes")
    lines.append("")
    lines.append("| Service | Instruments enumerated | Probed | Catalog error |")
    lines.append("| --- | ---: | ---: | --- |")
    for service in args.services:
        catalog = catalogs.get(service)
        probed = sum(by_service[service].values())
        lines.append(
            f"| {service} | {len(catalog.symbols) if catalog else 0} | {probed} | "
            f"{catalog.error if catalog and catalog.error else ''} |"
        )
    lines.append("")

    lines.append("## Counts per service and classification")
    lines.append("")
    header = "| Service | " + " | ".join(CLASSIFICATIONS) + " | total | ok rate |"
    lines.append(header)
    lines.append("| --- | " + " | ".join("---:" for _ in CLASSIFICATIONS) + " | ---: | ---: |")
    for service in args.services:
        counts = by_service[service]
        total = sum(counts.values())
        rate = f"{counts['ok'] / total:.0%}" if total else "n/a"
        lines.append(
            f"| {service} | " + " | ".join(str(counts[c]) for c in CLASSIFICATIONS)
            + f" | {total} | {rate} |"
        )
    lines.append("")

    lines.append("## Counts per asset class")
    lines.append("")
    lines.append("| Service | Asset class | " + " | ".join(CLASSIFICATIONS) + " | total |")
    lines.append("| --- | --- | " + " | ".join("---:" for _ in CLASSIFICATIONS) + " | ---: |")
    for (service, asset_class), counts in sorted(by_class.items()):
        lines.append(
            f"| {service} | {asset_class} | "
            + " | ".join(str(counts[c]) for c in CLASSIFICATIONS)
            + f" | {sum(counts.values())} |"
        )
    lines.append("")

    lines.append("## Verdict per asset class")
    lines.append("")
    lines.append(
        "Thresholds: SAFE when at least 95% of the audited symbols are `ok` (minimum 3); "
        "ALLOWLIST ONLY when 50-95% are `ok`; NOT SAFE below 50%; NO DATA when nothing was probed."
    )
    lines.append("")
    lines.append("| Asset class | Verdict | Evidence | Public accuracy claim |")
    lines.append("| --- | --- | --- | --- |")
    verdicts: dict[str, tuple[str, str]] = {}
    for key, label in ASSET_CLASS_LABELS.items():
        if key[0] not in args.services:
            continue
        counts = by_class.get(key, Counter())
        total = sum(counts.values())
        verdict, evidence = _verdict(total, counts["ok"])
        verdicts[label] = (verdict, evidence)
        claim = {
            "SAFE": "Yes",
            "ALLOWLIST ONLY": "Only for the listed ok symbols",
            "NOT SAFE": "No",
            "NO DATA": "No",
        }[verdict]
        lines.append(f"| {label} | **{verdict}** | {evidence} | {claim} |")
    lines.append("")

    # Showcase candidates: ok on both crypto VWAP and crypto bid/ask.
    ok_vwap = {r.symbol for r in results if r.service == "vwap" and r.classification == "ok"}
    ok_bidask = {
        r.symbol for r in results
        if r.service == "bidask" and r.asset_class == "crypto" and r.classification == "ok"
    }
    both = sorted(ok_vwap & ok_bidask)
    lines.append("## SHOWCASE_LIVE_SYMBOLS candidates")
    lines.append("")
    lines.append(
        "Symbols that returned fresh, non-zero data on BOTH crypto VWAP and crypto bid/ask "
        "in this run. Only symbols from this list should be added to `SHOWCASE_LIVE_SYMBOLS`."
    )
    lines.append("")
    if both:
        lines.append(f"Count: {len(both)}")
        lines.append("")
        lines.append("```")
        lines.append(",".join(both))
        lines.append("```")
    else:
        lines.append("None qualified in this run.")
    lines.append("")

    lines.append("## Non-ok symbols")
    lines.append("")
    non_ok = [r for r in results if r.classification != "ok"]
    if not non_ok:
        lines.append("All probed symbols returned ok.")
    for service in args.services:
        for classification in ("error", "zero_or_missing_values", "stale"):
            rows = sorted(
                (r for r in non_ok if r.service == service and r.classification == classification),
                key=lambda r: (r.asset_class, r.symbol),
            )
            if not rows:
                continue
            lines.append(f"### {service} / {classification} ({len(rows)})")
            lines.append("")
            if classification == "error":
                lines.append("| Symbol | Asset class | Error | Message |")
                lines.append("| --- | --- | --- | --- |")
                for r in rows:
                    lines.append(
                        f"| {r.symbol} | {r.asset_class} | {r.error_type} | "
                        f"{r.error_message.replace('|', '/')} |"
                    )
            else:
                lines.append("| Symbol | Asset class | Value | Bid | Ask | Age (s) | Timestamp source | Notes |")
                lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
                for r in rows:
                    lines.append(
                        f"| {r.symbol} | {r.asset_class} | {_fmt(r.primary_value)} | {_fmt(r.bid)} | "
                        f"{_fmt(r.ask)} | {_fmt(r.age_seconds)} | {r.timestamp_source} | {r.notes} |"
                    )
            lines.append("")

    # Error summary by type for quick reading.
    error_types = Counter((r.service, r.error_type) for r in results if r.classification == "error")
    if error_types:
        lines.append("## Error types")
        lines.append("")
        lines.append("| Service | Error type | Count |")
        lines.append("| --- | --- | ---: |")
        for (service, error_type), count in sorted(error_types.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {service} | {error_type} | {count} |")
        lines.append("")

    lines.append("## Re-running")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv/bin/python scripts/audit_instrument_quality.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "Flags: `--limit N` (cheap dry run), `--services vwap,bidask,fx,metal,state`, "
        "`--stale-seconds 300`, `--timeout 30`, `--concurrency 8`, `--out-dir docs/gtm`, "
        "`--state-http-only` (audit the pool HTTP fallback instead of the websocket aggregate)."
    )
    lines.append("")

    text = "\n".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    return f"{value:.6g}"


# ---------------------------------------------------------------------------
# VWAP live-coverage gate output
# ---------------------------------------------------------------------------


def build_vwap_coverage(
    results: list[ProbeResult], *, stale_seconds: float, generated_at: datetime, source: str
) -> dict[str, Any]:
    """Turn the vwap probe rows into the gate file read by src/vwap_coverage.py.

    Only engine-level "ticker not found" errors become `unavailable`; timeouts and
    transient upstream errors never blacklist a symbol.
    """
    vwap_rows = [r for r in results if r.service == "vwap"]
    live = sorted(r.symbol for r in vwap_rows if r.classification == "ok")
    low_activity = {
        r.symbol: round(float(r.age_seconds), 1)
        for r in sorted(vwap_rows, key=lambda r: r.symbol)
        if r.classification == "stale" and r.age_seconds is not None
    }
    # -32603 "internal error" on vwap_latest is how the engine reports a catalog
    # ticker it has no aggregate for; the detail field reads "ticker X not found"
    # and the websocket vwap_subscribe returns no snapshot row for it either.
    # Timeouts and other error types never blacklist a symbol.
    unavailable = sorted(
        r.symbol
        for r in vwap_rows
        if r.classification == "error"
        and (
            "not found" in (r.error_message or "").lower()
            or r.error_type == "BlocksizeAPIError[-32603]"
        )
    )
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "source": source,
        "stale_seconds": stale_seconds,
        "catalog_size": len(vwap_rows),
        "live": live,
        "low_activity": low_activity,
        "unavailable": unavailable,
        "notes": [
            "Generated by scripts/audit_instrument_quality.py; do not edit by hand.",
            "unavailable = upstream vwap_latest returned -32603 'ticker not found' (catalog/engine mismatch); verified absent from vwap_subscribe too.",
            "low_activity = VWAP served but last trade older than stale_seconds at audit time.",
        ],
    }


def write_vwap_coverage(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def results_from_csv(csv_path: Path) -> list[ProbeResult]:
    """Rebuild probe rows from a previous run's CSV (for regenerating the gate file)."""
    rows: list[ProbeResult] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                ProbeResult(
                    service=raw["service"],
                    asset_class=raw["asset_class"],
                    symbol=raw["symbol"],
                    classification=raw["classification"],
                    age_seconds=float(raw["age_seconds"]) if raw.get("age_seconds") else None,
                    error_type=raw.get("error_type", ""),
                    error_message=raw.get("error_message", ""),
                    notes=raw.get("notes", ""),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--services", default=",".join(SERVICES),
                        help=f"comma-separated subset of {','.join(SERVICES)}")
    parser.add_argument("--limit", type=int, default=0,
                        help="probe at most N symbols per service (0 = all)")
    parser.add_argument("--stale-seconds", type=float, default=300.0,
                        help="timestamps older than this are classified stale")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-symbol timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=8, help="max in-flight probes")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "docs" / "gtm"))
    parser.add_argument("--date", default=None, help="override the report date (YYYY-MM-DD)")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--state-http-only", action="store_true",
                        help="audit state through state_instruments+state_pool HTTP only (the production fallback path)")
    parser.add_argument("--state-ws-wait", type=float, default=60.0,
                        help="seconds to wait for the first state_subscribe websocket snapshot")
    parser.add_argument("--coverage-out", default=str(vwap_coverage.PACKAGED_COVERAGE_PATH),
                        help="where to write the VWAP live-coverage gate file after a full vwap run")
    parser.add_argument("--no-coverage-out", action="store_true",
                        help="do not write the VWAP live-coverage gate file")
    parser.add_argument("--coverage-from-csv", default=None,
                        help="rebuild only the coverage gate file from a previous run's CSV and exit")
    args = parser.parse_args(argv)
    services = [s.strip().lower() for s in args.services.split(",") if s.strip()]
    unknown = [s for s in services if s not in SERVICES]
    if unknown:
        parser.error(f"unknown services: {', '.join(unknown)}")
    args.services = [s for s in SERVICES if s in services]
    return args


def _api_key_configured() -> bool:
    if settings is None:
        return False
    try:
        key = settings.blocksize.api_key
    except Exception:  # noqa: BLE001
        return False
    return bool(key) and "your_blocksize_api_key" not in key.lower()


async def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc)
    report_date = args.date or datetime.now().astimezone().date().isoformat()
    args.report_date = report_date
    out_dir = Path(args.out_dir)
    csv_path = out_dir / f"instrument_quality_audit_{report_date}.csv"
    md_path = out_dir / f"instrument_quality_audit_{report_date}.md"

    client = AuditClient(timeout=args.timeout)
    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[ProbeResult] = []
    catalogs: dict[str, Catalog] = {}
    state_caches: list[BlocksizeStreamCache | None] = []
    log = (lambda *a, **k: None) if args.quiet else (lambda *a, **k: print(*a, **k, file=sys.stderr, flush=True))

    try:
        for service in args.services:
            catalog = await _enumerate(client, service)
            catalogs[service] = catalog
            if catalog.error:
                log(f"[{service}] catalog error: {catalog.error}")
                continue
            symbols = catalog.symbols[: args.limit] if args.limit else catalog.symbols
            log(f"[{service}] {len(catalog.symbols)} instruments, probing {len(symbols)}")
            state_cache = None
            if service == "state" and not args.state_http_only:
                state_cache = await _start_state_cache(
                    client, symbols, wait_seconds=args.state_ws_wait, log=log
                )
                state_caches.append(state_cache)
            call = _call_for(client, service, state_cache)
            tasks = [
                _probe(
                    client, service, catalog.asset_class_of.get(symbol, "unknown"), symbol,
                    call=call, timeout=args.timeout, stale_seconds=args.stale_seconds,
                    semaphore=semaphore,
                )
                for symbol in symbols
            ]
            done = 0
            for future in asyncio.as_completed(tasks):
                results.append(await future)
                done += 1
                if done % 100 == 0 or done == len(tasks):
                    log(f"[{service}] {done}/{len(tasks)} probed")
    finally:
        for cache in state_caches:
            if cache is not None:
                await cache.stop()
        await client.close()

    finished_at = datetime.now(timezone.utc)
    write_csv(csv_path, results)
    if "vwap" in args.services and not args.limit and not args.no_coverage_out:
        coverage_path = Path(args.coverage_out)
        write_vwap_coverage(
            coverage_path,
            build_vwap_coverage(
                results,
                stale_seconds=args.stale_seconds,
                generated_at=finished_at,
                source=str(csv_path),
            ),
        )
        print(f"wrote {coverage_path}")
    write_markdown(
        md_path, results=results, catalogs=catalogs, args=args,
        started_at=started_at, finished_at=finished_at, csv_path=csv_path,
    )

    # Compact stdout summary; the markdown has the detail.
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        counts[r.service][r.classification] += 1
    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")
    for service in args.services:
        c = counts[service]
        print(f"{service:7s} " + " ".join(f"{k}={c[k]}" for k in CLASSIFICATIONS))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.coverage_from_csv:
        csv_path = Path(args.coverage_from_csv)
        results = results_from_csv(csv_path)
        payload = build_vwap_coverage(
            results,
            stale_seconds=args.stale_seconds,
            generated_at=datetime.now(timezone.utc),
            source=str(csv_path),
        )
        write_vwap_coverage(Path(args.coverage_out), payload)
        print(
            f"wrote {args.coverage_out}: live={len(payload['live'])} "
            f"low_activity={len(payload['low_activity'])} unavailable={len(payload['unavailable'])}"
        )
        return 0
    if not _api_key_configured():
        print(
            "BLOCKSIZE_API_KEY is not configured (environment or .env). "
            "Set it and re-run; no credentials are guessed.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
