#!/usr/bin/env python3
"""Measure BlockSize Matrix WebSocket delivery latency, cadence and batch sizes.

Connects to the Matrix WebSocket with the configured API key, subscribes to
bidask / vwap / trade for a few symbols, and for a fixed duration records each
notification row: local receipt time, the row's own timestamp (normalised from
ms / us / s), the gap since the previous notification and the rows per
notification. It then writes a markdown summary.

What the numbers are: ``receipt - row ts`` is exchange (or aggregator) time to
local receipt. Local receipt time is corrected with a clock offset measured
against public NTP servers (SNTP, lowest-RTT sample) before and after the run,
so the result carries an uncertainty of roughly half the best NTP round trip.
Repeat rows (same ts as the previous row for that method and symbol) are
excluded from latency statistics but counted.

The API key is read through src.config and is never printed or written.

Usage:
    .venv/bin/python scripts/measure_ws_latency.py --location "Austin, TX, US"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import socket
import statistics
import struct
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402

NTP_SERVERS = ("time.cloudflare.com", "time.google.com", "pool.ntp.org")
NTP_UNIX_DELTA = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01
SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD")


@dataclass(frozen=True)
class MethodSpec:
    name: str  # short name used in the report
    subscribe: str  # JSON-RPC subscribe method
    notification: str  # notification method name
    row_keys: tuple[str, ...]  # keys that may hold the row list
    ts_key: str
    dedupe: bool  # True when rows can repeat an earlier ts (re-sends)


METHODS = (
    MethodSpec("bidask", "bidask_subscribe", "bidask", ("updates", "snapshot"), "ts", True),
    MethodSpec("vwap", "vwap_subscribe", "vwap", ("updates", "snapshot"), "ts", True),
    MethodSpec("trade", "trade_subscribe", "trade", ("trades", "updates"), "timestamp", False),
)
SPEC_BY_NOTIFICATION = {m.notification: m for m in METHODS}


# --------------------------------------------------------------------------- clock


@dataclass
class ClockSample:
    server: str
    offset_ns: int  # server_time - local_time
    rtt_ns: int


def sntp_sample(server: str, timeout: float = 2.0) -> ClockSample | None:
    packet = b"\x1b" + 47 * b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            addr = (socket.gethostbyname(server), 123)
            t0 = time.time_ns()
            sock.sendto(packet, addr)
            data, _ = sock.recvfrom(512)
            t3 = time.time_ns()
    except OSError:
        return None
    if len(data) < 48:
        return None
    fields = struct.unpack("!12I", data[:48])
    t1 = _ntp_to_ns(fields[8], fields[9])  # server receive
    t2 = _ntp_to_ns(fields[10], fields[11])  # server transmit
    if t1 == 0 or t2 == 0:
        return None
    offset = ((t1 - t0) + (t2 - t3)) // 2
    rtt = (t3 - t0) - (t2 - t1)
    return ClockSample(server, offset, rtt)


def _ntp_to_ns(seconds: int, fraction: int) -> int:
    return (seconds - NTP_UNIX_DELTA) * 1_000_000_000 + (fraction * 1_000_000_000) // (1 << 32)


@dataclass
class ClockSync:
    offset_ns: int
    rtt_ns: int
    server: str
    spread_ns: int  # spread of the best few samples' offsets
    n_samples: int
    at_ns: int


def sync_clock(per_server: int = 8) -> ClockSync | None:
    samples: list[ClockSample] = []
    for server in NTP_SERVERS:
        for _ in range(per_server):
            sample = sntp_sample(server)
            if sample is not None and sample.rtt_ns > 0:
                samples.append(sample)
            time.sleep(0.05)
    if not samples:
        return None
    samples.sort(key=lambda s: s.rtt_ns)
    best = samples[0]
    top = samples[: min(5, len(samples))]
    spread = max(s.offset_ns for s in top) - min(s.offset_ns for s in top)
    return ClockSync(best.offset_ns, best.rtt_ns, best.server, spread, len(samples), time.time_ns())


# --------------------------------------------------------------------------- capture


@dataclass
class Notification:
    recv_ns: int
    method: str
    n_rows: int
    symbols: int


@dataclass
class Row:
    recv_ns: int
    method: str
    symbol: str
    ts_ns: int
    unit: str
    exchange: str
    repeat: bool


@dataclass
class Capture:
    notifications: list[Notification] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    snapshots: dict[str, int] = field(default_factory=dict)  # method -> rows in snapshot
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)  # method -> {code,message}
    subscribed: dict[str, list[str]] = field(default_factory=dict)  # method -> tickers
    unknown_methods: Counter = field(default_factory=Counter)
    reconnects: int = 0
    time_now_checks: list[dict[str, Any]] = field(default_factory=list)
    last_ts: dict[tuple[str, str], int] = field(default_factory=dict)


def normalise_ts(value: Any) -> tuple[int, str] | None:
    """Return (epoch ns, detected unit) for a numeric timestamp."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    if value > 1e14:
        return int(value * 1_000), "us"
    if value > 1e11:
        return int(value * 1_000_000), "ms"
    return int(value * 1_000_000_000), "s"


def decode_params(params: Any) -> dict[str, Any] | None:
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            return None
    return params if isinstance(params, dict) else None


def extract_rows(spec: MethodSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    for key in spec.row_keys:
        rows = params.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def record_rows(cap: Capture, spec: MethodSpec, rows: list[dict[str, Any]], recv_ns: int, *, snapshot: bool) -> int:
    kept = 0
    for row in rows:
        symbol = str(row.get("ticker") or "").upper()
        parsed = normalise_ts(row.get(spec.ts_key))
        if not symbol or parsed is None:
            continue
        ts_ns, unit = parsed
        key = (spec.name, symbol)
        if snapshot:
            cap.last_ts[key] = ts_ns
            continue
        repeat = spec.dedupe and cap.last_ts.get(key) == ts_ns
        cap.last_ts[key] = ts_ns
        cap.rows.append(Row(recv_ns, spec.name, symbol, ts_ns, unit, str(row.get("exchange") or ""), repeat))
        kept += 1
    return kept


def handle_notification(cap: Capture, message: dict[str, Any], recv_ns: int) -> None:
    method = message.get("method")
    spec = SPEC_BY_NOTIFICATION.get(str(method))
    if spec is None:
        cap.unknown_methods[str(method)] += 1
        return
    params = decode_params(message.get("params"))
    if params is None:
        return
    rows = extract_rows(spec, params)
    if not rows:
        return
    symbols = {str(r.get("ticker") or "").upper() for r in rows}
    cap.notifications.append(Notification(recv_ns, spec.name, len(rows), len(symbols)))
    record_rows(cap, spec, rows, recv_ns, snapshot=False)


async def rpc(ws: Any, cap: Capture, method: str, params: dict[str, Any], timeout: float = 15.0) -> Any:
    """Send a request and wait for its reply; notifications arriving meanwhile are recorded."""
    request_id = str(uuid.uuid4())
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
    deadline = time.monotonic() + timeout
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
        recv_ns = time.time_ns()
        message = json.loads(raw)
        if message.get("id") == request_id:
            if "error" in message:
                err = message["error"] or {}
                raise RpcError(err.get("code", -1), str(err.get("message", "error")))
            return message.get("result")
        handle_notification(cap, message, recv_ns)


class RpcError(Exception):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def time_now_check(ws: Any, cap: Capture, label: str, sync: ClockSync | None) -> None:
    try:
        t0 = time.time_ns()
        result = await rpc(ws, cap, "time_now", {})
        t1 = time.time_ns()
    except (RpcError, asyncio.TimeoutError):
        return
    server_s = result.get("timestamp") if isinstance(result, dict) else None
    if not isinstance(server_s, (int, float)):
        return
    mid_corrected = (t0 + t1) // 2 + (sync.offset_ns if sync else 0)
    cap.time_now_checks.append({
        "label": label,
        "server_s": server_s,
        "local_corrected_s": mid_corrected / 1e9,
        "diff_s": mid_corrected / 1e9 - float(server_s),
        "rtt_ms": (t1 - t0) / 1e6,
    })


async def subscribe_all(ws: Any, cap: Capture, symbols: list[str], want_trade: bool) -> None:
    for spec in METHODS:
        if spec.name == "trade" and not want_trade:
            continue
        if spec.name in cap.errors:
            continue  # plan already rejected this method on a previous connection
        try:
            result = await rpc(ws, cap, spec.subscribe, {"tickers": symbols})
            accepted = list(symbols)
        except RpcError as exc:
            if exc.code == 4003:
                cap.errors[spec.name] = {"code": exc.code, "message": exc.message, "scope": "all tickers"}
                print(f"[{spec.name}] rejected by plan (4003): {exc.message}", flush=True)
                continue
            # Retry per ticker so one bad ticker does not hide the rest.
            accepted = []
            for symbol in symbols:
                try:
                    result = await rpc(ws, cap, spec.subscribe, {"tickers": [symbol]})
                    accepted.append(symbol)
                    snap = extract_rows(spec, result) if isinstance(result, dict) else []
                    record_rows(cap, spec, snap, time.time_ns(), snapshot=True)
                    cap.snapshots[spec.name] = cap.snapshots.get(spec.name, 0) + len(snap)
                except RpcError as inner:
                    cap.errors.setdefault(spec.name, {"code": inner.code, "message": inner.message, "scope": symbol})
                    print(f"[{spec.name}/{symbol}] rejected ({inner.code}): {inner.message}", flush=True)
            if accepted:
                cap.subscribed[spec.name] = accepted
            continue
        snap = extract_rows(spec, result) if isinstance(result, dict) else []
        record_rows(cap, spec, snap, time.time_ns(), snapshot=True)
        cap.snapshots[spec.name] = cap.snapshots.get(spec.name, 0) + len(snap)
        cap.subscribed[spec.name] = accepted
        print(f"[{spec.name}] subscribed {','.join(accepted)} (snapshot rows: {len(snap)})", flush=True)


async def capture(duration: float, symbols: list[str], want_trade: bool, sync: ClockSync | None) -> tuple[Capture, float, float, str]:
    import websockets

    cap = Capture()
    start_wall = time.time()
    start_mono = time.monotonic()
    deadline = start_mono + duration
    ws_url = settings.blocksize.ws_url
    first = True
    window_start_mono = start_mono
    while time.monotonic() < deadline:
        try:
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                # Same logon as src/blocksize_stream_cache.py. The key is never logged.
                await rpc(ws, cap, "authentication_logon", {"api_key": settings.blocksize.api_key, "token": ""})
                if first:
                    await time_now_check(ws, cap, "start", sync)
                await subscribe_all(ws, cap, symbols, want_trade)
                if not cap.subscribed:
                    print("No method could be subscribed; stopping.", flush=True)
                    return cap, start_wall, time.time(), "no subscriptions"
                if first:
                    # Measurement window begins once subscriptions are live.
                    window_start_mono = time.monotonic()
                    deadline = window_start_mono + duration
                    first = False
                    start_wall = time.time()
                    print(f"Capturing for {duration:.0f}s ...", flush=True)
                next_progress = time.monotonic() + 60
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await time_now_check(ws, cap, "end", sync)
                        return cap, start_wall, time.time(), "ok"
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        continue
                    recv_ns = time.time_ns()
                    handle_notification(cap, json.loads(raw), recv_ns)
                    if time.monotonic() >= next_progress:
                        next_progress += 60
                        print(f"  {len(cap.rows)} rows, {len(cap.notifications)} notifications so far", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # reconnect and keep measuring
            cap.reconnects += 1
            print(f"Connection problem ({type(exc).__name__}); reconnecting in 2s", flush=True)
            await asyncio.sleep(2)
    return cap, start_wall, time.time(), "ok"


# --------------------------------------------------------------------------- stats


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def fmt(value: float, digits: int = 1) -> str:
    return "n/a" if value != value else f"{value:,.{digits}f}"


def offset_at(recv_ns: int, pre: ClockSync | None, post: ClockSync | None) -> int:
    if pre is None and post is None:
        return 0
    if pre is None or post is None or post.at_ns <= pre.at_ns:
        return (pre or post).offset_ns  # type: ignore[union-attr]
    frac = min(1.0, max(0.0, (recv_ns - pre.at_ns) / (post.at_ns - pre.at_ns)))
    return int(pre.offset_ns + (post.offset_ns - pre.offset_ns) * frac)


GAP_BUCKETS = [(0, 50), (50, 100), (100, 150), (150, 200), (200, 225), (225, 275), (275, 300),
               (300, 400), (400, 500), (500, 750), (750, 1000), (1000, 5000), (5000, 1e12)]


def gap_histogram(gaps_ms: list[float]) -> list[tuple[str, int, float]]:
    out = []
    for lo, hi in GAP_BUCKETS:
        n = sum(1 for g in gaps_ms if lo <= g < hi)
        label = f"{lo:g}-{hi:g} ms" if hi < 1e11 else f">={lo:g} ms"
        out.append((label, n, n / len(gaps_ms) if gaps_ms else 0.0))
    return out


def phase_stats(times_ns: list[int], period_ms: float = 250.0) -> tuple[float, float, float, list[int]]:
    """Concentration of arrival times around a 250 ms clock. Returns (R, p_value, mean phase ms, 10-bin histogram)."""
    if not times_ns:
        return float("nan"), float("nan"), float("nan"), []
    period_ns = int(period_ms * 1e6)
    angles = [2 * math.pi * ((t % period_ns) / period_ns) for t in times_ns]
    c = sum(math.cos(a) for a in angles) / len(angles)
    s = sum(math.sin(a) for a in angles) / len(angles)
    r = math.hypot(c, s)
    n = len(angles)
    p = math.exp(-n * r * r)
    mean_phase = (math.atan2(s, c) % (2 * math.pi)) / (2 * math.pi) * period_ms
    bins = [0] * 10
    for t in times_ns:
        bins[int(((t % period_ns) / period_ns) * 10) % 10] += 1
    return r, p, mean_phase, bins


def near_multiple_share(gaps_ms: list[float], base: float = 250.0, tol: float = 25.0, max_k: int = 8) -> float:
    if not gaps_ms:
        return float("nan")
    hit = 0
    for g in gaps_ms:
        k = round(g / base)
        if 1 <= k <= max_k and abs(g - k * base) <= tol:
            hit += 1
    return hit / len(gaps_ms)


def batch_bucket(n: int) -> str:
    for hi, label in ((1, "1"), (2, "2"), (3, "3"), (5, "4-5"), (10, "6-10"), (20, "11-20"), (50, "21-50")):
        if n <= hi:
            return label
    return "51+"


BATCH_ORDER = ["1", "2", "3", "4-5", "6-10", "11-20", "21-50", "51+"]


# --------------------------------------------------------------------------- report


def build_report(
    cap: Capture,
    args: argparse.Namespace,
    pre: ClockSync | None,
    post: ClockSync | None,
    start_wall: float,
    end_wall: float,
    status: str,
) -> str:
    lines: list[str] = []
    w = lines.append
    started = datetime.fromtimestamp(start_wall, tz=timezone.utc)
    ended = datetime.fromtimestamp(end_wall, tz=timezone.utc)
    duration = end_wall - start_wall

    w(f"# BlockSize Matrix WebSocket latency measurement ({started:%Y-%m-%d})")
    w("")
    w("Generated by `scripts/measure_ws_latency.py`. Latency here is **local receipt time minus the timestamp carried in the row**, "
      "with local time corrected by an NTP offset. The row timestamp is the exchange-provided time of the last trade for VWAP and trades, "
      "and an undocumented origin for bid/ask. Nothing in the feed says when BlockSize sent the message, so these figures are "
      "exchange-to-client totals, not BlockSize-internal or network-only latency.")
    w("")
    w("## Setup")
    w("")
    w(f"- Run window (UTC): {started:%Y-%m-%d %H:%M:%S} to {ended:%H:%M:%S} ({duration:.0f} s of capture, status: {status})")
    w(f"- Endpoint: `{settings.blocksize.ws_url}`; logon via `authentication_logon` with the configured API key (not recorded)")
    w(f"- Machine location: {args.location}")
    w(f"- Host: {platform.node() or 'unknown'}, {platform.system()} {platform.release()}, Python {platform.python_version()}; websockets client over TLS, no proxy")
    w(f"- Symbols: {', '.join(args.symbols)}")
    sub_lines = []
    for spec in METHODS:
        if spec.name in cap.subscribed:
            sub_lines.append(f"{spec.name}: subscribed ({','.join(cap.subscribed[spec.name])}; snapshot rows {cap.snapshots.get(spec.name, 0)})")
        elif spec.name in cap.errors:
            e = cap.errors[spec.name]
            sub_lines.append(f"{spec.name}: **rejected**, error {e['code']} \"{e['message']}\" ({e['scope']})")
        elif spec.name == "trade" and args.no_trade:
            sub_lines.append("trade: skipped (--no-trade)")
        else:
            sub_lines.append(f"{spec.name}: not subscribed")
    for item in sub_lines:
        w(f"- {item}")
    w(f"- Reconnects during the run: {cap.reconnects}")
    if cap.unknown_methods:
        w(f"- Unexpected notification methods ignored: {dict(cap.unknown_methods)}")
    w("")

    w("## Clock synchronisation")
    w("")
    if pre is None:
        w("NTP was unreachable; no offset correction was applied, so absolute latencies below are unreliable.")
    else:
        w("| Point | Best server | Offset (server - local) | Best-sample RTT | Spread of best 5 offsets | Samples |")
        w("|---|---|---:|---:|---:|---:|")
        for label, s in (("Before", pre), ("After", post)):
            if s is None:
                w(f"| {label} | unavailable | | | | |")
            else:
                w(f"| {label} | {s.server} | {s.offset_ns / 1e6:+.2f} ms | {s.rtt_ns / 1e6:.2f} ms | {s.spread_ns / 1e6:.2f} ms | {s.n_samples} |")
        if post is not None:
            w("")
            w(f"Offset drift over the run: {(post.offset_ns - pre.offset_ns) / 1e6:+.2f} ms. Offsets were linearly interpolated per row. "
              f"SNTP cannot resolve path asymmetry, so each absolute latency carries an uncertainty of about +/-{max(pre.rtt_ns, post.rtt_ns) / 2e6:.0f} ms.")
        else:
            w("")
            w(f"Only the pre-run offset was available; uncertainty is about +/-{pre.rtt_ns / 2e6:.0f} ms plus unmeasured drift.")
    if cap.time_now_checks:
        w("")
        w("Cross-check with the API's `time_now` (whole seconds, so only a sanity check): "
          + "; ".join(f"{c['label']}: corrected local {c['local_corrected_s']:.3f} s vs server {c['server_s']} s (diff {c['diff_s']:+.3f} s, RTT {c['rtt_ms']:.0f} ms)" for c in cap.time_now_checks))
    w("")

    # ---- row-level latency
    w("## Latency: receipt minus row timestamp")
    w("")
    w("Repeat rows (same `ts` as the previous row for that method and symbol: keep-alive re-sends and price changes that carry no new timestamp) are excluded; "
      "their counts are shown. Negative values mean the row timestamp is ahead of the corrected local clock, which is within clock uncertainty only if small.")
    w("")
    w("| Method | Symbol | Rows | Repeats | Unit | p50 ms | p95 ms | p99 ms | min ms | max ms | Negative |")
    w("|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    lat: dict[tuple[str, str], list[float]] = defaultdict(list)
    repeats: Counter = Counter()
    units: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in cap.rows:
        key = (r.method, r.symbol)
        units[key][r.unit] += 1
        if r.repeat:
            repeats[key] += 1
            continue
        off = offset_at(r.recv_ns, pre, post)
        lat[key].append((r.recv_ns + off - r.ts_ns) / 1e6)
    per_method: dict[str, list[float]] = defaultdict(list)
    for spec in METHODS:
        for symbol in args.symbols:
            key = (spec.name, symbol)
            vals = lat.get(key, [])
            per_method[spec.name].extend(vals)
            if not vals and key not in repeats:
                continue
            unit = "/".join(f"{u}" for u, _ in units[key].most_common()) or "-"
            neg = sum(1 for v in vals if v < 0)
            w(f"| {spec.name} | {symbol} | {len(vals):,} | {repeats[key]:,} | {unit} | {fmt(pct(vals, 50))} | {fmt(pct(vals, 95))} | {fmt(pct(vals, 99))} | {fmt(min(vals) if vals else float('nan'))} | {fmt(max(vals) if vals else float('nan'))} | {neg} |")
        vals = per_method.get(spec.name, [])
        if vals:
            neg = sum(1 for v in vals if v < 0)
            w(f"| **{spec.name}** | **all** | {len(vals):,} | {sum(repeats[(spec.name, s)] for s in args.symbols):,} | | **{fmt(pct(vals, 50))}** | **{fmt(pct(vals, 95))}** | **{fmt(pct(vals, 99))}** | {fmt(min(vals))} | {fmt(max(vals))} | {neg} |")
    w("")

    # row-timestamp alignment to the 250 ms grid
    w("### Is the row timestamp itself on a 250 ms grid?")
    w("")
    w("If rows were stamped by a 250 ms batch timer, their timestamps would sit on multiples of 250 ms. "
      "For genuine exchange trade times the chance of landing on a 250 ms multiple is 0.4% at millisecond resolution.")
    w("")
    w("| Method | Rows checked | ts is a multiple of 250 ms | Share | Distinct ts mod 1000 ms (top 4) |")
    w("|---|---:|---:|---:|---|")
    for spec in METHODS:
        rows = [r for r in cap.rows if r.method == spec.name and not r.repeat]
        if not rows:
            continue
        aligned = sum(1 for r in rows if (r.ts_ns // 1_000_000) % 250 == 0 and r.ts_ns % 1_000_000 == 0)
        mods = Counter((r.ts_ns // 1_000_000) % 1000 for r in rows).most_common(4)
        w(f"| {spec.name} | {len(rows):,} | {aligned:,} | {aligned / len(rows):.1%} | {', '.join(f'{m}: {c}' for m, c in mods)} |")
    w("")

    # trade by exchange
    trade_rows = [r for r in cap.rows if r.method == "trade" and not r.repeat]
    if trade_rows:
        w("### Trade latency by exchange")
        w("")
        w("| Exchange | Trades | p50 ms | p95 ms | p99 ms |")
        w("|---|---:|---:|---:|---:|")
        by_ex: dict[str, list[float]] = defaultdict(list)
        for r in trade_rows:
            by_ex[r.exchange or "(none)"].append((r.recv_ns + offset_at(r.recv_ns, pre, post) - r.ts_ns) / 1e6)
        for ex, vals in sorted(by_ex.items(), key=lambda kv: -len(kv[1])):
            if len(vals) >= 20:
                w(f"| {ex} | {len(vals):,} | {fmt(pct(vals, 50))} | {fmt(pct(vals, 95))} | {fmt(pct(vals, 99))} |")
        w("")

    # ---- cadence
    w("## Update cadence")
    w("")
    w("A notification is one WebSocket message for a method. Gaps are between consecutive notifications of the same method (any symbol). "
      "'Within 25 ms of k x 250' counts gaps near 250, 500, 750 ... ms; an unstructured process would put about 20% of gaps there.")
    w("")
    w("| Method | Notifications | Rate /s | Gap p50 ms | Gap p95 ms | Gap p99 ms | Within 25 ms of k x 250 | Phase concentration R (250 ms) | p-value |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    notes_by_method: dict[str, list[Notification]] = defaultdict(list)
    for n in cap.notifications:
        notes_by_method[n.method].append(n)
    hist_blocks = []
    for spec in METHODS:
        notes = notes_by_method.get(spec.name, [])
        if len(notes) < 2:
            continue
        times = [n.recv_ns for n in notes]
        gaps = [(b - a) / 1e6 for a, b in zip(times, times[1:])]
        r, p, mean_phase, bins = phase_stats(times)
        rate = len(notes) / duration if duration else float("nan")
        w(f"| {spec.name} | {len(notes):,} | {rate:.2f} | {fmt(pct(gaps, 50))} | {fmt(pct(gaps, 95))} | {fmt(pct(gaps, 99))} | {near_multiple_share(gaps):.1%} | {r:.3f} | {p:.2g} |")
        hist_blocks.append((spec.name, gaps, mean_phase, bins))
    w("")
    w("Phase concentration R is 0 for arrivals spread evenly across a 250 ms cycle and 1 for arrivals at one phase; "
      "it measures whether notifications line up on a fixed 250 ms clock, independent of the gap distribution.")
    w("")
    for name, gaps, mean_phase, bins in hist_blocks:
        w(f"**{name}: notification gap histogram** (mean arrival phase within a 250 ms cycle: {mean_phase:.0f} ms)")
        w("")
        w("| Gap | Count | Share |")
        w("|---|---:|---:|")
        for label, n, share in gap_histogram(gaps):
            if n:
                w(f"| {label} | {n:,} | {share:.1%} |")
        w("")
        w(f"Arrival phase histogram, 10 bins of 25 ms: {bins}")
        w("")

    # per symbol gaps (row level)
    w("### Per-symbol update gaps")
    w("")
    w("Time between consecutive notifications that contain a non-repeat row for the symbol.")
    w("")
    w("| Method | Symbol | Updates | Gap p50 ms | Gap p95 ms | Gap p99 ms | Within 25 ms of k x 250 |")
    w("|---|---|---:|---:|---:|---:|---:|")
    for spec in METHODS:
        for symbol in args.symbols:
            times = sorted({r.recv_ns for r in cap.rows if r.method == spec.name and r.symbol == symbol and not r.repeat})
            if len(times) < 2:
                continue
            gaps = [(b - a) / 1e6 for a, b in zip(times, times[1:])]
            w(f"| {spec.name} | {symbol} | {len(times):,} | {fmt(pct(gaps, 50))} | {fmt(pct(gaps, 95))} | {fmt(pct(gaps, 99))} | {near_multiple_share(gaps):.1%} |")
    w("")

    # ---- batch size
    w("## Batch size (rows per notification)")
    w("")
    w("| Method | Notifications | Mean rows | p50 | p95 | Max | Mean distinct symbols | " + " | ".join(BATCH_ORDER) + " |")
    w("|---|---:|---:|---:|---:|---:|---:|" + "---:|" * len(BATCH_ORDER))
    for spec in METHODS:
        notes = notes_by_method.get(spec.name, [])
        if not notes:
            continue
        sizes = [n.n_rows for n in notes]
        buckets = Counter(batch_bucket(s) for s in sizes)
        cells = " | ".join(f"{buckets.get(b, 0) / len(sizes):.1%}" if buckets.get(b) else "-" for b in BATCH_ORDER)
        w(f"| {spec.name} | {len(notes):,} | {statistics.fmean(sizes):.2f} | {fmt(pct([float(s) for s in sizes], 50), 0)} | {fmt(pct([float(s) for s in sizes], 95), 0)} | {max(sizes)} | {statistics.fmean(n.symbols for n in notes):.2f} | {cells} |")
    w("")
    w("Bucket columns are the share of notifications with that many rows.")
    w("")
    return "\n".join(lines)


def write_raw(cap: Capture, path: Path, pre: ClockSync | None, post: ClockSync | None) -> None:
    with path.open("w") as fh:
        fh.write("recv_ns,corrected_latency_ms,method,symbol,ts_ns,unit,exchange,repeat\n")
        for r in cap.rows:
            off = offset_at(r.recv_ns, pre, post)
            fh.write(f"{r.recv_ns},{(r.recv_ns + off - r.ts_ns) / 1e6:.3f},{r.method},{r.symbol},{r.ts_ns},{r.unit},{r.exchange},{int(r.repeat)}\n")


# --------------------------------------------------------------------------- main


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--duration", type=float, default=300.0, help="capture seconds (default 300)")
    p.add_argument("--symbols", default=",".join(SYMBOLS), help="comma-separated tickers")
    p.add_argument("--no-trade", action="store_true", help="skip trade_subscribe")
    p.add_argument("--location", default="unspecified", help="machine location to state in the report")
    p.add_argument("--out", default=None, help="markdown output path (default reports/ws_latency_measurement_<date>.md)")
    p.add_argument("--raw-out", default=None, help="optional CSV of every row (keep outside the repo)")
    args = p.parse_args()
    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return args


def main() -> int:
    args = parse_args()
    print("Syncing clock against NTP ...", flush=True)
    pre = sync_clock()
    if pre is None:
        print("NTP unreachable (UDP 123 blocked?); aborting rather than reporting uncorrected latencies.", flush=True)
        return 2
    print(f"  offset {pre.offset_ns / 1e6:+.2f} ms via {pre.server}, best RTT {pre.rtt_ns / 1e6:.1f} ms", flush=True)

    cap, start_wall, end_wall, status = asyncio.run(capture(args.duration, args.symbols, not args.no_trade, pre))

    print("Re-syncing clock ...", flush=True)
    post = sync_clock()
    if post is not None:
        print(f"  offset {post.offset_ns / 1e6:+.2f} ms (drift {(post.offset_ns - pre.offset_ns) / 1e6:+.2f} ms)", flush=True)

    report = build_report(cap, args, pre, post, start_wall, end_wall, status)
    out = Path(args.out) if args.out else ROOT / "reports" / f"ws_latency_measurement_{datetime.fromtimestamp(start_wall, tz=timezone.utc):%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    if args.raw_out:
        write_raw(cap, Path(args.raw_out), pre, post)
    print(f"Wrote {out} ({len(cap.rows)} rows, {len(cap.notifications)} notifications)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
