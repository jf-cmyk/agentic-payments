"""Privacy-safe local usage telemetry for Blocksize agent surfaces."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlparse

from src.security_config import hash_salt


REGISTRY_ENDPOINTS = {
    "/server.json": "mcp_registry",
    "/mcp/manifest.json": "mcp_manifest",
    "/.well-known/glama.json": "glama",
    "/.well-known/mcp-registry-auth": "mcp_registry_auth",
    "/.well-known/x402": "x402_directory",
    "/openapi.json": "openapi",
}

SYMBOL_OPPORTUNITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,31}$")
TRUSTED_IDENTITY_LEVELS = frozenset({"verified_oauth", "verified_beta", "verified_x402"})
LIVE_DATA_MCP_TOOLS = frozenset(
    {
        "get_vwap",
        "get_vwap_30min",
        "get_vwap_24h",
        "get_bid_ask",
        "get_state_price",
        "get_fx_rate",
        "get_metal_price",
        "resolve_and_get_market_data",
    }
)
PUBLIC_MCP_ENDPOINTS = frozenset({"/mcp/server"})
AUTHENTICATED_MCP_ENDPOINTS = frozenset(
    {"/anthropic/mcp", "/cursor/mcp", "/openai/mcp"}
)
KNOWN_MONITOR_USER_AGENT_MARKERS = frozenset(
    {
        "healthcheck",
        "health-check",
        "heartbeat",
        "monitor",
        "probe",
        "prober",
        "scanner",
        "synthetic",
        "uptime",
        "statuscake",
        "pingdom",
    }
)
PRODUCT_ROUTE_IDS = {
    "/v1/briefs/market": "agent_market_brief",
    "/v1/checks/pre-trade": "pre_trade_check",
    "/v1/receipts/price": "audit_price_receipt",
    "/v1/snapshots/macro": "macro_snapshot",
    "/v1/monitors/evaluate": "market_monitor",
    "/v1/indicators/token-quality": "token_quality_indicator",
    "/v1/indicators/state-divergence": "state_divergence_indicator",
    "/v1/signals/solana-token-brief": "solana_token_brief",
    "/v1/signals/trader-alpha-pack": "trader_alpha_pack",
    "/v1/rwa/benchmark/blocksize": "rwa_blocksize_benchmark",
}
MARKET_INTELLIGENCE_PRODUCT_IDS = frozenset(PRODUCT_ROUTE_IDS.values())


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def fingerprint(value: str | None, *, salt_env: str = "OBSERVABILITY_HASH_SALT") -> str | None:
    """Hash user-identifying values before storing telemetry."""
    if not value:
        return None
    salt = hash_salt(salt_env)
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def normalize_symbol_opportunity(value: str | None) -> str | None:
    """Return a bounded symbol-like query suitable for demand aggregation."""
    if not value:
        return None
    clean = value.strip()
    if not SYMBOL_OPPORTUNITY_RE.fullmatch(clean):
        return None
    # Keep market pairs such as BTC-USD, BTC/USD and BRK.B while rejecting
    # prose-like slugs such as DATA-API-FOR-AI from the sourcing backlog.
    if len(re.split(r"[-/:]", clean)) > 2:
        return None
    return clean.upper()


def registry_name_for_path(path: str) -> str | None:
    if path in REGISTRY_ENDPOINTS:
        return REGISTRY_ENDPOINTS[path]
    if path.startswith("/assets/listings/"):
        return "listing_asset"
    return None


def surface_for_path(path: str) -> str:
    if path == "/mcp/server" or path.startswith("/mcp/server/"):
        return "public_mcp"
    if path == "/anthropic/mcp" or path.startswith("/anthropic/mcp/"):
        return "anthropic_mcp"
    if path == "/cursor/mcp" or path.startswith("/cursor/mcp/"):
        return "cursor_mcp"
    if path == "/openai/mcp" or path.startswith("/openai/mcp/"):
        return "openai_mcp"
    if registry_name_for_path(path):
        return "registry"
    if path == "/go" or path.startswith("/go/"):
        return "marketing_redirect"
    if path.startswith("/v1/"):
        return "http_api"
    if path in {"/", "/quickstart/remote-mcp", "/prompt-examples", "/support", "/privacy"}:
        return "developer_portal"
    return "other"


class UsageEventStore:
    """SQLite-backed local event store for early-stage product observability."""

    def __init__(self, db_path: str | Path = "usage_events.db") -> None:
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        path = Path(self.db_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    surface TEXT,
                    endpoint TEXT,
                    method TEXT,
                    status_code INTEGER,
                    latency_ms REAL,
                    ip_hash TEXT,
                    user_agent TEXT,
                    referrer TEXT,
                    wallet_hash TEXT,
                    subject TEXT,
                    asset_class TEXT,
                    price_usdc REAL,
                    network TEXT,
                    reason TEXT,
                    tool_name TEXT,
                    metadata_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp
                ON usage_events(timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_usage_events_event
                ON usage_events(event)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_usage_events_surface
                ON usage_events(surface)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS marketplace_metric_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    source_url TEXT,
                    status TEXT DEFAULT 'ok',
                    metrics_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_marketplace_metrics_timestamp
                ON marketplace_metric_snapshots(timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_marketplace_metrics_platform
                ON marketplace_metric_snapshots(platform_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_milestones (
                    event TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (event, identity_hash)
                )
                """
            )

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM usage_events")
            conn.execute("DELETE FROM marketplace_metric_snapshots")
            conn.execute("DELETE FROM event_milestones")

    def claim_milestone(self, event: str, identity_hash: str) -> bool:
        """Atomically claim a once-per-identity event milestone."""
        if not event or not identity_hash:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_milestones (event, identity_hash, timestamp)
                VALUES (?, ?, ?)
                """,
                (event, identity_hash, utc_now_iso()),
            )
        return cursor.rowcount == 1

    def record(
        self,
        event: str,
        *,
        surface: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        status_code: int | None = None,
        latency_ms: float | None = None,
        ip_hash: str | None = None,
        user_agent: str | None = None,
        referrer: str | None = None,
        wallet_hash: str | None = None,
        subject: str | None = None,
        asset_class: str | None = None,
        price_usdc: float | str | None = None,
        network: str | None = None,
        reason: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        metadata_json = json.dumps(metadata or {}, sort_keys=True, default=str)
        price = float(price_usdc) if price_usdc is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    timestamp, event, surface, endpoint, method, status_code,
                    latency_ms, ip_hash, user_agent, referrer, wallet_hash,
                    subject, asset_class, price_usdc, network, reason,
                    tool_name, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    event,
                    surface,
                    endpoint,
                    method,
                    status_code,
                    latency_ms,
                    ip_hash,
                    user_agent,
                    referrer,
                    wallet_hash,
                    subject,
                    asset_class,
                    price,
                    network,
                    reason,
                    tool_name,
                    metadata_json,
                ),
            )

    def record_marketplace_metrics(
        self,
        *,
        platform_id: str,
        metrics: dict[str, Any],
        source_url: str | None = None,
        status: str = "ok",
    ) -> None:
        metrics_json = json.dumps(metrics or {}, sort_keys=True, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO marketplace_metric_snapshots (
                    timestamp, platform_id, source_url, status, metrics_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now_iso(), platform_id, source_url, status, metrics_json),
            )

    def marketplace_metrics_summary(self, *, days: int = 30) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, platform_id, source_url, status, metrics_json
                FROM marketplace_metric_snapshots
                WHERE timestamp >= ?
                ORDER BY timestamp DESC, id DESC
                """,
                (cutoff.isoformat(),),
            ).fetchall()

        snapshots = []
        latest_by_platform: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = dict(row)
            try:
                data["metrics"] = json.loads(data.pop("metrics_json") or "{}")
            except json.JSONDecodeError:
                data["metrics"] = {}
            snapshots.append(data)
            latest_by_platform.setdefault(str(data["platform_id"]), data)

        return {
            "window_days": days,
            "total_snapshots": len(snapshots),
            "platforms_configured": sorted(latest_by_platform),
            "latest_by_platform": latest_by_platform,
            "recent_snapshots": snapshots[:50],
        }

    def recent_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event, surface, endpoint, method, status_code,
                       latency_ms, user_agent, referrer, wallet_hash, subject,
                       asset_class, price_usdc, network, reason, tool_name,
                       metadata_json
                FROM usage_events
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _metadata(event: dict[str, Any]) -> dict[str, Any]:
        metadata = event.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    @classmethod
    def _correlate_economic_events(cls, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Join payment and credit lifecycles before computing business KPIs."""
        submissions: set[str] = set()
        authorization_attempts: set[str] = set()
        failed_attempts: set[str] = set()
        settled_candidates: list[tuple[dict[str, Any], str, str]] = []
        mcp_drawdowns: dict[tuple[str, str], dict[str, Any]] = {}
        http_drawdowns: dict[str, dict[str, Any]] = {}
        failed_charges: set[tuple[str, str]] = set()
        uncorrelated = 0

        for event in events:
            event_name = str(event.get("event") or "")
            metadata = cls._metadata(event)
            attempt_id = str(metadata.get("attempt_id") or "").strip()
            payment_id = str(metadata.get("payment_id") or "").strip()
            charge_id = str(metadata.get("charge_id") or "").strip()
            if event_name == "payment_proof_submitted":
                if attempt_id:
                    submissions.add(attempt_id)
                else:
                    uncorrelated += 1
            elif event_name in {"payment_authorization_verified", "payment_verified"}:
                if attempt_id:
                    authorization_attempts.add(attempt_id)
                else:
                    uncorrelated += 1
            elif event_name == "payment_settled":
                if attempt_id and payment_id:
                    settled_candidates.append((event, attempt_id, payment_id))
                else:
                    uncorrelated += 1
            elif event_name == "payment_failed":
                if attempt_id:
                    failed_attempts.add(attempt_id)
                else:
                    uncorrelated += 1
            elif event_name == "mcp_credit_drawdown_success":
                if attempt_id and charge_id:
                    mcp_drawdowns[(attempt_id, charge_id)] = event
                else:
                    uncorrelated += 1
            elif event_name == "credit_drawdown_success":
                if charge_id:
                    http_drawdowns[charge_id] = event
                else:
                    uncorrelated += 1
            elif event_name in {"mcp_tool_error", "mcp_credit_drawdown_failed"}:
                if attempt_id and charge_id:
                    failed_charges.add((attempt_id, charge_id))
                elif event_name == "mcp_tool_error":
                    uncorrelated += 1
            elif event_name == "charged_delivery_failed" and charge_id:
                failed_charges.add((attempt_id, charge_id))

        conflicting_attempts = {
            attempt_id
            for _, attempt_id, _ in settled_candidates
            if attempt_id in failed_attempts
        }
        settled_by_payment: dict[str, dict[str, Any]] = {}
        settled_pairs: set[tuple[str, str]] = set()
        duplicate_settlements = 0
        for event, attempt_id, payment_id in settled_candidates:
            if (
                attempt_id not in submissions
                or attempt_id in conflicting_attempts
                or attempt_id not in authorization_attempts
            ):
                continue
            if payment_id in settled_by_payment:
                duplicate_settlements += 1
                continue
            settled_by_payment[payment_id] = event
            settled_pairs.add((attempt_id, payment_id))

        valid_delivery_event_ids: set[int] = set()
        valid_failure_event_ids: set[int] = set()
        resolved_mcp_charges: set[tuple[str, str]] = set()
        resolved_http_charges: set[str] = set()
        delivered_x402_payments: set[str] = set()
        for event in events:
            event_name = str(event.get("event") or "")
            metadata = cls._metadata(event)
            attempt_id = str(metadata.get("attempt_id") or "").strip()
            payment_id = str(metadata.get("payment_id") or "").strip()
            charge_id = str(metadata.get("charge_id") or "").strip()
            payment_mode = str(metadata.get("payment_mode") or "")
            if event_name == "data_delivered":
                if (
                    payment_mode == "x402"
                    and metadata.get("payment_state") == "finalized"
                    and (attempt_id, payment_id) in settled_pairs
                    and payment_id not in delivered_x402_payments
                ):
                    valid_delivery_event_ids.add(id(event))
                    delivered_x402_payments.add(payment_id)
                elif (
                    payment_mode == "starter_credit"
                    and charge_id in http_drawdowns
                    and (attempt_id, charge_id) not in failed_charges
                    and charge_id not in resolved_http_charges
                ):
                    valid_delivery_event_ids.add(id(event))
                    resolved_http_charges.add(charge_id)
            elif event_name == "mcp_data_delivered":
                charge_key = (attempt_id, charge_id)
                if (
                    charge_key in mcp_drawdowns
                    and charge_key not in failed_charges
                    and charge_key not in resolved_mcp_charges
                ):
                    valid_delivery_event_ids.add(id(event))
                    resolved_mcp_charges.add(charge_key)
            elif event_name == "mcp_tool_error":
                charge_key = (attempt_id, charge_id)
                if charge_key in mcp_drawdowns:
                    valid_failure_event_ids.add(id(event))
                    resolved_mcp_charges.add(charge_key)
            elif event_name == "charged_delivery_failed":
                if (
                    charge_id in http_drawdowns
                    or (attempt_id and payment_id and attempt_id in authorization_attempts)
                ):
                    valid_failure_event_ids.add(id(event))
                    if charge_id:
                        resolved_http_charges.add(charge_id)

        revenue = sum(
            float(event.get("price_usdc") or 0.0)
            for event in settled_by_payment.values()
        )
        active_wallets = {
            str(event.get("wallet_hash"))
            for event in settled_by_payment.values()
            if event.get("wallet_hash")
            and cls._metadata(event).get("identity_trust") == "verified_x402"
        }
        active_principals = {
            str(cls._metadata(event).get("identity_hash"))
            for event in events
            if id(event) in valid_delivery_event_ids
            and cls._metadata(event).get("identity_hash")
            and cls._metadata(event).get("identity_trust") in TRUSTED_IDENTITY_LEVELS
        }
        unresolved_drawdowns = (
            len(set(mcp_drawdowns) - resolved_mcp_charges)
            + len(set(http_drawdowns) - resolved_http_charges)
        )
        return {
            "valid_delivery_event_ids": valid_delivery_event_ids,
            "valid_failure_event_ids": valid_failure_event_ids,
            "settled_event_ids": {id(event) for event in settled_by_payment.values()},
            "recognized_revenue_usdc": revenue,
            "active_wallets": active_wallets,
            "active_principals": active_principals,
            "submitted_attempts": submissions,
            "authorization_attempts": authorization_attempts,
            "settled_attempts": {attempt for attempt, _ in settled_pairs},
            "unresolved_drawdowns": unresolved_drawdowns,
            "uncorrelated_legacy_events": uncorrelated,
            "correlation_conflicts": len(conflicting_attempts),
            "duplicate_settlements": duplicate_settlements,
        }

    def summarize(
        self,
        *,
        days: int = 30,
        include_synthetic: bool = True,
    ) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event, surface, endpoint, method, status_code,
                       latency_ms, ip_hash, user_agent, referrer, wallet_hash,
                       subject, asset_class, price_usdc, network, reason,
                       tool_name, metadata_json
                FROM usage_events
                WHERE timestamp >= ?
                ORDER BY timestamp ASC, id ASC
                """,
                (cutoff.isoformat(),),
            ).fetchall()

        all_events = [self._row_to_dict(row) for row in rows]
        synthetic_events = [
            event for event in all_events if self._is_synthetic_event(event)
        ]
        events = (
            all_events
            if include_synthetic
            else [event for event in all_events if not self._is_synthetic_event(event)]
        )
        event_counts = Counter(event["event"] for event in events)
        correlation = self._correlate_economic_events(events)
        surface_counts = Counter(event.get("surface") or "unknown" for event in events)
        endpoint_counts = Counter(event.get("endpoint") or "unknown" for event in events)
        status_counts = Counter(str(event.get("status_code") or "unknown") for event in events)
        unique_clients = {event.get("ip_hash") for event in events if event.get("ip_hash")}
        active_wallets = correlation["active_wallets"]

        called_data_events = {
            "free_discovery_call",
            "mcp_tool_call",
            "payment_required",
            "mcp_credit_drawdown_success",
            "mcp_credit_drawdown_failed",
            "mcp_data_delivered",
            "mcp_tool_error",
            "data_delivered",
            "charged_delivery_failed",
            "refunded_delivery_failed",
        }
        paid_calls = len(correlation["valid_delivery_event_ids"])
        revenue = float(correlation["recognized_revenue_usdc"])
        proof_submissions = len(correlation["submitted_attempts"])
        payment_success_rate = (
            len(correlation["settled_attempts"]) / proof_submissions
            if proof_submissions
            else None
        )

        latencies = [
            float(event["latency_ms"])
            for event in events
            if event.get("event") == "http_request" and event.get("latency_ms") is not None
        ]
        endpoint_mix = Counter(
            event.get("endpoint") or "unknown"
            for event in events
            if id(event) in correlation["valid_delivery_event_ids"]
        )
        asset_mix = Counter(
            event.get("asset_class") or "unknown"
            for event in events
            if id(event) in correlation["valid_delivery_event_ids"]
        )
        registry_mix = Counter(
            event.get("endpoint") or "unknown"
            for event in events
            if event["event"] == "registry_request"
        )
        registry_source_mix = Counter(
            source
            for event in events
            if self._counts_toward_registry_source(event)
            and (source := self._registry_source_for_event(event)) is not None
        )
        mcp_tool_mix = Counter(
            f"{event.get('surface') or 'unknown'}:{event.get('tool_name') or 'unknown'}"
            for event in events
            if event["event"] == "mcp_tool_call"
        )
        failure_reasons = Counter(
            event.get("reason") or "unknown"
            for event in events
            if event["event"]
            in {
                "payment_failed",
                "credit_drawdown_failed",
                "mcp_tool_error",
                "charged_delivery_failed",
                "refunded_delivery_failed",
            }
        )
        top_subjects = Counter(
            event.get("subject") or "unknown"
            for event in events
            if event.get("subject")
            and self._is_live_data_request_event(event)
        )
        service_mix = Counter(
            self._service_for_event(event)
            for event in events
            if self._is_live_data_related_event(event, called_data_events)
        )
        origin_mix = Counter(
            self._origin_for_event(event)
            for event in events
            if event["event"] in {"http_request", "mcp_tool_call", "registry_request"}
        )
        referrer_mix = Counter(
            self._referrer_host(event.get("referrer"))
            for event in events
            if event.get("referrer")
        )
        campaign_mix = Counter(
            str(event["metadata"].get("utm_campaign"))
            for event in events
            if event["event"] == "http_request"
            and isinstance(event.get("status_code"), int)
            and 200 <= int(event["status_code"]) < 400
            and isinstance(event.get("metadata"), dict)
            and event["metadata"].get("utm_campaign")
        )
        campaign_source_mix = Counter(
            str(event["metadata"].get("utm_source"))
            for event in events
            if event["event"] == "http_request"
            and isinstance(event.get("status_code"), int)
            and 200 <= int(event["status_code"]) < 400
            and isinstance(event.get("metadata"), dict)
            and event["metadata"].get("utm_source")
        )
        selection_source_mix = Counter(
            str(
                event["metadata"].get("selection_source") or "unattributed"
                if isinstance(event.get("metadata"), dict)
                else "unattributed"
            )
            for event in events
            if self._is_live_data_related_event(event, called_data_events)
        )
        delivered_selection_source_mix = Counter(
            str(
                event["metadata"].get("selection_source") or "unattributed"
                if isinstance(event.get("metadata"), dict)
                else "unattributed"
            )
            for event in events
            if id(event) in correlation["valid_delivery_event_ids"]
        )
        recognized_revenue_by_selection_source_usdc: Counter[str] = Counter()
        for event in events:
            if id(event) not in correlation["valid_delivery_event_ids"]:
                continue
            metadata = event.get("metadata")
            source = (
                str(metadata.get("selection_source") or "unattributed")
                if isinstance(metadata, dict)
                else "unattributed"
            )
            recognized_revenue_by_selection_source_usdc[source] += float(
                event.get("price_usdc") or 0.0
            )
        resolved_events = [event for event in events if event["event"] == "instrument_resolved"]
        resolved_symbols = {
            str(event.get("subject")) for event in resolved_events if event.get("subject")
        }
        resolver_deliveries = sum(
            count
            for source, count in delivered_selection_source_mix.items()
            if source in {"authenticated_resolver", "public_mcp_resolver"}
        )
        published_example_payment_prompts = sum(
            1
            for event in events
            if event["event"] == "payment_required"
            and isinstance(event.get("metadata"), dict)
            and event["metadata"].get("selection_source") == "published_example_path"
        )
        outbound_destination_mix = Counter(
            str(event["metadata"].get("destination") or event.get("subject"))
            for event in events
            if event["event"] == "outbound_conversion_click"
            and isinstance(event.get("metadata"), dict)
            and (event["metadata"].get("destination") or event.get("subject"))
        )
        user_agent_mix = Counter(
            self._user_agent_family(event.get("user_agent"))
            for event in events
            if event.get("user_agent")
        )
        client_fingerprint_mix = Counter(
            str(event.get("ip_hash"))[:12]
            for event in events
            if event.get("ip_hash") and event["event"] == "http_request"
        )
        data_called = self._data_called(events, called_data_events, correlation)
        popularity = self._popularity(events, correlation)
        transport_requests = self._transport_request_summary(events)
        request_quality = self._request_quality_summary(events)
        product_performance = self._product_performance_summary(events, correlation)
        resolver_funnel = self._resolver_funnel_summary(events, correlation)
        settled_attempts = len(correlation["settled_attempts"])
        correlated_proof_attempts = len(correlation["submitted_attempts"])
        verified_authorizations = len(correlation["authorization_attempts"])
        raw_proof_events = int(event_counts["payment_proof_submitted"])
        failed_proof_events = int(event_counts["payment_failed"])
        payment_prompts = int(event_counts["payment_required"])
        live_data_requests = int(request_quality["gross_live_data_requests"])
        x402_deliveries = sum(
            1
            for event in events
            if id(event) in correlation["valid_delivery_event_ids"]
            and event.get("event") == "data_delivered"
            and self._metadata(event).get("payment_mode") == "x402"
        )
        payment_funnel = {
            "live_data_requests": live_data_requests,
            "non_monitor_live_data_requests": int(request_quality["non_monitor_requests"]),
            "known_monitor_live_data_requests": int(
                request_quality["known_monitor_requests"]
            ),
            "x402_prompts": payment_prompts,
            "proof_submissions": raw_proof_events,
            "raw_proof_submission_events": raw_proof_events,
            "correlated_proof_attempts": correlated_proof_attempts,
            "verified_authorizations": verified_authorizations,
            "failed_or_rejected_proof_events": failed_proof_events,
            "proof_failure_reasons": dict(
                Counter(
                    str(event.get("reason") or "unknown")
                    for event in events
                    if event.get("event") == "payment_failed"
                ).most_common()
            ),
            "settled_attempts": settled_attempts,
            "successful_deliveries": paid_calls,
            "x402_deliveries": x402_deliveries,
            "credit_backed_deliveries": max(paid_calls - x402_deliveries, 0),
            "prompt_to_proof_rate": (
                raw_proof_events / payment_prompts if payment_prompts else None
            ),
            "proof_event_to_correlated_attempt_rate": (
                correlated_proof_attempts / raw_proof_events if raw_proof_events else None
            ),
            "proof_to_settlement_rate": (
                settled_attempts / correlated_proof_attempts
                if correlated_proof_attempts
                else None
            ),
            "prompt_to_delivery_rate": (
                x402_deliveries / payment_prompts if payment_prompts else None
            ),
            "live_request_to_delivery_rate": (
                paid_calls / live_data_requests if live_data_requests else None
            ),
        }
        growth_funnel = self._growth_funnel(events, correlation)
        reliability = self._reliability_summary(events, correlation)
        evidence = self._source_evidence(events)
        unsupported_symbol_opportunities = self._unsupported_symbol_opportunities(events)
        most_used_service = service_mix.most_common(1)[0][0] if service_mix else None
        marketplace_metrics = self.marketplace_metrics_summary(days=days)
        confidence_reasons: list[str] = []
        if include_synthetic:
            confidence_reasons.append("Tagged test and synthetic events are included.")
        elif synthetic_events:
            confidence_reasons.append(
                "Tagged test events are excluded, but historical untagged automation cannot be identified retroactively."
            )
        if not marketplace_metrics["platforms_configured"]:
            confidence_reasons.append("No external marketplace metrics are configured.")
        if not event_counts["registry_request"]:
            confidence_reasons.append("No registry-attributed requests are present in this window.")
        if proof_submissions and not correlation["settled_attempts"]:
            confidence_reasons.append(
                f"{proof_submissions} correlated payment proof submissions are present, but none are settled."
            )
        elif not evidence["transaction_or_proof_hash_events"]:
            confidence_reasons.append("No transaction or payment-proof evidence is present.")
        if reliability["charged_delivery_failures"]:
            confidence_reasons.append(
                f"{reliability['charged_delivery_failures']} delivery attempts failed after credit or payment acceptance."
            )
        if event_counts["payment_settlement_unreconciled"]:
            confidence_reasons.append(
                f"{event_counts['payment_settlement_unreconciled']} remote settlement outcome(s) require immediate ledger reconciliation."
            )
        if correlation["uncorrelated_legacy_events"]:
            confidence_reasons.append(
                f"{correlation['uncorrelated_legacy_events']} legacy economic events lack lifecycle IDs and are excluded from decision-grade KPIs."
            )
        if correlation["correlation_conflicts"]:
            confidence_reasons.append(
                f"{correlation['correlation_conflicts']} payment attempts have conflicting terminal outcomes and are excluded."
            )
        if raw_proof_events > correlated_proof_attempts:
            confidence_reasons.append(
                f"{raw_proof_events - correlated_proof_attempts} payment proof events lack a complete correlated lifecycle and are excluded from settlement conversion."
            )
        known_monitor_share = request_quality.get("known_monitor_share")
        if known_monitor_share is not None and float(known_monitor_share) >= 0.5:
            confidence_reasons.append(
                f"Known ecosystem monitors account for {round(float(known_monitor_share) * 100, 1)}% of gross live-data attempts."
            )
        decision_confidence = {
            "level": "limited" if confidence_reasons else "decision_ready",
            "label": "Operational only" if confidence_reasons else "Decision-ready",
            "summary": (
                "Use this snapshot for reliability debugging and demand triage, not external growth or revenue claims."
                if confidence_reasons
                else "The reviewed evidence supports operational, acquisition, and monetization decisions."
            ),
            "safe_uses": ["Reliability debugging", "Demand triage", "Connector QA"],
            "unsafe_uses": (
                ["External growth claims", "Marketplace conversion", "Revenue attribution"]
                if confidence_reasons
                else []
            ),
            "reasons": confidence_reasons,
        }

        timeline = self._timeline(events, days, correlation)
        return {
            "window_days": days,
            "generated_at": utc_now_iso(),
            "telemetry_scope": {
                "include_synthetic": include_synthetic,
                "matching_events": len(all_events),
                "included_events": len(events),
                "excluded_synthetic_events": (
                    0 if include_synthetic else len(synthetic_events)
                ),
                "detected_synthetic_events": len(synthetic_events),
            },
            "overview": {
                "total_events": len(events),
                "total_http_requests": event_counts["http_request"],
                "unique_client_fingerprints": len(unique_clients),
                "paid_calls": paid_calls,
                "estimated_revenue_usdc": round(revenue, 6),
                "active_paying_wallets": len(active_wallets),
                "active_verified_principals": len(correlation["active_principals"]),
                "payment_success_rate": payment_success_rate,
                "mcp_tool_calls": event_counts["mcp_tool_call"],
                "registry_requests": event_counts["registry_request"],
                "free_discovery_calls": event_counts["free_discovery_call"],
                "first_live_price_deliveries": event_counts["first_live_price_delivered"],
                "unsupported_symbol_requests": event_counts["unsupported_symbol_request"],
                "instrument_resolutions": len(resolved_events),
                "distinct_resolved_symbols": len(resolved_symbols),
                "resolver_deliveries": resolver_deliveries,
                "published_example_payment_prompts": published_example_payment_prompts,
                "http_error_rate": self._error_rate(events),
                "http_error_rate_excluding_payment_required": self._error_rate(
                    events,
                    exclude_status_codes={402},
                ),
                "server_error_rate": reliability["server_error_rate"],
                "post_credit_failure_rate": reliability["post_credit_failure_rate"],
                "avg_latency_ms": round(mean(latencies), 2) if latencies else None,
                "p95_latency_ms": self._percentile(latencies, 95),
                "most_used_service": most_used_service,
                "live_data_requests": live_data_requests,
                "non_monitor_live_data_requests": int(
                    request_quality["non_monitor_requests"]
                ),
                "known_monitor_live_data_requests": int(
                    request_quality["known_monitor_requests"]
                ),
                "live_data_delivery_rate": payment_funnel[
                    "live_request_to_delivery_rate"
                ],
            },
            "event_counts": dict(event_counts.most_common()),
            "surface_mix": dict(surface_counts.most_common()),
            "status_mix": dict(status_counts.most_common()),
            "endpoint_mix": dict(endpoint_counts.most_common(20)),
            "paid_endpoint_mix": dict(endpoint_mix.most_common(12)),
            "asset_class_mix": dict(asset_mix.most_common()),
            "registry_mix": dict(registry_mix.most_common()),
            "registry_source_mix": dict(registry_source_mix.most_common(20)),
            "mcp_tool_mix": dict(mcp_tool_mix.most_common(20)),
            "transport_requests": transport_requests,
            "request_quality": request_quality,
            "payment_funnel": payment_funnel,
            "product_performance": product_performance,
            "resolver_funnel": resolver_funnel,
            "service_mix": dict(service_mix.most_common(20)),
            "origin_mix": dict(origin_mix.most_common(20)),
            "referrer_mix": dict(referrer_mix.most_common(20)),
            "campaign_mix": dict(campaign_mix.most_common(20)),
            "campaign_source_mix": dict(campaign_source_mix.most_common(20)),
            "selection_source_mix": dict(selection_source_mix.most_common(20)),
            "delivered_selection_source_mix": dict(
                delivered_selection_source_mix.most_common(20)
            ),
            "recognized_revenue_by_selection_source_usdc": {
                source: round(value, 6)
                for source, value in recognized_revenue_by_selection_source_usdc.most_common(20)
            },
            "outbound_destination_mix": dict(outbound_destination_mix.most_common(20)),
            "user_agent_mix": dict(user_agent_mix.most_common(20)),
            "client_fingerprint_mix": dict(client_fingerprint_mix.most_common(20)),
            "data_called": data_called,
            "popularity": popularity,
            "growth_funnel": growth_funnel,
            "reliability": reliability,
            "source_evidence": evidence,
            "decision_confidence": decision_confidence,
            "economic_correlation": {
                "unresolved_drawdowns": correlation["unresolved_drawdowns"],
                "uncorrelated_legacy_events": correlation["uncorrelated_legacy_events"],
                "correlation_conflicts": correlation["correlation_conflicts"],
                "duplicate_settlements_deduplicated": correlation["duplicate_settlements"],
                "unreconciled_settlements": event_counts[
                    "payment_settlement_unreconciled"
                ],
            },
            "unsupported_symbol_opportunities": unsupported_symbol_opportunities,
            "marketplace_metrics": marketplace_metrics,
            "failure_reasons": dict(failure_reasons.most_common(10)),
            "top_subjects": dict(top_subjects.most_common(20)),
            "timeline": timeline,
            "recent_events": [
                {key: value for key, value in event.items() if key != "ip_hash"}
                for event in reversed(events[-50:])
            ],
            "notes": [
                "Client IPs, wallets, and payment proofs are stored only as salted hashes.",
                "Paid calls count completed HTTP deliveries plus completed authenticated MCP deliveries; credit drawdown alone is not treated as delivery.",
                "First-live-price activation is counted once per privacy-safe explicit user, agent, wallet, device, or session identity.",
                "Growth-funnel identity attribution uses salted identity hashes or wallet hashes; IP fingerprints are never used as funnel identities.",
                "Unsupported-symbol opportunities include only bounded symbol-like searches with zero results; arbitrary free text is excluded.",
                "Campaign attribution retains only bounded allowlisted UTM values; full query strings are never stored.",
                "Server reliability is measured from HTTP 5xx responses and charged-delivery failures; expected payment, auth, rate-limit, and client/protocol responses are reported separately.",
                "Synthetic filtering is tag-based: testclient, smoke, and synthetic user agents plus mock/test metadata are excluded; untagged historical automation cannot be identified retroactively.",
            ],
        }

    @classmethod
    def _reliability_summary(
        cls,
        events: list[dict[str, Any]],
        correlation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Separate server failures from expected payment and client/protocol responses."""
        correlation = correlation or cls._correlate_economic_events(events)
        http_events = [event for event in events if event.get("event") == "http_request"]
        status_counts = Counter(int(event.get("status_code") or 0) for event in http_events)
        total = len(http_events)
        server_errors = sum(count for status, count in status_counts.items() if status >= 500)
        payment_required = status_counts[402]
        auth_required = status_counts[401] + status_counts[403]
        rate_limited = status_counts[429]
        client_protocol_statuses = {400, 404, 405, 406, 416, 422}
        client_protocol = sum(status_counts[status] for status in client_protocol_statuses)
        other_http_errors = sum(
            count
            for status, count in status_counts.items()
            if 400 <= status < 500
            and status not in client_protocol_statuses | {401, 402, 403, 429}
        )
        successful_http = sum(count for status, count in status_counts.items() if 200 <= status < 400)
        refunded_http_delivery_failure_events = [
            event for event in events if event.get("event") == "refunded_delivery_failed"
        ]
        refunded_http_delivery_failures = len(refunded_http_delivery_failure_events)
        charged_delivery_failures = len(correlation["valid_failure_event_ids"])
        charged_delivery_successes = len(correlation["valid_delivery_event_ids"])
        charged_delivery_attempts = charged_delivery_successes + charged_delivery_failures
        charged_delivery_failure_events = [
            event
            for event in events
            if id(event) in correlation["valid_failure_event_ids"]
        ]
        recent_cutoff = datetime.now(UTC) - timedelta(hours=24)
        charged_delivery_failures_last_24h = sum(
            1
            for event in charged_delivery_failure_events
            if (event_time := cls._event_time(event)) is not None
            and event_time >= recent_cutoff
        )
        refunded_delivery_failures_last_24h = sum(
            1
            for event in refunded_http_delivery_failure_events
            if (event_time := cls._event_time(event)) is not None
            and event_time >= recent_cutoff
        )
        latest_charged_delivery_failure_at = max(
            (str(event.get("timestamp")) for event in charged_delivery_failure_events),
            default=None,
        )
        latest_refunded_delivery_failure_at = max(
            (str(event.get("timestamp")) for event in refunded_http_delivery_failure_events),
            default=None,
        )
        return {
            "http_requests": total,
            "successful_http_responses": successful_http,
            "payment_required_responses": payment_required,
            "auth_required_responses": auth_required,
            "rate_limited_responses": rate_limited,
            "client_protocol_responses": client_protocol,
            "other_http_errors": other_http_errors,
            "server_errors": server_errors,
            "server_error_rate": round(server_errors / total, 6) if total else None,
            "charged_delivery_successes": charged_delivery_successes,
            "charged_delivery_failures": charged_delivery_failures,
            "refunded_delivery_failures": refunded_http_delivery_failures,
            "charged_delivery_failures_last_24h": charged_delivery_failures_last_24h,
            "refunded_delivery_failures_last_24h": refunded_delivery_failures_last_24h,
            "latest_charged_delivery_failure_at": latest_charged_delivery_failure_at,
            "latest_refunded_delivery_failure_at": latest_refunded_delivery_failure_at,
            "post_credit_failure_rate": (
                round(charged_delivery_failures / charged_delivery_attempts, 6)
                if charged_delivery_attempts
                else None
            ),
            "definitions": {
                "server_error_rate": "HTTP 5xx responses divided by all HTTP requests.",
                "post_credit_failure_rate": "Correlated unrecovered charged HTTP or MCP delivery failures divided by successful plus failed charged deliveries; refunded HTTP failures are reported separately.",
                "charged_delivery_failures_last_24h": "Correlated unrecovered charged HTTP or MCP delivery failures observed during the trailing 24 hours.",
                "client_protocol_responses": "HTTP 400, 404, 405, 406, 416 and 422 responses; visible for diagnosis but excluded from the server-error KPI.",
            },
        }

    @staticmethod
    def _growth_identity(event: dict[str, Any]) -> str | None:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if metadata.get("identity_trust") not in TRUSTED_IDENTITY_LEVELS:
            return None
        identity_hash = str(metadata.get("identity_hash") or "").strip()
        if identity_hash:
            return identity_hash
        return None

    @staticmethod
    def _event_time(event: dict[str, Any]) -> datetime | None:
        value = str(event.get("timestamp") or "").strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @classmethod
    def _growth_funnel(
        cls,
        events: list[dict[str, Any]],
        correlation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build privacy-safe activation, retention, and monetization cohorts."""
        correlation = correlation or cls._correlate_economic_events(events)
        eligible_event_names = {
            "mcp_tool_call",
            "payment_required",
            "credit_drawdown_success",
        }
        delivery_event_names = {"data_delivered", "mcp_data_delivered"}
        conversion_event_names = {"payment_settled"}

        eligible_first_seen: dict[str, datetime] = {}
        activations: dict[str, datetime] = {}
        activation_modes: dict[str, str] = {}
        deliveries: dict[str, list[datetime]] = {}
        conversions: dict[str, list[datetime]] = {}
        exhausted: set[str] = set()
        activation_events = 0

        for event in events:
            event_name = str(event.get("event") or "")
            event_time = cls._event_time(event)
            identity = cls._growth_identity(event)
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if event_name == "first_live_price_delivered":
                activation_events += 1
            if identity is None or event_time is None:
                continue
            if event_name in eligible_event_names:
                eligible_first_seen.setdefault(identity, event_time)
            if event_name == "first_live_price_delivered":
                activations.setdefault(identity, event_time)
                activation_modes.setdefault(identity, str(metadata.get("payment_mode") or "unknown"))
            if (
                event_name in delivery_event_names
                and id(event) in correlation["valid_delivery_event_ids"]
            ):
                deliveries.setdefault(identity, []).append(event_time)
            if (
                event_name in conversion_event_names
                and id(event) in correlation["settled_event_ids"]
            ):
                conversions.setdefault(identity, []).append(event_time)
            if (
                event_name == "credit_drawdown_failed"
                and str(event.get("reason") or "") == "insufficient_credits"
                and float(metadata.get("credits_remaining") or 0) <= 0
            ):
                exhausted.add(identity)

        activated_identities = set(activations)
        eligible_identities = set(eligible_first_seen) | activated_identities
        activation_rate = (
            len(activated_identities) / len(eligible_identities)
            if eligible_identities
            else None
        )

        time_to_value_seconds: list[float] = []
        for identity, activated_at in activations.items():
            started_at = eligible_first_seen.get(identity)
            if started_at is not None and started_at <= activated_at:
                time_to_value_seconds.append((activated_at - started_at).total_seconds())
        within_three_minutes = sum(value <= 180 for value in time_to_value_seconds)
        within_three_minutes_rate = (
            within_three_minutes / len(time_to_value_seconds)
            if time_to_value_seconds
            else None
        )

        observed_at = datetime.now(UTC)
        matured_activations = {
            identity
            for identity, activated_at in activations.items()
            if activated_at <= observed_at - timedelta(days=7)
        }
        repeated_within_seven_days = {
            identity
            for identity in matured_activations
            if sum(
                activations[identity] <= delivered_at <= activations[identity] + timedelta(days=7)
                for delivered_at in deliveries.get(identity, [])
            ) >= 2
        }
        repeat_rate = (
            len(repeated_within_seven_days) / len(matured_activations)
            if matured_activations
            else None
        )

        starter_activated = {
            identity
            for identity, mode in activation_modes.items()
            if mode == "starter_credit"
        }
        starter_converted = {
            identity
            for identity in starter_activated
            if any(converted_at >= activations[identity] for converted_at in conversions.get(identity, []))
        }
        starter_to_paid_rate = (
            len(starter_converted) / len(starter_activated)
            if starter_activated
            else None
        )

        median_time_to_value = None
        if time_to_value_seconds:
            ordered = sorted(time_to_value_seconds)
            midpoint = len(ordered) // 2
            median_time_to_value = (
                ordered[midpoint]
                if len(ordered) % 2
                else (ordered[midpoint - 1] + ordered[midpoint]) / 2
            )

        return {
            "summary": {
                "eligible_identities": len(eligible_identities),
                "activated_identities": len(activated_identities),
                "activation_rate": activation_rate,
                "activation_events": activation_events,
                "unattributed_activation_events": max(0, activation_events - len(activated_identities)),
                "median_time_to_first_live_price_seconds": median_time_to_value,
                "first_live_price_within_3m_rate": within_three_minutes_rate,
                "repeat_7d_eligible_identities": len(matured_activations),
                "repeat_7d_identities": len(repeated_within_seven_days),
                "repeat_7d_rate": repeat_rate,
                "starter_activated_identities": len(starter_activated),
                "starter_to_paid_identities": len(starter_converted),
                "starter_to_paid_rate": starter_to_paid_rate,
                "credits_exhausted_identities": len(exhausted),
            },
            "stages": [
                {"stage": "Eligible explicit identities", "identities": len(eligible_identities)},
                {"stage": "First live price", "identities": len(activated_identities)},
                {"stage": "Repeated within 7 days", "identities": len(repeated_within_seven_days)},
                {"stage": "Converted after starter", "identities": len(starter_converted)},
            ],
            "targets": {
                "first_live_price_within_3m_rate": 0.5,
                "repeat_7d_rate": 0.25,
                "starter_to_paid_rate": 0.05,
            },
            "definitions": {
                "eligible_identity": "Salted identity asserted by verified OAuth, beta-token, or x402 payer evidence; anonymous and legacy caller claims are excluded.",
                "activation": "First successfully delivered live price, recorded once per verified identity.",
                "repeat_7d": "At least two successful paid or starter-credit delivery events during the seven days beginning at activation; only mature seven-day cohorts enter the denominator.",
                "starter_to_paid": "Starter-credit activated identity later observed with a finalized x402 settlement in the selected window.",
                "measurement_boundary": "Rates include trusted identities observed inside the selected dashboard window; anonymous IP acquisition and legacy untrusted identity rows are excluded.",
            },
        }

    @staticmethod
    def _unsupported_symbol_opportunities(events: list[dict[str, Any]]) -> dict[str, Any]:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            if event.get("event") != "unsupported_symbol_request":
                continue
            subject = normalize_symbol_opportunity(str(event.get("subject") or ""))
            if subject is None:
                continue
            asset_class = str(event.get("asset_class") or "all")
            key = (subject, asset_class)
            row = rows.setdefault(
                key,
                {
                    "symbol": subject,
                    "asset_class": asset_class,
                    "request_count": 0,
                    "surfaces": set(),
                    "first_seen": event.get("timestamp"),
                    "last_seen": event.get("timestamp"),
                },
            )
            row["request_count"] += 1
            row["surfaces"].add(str(event.get("surface") or "unknown"))
            row["last_seen"] = event.get("timestamp")

        ranked = []
        for row in rows.values():
            ranked.append({**row, "surfaces": sorted(row["surfaces"])})
        ranked.sort(key=lambda row: (-row["request_count"], row["symbol"], row["asset_class"]))
        return {
            "total_requests": sum(row["request_count"] for row in ranked),
            "unique_symbol_asset_class_pairs": len(ranked),
            "rows": ranked[:100],
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    @staticmethod
    def _is_live_data_request_event(event: dict[str, Any]) -> bool:
        """Count commercial data intent without treating MCP transport as demand."""
        event_name = str(event.get("event") or "")
        if event_name == "payment_required":
            return True
        return (
            event_name == "mcp_tool_call"
            and str(event.get("tool_name") or "") in LIVE_DATA_MCP_TOOLS
        )

    @classmethod
    def _is_live_data_related_event(
        cls,
        event: dict[str, Any],
        related_event_names: set[str],
    ) -> bool:
        event_name = str(event.get("event") or "")
        if event_name in {"free_discovery_call", "registry_request"}:
            return False
        if event_name == "mcp_tool_call":
            return cls._is_live_data_request_event(event)
        return event_name in related_event_names

    @staticmethod
    def _transport_request_summary(events: list[dict[str, Any]]) -> dict[str, int]:
        summary = {
            "public_mcp_transport_requests": 0,
            "authenticated_mcp_transport_requests": 0,
            "registry_transport_requests": 0,
            "live_data_http_requests": 0,
            "other_http_requests": 0,
        }
        live_prefixes = (
            "/v1/vwap/",
            "/v1/bidask/",
            "/v1/state/",
            "/v1/vwap30m/",
            "/v1/vwap24h/",
            "/v1/fx/",
            "/v1/metal/",
            "/v1/batch",
            "/v1/briefs/market",
            "/v1/checks/pre-trade",
            "/v1/receipts/price",
            "/v1/snapshots/macro",
            "/v1/monitors/evaluate",
            "/v1/indicators/",
            "/v1/signals/",
            "/v1/rwa/benchmark/blocksize",
        )
        for event in events:
            if event.get("event") != "http_request":
                continue
            endpoint = str(event.get("endpoint") or "")
            surface = str(event.get("surface") or "")
            if any(
                endpoint == path or endpoint.startswith(f"{path}/")
                for path in PUBLIC_MCP_ENDPOINTS
            ):
                summary["public_mcp_transport_requests"] += 1
            elif any(
                endpoint == path or endpoint.startswith(f"{path}/")
                for path in AUTHENTICATED_MCP_ENDPOINTS
            ):
                summary["authenticated_mcp_transport_requests"] += 1
            elif surface == "registry" or registry_name_for_path(endpoint):
                summary["registry_transport_requests"] += 1
            elif endpoint.startswith(live_prefixes):
                summary["live_data_http_requests"] += 1
            else:
                summary["other_http_requests"] += 1
        summary["total_transport_requests"] = sum(summary.values())
        return summary

    @classmethod
    def _request_actor_class(cls, event: dict[str, Any]) -> str:
        """Classify intent conservatively without pretending every agent is a user."""
        metadata = cls._metadata(event)
        user_agent = str(event.get("user_agent") or "").lower()
        if cls._is_synthetic_event(event):
            return "internal_test"
        if any(marker in user_agent for marker in KNOWN_MONITOR_USER_AGENT_MARKERS):
            return "ecosystem_monitor"
        selection_source = str(metadata.get("selection_source") or "")
        if selection_source == "published_example_path":
            return "published_example"
        if metadata.get("identity_trust") in TRUSTED_IDENTITY_LEVELS:
            return "verified_agent_or_payer"
        if selection_source in {"authenticated_resolver", "public_mcp_resolver"}:
            return "resolver_selected"
        if any(
            client in user_agent
            for client in ("claude", "anthropic", "chatgpt", "openai", "cursor")
        ):
            return "named_agent_client"
        if user_agent:
            return "unverified_client"
        return "unknown"

    @classmethod
    def _request_quality_summary(cls, events: list[dict[str, Any]]) -> dict[str, Any]:
        actor_mix: Counter[str] = Counter()
        prompt_actor_mix: Counter[str] = Counter()
        selection_mix: Counter[str] = Counter()
        gross_requests = 0
        known_monitor_requests = 0
        internal_test_requests = 0
        for event in events:
            if not cls._is_live_data_request_event(event):
                continue
            gross_requests += 1
            actor_class = cls._request_actor_class(event)
            actor_mix[actor_class] += 1
            if actor_class == "ecosystem_monitor":
                known_monitor_requests += 1
            elif actor_class == "internal_test":
                internal_test_requests += 1
            if event.get("event") == "payment_required":
                prompt_actor_mix[actor_class] += 1
            selection_mix[
                str(cls._metadata(event).get("selection_source") or "unattributed")
            ] += 1
        non_monitor_requests = max(
            gross_requests - known_monitor_requests - internal_test_requests,
            0,
        )
        return {
            "gross_live_data_requests": gross_requests,
            "known_monitor_requests": known_monitor_requests,
            "internal_test_requests": internal_test_requests,
            "non_monitor_requests": non_monitor_requests,
            "known_monitor_share": (
                known_monitor_requests / gross_requests if gross_requests else None
            ),
            "actor_mix": dict(actor_mix.most_common()),
            "prompt_actor_mix": dict(prompt_actor_mix.most_common()),
            "selection_source_mix": dict(selection_mix.most_common()),
            "status": "measured" if gross_requests else "no_live_data_requests",
            "definitions": {
                "gross_live_data_requests": "Payment-required HTTP attempts plus authenticated live-data MCP tool calls; transport and discovery tools are excluded.",
                "known_monitor_requests": "Gross attempts whose user agent explicitly identifies a probe, monitor, scanner, health check, uptime check, or synthetic runner.",
                "non_monitor_requests": "Gross attempts excluding known monitors and tagged internal tests. This is a demand candidate, not proof of a unique paying user.",
                "published_example": "A live-data path matching a published example; kept visible because a real user may copy it, but not treated as independent instrument preference.",
            },
        }

    @classmethod
    def _product_performance_summary(
        cls,
        events: list[dict[str, Any]],
        correlation: dict[str, Any],
    ) -> dict[str, Any]:
        grouped: dict[str, dict[str, Any]] = {}
        lifecycle_events = {
            "payment_proof_submitted",
            "payment_authorization_verified",
            "payment_settled",
            "payment_failed",
            "payment_settlement_unreconciled",
            "data_delivered",
            "charged_delivery_failed",
            "refunded_delivery_failed",
            "mcp_credit_drawdown_success",
            "mcp_credit_drawdown_failed",
            "mcp_data_delivered",
            "mcp_tool_error",
        }
        for event in events:
            event_name = str(event.get("event") or "")
            is_live_request = cls._is_live_data_request_event(event)
            if not is_live_request and event_name not in lifecycle_events:
                continue
            product_id = cls._service_for_event(event)
            if product_id in {
                "instrument_search",
                "instrument_list",
                "endpoint_builder",
                "pricing_info",
                "catalog_search",
                "catalog_fetch",
                "unknown",
            }:
                continue
            family = (
                "market_intelligence_package"
                if product_id in MARKET_INTELLIGENCE_PRODUCT_IDS
                else "raw_market_data"
            )
            row = grouped.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "product_family": family,
                    "gross_attempts": 0,
                    "known_monitor_attempts": 0,
                    "non_monitor_attempts": 0,
                    "payment_prompts": 0,
                    "raw_proof_events": 0,
                    "verified_authorizations": 0,
                    "settled_attempts": 0,
                    "validated_deliveries": 0,
                    "legacy_delivery_events": 0,
                    "payment_failures": 0,
                    "post_charge_failures": 0,
                    "credits_spent": 0.0,
                    "recognized_revenue_usdc": 0.0,
                },
            )
            actor_class = cls._request_actor_class(event)
            metadata = cls._metadata(event)
            if is_live_request:
                row["gross_attempts"] += 1
                if actor_class == "ecosystem_monitor":
                    row["known_monitor_attempts"] += 1
                elif actor_class != "internal_test":
                    row["non_monitor_attempts"] += 1
            if event_name == "payment_required":
                row["payment_prompts"] += 1
            elif event_name == "payment_proof_submitted":
                row["raw_proof_events"] += 1
            elif event_name == "payment_authorization_verified":
                row["verified_authorizations"] += 1
            elif id(event) in correlation["settled_event_ids"]:
                row["settled_attempts"] += 1
                row["recognized_revenue_usdc"] += float(event.get("price_usdc") or 0.0)
            elif event_name == "payment_failed":
                row["payment_failures"] += 1
            if id(event) in correlation["valid_delivery_event_ids"]:
                row["validated_deliveries"] += 1
                row["credits_spent"] += float(metadata.get("credits_spent") or 0.0)
            elif event_name in {"data_delivered", "mcp_data_delivered"}:
                row["legacy_delivery_events"] += 1
            if (
                id(event) in correlation["valid_failure_event_ids"]
                or event_name == "refunded_delivery_failed"
            ):
                row["post_charge_failures"] += 1

        rows = list(grouped.values())
        for row in rows:
            row["credits_spent"] = round(float(row["credits_spent"]), 3)
            row["recognized_revenue_usdc"] = round(
                float(row["recognized_revenue_usdc"]), 6
            )
            row["prompt_to_delivery_rate"] = (
                row["validated_deliveries"] / row["payment_prompts"]
                if row["payment_prompts"]
                else None
            )
        rows.sort(
            key=lambda row: (
                -int(row["gross_attempts"]),
                str(row["product_family"]),
                str(row["product_id"]),
            )
        )

        family_rows: list[dict[str, Any]] = []
        for family in ("raw_market_data", "market_intelligence_package"):
            members = [row for row in rows if row["product_family"] == family]
            family_rows.append(
                {
                    "product_family": family,
                    "products_observed": len(members),
                    "gross_attempts": sum(int(row["gross_attempts"]) for row in members),
                    "known_monitor_attempts": sum(
                        int(row["known_monitor_attempts"]) for row in members
                    ),
                    "non_monitor_attempts": sum(
                        int(row["non_monitor_attempts"]) for row in members
                    ),
                    "payment_prompts": sum(
                        int(row["payment_prompts"]) for row in members
                    ),
                    "settled_attempts": sum(
                        int(row["settled_attempts"]) for row in members
                    ),
                    "validated_deliveries": sum(
                        int(row["validated_deliveries"]) for row in members
                    ),
                    "recognized_revenue_usdc": round(
                        sum(float(row["recognized_revenue_usdc"]) for row in members),
                        6,
                    ),
                }
            )
        return {
            "rows": rows[:50],
            "family_rows": family_rows,
            "market_intelligence_rows": [
                row
                for row in rows
                if row["product_family"] == "market_intelligence_package"
            ],
            "status": "measured" if rows else "no_product_activity",
            "definitions": {
                "attempt": "One gross commercial live-data request signal; known monitoring is shown separately.",
                "validated_delivery": "A delivery joined to a finalized x402 settlement or a correlated successful credit drawdown.",
                "legacy_delivery": "A historical delivery event without the lifecycle identifiers required for decision-grade attribution.",
                "recognized_revenue": "Finalized, deduplicated x402 settlements only.",
            },
        }

    @classmethod
    def _resolver_funnel_summary(
        cls,
        events: list[dict[str, Any]],
        correlation: dict[str, Any],
    ) -> dict[str, Any]:
        search_events = 0
        resolved_events = 0
        zero_or_unsupported = 0
        resolver_live_attempts = 0
        resolver_deliveries = 0
        resolved_symbols: set[str] = set()
        for event in events:
            event_name = str(event.get("event") or "")
            endpoint = str(event.get("endpoint") or "")
            tool_name = str(event.get("tool_name") or "")
            selection_source = str(cls._metadata(event).get("selection_source") or "")
            if (
                event_name == "mcp_tool_call" and tool_name == "search_pairs"
            ) or (
                event_name == "free_discovery_call" and endpoint == "/v1/search"
            ):
                search_events += 1
            if event_name == "instrument_resolved":
                resolved_events += 1
                if event.get("subject"):
                    resolved_symbols.add(str(event["subject"]))
            if event_name == "unsupported_symbol_request":
                zero_or_unsupported += 1
            if (
                cls._is_live_data_request_event(event)
                and selection_source in {"authenticated_resolver", "public_mcp_resolver"}
            ):
                resolver_live_attempts += 1
            if (
                id(event) in correlation["valid_delivery_event_ids"]
                and selection_source in {"authenticated_resolver", "public_mcp_resolver"}
            ):
                resolver_deliveries += 1
        return {
            "search_events": search_events,
            "resolved_events": resolved_events,
            "distinct_resolved_symbols": len(resolved_symbols),
            "zero_or_unsupported_events": zero_or_unsupported,
            "resolver_live_attempts": resolver_live_attempts,
            "resolver_deliveries": resolver_deliveries,
            "search_to_resolution_rate": (
                resolved_events / search_events if search_events else None
            ),
            "resolver_to_delivery_rate": (
                resolver_deliveries / resolver_live_attempts
                if resolver_live_attempts
                else None
            ),
            "status": (
                "measured"
                if resolved_events or resolver_live_attempts
                else "collecting_after_instrumentation"
            ),
            "definitions": {
                "search": "Explicit HTTP or MCP instrument-search action; HTTP transport duplicates are excluded.",
                "resolution": "A query mapped to a canonical supported instrument.",
                "resolver_live_attempt": "A paid endpoint or live tool selected from the public or authenticated resolver.",
                "resolver_delivery": "A resolver-selected attempt that reached validated paid or credit-backed delivery.",
            },
        }

    @staticmethod
    def _service_for_event(event: dict[str, Any]) -> str:
        endpoint = str(event.get("endpoint") or "")
        tool_name = str(event.get("tool_name") or "")
        if endpoint in PRODUCT_ROUTE_IDS:
            return PRODUCT_ROUTE_IDS[endpoint]
        if endpoint.startswith("/v1/vwap"):
            return "vwap"
        if endpoint.startswith("/v1/bidask"):
            return "bidask"
        if endpoint.startswith("/v1/state"):
            return "state"
        if endpoint.startswith("/v1/fx"):
            return "fx"
        if endpoint.startswith("/v1/metal"):
            return "metal"
        if endpoint.startswith("/v1/batch"):
            return "batch"
        if endpoint.startswith("/v1/search") or tool_name == "search_pairs":
            return "instrument_search"
        if endpoint.startswith("/v1/instruments") or tool_name == "list_instruments":
            return "instrument_list"
        if tool_name == "get_market_data_endpoint":
            return "endpoint_builder"
        if tool_name == "get_pricing_info":
            return "pricing_info"
        if tool_name == "search":
            return "catalog_search"
        if tool_name == "fetch":
            return "catalog_fetch"
        if tool_name.startswith("get_"):
            return tool_name.removeprefix("get_")
        return endpoint or tool_name or "unknown"

    @staticmethod
    def _origin_for_event(event: dict[str, Any]) -> str:
        surface = str(event.get("surface") or "unknown")
        referrer = UsageEventStore._referrer_host(event.get("referrer"))
        user_agent = UsageEventStore._user_agent_family(event.get("user_agent"))
        if referrer != "unknown":
            return f"{surface} from {referrer}"
        if user_agent != "unknown":
            return f"{surface} via {user_agent}"
        return surface

    @staticmethod
    def _referrer_host(referrer: Any) -> str:
        if not referrer:
            return "unknown"
        try:
            parsed = urlparse(str(referrer))
        except ValueError:
            return "unknown"
        return parsed.netloc or "direct"

    @staticmethod
    def _registry_source_for_event(event: dict[str, Any]) -> str | None:
        endpoint = str(event.get("endpoint") or "").lower()
        referrer = str(event.get("referrer") or "").lower()
        user_agent = str(event.get("user_agent") or "").lower()
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        registry = str(metadata.get("registry") or "").lower()
        haystack = " ".join([endpoint, referrer, user_agent, registry])

        if event.get("event") != "registry_request" and not any(
            marker in haystack
            for marker in (
                "glama",
                "pay.sh",
                "pay-sh",
                "paysh",
                "pay-skills",
                "solana-foundation/pay-skills",
                "smithery",
                "x402scan",
                "modelcontextprotocol",
                "mcp-registry",
                "awesome-mcp",
                "github",
                "gitlab",
            )
        ):
            return None

        if "glama" in haystack:
            return "Glama"
        if "pay.sh" in haystack or "pay-sh" in haystack or "paysh" in haystack:
            return "Pay.sh"
        if "pay-skills" in haystack or "solana-foundation/pay-skills" in haystack:
            return "Pay.sh"
        if "smithery" in haystack:
            return "Smithery"
        if "x402scan" in haystack:
            return "x402scan"
        if "awesome-mcp" in haystack:
            return "Awesome MCP"
        if "modelcontextprotocol" in haystack or "mcp-registry" in haystack:
            return "MCP Registry"
        if "github" in haystack:
            return "GitHub"
        if "gitlab" in haystack:
            return "GitLab"
        if endpoint == "/server.json" or registry == "mcp_registry":
            return "MCP Registry"
        if endpoint == "/openapi.json" or registry == "openapi":
            return "OpenAPI crawlers"
        if registry == "x402_directory":
            return "x402 Directory"
        if registry == "listing_asset":
            return "Listing asset crawler"
        return "Unknown registry/direct"

    @staticmethod
    def _counts_toward_registry_source(event: dict[str, Any]) -> bool:
        if event.get("event") == "registry_request":
            return True
        if event.get("surface") == "registry":
            return False
        referrer = str(event.get("referrer") or "").lower()
        user_agent = str(event.get("user_agent") or "").lower()
        return any(
            marker in f"{referrer} {user_agent}"
            for marker in (
                "glama",
                "pay.sh",
                "pay-sh",
                "paysh",
                "pay-skills",
                "solana-foundation/pay-skills",
                "smithery",
                "x402scan",
                "modelcontextprotocol",
                "mcp-registry",
                "awesome-mcp",
                "github",
                "gitlab",
            )
        )

    @staticmethod
    def _user_agent_family(user_agent: Any) -> str:
        value = str(user_agent or "").lower()
        if not value:
            return "unknown"
        if "claude" in value or "anthropic" in value:
            return "claude"
        if "cursor" in value:
            return "cursor"
        if "chatgpt" in value or "openai" in value:
            return "chatgpt"
        if "curl" in value:
            return "curl"
        if "python" in value or "httpx" in value or "requests" in value:
            return "python"
        if "node" in value or "undici" in value:
            return "node"
        if any(browser in value for browser in ("chrome", "safari", "firefox", "edg/")):
            return "browser"
        return "other"

    @classmethod
    def _data_called(
        cls,
        events: list[dict[str, Any]],
        called_data_events: set[str],
        correlation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        correlation = correlation or cls._correlate_economic_events(events)
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for event in events:
            if not event.get("subject"):
                continue
            if (
                event.get("event") == "mcp_tool_call"
                and not cls._is_live_data_request_event(event)
            ):
                continue
            if event.get("event") == "free_discovery_call":
                continue
            if event["event"] not in called_data_events:
                continue
            service = cls._service_for_event(event)
            subject = str(event.get("subject") or "unknown")
            asset_class = str(event.get("asset_class") or "unknown")
            surface = str(event.get("surface") or "unknown")
            key = (service, subject, asset_class, surface)
            row = grouped.setdefault(
                key,
                {
                    "service": service,
                    "subject": subject,
                    "asset_class": asset_class,
                    "surface": surface,
                    "calls": 0,
                    "paid_successes": 0,
                    "_request_signals": 0,
                    "_delivery_attempts": 0,
                    "_payment_ids": set(),
                    "first_seen": event.get("timestamp"),
                    "last_seen": event.get("timestamp"),
                    "latest_event": event.get("event"),
                    "latest_status_code": event.get("status_code"),
                    "latest_outcome": cls._outcome_for_event(event),
                    "payment_prompted": False,
                    "prompt_price_usdc": None,
                    "revenue_usdc": 0.0,
                },
            )
            if str(event.get("timestamp") or "") < str(row["first_seen"] or ""):
                row["first_seen"] = event.get("timestamp")
            preserve_terminal_outcome = (
                event.get("event") == "http_request"
                and row.get("latest_event")
                in {
                    "data_delivered",
                    "mcp_data_delivered",
                    "charged_delivery_failed",
                    "mcp_tool_error",
                }
            )
            if (
                str(event.get("timestamp") or "") >= str(row["last_seen"] or "")
                and not preserve_terminal_outcome
            ):
                row["last_seen"] = event.get("timestamp")
                row["latest_event"] = event.get("event")
                row["latest_status_code"] = event.get("status_code")
                row["latest_outcome"] = cls._outcome_for_event(event)
            if cls._is_live_data_request_event(event):
                row["_request_signals"] += 1
            if (
                id(event) in correlation["valid_delivery_event_ids"]
                or id(event) in correlation["valid_failure_event_ids"]
            ):
                row["_delivery_attempts"] += 1
            if event["event"] == "payment_required":
                row["payment_prompted"] = True
                row["prompt_price_usdc"] = float(event.get("price_usdc") or 0.0)
            if id(event) in correlation["valid_delivery_event_ids"]:
                row["paid_successes"] += 1
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if (
                id(event) in correlation["valid_delivery_event_ids"]
                and metadata.get("payment_mode") == "x402"
                and event.get("price_usdc") is not None
            ):
                payment_id = str(metadata.get("payment_id") or "")
                if payment_id and payment_id not in row["_payment_ids"]:
                    row["_payment_ids"].add(payment_id)
                    row["revenue_usdc"] += float(event.get("price_usdc") or 0.0)

        rows = list(grouped.values())
        for row in rows:
            row.pop("_payment_ids")
            row["calls"] = max(
                int(row.pop("_request_signals")),
                int(row.pop("_delivery_attempts")),
            )
            row["revenue_usdc"] = round(row["revenue_usdc"], 6)
        rows.sort(key=lambda row: (-int(row["calls"]), row["service"], row["subject"]))
        return rows[:50]

    @classmethod
    def _popularity(
        cls,
        events: list[dict[str, Any]],
        correlation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        correlation = correlation or cls._correlate_economic_events(events)
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        request_events = {
            "free_discovery_call",
            "mcp_tool_call",
            "payment_required",
        }
        delivered_events = {
            "data_delivered",
        }
        blocked_events = {
            "payment_required",
            "credit_drawdown_failed",
            "mcp_credit_drawdown_failed",
            "payment_failed",
        }
        failed_after_credit_events = {
            "mcp_tool_error",
            "charged_delivery_failed",
            "refunded_delivery_failed",
        }
        accounting_events = {"credit_drawdown_success"}

        for event in events:
            event_name = str(event.get("event") or "")
            is_live_request = cls._is_live_data_request_event(event)
            if event_name == "mcp_tool_call" and not is_live_request:
                continue
            if event_name == "free_discovery_call":
                continue
            if event_name not in (
                request_events
                | delivered_events
                | blocked_events
                | failed_after_credit_events
                | accounting_events
                | {"mcp_credit_drawdown_success", "mcp_data_delivered"}
            ):
                continue
            service = cls._service_for_event(event)
            subject = str(
                event.get("subject")
                or event.get("endpoint")
                or event.get("tool_name")
                or "unknown"
            )
            surface = str(event.get("surface") or "unknown")
            key = (service, subject, surface)
            row = grouped.setdefault(
                key,
                {
                    "service": service,
                    "subject": subject,
                    "surface": surface,
                    "requested": 0,
                    "payment_prompts": 0,
                    "delivered": 0,
                    "blocked": 0,
                    "failed_after_credit": 0,
                    "refunded_after_credit": 0,
                    "credits_spent": 0.0,
                    "estimated_revenue_usdc": 0.0,
                    "synthetic_events": 0,
                    "_payment_ids": set(),
                    "first_seen": event.get("timestamp"),
                    "last_seen": event.get("timestamp"),
                    "latest_outcome": cls._outcome_for_event(event),
                },
            )
            if str(event.get("timestamp") or "") < str(row["first_seen"] or ""):
                row["first_seen"] = event.get("timestamp")
            if str(event.get("timestamp") or "") >= str(row["last_seen"] or ""):
                row["last_seen"] = event.get("timestamp")
                row["latest_outcome"] = cls._outcome_for_event(event)

            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if is_live_request:
                row["requested"] += 1
            if event_name == "payment_required":
                row["payment_prompts"] += 1
            if id(event) in correlation["valid_delivery_event_ids"]:
                row["delivered"] += 1
                if event_name == "data_delivered" and metadata.get("payment_mode") == "x402":
                    payment_id = str(metadata.get("payment_id") or "")
                    if payment_id and payment_id not in row["_payment_ids"]:
                        row["_payment_ids"].add(payment_id)
                        row["estimated_revenue_usdc"] += float(event.get("price_usdc") or 0.0)
            if event_name in blocked_events:
                row["blocked"] += 1
            if (
                id(event) in correlation["valid_failure_event_ids"]
                or event_name == "refunded_delivery_failed"
            ):
                row["failed_after_credit"] += 1
            if event_name == "refunded_delivery_failed":
                row["refunded_after_credit"] += 1
            if id(event) in correlation["valid_delivery_event_ids"]:
                row["credits_spent"] += float(metadata.get("credits_spent") or 0.0)
            if event_name == "refunded_delivery_failed":
                row["credits_spent"] += float(metadata.get("credits_spent") or 0.0)
                row["credits_spent"] -= float(metadata.get("credits_refunded") or 0.0)
            if cls._is_synthetic_event(event):
                row["synthetic_events"] += 1

        rows = list(grouped.values())
        for row in rows:
            row.pop("_payment_ids")
            row["requested"] = max(
                int(row["requested"]),
                int(row["delivered"]) + int(row["failed_after_credit"]),
            )
            row["credits_spent"] = round(float(row["credits_spent"] or 0.0), 3)
            row["estimated_revenue_usdc"] = round(float(row["estimated_revenue_usdc"] or 0.0), 6)
            row["popularity_score"] = (
                int(row["requested"])
                + (2 * int(row["delivered"]))
                + float(row["credits_spent"] or 0.0)
                - int(row["failed_after_credit"])
            )
            if int(row["delivered"]) > 0:
                row["leading_outcome"] = "Data delivered"
            elif int(row["blocked"]) > 0:
                row["leading_outcome"] = "Blocked or prompted"
            elif int(row["failed_after_credit"]) > 0:
                row["leading_outcome"] = "Credit used then failed"
            else:
                row["leading_outcome"] = row["latest_outcome"]

        rows.sort(
            key=lambda row: (
                -float(row["popularity_score"]),
                -int(row["requested"]),
                row["service"],
                row["subject"],
            )
        )
        return {
            "total_requested": sum(int(row["requested"]) for row in rows),
            "total_delivered": sum(int(row["delivered"]) for row in rows),
            "total_blocked": sum(int(row["blocked"]) for row in rows),
            "total_failed_after_credit": sum(int(row["failed_after_credit"]) for row in rows),
            "total_refunded_after_credit": sum(int(row["refunded_after_credit"]) for row in rows),
            "total_credits_spent": round(sum(float(row["credits_spent"] or 0.0) for row in rows), 3),
            "synthetic_events": sum(int(row["synthetic_events"]) for row in rows),
            "rows": rows[:50],
        }

    @staticmethod
    def _is_synthetic_event(event: dict[str, Any]) -> bool:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        user_agent = str(event.get("user_agent") or "").lower()
        subject = str(event.get("subject") or "").lower()
        return (
            "testclient" in user_agent
            or "smoke" in user_agent
            or "synthetic" in user_agent
            or bool(metadata.get("mock"))
            or bool(metadata.get("synthetic"))
            or bool(metadata.get("test"))
            or subject.startswith("mock_")
            or subject.startswith("test_")
        )

    @classmethod
    def _source_evidence(cls, events: list[dict[str, Any]]) -> dict[str, Any]:
        evidence_events = [
            event
            for event in events
            if event.get("event")
            in {
                "payment_required",
                "payment_proof_submitted",
                "payment_authorization_verified",
                "payment_settled",
                "payment_failed",
                "payment_settlement_unreconciled",
                "data_delivered",
                "charged_delivery_failed",
                "refunded_delivery_failed",
                "credit_drawdown_success",
                "credit_drawdown_failed",
                "registry_request",
                "mcp_tool_call",
                "mcp_credit_drawdown_success",
                "mcp_data_delivered",
                "mcp_tool_error",
            }
        ]
        synthetic_count = sum(1 for event in evidence_events if cls._is_synthetic_event(event))
        registry_count = sum(1 for event in evidence_events if event.get("event") == "registry_request")
        correlation = cls._correlate_economic_events(events)
        decision_grade_ids = (
            correlation["settled_event_ids"]
            | correlation["valid_delivery_event_ids"]
        )
        paid_evidence_count = sum(
            1 for event in evidence_events if id(event) in decision_grade_ids
        )
        tx_hash_evidence_count = sum(
            1
            for event in evidence_events
            if (
                isinstance(event.get("metadata"), dict)
                and (
                    event["metadata"].get("tx_hash")
                    or event["metadata"].get("proof_hash")
                    or event["metadata"].get("transaction_hash")
                )
            )
        )
        rows = []
        for event in reversed(evidence_events[-25:]):
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            rows.append(
                {
                    "timestamp": event.get("timestamp"),
                    "event": event.get("event"),
                    "surface": event.get("surface"),
                    "endpoint": event.get("endpoint"),
                    "subject": event.get("subject"),
                    "status_code": event.get("status_code"),
                    "source": cls._registry_source_for_event(event)
                    or cls._origin_for_event(event),
                    "user_agent_family": cls._user_agent_family(event.get("user_agent")),
                    "referrer_host": cls._referrer_host(event.get("referrer")),
                    "has_transaction_or_proof_hash": bool(
                        metadata.get("tx_hash")
                        or metadata.get("proof_hash")
                        or metadata.get("transaction_hash")
                    ),
                    "synthetic": cls._is_synthetic_event(event),
                }
            )

        return {
            "events_reviewed": len(evidence_events),
            "synthetic_events": synthetic_count,
            "registry_events": registry_count,
            "paid_evidence_events": paid_evidence_count,
            "transaction_or_proof_hash_events": tx_hash_evidence_count,
            "recent_rows": rows,
        }

    @staticmethod
    def _outcome_for_event(event: dict[str, Any]) -> str:
        event_name = str(event.get("event") or "")
        status_code = event.get("status_code")
        if event_name == "payment_required" or status_code == 402:
            return "Prompted to pay; no data returned"
        if event_name in {"data_delivered", "mcp_data_delivered"}:
            return "Data returned after payment or credits"
        if event_name == "mcp_credit_drawdown_success":
            return "Credits accepted; waiting for delivery result"
        if event_name == "mcp_tool_error":
            return "Credit used then refunded after data retrieval failed"
        if event_name == "charged_delivery_failed":
            return "Charged or credited call failed; refund or retry needed"
        if event_name == "refunded_delivery_failed":
            return "Delivery failed; starter credits were refunded and retry is safe"
        if event_name == "payment_authorization_verified":
            return "Payment authorization verified; waiting for settlement"
        if event_name == "payment_verified":
            return "Payment accepted; waiting for delivery result"
        if event_name == "payment_settled":
            return "Payment settled and durably finalized"
        if event_name == "credit_drawdown_success":
            return "Credits accepted; waiting for delivery result"
        if event_name == "payment_failed":
            return "Payment failed; no data returned"
        if event_name == "payment_settlement_unreconciled":
            return "Payment settled remotely; operator reconciliation required"
        if isinstance(status_code, int) and status_code >= 500:
            return "Upstream/server error; no data returned"
        if isinstance(status_code, int) and status_code >= 400:
            return "Request rejected; no data returned"
        if isinstance(status_code, int) and status_code < 400:
            return "Data or metadata returned"
        if event_name == "mcp_tool_call":
            return "MCP tool requested"
        return "Observed"

    @staticmethod
    def _error_rate(
        events: list[dict[str, Any]],
        *,
        exclude_status_codes: set[int] | None = None,
    ) -> float | None:
        exclude_status_codes = exclude_status_codes or set()
        http_events = [event for event in events if event["event"] == "http_request"]
        if exclude_status_codes:
            http_events = [
                event
                for event in http_events
                if int(event.get("status_code") or 0) not in exclude_status_codes
            ]
        if not http_events:
            return None
        errors = sum(1 for event in http_events if int(event.get("status_code") or 0) >= 400)
        return round(errors / len(http_events), 4)

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
        return round(ordered[index], 2)

    @classmethod
    def _timeline(
        cls,
        events: list[dict[str, Any]],
        days: int,
        correlation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        correlation = correlation or cls._correlate_economic_events(events)
        start = datetime.now(UTC).date() - timedelta(days=days - 1)
        buckets: dict[str, dict[str, Any]] = {}
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            buckets[day] = {
                "date": day,
                "http_requests": 0,
                "paid_calls": 0,
                "mcp_tool_calls": 0,
                "registry_requests": 0,
                "registry_sources": {},
                "revenue_usdc": 0.0,
            }
        for event in events:
            day = str(event["timestamp"])[:10]
            if day not in buckets:
                continue
            if event["event"] == "http_request":
                buckets[day]["http_requests"] += 1
            if id(event) in correlation["valid_delivery_event_ids"]:
                buckets[day]["paid_calls"] += 1
            if event["event"] == "mcp_tool_call":
                buckets[day]["mcp_tool_calls"] += 1
            if event["event"] == "registry_request":
                buckets[day]["registry_requests"] += 1
                source = UsageEventStore._registry_source_for_event(event) or "Unknown registry/direct"
                registry_sources = buckets[day]["registry_sources"]
                registry_sources[source] = int(registry_sources.get(source, 0)) + 1
            if id(event) in correlation["settled_event_ids"]:
                buckets[day]["revenue_usdc"] += float(event.get("price_usdc") or 0.0)

        for bucket in buckets.values():
            bucket["revenue_usdc"] = round(bucket["revenue_usdc"], 6)
        return list(buckets.values())


_GLOBAL_STORE: UsageEventStore | None = None


def configure_global_store(store: UsageEventStore | None) -> None:
    global _GLOBAL_STORE
    _GLOBAL_STORE = store


def get_global_store() -> UsageEventStore | None:
    return _GLOBAL_STORE


def record_usage_event(event: str, **kwargs: Any) -> None:
    store = get_global_store()
    if store is None:
        return
    try:
        store.record(event, **kwargs)
    except Exception:
        # Telemetry must never break the product path.
        return


def record_usage_event_once(event: str, identity_hash: str | None, **kwargs: Any) -> bool:
    """Record a milestone once per privacy-safe identity without affecting product flow."""
    store = get_global_store()
    if store is None or not identity_hash:
        return False
    try:
        if not store.claim_milestone(event, identity_hash):
            return False
        store.record(event, **kwargs)
        return True
    except Exception:
        return False
