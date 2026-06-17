"""Privacy-safe local usage telemetry for Blocksize agent surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlparse


REGISTRY_ENDPOINTS = {
    "/server.json": "mcp_registry",
    "/mcp/manifest.json": "mcp_manifest",
    "/.well-known/glama.json": "glama",
    "/.well-known/mcp-registry-auth": "mcp_registry_auth",
    "/.well-known/x402": "x402_directory",
    "/openapi.json": "openapi",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def fingerprint(value: str | None, *, salt_env: str = "OBSERVABILITY_HASH_SALT") -> str | None:
    """Hash user-identifying values before storing telemetry."""
    if not value:
        return None
    salt = os.environ.get(salt_env, "blocksize-agentic-payments-observability")
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


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
    if registry_name_for_path(path):
        return "registry"
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

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM usage_events")

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

    def summarize(self, *, days: int = 30) -> dict[str, Any]:
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

        events = [self._row_to_dict(row) for row in rows]
        event_counts = Counter(event["event"] for event in events)
        surface_counts = Counter(event.get("surface") or "unknown" for event in events)
        endpoint_counts = Counter(event.get("endpoint") or "unknown" for event in events)
        status_counts = Counter(str(event.get("status_code") or "unknown") for event in events)
        unique_clients = {event.get("ip_hash") for event in events if event.get("ip_hash")}
        active_wallets = {
            event.get("wallet_hash")
            for event in events
            if event.get("wallet_hash")
            and event["event"] in {"payment_verified", "credit_drawdown_success", "bulk_credit_claimed"}
        }

        paid_call_events = {
            "payment_verified",
            "credit_drawdown_success",
            "mcp_credit_drawdown_success",
        }
        called_data_events = {
            "free_discovery_call",
            "mcp_tool_call",
            "payment_required",
            "payment_verified",
            "credit_drawdown_success",
            "mcp_credit_drawdown_success",
        }
        revenue_events = {"payment_verified", "bulk_credit_claimed"}
        paid_calls = sum(1 for event in events if event["event"] in paid_call_events)
        revenue = sum(
            float(event.get("price_usdc") or 0.0)
            for event in events
            if event["event"] in revenue_events
        )
        proof_submissions = event_counts["payment_proof_submitted"]
        payment_success_rate = (
            event_counts["payment_verified"] / proof_submissions if proof_submissions else None
        )

        latencies = [
            float(event["latency_ms"])
            for event in events
            if event.get("event") == "http_request" and event.get("latency_ms") is not None
        ]
        endpoint_mix = Counter(
            event.get("endpoint") or "unknown"
            for event in events
            if event["event"] in paid_call_events
        )
        asset_mix = Counter(
            event.get("asset_class") or "unknown"
            for event in events
            if event["event"] in paid_call_events
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
            if event["event"] in {"payment_failed", "credit_drawdown_failed", "mcp_tool_error"}
        )
        top_subjects = Counter(
            event.get("subject") or "unknown"
            for event in events
            if event.get("subject")
            and event["event"] in {"free_discovery_call", "mcp_tool_call", "payment_required"}
        )
        service_mix = Counter(
            self._service_for_event(event)
            for event in events
            if event["event"] in called_data_events
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
        data_called = self._data_called(events, called_data_events)
        most_used_service = service_mix.most_common(1)[0][0] if service_mix else None

        timeline = self._timeline(events, days)
        return {
            "window_days": days,
            "generated_at": utc_now_iso(),
            "overview": {
                "total_events": len(events),
                "total_http_requests": event_counts["http_request"],
                "unique_client_fingerprints": len(unique_clients),
                "paid_calls": paid_calls,
                "estimated_revenue_usdc": round(revenue, 6),
                "active_paying_wallets": len(active_wallets),
                "payment_success_rate": payment_success_rate,
                "mcp_tool_calls": event_counts["mcp_tool_call"],
                "registry_requests": event_counts["registry_request"],
                "free_discovery_calls": event_counts["free_discovery_call"],
                "http_error_rate": self._error_rate(events),
                "avg_latency_ms": round(mean(latencies), 2) if latencies else None,
                "p95_latency_ms": self._percentile(latencies, 95),
                "most_used_service": most_used_service,
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
            "service_mix": dict(service_mix.most_common(20)),
            "origin_mix": dict(origin_mix.most_common(20)),
            "referrer_mix": dict(referrer_mix.most_common(20)),
            "user_agent_mix": dict(user_agent_mix.most_common(20)),
            "client_fingerprint_mix": dict(client_fingerprint_mix.most_common(20)),
            "data_called": data_called,
            "failure_reasons": dict(failure_reasons.most_common(10)),
            "top_subjects": dict(top_subjects.most_common(20)),
            "timeline": timeline,
            "recent_events": self.recent_events(limit=50),
            "notes": [
                "Client IPs, wallets, and payment proofs are stored only as salted hashes.",
                "Credit drawdown calls count as paid product usage; revenue is counted when direct x402 payment or bulk credit purchase is verified.",
            ],
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
    def _service_for_event(event: dict[str, Any]) -> str:
        endpoint = str(event.get("endpoint") or "")
        tool_name = str(event.get("tool_name") or "")
        if endpoint.startswith("/v1/vwap"):
            return "vwap"
        if endpoint.startswith("/v1/bidask"):
            return "bidask"
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
                "smithery",
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
        if "smithery" in haystack:
            return "Smithery"
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
                "smithery",
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
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for event in events:
            if not event.get("subject"):
                continue
            if event["event"] not in called_data_events and event["event"] != "http_request":
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
            if str(event.get("timestamp") or "") >= str(row["last_seen"] or ""):
                row["last_seen"] = event.get("timestamp")
                row["latest_event"] = event.get("event")
                row["latest_status_code"] = event.get("status_code")
                row["latest_outcome"] = cls._outcome_for_event(event)
            if event["event"] in called_data_events:
                row["calls"] += 1
            if event["event"] == "payment_required":
                row["payment_prompted"] = True
                row["prompt_price_usdc"] = float(event.get("price_usdc") or 0.0)
            if event["event"] in {
                "payment_verified",
                "credit_drawdown_success",
                "mcp_credit_drawdown_success",
            }:
                row["paid_successes"] += 1
            if event["event"] == "payment_verified":
                row["revenue_usdc"] += float(event.get("price_usdc") or 0.0)

        rows = list(grouped.values())
        for row in rows:
            row["revenue_usdc"] = round(row["revenue_usdc"], 6)
            if int(row["paid_successes"] or 0) > 0:
                row["latest_outcome"] = "Data returned after payment or credits"
        rows.sort(key=lambda row: (-int(row["calls"]), row["service"], row["subject"]))
        return rows[:50]

    @staticmethod
    def _outcome_for_event(event: dict[str, Any]) -> str:
        event_name = str(event.get("event") or "")
        status_code = event.get("status_code")
        if event_name == "payment_required" or status_code == 402:
            return "Prompted to pay; no data returned"
        if event_name in {"payment_verified", "credit_drawdown_success", "mcp_credit_drawdown_success"}:
            return "Data returned after payment or credits"
        if event_name == "payment_failed":
            return "Payment failed; no data returned"
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
    def _error_rate(events: list[dict[str, Any]]) -> float | None:
        http_events = [event for event in events if event["event"] == "http_request"]
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

    @staticmethod
    def _timeline(events: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
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

        paid_call_events = {
            "payment_verified",
            "credit_drawdown_success",
            "mcp_credit_drawdown_success",
        }
        for event in events:
            day = str(event["timestamp"])[:10]
            if day not in buckets:
                continue
            if event["event"] == "http_request":
                buckets[day]["http_requests"] += 1
            if event["event"] in paid_call_events:
                buckets[day]["paid_calls"] += 1
            if event["event"] == "mcp_tool_call":
                buckets[day]["mcp_tool_calls"] += 1
            if event["event"] == "registry_request":
                buckets[day]["registry_requests"] += 1
                source = UsageEventStore._registry_source_for_event(event) or "Unknown registry/direct"
                registry_sources = buckets[day]["registry_sources"]
                registry_sources[source] = int(registry_sources.get(source, 0)) + 1
            if event["event"] in {"payment_verified", "bulk_credit_claimed"}:
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
