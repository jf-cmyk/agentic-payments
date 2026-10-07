"""Operator usage insights: calls by avenue and ticker, unique users, paid calls.

The command-center summary in ``observability.py`` loads every event of the
window into Python. This module answers the operator's everyday questions with
grouped SQL instead, so the dashboard stays fast at hundreds of thousands of
events per month:

* which avenue a call arrived through (direct MCP, x402 HTTP, Claude, OpenAI,
  Cursor, Smithery, Glama, Pay.sh), with monitors and probes kept apart;
* which tickers are requested and which ones are paid for;
* how many distinct clients call, and how many come back;
* how many calls were paid, and whether those payments look scripted;
* an assessment with an improvement plan derived from the same numbers.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from src.claude_data_retention import CLAUDE_SURFACES, RETENTION_DAYS
from src.published_examples import SELECTION_SOURCE as PUBLISHED_EXAMPLE_SOURCE
from src.observability import (
    KNOWN_MONITOR_USER_AGENT_MARKERS,
    LIVE_DATA_MCP_TOOLS,
    UsageEventStore,
    fingerprint,
)


# Avenues in a fixed order. The dashboard colors follow this order, so an
# avenue keeps its color whatever the traffic mix is.
CHANNELS: list[tuple[str, str]] = [
    ("x402_http", "x402 HTTP API"),
    ("direct_mcp", "Direct MCP"),
    ("claude", "Claude"),
    ("openai", "OpenAI / ChatGPT"),
    ("cursor", "Cursor"),
    ("pay_sh", "Pay.sh"),
    ("smithery", "Smithery"),
    ("glama", "Glama"),
    ("monitor", "Monitors & probes"),
    ("other", "Other / unknown"),
]
CHANNEL_LABELS = dict(CHANNELS)

# Surfaces whose HTTP requests are product calls or MCP transport, as opposed
# to registry crawls, the developer portal and marketing redirects.
CALL_SURFACES = (
    "http_api",
    "public_mcp",
    "local_mcp",
    "anthropic_mcp",
    "claude_mcp",
    "cursor_mcp",
    "openai_mcp",
)
MCP_SURFACES = frozenset(CALL_SURFACES) - {"http_api"}

LIVE_DATA_PATH_PREFIXES = (
    "/v1/vwap",
    "/v1/bidask/",
    "/v1/state/",
    "/v1/fx/",
    "/v1/metal/",
    "/v1/batch",
    "/v1/briefs/",
    "/v1/checks/",
    "/v1/receipts/",
    "/v1/snapshots/",
    "/v1/monitors/",
    "/v1/indicators/",
    "/v1/signals/",
    "/v1/rwa/benchmark/",
)
TICKER_MCP_TOOLS = LIVE_DATA_MCP_TOOLS | {"get_market_data_endpoint"}
SEARCH_MCP_TOOLS = frozenset({"search_pairs", "search"})

# User agents that identify probes, uptime checks and crawlers. This extends
# the shared monitor list with agents seen in production that it misses
# (enclave402 verifier, TridentStatus, generic bots and crawlers).
MONITOR_MARKERS = tuple(KNOWN_MONITOR_USER_AGENT_MARKERS) + (
    "verifier",
    "status/",
    "bot/",
    "bot;",
    "crawler",
    "spider",
    "enclave402",
    "proofbench",
    "checkly",
    "betteruptime",
    "better-uptime",
)
X402_DIRECTORY_HOST_MARKERS = ("x402scan", "x402list", "x402.org", "agenteconomy")
TICKER_QUOTES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "CHF")
INTERNAL_WALLETS_ENV = "OBSERVABILITY_INTERNAL_PAYER_WALLETS"
PAYMENT_EVENTS = (
    "payment_proof_submitted",
    "payment_authorization_verified",
    "payment_verified",
    "payment_settled",
    "payment_failed",
    "data_delivered",
    "charged_delivery_failed",
    "credit_drawdown_success",
    "mcp_credit_drawdown_success",
    "mcp_credit_drawdown_failed",
    "mcp_data_delivered",
    "mcp_tool_error",
)
BURST_GAP_SECONDS = 600
BURST_MIN_SIZE = 3
CACHE_TTL_SECONDS = 120
# Findings need at least this many calls in the window before they fire.
MIN_SAMPLE = 100

_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


def _meta(key: str) -> str:
    return (
        "CASE WHEN json_valid(metadata_json) "
        f"THEN json_extract(metadata_json, '$.{key}') END"
    )


def _truthy_meta(key: str) -> str:
    return f"COALESCE({_meta(key)}, 0) NOT IN (0, '')"


# Mirrors UsageEventStore._is_synthetic_event so test traffic stays out.
NOT_SYNTHETIC_SQL = f"""
    NOT ({_truthy_meta('synthetic')})
    AND NOT ({_truthy_meta('test')})
    AND NOT ({_truthy_meta('mock')})
    AND lower(COALESCE(user_agent, '')) NOT LIKE '%testclient%'
    AND lower(COALESCE(user_agent, '')) NOT LIKE '%smoke%'
    AND lower(COALESCE(user_agent, '')) NOT LIKE '%synthetic%'
    AND lower(COALESCE(subject, '')) NOT LIKE 'mock\\_%' ESCAPE '\\'
    AND lower(COALESCE(subject, '')) NOT LIKE 'test\\_%' ESCAPE '\\'
"""


def is_monitor_user_agent(user_agent: str | None) -> bool:
    value = (user_agent or "").lower()
    return bool(value) and any(marker in value for marker in MONITOR_MARKERS)


def channel_for(
    *,
    surface: str | None,
    user_agent: str | None,
    referrer: str | None = None,
    utm_source: str | None = None,
) -> str:
    """Attribute one call to the avenue it arrived through."""
    surface = surface or ""
    ua = (user_agent or "").lower()
    if is_monitor_user_agent(ua):
        return "monitor"
    if surface in {"anthropic_mcp", "claude_mcp"} or "claude" in ua or "anthropic" in ua:
        return "claude"
    if surface == "openai_mcp" or "chatgpt" in ua or "openai" in ua:
        return "openai"
    if surface == "cursor_mcp" or "cursor" in ua:
        return "cursor"
    haystack = " ".join([ua, (referrer or "").lower(), (utm_source or "").lower()])
    if "smithery" in haystack:
        return "smithery"
    if "glama" in haystack:
        return "glama"
    if any(marker in haystack for marker in ("pay.sh", "pay-sh", "paysh", "pay-skills")):
        return "pay_sh"
    if surface in MCP_SURFACES:
        return "direct_mcp"
    if surface == "http_api":
        return "x402_http"
    return "other"


def canonical_ticker(raw: str | None) -> str | None:
    """Fold BTCUSD, BTC/USD and vwap:BTC-USD into one BTC-USD row."""
    if not raw:
        return None
    value = raw.strip().upper()
    if ":" in value:
        value = value.split(":", 1)[1]
    value = value.replace("/", "-").replace("_", "-")
    if not value or len(value) > 24 or not all(ch.isalnum() or ch in ".-" for ch in value):
        return None
    if "-" in value:
        base, _, quote = value.partition("-")
        return f"{base}-{quote}" if base and quote and "-" not in quote else None
    for quote in TICKER_QUOTES:
        if value.endswith(quote) and len(value) > len(quote) + 1:
            return f"{value[: -len(quote)]}-{quote}"
    return value


def _tickers_in(subject: str | None) -> list[str]:
    if not subject:
        return []
    tickers = []
    for part in subject.split(","):
        ticker = canonical_ticker(part)
        if ticker:
            tickers.append(ticker)
    return tickers


def _network_label(network: str | None) -> str:
    value = (network or "").lower()
    if value.startswith("solana"):
        return "Solana"
    if value in {"eip155:8453", "base"}:
        return "Base"
    if value.startswith("eip155:"):
        return f"EVM {value.split(':', 1)[1]}"
    return network or "unknown"


def _referrer_host(referrer: str | None) -> str:
    if not referrer:
        return ""
    try:
        return urlparse(referrer).netloc.lower()
    except ValueError:
        return ""


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def internal_wallet_hashes() -> set[str]:
    """Hashes of payer wallets the operator marked as their own test wallets."""
    hashes: set[str] = set()
    for raw in os.environ.get(INTERNAL_WALLETS_ENV, "").split(","):
        wallet = raw.strip()
        if not wallet:
            continue
        for candidate in {wallet, wallet.lower()}:
            if hashed := fingerprint(candidate):
                hashes.add(hashed)
    return hashes


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def _day_list(start: datetime, days: int) -> list[str]:
    first = start.date()
    return [(first + timedelta(days=offset)).isoformat() for offset in range(days)]


class _Window:
    """Accumulates call-level counters for one time window."""

    def __init__(self) -> None:
        self.calls = 0
        self.monitor_calls = 0
        self.mcp_tool_calls = 0
        self.live_data_calls = 0
        self.channel_calls: Counter[str] = Counter()
        self.channel_users: dict[str, set[str]] = defaultdict(set)
        self.users: set[str] = set()
        self.user_days: dict[str, set[str]] = defaultdict(set)
        self.client_requests: Counter[str] = Counter()
        self.http_requests = 0
        self.status: Counter[str] = Counter()
        self.error_endpoints: Counter[tuple[str, int]] = Counter()
        self.daily_channel_calls: dict[str, Counter[str]] = defaultdict(Counter)
        self.daily_users: dict[str, set[str]] = defaultdict(set)
        self.ticker_calls: dict[str, Counter[str]] = defaultdict(Counter)
        self.ticker_monitor_calls: Counter[str] = Counter()
        # Calls whose URL carried selection_source=published_example_path: a
        # copied example from docs or a listing, not a chosen instrument.
        self.ticker_example_calls: Counter[str] = Counter()
        self.ticker_mcp_lookups: Counter[str] = Counter()
        self.search_terms: Counter[str] = Counter()
        self.tool_calls: Counter[str] = Counter()
        self.user_agents: Counter[str] = Counter()
        self.user_agent_channel: dict[str, str] = {}


def _status_bucket(status_code: int | None) -> str | None:
    if status_code is None:
        return None
    if status_code == 402:
        return "payment_required"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "ok"


class UsageInsights:
    """Build the operator usage view from the shared SQLite event store."""

    def __init__(self, store: UsageEventStore) -> None:
        self.store = store

    def build(self, *, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
        cache_key = (self.store.db_path, days)
        cached = _CACHE.get(cache_key)
        if now is None and cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        result = self._build(days=days, now=now or datetime.now(UTC))
        if now is None:
            _CACHE[cache_key] = (time.monotonic(), result)
        return result

    def _build(self, *, days: int, now: datetime) -> dict[str, Any]:
        current_start = now - timedelta(days=days)
        previous_start = now - timedelta(days=2 * days)
        current_start_iso = current_start.isoformat()

        with self.store._connect() as conn:
            call_rows = self._call_rows(conn, previous_start.isoformat())
            payment_rows = conn.execute(
                f"""
                SELECT timestamp, event, surface, endpoint, method, status_code,
                       latency_ms, user_agent, referrer, wallet_hash, subject,
                       asset_class, price_usdc, network, reason, tool_name,
                       metadata_json
                FROM usage_events
                WHERE timestamp >= ?
                  AND event IN ({",".join("?" for _ in PAYMENT_EVENTS)})
                ORDER BY timestamp ASC, id ASC
                """,
                (previous_start.isoformat(), *PAYMENT_EVENTS),
            ).fetchall()
            identity_rows = conn.execute(
                f"""
                SELECT timestamp, surface, {_meta('identity_hash')} AS identity_hash
                FROM usage_events
                WHERE timestamp >= ?
                  AND metadata_json LIKE '%identity_hash%'
                  AND {_meta('identity_trust')} IN ('verified_oauth', 'verified_beta', 'verified_x402')
                """,
                (previous_start.isoformat(),),
            ).fetchall()
            p95_latency = self._p95_latency(conn, current_start_iso)

        # Claude connector events are deleted after RETENTION_DAYS. When the
        # previous window reaches past that, compare like for like: leave those
        # surfaces out of both sides of every period-over-period figure, while
        # the current window's own totals still include them.
        comparison_excludes_claude = 2 * days > RETENTION_DAYS
        current, previous = _Window(), _Window()
        current_cmp, previous_cmp = (
            (_Window(), _Window()) if comparison_excludes_claude else (current, previous)
        )
        for row in call_rows:
            is_current = row["day"] >= current_start_iso[:10]
            # The boundary day belongs to both windows by date; split it by the
            # exact first timestamp of the group instead.
            if row["day"] == current_start_iso[:10]:
                is_current = row["first_ts"] >= current_start_iso
            # The full previous window is read only when it is also the
            # comparison window; otherwise previous_cmp takes its place.
            if is_current or not comparison_excludes_claude:
                self._accumulate(current if is_current else previous, row)
            if comparison_excludes_claude and row["surface"] not in CLAUDE_SURFACES:
                self._accumulate(current_cmp if is_current else previous_cmp, row)

        payments_current, payments_previous = self._payments(
            payment_rows, current_start_iso
        )
        identities_current = {
            row["identity_hash"]
            for row in identity_rows
            if row["identity_hash"] and row["timestamp"] >= current_start_iso
        }
        comparable_identity_rows = [
            row for row in identity_rows
            if not comparison_excludes_claude or row["surface"] not in CLAUDE_SURFACES
        ]
        identities_current_cmp = {
            row["identity_hash"]
            for row in comparable_identity_rows
            if row["identity_hash"] and row["timestamp"] >= current_start_iso
        }
        identities_previous_cmp = {
            row["identity_hash"]
            for row in comparable_identity_rows
            if row["identity_hash"] and row["timestamp"] < current_start_iso
        }

        kpis = self._kpis(
            current,
            current_cmp,
            previous_cmp,
            payments_current,
            payments_previous,
            len(identities_current),
            len(identities_current_cmp),
            len(identities_previous_cmp),
            p95_latency,
        )
        channels = self._channels(current, payments_current, days)
        tickers = self._tickers(current, payments_current)
        users = self._users(
            current, current_cmp, previous_cmp, len(identities_current), payments_current
        )
        activity = self._activity(current, payments_current, current_start, days)
        health = self._health(current)
        paid = self._paid(payments_current)
        result = {
            "generated_at": now.isoformat(),
            "window_days": days,
            "window_start": current_start_iso,
            "retention": {
                "claude_days": RETENTION_DAYS,
                "claude_surfaces": list(CLAUDE_SURFACES),
                "window_exceeds_claude_retention": days > RETENTION_DAYS,
                "comparison_excludes_claude": comparison_excludes_claude,
            },
            "totals": {
                "calls": current.calls,
                "monitor_calls": current.monitor_calls,
                "live_data_calls": current.live_data_calls,
                "mcp_tool_calls": current.mcp_tool_calls,
                "http_requests": current.http_requests,
            },
            "definitions": DEFINITIONS,
            "channel_order": [{"id": cid, "label": label} for cid, label in CHANNELS],
            "kpis": kpis,
            "channels": channels,
            "tickers": tickers,
            "search_terms": [
                {"term": term, "count": count}
                for term, count in current.search_terms.most_common(15)
            ],
            "mcp_tools": [
                {"tool": tool, "count": count}
                for tool, count in current.tool_calls.most_common(12)
            ],
            "users": users,
            "activity": activity,
            "paid": paid,
            "health": health,
            "user_agents": [
                {
                    "user_agent": ua or "(none)",
                    "calls": count,
                    "channel": current.user_agent_channel.get(ua, "other"),
                }
                for ua, count in current.user_agents.most_common(25)
            ],
        }
        result["assessment"] = build_assessment(result)
        result["improvement_plan"] = IMPROVEMENT_PLAN
        return result

    @staticmethod
    def _call_rows(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
        surfaces = ",".join(f"'{surface}'" for surface in CALL_SURFACES)
        return conn.execute(
            f"""
            SELECT substr(timestamp, 1, 10) AS day,
                   MIN(timestamp) AS first_ts,
                   event, surface, endpoint, status_code, user_agent, referrer,
                   {_meta('utm_source')} AS utm_source,
                   {_meta('selection_source')} AS selection_source,
                   subject, tool_name, ip_hash,
                   COUNT(*) AS n
            FROM usage_events
            WHERE timestamp >= ?
              AND (
                event = 'mcp_tool_call'
                OR (event = 'http_request' AND surface IN ({surfaces}))
              )
              AND {NOT_SYNTHETIC_SQL}
            GROUP BY day, event, surface, endpoint, status_code, user_agent,
                     referrer, utm_source, selection_source, subject, tool_name,
                     ip_hash
            """,
            (since_iso,),
        ).fetchall()

    @staticmethod
    def _p95_latency(conn: sqlite3.Connection, since_iso: str) -> float | None:
        surfaces = ",".join(f"'{surface}'" for surface in CALL_SURFACES)
        where = f"""
            FROM usage_events
            WHERE timestamp >= ? AND event = 'http_request'
              AND surface IN ({surfaces}) AND latency_ms IS NOT NULL
        """
        total = conn.execute(f"SELECT COUNT(*) {where}", (since_iso,)).fetchone()[0]
        if not total:
            return None
        offset = min(total - 1, int(total * 0.95))
        row = conn.execute(
            f"SELECT latency_ms {where} ORDER BY latency_ms LIMIT 1 OFFSET ?",
            (since_iso, offset),
        ).fetchone()
        return round(float(row[0]), 1) if row else None

    @staticmethod
    def _accumulate(window: _Window, row: sqlite3.Row) -> None:
        count = int(row["n"])
        event = row["event"]
        surface = row["surface"] or ""
        endpoint = row["endpoint"] or ""
        user_agent = row["user_agent"] or ""
        tool_name = row["tool_name"] or ""
        channel = channel_for(
            surface=surface,
            user_agent=user_agent,
            referrer=row["referrer"],
            utm_source=row["utm_source"],
        )
        is_monitor = channel == "monitor"
        ip_hash = row["ip_hash"]
        day = row["day"]

        if event == "http_request":
            window.http_requests += count
            bucket = _status_bucket(row["status_code"])
            if bucket and not is_monitor:
                window.status[bucket] += count
                if bucket in {"client_error", "server_error"}:
                    window.error_endpoints[(endpoint[:80], int(row["status_code"]))] += count
            if ip_hash and not is_monitor:
                window.users.add(ip_hash)
                window.user_days[ip_hash].add(day)
                window.channel_users[channel].add(ip_hash)
                window.daily_users[day].add(ip_hash)
                window.client_requests[ip_hash] += count
            # MCP transport requests are counted as users above; the tool call
            # event carries the call itself.
            if surface != "http_api":
                return
            is_call = True
            is_live = endpoint.startswith(LIVE_DATA_PATH_PREFIXES)
            ticker_source = is_live
        else:  # mcp_tool_call
            is_call = True
            is_live = tool_name in LIVE_DATA_MCP_TOOLS
            ticker_source = tool_name in TICKER_MCP_TOOLS
            window.mcp_tool_calls += count
            window.tool_calls[tool_name or "unknown"] += count
            if tool_name in SEARCH_MCP_TOOLS and row["subject"]:
                term = str(row["subject"]).strip().lower()[:40]
                if term:
                    window.search_terms[term] += count

        if not is_call:
            return
        window.calls += count
        window.channel_calls[channel] += count
        window.daily_channel_calls[day][channel] += count
        ua_key = user_agent[:100]
        window.user_agents[ua_key] += count
        window.user_agent_channel.setdefault(ua_key, channel)
        if is_monitor:
            window.monitor_calls += count
        if is_live and not is_monitor:
            window.live_data_calls += count
        if endpoint.startswith("/v1/search") and row["subject"]:
            term = str(row["subject"]).strip().lower()[:40]
            if term:
                window.search_terms[term] += count
        if ticker_source:
            copied_example = row["selection_source"] == PUBLISHED_EXAMPLE_SOURCE
            for ticker in _tickers_in(row["subject"]):
                if is_monitor:
                    window.ticker_monitor_calls[ticker] += count
                else:
                    window.ticker_calls[ticker][channel] += count
                    if copied_example:
                        window.ticker_example_calls[ticker] += count
                if tool_name == "get_market_data_endpoint":
                    window.ticker_mcp_lookups[ticker] += count

    def _payments(
        self, rows: list[sqlite3.Row], current_start_iso: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        events = [self.store._row_to_dict(row) for row in rows]
        events = [event for event in events if not self.store._is_synthetic_event(event)]
        current = [event for event in events if event["timestamp"] >= current_start_iso]
        previous = [event for event in events if event["timestamp"] < current_start_iso]
        internal = internal_wallet_hashes()
        return (
            self._payment_window(current, internal),
            self._payment_window(previous, internal),
        )

    @staticmethod
    def _payment_window(events: list[dict[str, Any]], internal: set[str]) -> dict[str, Any]:
        correlation = UsageEventStore._correlate_economic_events(events)
        valid = correlation["valid_delivery_event_ids"]
        x402_deliveries = [
            event
            for event in events
            if id(event) in valid
            and event["event"] == "data_delivered"
            and (event.get("metadata") or {}).get("payment_mode") == "x402"
        ]
        x402_ids = {id(event) for event in x402_deliveries}
        credit_deliveries = [
            event for event in events if id(event) in valid and id(event) not in x402_ids
        ]
        settled = sorted(
            (event for event in events if id(event) in correlation["settled_event_ids"]),
            key=lambda event: event["timestamp"],
        )
        # Group settlements into bursts: runs where each payment follows the
        # previous one within BURST_GAP_SECONDS.
        burst_flags = [False] * len(settled)
        run_start = 0
        for index in range(1, len(settled) + 1):
            gap_breaks = index == len(settled) or (
                _parse_ts(settled[index]["timestamp"])
                - _parse_ts(settled[index - 1]["timestamp"])
            ).total_seconds() > BURST_GAP_SECONDS
            if gap_breaks:
                if index - run_start >= BURST_MIN_SIZE:
                    for flagged in range(run_start, index):
                        burst_flags[flagged] = True
                run_start = index

        wallet_rank: dict[str, int] = {}
        rows = []
        for event, in_burst in zip(settled, burst_flags):
            wallet = event.get("wallet_hash") or ""
            if wallet and wallet not in wallet_rank:
                wallet_rank[wallet] = len(wallet_rank) + 1
            tickers = _tickers_in(event.get("subject"))
            rows.append(
                {
                    "timestamp": event["timestamp"],
                    "ticker": tickers[0] if tickers else (event.get("subject") or "—"),
                    "endpoint": event.get("endpoint"),
                    "network": _network_label(event.get("network")),
                    "amount_usdc": float(event.get("price_usdc") or 0.0),
                    "channel": channel_for(
                        surface=event.get("surface"),
                        user_agent=event.get("user_agent"),
                        referrer=event.get("referrer"),
                        utm_source=(event.get("metadata") or {}).get("utm_source"),
                    ),
                    "payer": f"wallet {wallet_rank[wallet]}" if wallet else "unknown",
                    "internal": bool(wallet and wallet in internal),
                    "burst": in_burst,
                }
            )
        external_rows = [row for row in rows if not row["internal"]]
        return {
            "x402_paid_calls": len(x402_deliveries),
            "credit_calls": len(credit_deliveries),
            "settled_payments": len(rows),
            "external_settled_payments": len(external_rows),
            "revenue_usdc": round(float(correlation["recognized_revenue_usdc"]), 6),
            "paying_wallets": len(wallet_rank),
            "proof_attempts": len(correlation["submitted_attempts"]),
            "settled_attempts": len(correlation["settled_attempts"]),
            "proof_failures": Counter(
                str(event.get("reason") or "unknown")
                for event in events
                if event["event"] == "payment_failed"
            ),
            "rows": rows,
            "burst_settlements": sum(burst_flags),
            "x402_delivery_events": x402_deliveries,
            "credit_delivery_events": credit_deliveries,
        }

    @staticmethod
    def _kpis(
        current: _Window,
        current_cmp: _Window,
        previous: _Window,
        pay: dict[str, Any],
        pay_prev: dict[str, Any],
        identities: int,
        identities_cmp: int,
        identities_prev: int,
        p95_latency: float | None,
    ) -> list[dict[str, Any]]:
        # ``previous`` and ``current_cmp`` leave out Claude connector surfaces
        # when their history is truncated; ``comparable_value`` is the current
        # figure on that same basis, and ``delta`` compares like with like.
        def kpi(kpi_id, label, value, prev, unit="count", *, cmp=None, good="up", note=None):
            comparable = value if cmp is None else cmp
            return {
                "id": kpi_id,
                "label": label,
                "value": value,
                "comparable_value": comparable,
                "previous": prev,
                "delta": _delta(comparable, prev),
                "unit": unit,
                "good_direction": good,
                "note": note,
            }

        organic = current.calls - current.monitor_calls
        organic_cmp = current_cmp.calls - current_cmp.monitor_calls
        organic_prev = previous.calls - previous.monitor_calls
        proof_rate = _rate(pay["settled_attempts"], pay["proof_attempts"])
        proof_rate_prev = _rate(pay_prev["settled_attempts"], pay_prev["proof_attempts"])
        http_ok = sum(current.status.values())
        return [
            kpi("calls", "Calls excl. monitors", organic, organic_prev, cmp=organic_cmp,
                note=f"{current.calls:,} incl. monitors"),
            kpi("unique_users", "Unique clients", len(current.users), len(previous.users),
                cmp=len(current_cmp.users),
                note="Distinct client IP hashes, monitors excluded"),
            kpi("paid_calls", "Paid calls (x402)", pay["x402_paid_calls"],
                pay_prev["x402_paid_calls"],
                note=f"{pay['credit_calls']:,} free-credit calls on top"),
            kpi("revenue", "Recognized revenue", pay["revenue_usdc"],
                pay_prev["revenue_usdc"], "usdc"),
            kpi("paying_wallets", "Paying wallets", pay["paying_wallets"],
                pay_prev["paying_wallets"],
                note=f"{identities:,} verified identities active"),
            kpi("proof_to_settlement", "Payment success", proof_rate, proof_rate_prev,
                "rate", note=f"{pay['settled_attempts']} of {pay['proof_attempts']} proofs settled"),
            kpi("monitor_share", "Monitor share of calls",
                _rate(current.monitor_calls, current.calls),
                _rate(previous.monitor_calls, previous.calls), "rate",
                cmp=_rate(current_cmp.monitor_calls, current_cmp.calls), good="down"),
            kpi("server_error_rate", "Server error rate",
                _rate(current.status["server_error"], http_ok),
                _rate(previous.status["server_error"], sum(previous.status.values())),
                "rate",
                cmp=_rate(current_cmp.status["server_error"], sum(current_cmp.status.values())),
                good="down", note=f"p95 latency {p95_latency:,.0f} ms"
                if p95_latency is not None else None),
        ] + [
            kpi("verified_identities", "Verified identities", identities, identities_prev,
                cmp=identities_cmp),
        ]

    @staticmethod
    def _channels(current: _Window, pay: dict[str, Any], days: int) -> list[dict[str, Any]]:
        paid_by_channel: Counter[str] = Counter()
        revenue_by_channel: Counter[str] = Counter()
        for row in pay["rows"]:
            paid_by_channel[row["channel"]] += 1
            revenue_by_channel[row["channel"]] += row["amount_usdc"]
        return [
            {
                "id": cid,
                "label": label,
                "calls": current.channel_calls[cid],
                "share": _rate(current.channel_calls[cid], current.calls),
                "users": len(current.channel_users.get(cid, ())),
                "paid_calls": paid_by_channel[cid],
                "revenue_usdc": round(revenue_by_channel[cid], 6),
                "retention_note": (
                    f"Claude connector data is kept {RETENTION_DAYS} days, so connector "
                    f"calls older than that are missing; Claude traffic on the public MCP "
                    "endpoint keeps its full history."
                    if cid == "claude" and days > RETENTION_DAYS else None
                ),
            }
            for cid, label in CHANNELS
        ]

    @staticmethod
    def _tickers(current: _Window, pay: dict[str, Any]) -> dict[str, Any]:
        paid: Counter[str] = Counter()
        revenue: Counter[str] = Counter()
        for row in pay["rows"]:
            paid[row["ticker"]] += 1
            revenue[row["ticker"]] += row["amount_usdc"]
        credit: Counter[str] = Counter()
        for event in pay["credit_delivery_events"]:
            for ticker in _tickers_in(event.get("subject")):
                credit[ticker] += 1
        names = set(current.ticker_calls) | set(current.ticker_monitor_calls) | set(paid)
        rows = []
        for ticker in names:
            by_channel = current.ticker_calls.get(ticker, Counter())
            organic = sum(by_channel.values())
            rows.append(
                {
                    "ticker": ticker,
                    "calls": organic,
                    "copied_example_calls": current.ticker_example_calls[ticker],
                    "monitor_calls": current.ticker_monitor_calls[ticker],
                    "paid_calls": paid[ticker],
                    "credit_calls": credit[ticker],
                    "revenue_usdc": round(revenue[ticker], 6),
                    "mcp_lookups": current.ticker_mcp_lookups[ticker],
                    "by_channel": dict(by_channel.most_common()),
                }
            )
        rows.sort(key=lambda row: (row["calls"] + row["monitor_calls"], row["paid_calls"]),
                  reverse=True)
        total = sum(row["calls"] for row in rows)
        copied = sum(row["copied_example_calls"] for row in rows)
        # Demand concentration is judged on calls that chose their instrument;
        # a copied example path says nothing about what the caller wanted.
        chosen = [row["calls"] - row["copied_example_calls"] for row in rows]
        top3 = sum(sorted(chosen, reverse=True)[:3])
        return {
            "distinct_tickers": len(rows),
            "total_calls": total,
            "copied_example_calls": copied,
            "copied_example_share": _rate(copied, total),
            "top3_share": _rate(top3, total - copied),
            "rows": rows[:30],
        }

    @staticmethod
    def _users(
        current: _Window,
        current_cmp: _Window,
        previous: _Window,
        identities: int,
        pay: dict[str, Any],
    ) -> dict[str, Any]:
        returning = sum(1 for days in current.user_days.values() if len(days) >= 2)
        # Compared on the retained basis, so deleted Claude history cannot make
        # a long-standing connector client look new.
        new_users = len(current_cmp.users - previous.users)
        total_requests = sum(current.client_requests.values())
        top = current.client_requests.most_common(10)
        return {
            "unique_clients": len(current.users),
            "new_clients": new_users,
            "returning_clients": returning,
            "returning_rate": _rate(returning, len(current.users)),
            "verified_identities": identities,
            "paying_wallets": pay["paying_wallets"],
            "top_client_share": _rate(top[0][1], total_requests) if top else None,
            "top5_client_share": _rate(sum(n for _, n in top[:5]), total_requests),
            "top_clients": [
                {"client": f"client {index + 1}", "requests": n,
                 "share": _rate(n, total_requests),
                 "active_days": len(current.user_days.get(ip, ()))}
                for index, (ip, n) in enumerate(top)
            ],
        }

    @staticmethod
    def _activity(
        current: _Window,
        pay: dict[str, Any],
        start: datetime,
        days: int,
    ) -> list[dict[str, Any]]:
        paid_by_day: Counter[str] = Counter()
        revenue_by_day: Counter[str] = Counter()
        for row in pay["rows"]:
            paid_by_day[row["timestamp"][:10]] += 1
            revenue_by_day[row["timestamp"][:10]] += row["amount_usdc"]
        credit_by_day: Counter[str] = Counter(
            event["timestamp"][:10] for event in pay["credit_delivery_events"]
        )
        series = []
        for day in _day_list(start, days + 1):
            channel_calls = current.daily_channel_calls.get(day, Counter())
            series.append(
                {
                    "date": day,
                    "calls": sum(channel_calls.values()),
                    "by_channel": {cid: channel_calls[cid] for cid, _ in CHANNELS
                                   if channel_calls[cid]},
                    "unique_clients": len(current.daily_users.get(day, ())),
                    "paid_calls": paid_by_day[day],
                    "credit_calls": credit_by_day[day],
                    "revenue_usdc": round(revenue_by_day[day], 6),
                }
            )
        return series

    @staticmethod
    def _health(current: _Window) -> dict[str, Any]:
        total = sum(current.status.values())
        return {
            "http_calls": total,
            "status": {bucket: current.status[bucket] for bucket in (
                "ok", "payment_required", "client_error", "rate_limited", "server_error")},
            "top_errors": [
                {"endpoint": endpoint, "status_code": status, "count": count}
                for (endpoint, status), count in current.error_endpoints.most_common(10)
            ],
        }

    @staticmethod
    def _paid(pay: dict[str, Any]) -> dict[str, Any]:
        by_network: Counter[str] = Counter(row["network"] for row in pay["rows"])
        by_ticker: Counter[str] = Counter(row["ticker"] for row in pay["rows"])
        return {
            "x402_paid_calls": pay["x402_paid_calls"],
            "credit_calls": pay["credit_calls"],
            "settled_payments": pay["settled_payments"],
            "external_settled_payments": pay["external_settled_payments"],
            "internal_wallets_configured": bool(os.environ.get(INTERNAL_WALLETS_ENV, "").strip()),
            "revenue_usdc": pay["revenue_usdc"],
            "paying_wallets": pay["paying_wallets"],
            "burst_settlements": pay["burst_settlements"],
            "proof_attempts": pay["proof_attempts"],
            "settled_attempts": pay["settled_attempts"],
            "proof_failures": dict(pay["proof_failures"].most_common()),
            "by_network": dict(by_network.most_common()),
            "by_ticker": dict(by_ticker.most_common(10)),
            "rows": list(reversed(pay["rows"]))[:100],
        }


DEFINITIONS = {
    "call": (
        "One request to the HTTP API under /v1, or one MCP tool call on any "
        "connector. MCP transport requests (initialize, list tools) are not calls."
    ),
    "avenue": (
        "Where a call came from: the connector surface first, then the user "
        "agent, referrer and utm_source. Directory avenues (Smithery, Glama, "
        "Pay.sh) are only visible when their traffic identifies itself."
    ),
    "unique_client": (
        "Distinct salted client-IP hash on API and MCP traffic, with monitors "
        "excluded. Shared or rotating IPs make this an estimate, not a head count."
    ),
    "paid_call": (
        "A live-data response delivered after a settled, correlated x402 "
        "payment. Free-credit calls on the signed-in connectors are counted apart."
    ),
    "monitor": (
        "A call whose user agent names a probe, uptime check, verifier, crawler "
        "or bot. Kept visible but excluded from demand and user counts."
    ),
    "claude_retention": (
        f"Events on the Claude connector surfaces ({', '.join(CLAUDE_SURFACES)}) are "
        f"deleted after {RETENTION_DAYS} days. Windows longer than that undercount them, "
        "and changes against the prior period leave them out whenever that period "
        "reaches past the cutoff."
    ),
    "burst": (
        f"A settled payment that belongs to a run of {BURST_MIN_SIZE} or more "
        f"payments, each within {BURST_GAP_SECONDS // 60} minutes of the last - "
        "typical of scripted tests rather than an agent at work."
    ),
}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def build_assessment(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the window's numbers into ranked issues, each with a concrete fix."""
    issues: list[dict[str, Any]] = []
    kpis = {kpi["id"]: kpi for kpi in result["kpis"]}
    channels = {row["id"]: row for row in result["channels"]}
    paid = result["paid"]
    total_calls = sum(row["calls"] for row in result["channels"])
    organic_calls = kpis["calls"]["value"]

    def add(severity, area, title, evidence, fix):
        issues.append(
            {"severity": severity, "area": area, "title": title,
             "evidence": evidence, "fix": fix}
        )

    proof_rate = kpis["proof_to_settlement"]["value"]
    if proof_rate is not None and paid["proof_attempts"] >= 10 and proof_rate < 0.9:
        reasons = ", ".join(f"{reason} ({count})" for reason, count in
                            list(paid["proof_failures"].items())[:3]) or "no reason recorded"
        add("P0", "Payments", f"{_pct(1 - proof_rate)} of payment attempts fail",
            f"{paid['settled_attempts']} of {paid['proof_attempts']} submitted x402 proofs "
            f"settled ({_pct(proof_rate)}). Failures: {reasons}.",
            "Return a precise error body for unbound or v1 signatures that names the "
            "expected x402 v2 fields, publish copy-paste client examples, and retry or "
            "fail over when the facilitator is unavailable instead of rejecting the proof.")

    live_calls = result["totals"]["live_data_calls"]
    if live_calls >= MIN_SAMPLE and paid["x402_paid_calls"] / live_calls < 0.01:
        add("P0", "Monetization", "Live-data requests almost never turn into paid calls",
            f"{live_calls:,} non-monitor live-data requests produced "
            f"{paid['x402_paid_calls']} paid calls "
            f"({_pct(_rate(paid['x402_paid_calls'], live_calls))}).",
            "Treat the 402 response as the product's landing page: include a free "
            "sample value, the exact price, and a one-line x402 client snippet; route "
            "agents without a wallet to the free signed-in connector.")

    if paid["settled_payments"] and paid["burst_settlements"] / paid["settled_payments"] >= 0.5:
        add("P1", "Measurement", "Most paid calls look scripted, not organic",
            f"{paid['burst_settlements']} of {paid['settled_payments']} settlements arrived "
            f"in bursts of {BURST_MIN_SIZE}+ within {BURST_GAP_SECONDS // 60} minutes. "
            + (f"{paid['external_settled_payments']} settlements came from wallets not "
               "marked internal." if paid["internal_wallets_configured"] else
               "No payer wallets are marked internal, so every burst counts as customer revenue."),
            "Look up the bursty payer wallets on-chain and ask whether they are an "
            "integration test or an agent loop. If a wallet turns out to be yours, add it to "
            f"{INTERNAL_WALLETS_ENV} so the dashboard separates it.")

    monitor_share = kpis["monitor_share"]["value"]
    if monitor_share is not None and total_calls >= MIN_SAMPLE and monitor_share > 0.3:
        add("P1", "Measurement", "Monitors make up a large share of traffic",
            f"{_pct(monitor_share)} of calls come from probes, verifiers and crawlers.",
            "Keep them out of demand KPIs (done here), and serve known directory probes "
            "a cached, cheap response so they stop costing upstream quota.")

    generic_share = _rate(channels["other"]["calls"] + channels["x402_http"]["calls"]
                          + channels["direct_mcp"]["calls"], total_calls - channels["monitor"]["calls"])
    directory_calls = sum(channels[c]["calls"] for c in ("smithery", "glama", "pay_sh"))
    if generic_share is not None and organic_calls >= MIN_SAMPLE and generic_share > 0.8:
        add("P1", "Attribution", "Directory attribution is missing for most calls",
            f"{_pct(generic_share)} of non-monitor calls arrive as plain x402 HTTP or "
            f"direct MCP traffic with no directory marker; only {directory_calls:,} "
            "calls identify Smithery, Glama or Pay.sh.",
            "Publish a distinct endpoint URL per listing (for example "
            "/mcp/server?utm_source=smithery and /v1/...?utm_source=paysh) so every "
            "call carries its avenue, and ask directories for their gateway user agents.")

    connector_calls = sum(channels[c]["calls"] for c in ("claude", "openai", "cursor"))
    if organic_calls >= MIN_SAMPLE and connector_calls / organic_calls < 0.05:
        add("P1", "Growth", "The signed-in connectors are barely used",
            f"Claude, OpenAI and Cursor account for {connector_calls:,} calls "
            f"({_pct(_rate(connector_calls, organic_calls))} of non-monitor calls); "
            f"{kpis['verified_identities']['value']} verified identities were active."
            + (f" Claude connector figures cover only the last {RETENTION_DAYS} days."
               if result["retention"]["window_exceeds_claude_retention"] else ""),
            "Lead listings with the free signed-in connector rather than the "
            "x402 endpoint, and add a one-click install link for each client.")

    tickers = result["tickers"]
    top3 = tickers["top3_share"]
    chosen_calls = tickers["total_calls"] - tickers["copied_example_calls"]
    if top3 is not None and chosen_calls >= MIN_SAMPLE and top3 > 0.8:
        names = ", ".join(row["ticker"] for row in tickers["rows"][:3])
        copied = tickers["copied_example_share"] or 0
        add("P2", "Demand signal", "Ticker demand mirrors the published examples",
            f"The top three tickers ({names}) take {_pct(top3)} of live-data requests "
            f"that chose their instrument; {_pct(copied)} of requests were copied "
            "example paths and are excluded.",
            "Published examples already rotate weekly on generated listings and carry "
            "selection_source=published_example_path. Diversify the remaining static "
            "examples in docs and connector prompts.")

    top_client = result["users"]["top_client_share"]
    if top_client is not None and result["users"]["unique_clients"] >= 5 and top_client > 0.2:
        add("P2", "Demand signal", "A single client dominates the traffic",
            f"The busiest client sends {_pct(top_client)} of API and MCP requests; "
            f"the top five send {_pct(result['users']['top5_client_share'])}.",
            "Identify the top clients' user agents (see the table below) and either "
            "classify them as monitors or reach out to them as prospects.")

    status = result["health"]["status"]
    http_calls = result["health"]["http_calls"]
    client_errors = status["client_error"]
    if http_calls >= MIN_SAMPLE and client_errors / http_calls > 0.1:
        worst = ", ".join(f"{row['endpoint']} {row['status_code']}"
                          for row in result["health"]["top_errors"][:3])
        add("P2", "Reliability", "Many calls fail with client errors",
            f"{_pct(client_errors / http_calls)} of non-monitor HTTP requests return 4xx "
            f"(excluding 402 and 429). Top: {worst}.",
            "Add redirects or helpful error bodies for the most common wrong paths and "
            "methods, and accept the Accept headers MCP clients actually send.")
    if http_calls >= MIN_SAMPLE and status["rate_limited"] / http_calls > 0.02:
        add("P2", "Reliability", "Rate limiting rejects real traffic",
            f"{_pct(status['rate_limited'] / http_calls)} of non-monitor HTTP requests get 429.",
            "Check whether the limited clients are monitors; raise discovery limits "
            "for identified agents.")
    server_rate = kpis["server_error_rate"]["value"]
    if server_rate is not None and http_calls >= MIN_SAMPLE and server_rate > 0.01:
        add("P0", "Reliability", "Server errors above 1%",
            f"{_pct(server_rate)} of non-monitor HTTP requests return 5xx.",
            "Inspect the failing endpoints below and the upstream Blocksize API health.")

    severity_rank = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda issue: severity_rank[issue["severity"]])
    return issues


# Structural improvements to the measurement setup itself. These do not depend
# on the window's numbers, so they are listed as a plan rather than computed.
IMPROVEMENT_PLAN = [
    {
        "horizon": "Now",
        "title": "Find out who the bursty payers are",
        "detail": (
            "Most paid calls arrive in scripted bursts. Check the payer wallets on-chain; "
            f"if any wallet is your own, add it to {INTERNAL_WALLETS_ENV}."
        ),
    },
    {
        "horizon": "Now",
        "title": "Give each listing its own attributable URL",
        "detail": (
            "Point Smithery, Glama, Pay.sh, the MCP Registry and x402 directories at "
            "endpoint URLs carrying utm_source. MCP clients keep the query string on "
            "every transport request, so calls become attributable without referrers."
        ),
    },
    {
        "horizon": "Now",
        "title": "Fix x402 proof rejections",
        "detail": (
            "Make invalid-signature responses explain the expected x402 v2 payload, "
            "and retry the facilitator before rejecting a proof."
        ),
    },
    {
        "horizon": "Next",
        "title": "Carry the client hash on MCP tool calls",
        "detail": (
            "Public MCP tool-call events store no client hash or utm_source, so tools "
            "cannot be tied to users. Copy both from the transport request."
        ),
    },
    {
        "horizon": "Next",
        "title": "Add a retention and roll-up job",
        "detail": (
            "usage_events has no retention; the Railway volume held 7.1 of 9.8 GB on "
            "29 Sep 2026. "
            "Roll raw events older than 90 days into daily aggregates and delete them."
        ),
    },
    {
        "horizon": "Next",
        "title": "Slim the deep-dive stats payload",
        "detail": (
            "On 28 Sep 2026 /internal/observability/stats returned 3.1 MB, 3.0 MB of it "
            "the RWA pilot block. The pilot moved to /internal/observability/rwa-pilot "
            "on 7 Oct 2026; stats still loads every event of the window into memory."
        ),
    },
    {
        "horizon": "Later",
        "title": "Count people, not IPs",
        "detail": (
            "Make verified identities (OAuth, agent auth, payer wallets) the user "
            "north star and keep IP-based clients as a reach indicator."
        ),
    },
    {
        "horizon": "Later",
        "title": "Ingest directory-side metrics",
        "detail": (
            "Pull views and installs from Smithery, Glama and Pay.sh where they offer "
            "them, to see the funnel before the first call."
        ),
    },
]
