"""
FastAPI Resource Server with x402 Payment Middleware.

This server gates Blocksize Capital data endpoints behind the x402
payment protocol. Supports tiered pricing and dual-network settlement
(Solana primary, Base L2 fallback).

Endpoints:
  GET /v1/vwap/{pair}             — Real-time VWAP (crypto tier pricing)
  GET /v1/bidask/{pair}           — Bid/Ask snapshot (shared upstream namespace)
  GET /v1/fx/{pair}               — FX rate ($0.005)
  GET /v1/metal/{ticker}          — Metal price ($0.005)
  GET /v1/rwa/coverage            — RWA coverage and venue readiness (FREE)
  GET /v1/rwa/build-plan          — RWA VWAP/bid/ask build plan (FREE)
  GET /v1/rwa/provider-catalog    — RWA provider ingestion roadmap (FREE)
  GET /v1/rwa/source-readiness    — RWA external dependency readiness (FREE)
  GET /v1/rwa/derivative-venues   — RWA derivative venue discovery (FREE)
  GET /v1/rwa/rwa-xyz-monitor     — RWA.xyz New Asset Monitor catalog (FREE)
  GET /v1/rwa/daily-feed-agent    — Daily RWA new-feed discovery diff (FREE)
  GET /v1/rwa/discovery           — RWA feed discovery and promotion gates (FREE)
  GET /v1/rwa/blocker-resolution  — RWA blocker resolution ledger (FREE)
  GET /v1/rwa/source-rights       — RWA rights-to-source register (FREE)
  GET /v1/rwa/replay-inventory    — RWA route/pool replay inventory (FREE)
  POST /v1/rwa/observations/store — Operator-only replayable RWA evidence ledger
  GET /v1/coverage                  — Unified live and research coverage (FREE)
  GET /v1/search?q={query}        — Pair search (FREE)
  GET /v1/instruments/{service}   — Instrument list (FREE)
  GET /health                     — Health check (FREE)
"""

from __future__ import annotations

import asyncio
import os
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Deque
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from src.blocksize_client import (
    FIAT_CURRENCIES,
    METAL_TICKERS,
    BlocksizeAPIError,
    BlocksizeClient,
)
from src.blocksize_stream_cache import BlocksizeStreamCache
from src.cex_stream_cache import CEXBookCache, KrakenV2BookStream
from src.config import TOP_250_CRYPTO, settings
from src.credit_manager import (
    CREDIT_COSTS,
    STARTER_CREDIT_ALLOWANCE,
    CreditManager,
    BULK_TIERS,
    MAX_CACHED_PAYMENT_RESPONSE_BYTES,
)
from src.entitlement_manager import (
    connector_entitlement_db_path,
    connector_entitlement_manager,
)
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairInfo,
    PairSearchResponse,
    VWAPResponse,
)
from src.observability import (
    UsageEventStore,
    configure_global_store,
    fingerprint,
    normalize_symbol_opportunity,
    record_usage_event,
    record_usage_event_once,
    registry_name_for_path,
    surface_for_path,
)
from src.public_metadata import (
    AGENT_FRAMEWORK_INTEGRATIONS_URL,
    AGENT_MANUAL_URL,
    APP_VERSION,
    CLAUDE_CONNECTOR_URL,
    CATEGORY_HUBS_JSON_URL,
    DATA_CATALOG_URL,
    DATA_PACKAGES_JSON_URL,
    FIRST_PRICE_QUICKSTART_URL,
    GLAMA_MAINTAINER_EMAIL,
    GLAMA_WELL_KNOWN_URL,
    INSTRUMENT_EXPLORER_URL,
    LLMS_TXT_URL,
    MAIN_WEBSITE_CONTACT_URL,
    MAIN_WEBSITE_PRICING_URL,
    MCP_MANIFEST_URL,
    MCP_REGISTRY_AUTH_CONTENT,
    MCP_REGISTRY_AUTH_URL,
    OPENAPI_URL,
    ORACLE_LINEAGE_INDEX_PDF_URL,
    ORACLE_LINEAGE_INDEX_URL,
    PRICING_GUIDE_URL,
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    PUBLIC_DESCRIPTION,
    PUBLIC_DISPLAY_NAME,
    QUICKSTART_URL,
    REMOTE_MCP_PATH,
    REMOTE_MCP_URL,
    REPOSITORY_URL,
    RWA_COVERAGE_INDEX_PDF_URL,
    RWA_COVERAGE_INDEX_URL,
    ROBOTS_URL,
    SERVER_JSON_URL,
    SEO_LANDING_PAGES,
    SITEMAP_URL,
    SUPPORT_URL,
    SWAGGER_URL,
    USER_FLOW_URL,
    build_data_packages_json,
    build_category_hubs_json,
    build_llms_txt,
    build_instrument_explorer_html,
    build_open_graph_svg,
    build_robots_txt,
    build_server_json,
    build_seo_landing_page,
    build_sitemap_xml,
)
from src.runtime_data import (
    INSTALLED_DISTRIBUTION as INSTALLED_DISTRIBUTION,
    PACKAGED_DATA_ROOT,
    PROJECT_ROOT,
    REQUIRED_RWA_REPORT_FILENAMES,
    RWA_REPORTS_DIR,
    SOURCE_CHECKOUT,
    effective_rwa_report_paths,
    inspect_daily_xyz_reconciliation,
    inspect_required_rwa_report,
    resolve_data_directory,
)
from src.rwa_coverage import (
    QUALITY_ALIGNMENT,
    RWA_ASSET_MATRIX_DEFAULT_LIMIT,
    RWA_COLLECTION_DEFAULT_LIMIT,
    RWA_COLLECTION_MAX_LIMIT,
    build_dex_venue_quality_plan,
    build_oracle_parity_matrix,
    build_rwa_asset_matrix,
    build_rwa_build_plan,
    build_rwa_coverage_overview,
)
from src.rwa_api_collections import paginate_rows
from src.rwa_dex_allowlist import build_dex_allowlist
from src.rwa_derivative_venues import build_derivative_venue_report
from src.rwa_asset_identity import build_rwa_ticker_identity_audit
from src.rwa_pricing import calculate_bidask, calculate_block_vwap, detect_outliers
from src.rwa_aggregator import (
    aggregate_submitted_observations,
    build_aggregator_status,
    evaluate_feed_promotion,
)
from src.rwa_realtime_quality import build_realtime_requirements, evaluate_realtime_quality
from src.rwa_adapters import KrakenSpotAdapter, KrakenXStocksAdapter, RWA_ADAPTER_REGISTRY, build_default_registry
from src.rwa_sourcing import build_sourcing_jobs
from src.rwa_sourcing_runner import probe_sourcing_jobs, warm_sourcing_job_cache
from src.rwa_symbol_registry import (
    build_rwa_registry_overview,
    build_rwa_venue_registry_page,
    resolve_rwa_symbol,
)
from src.rwa_non_crypto_feeds import build_non_crypto_feed_catalog
from src.rwa_feed_discovery import build_feed_discovery_audit
from src.rwa_discovery_mitigation import build_discovery_mitigation_plan
from src.rwa_blocker_resolution import build_blocker_resolution_ledger
from src.rwa_source_rights import build_source_rights_registry
from src.rwa_replay_inventory import build_route_pool_replay_inventory
from src.rwa_equity_universes import build_equity_universe_sourcing_plan
from src.rwa_blocksize_benchmark import (
    build_blocksize_state_methodology,
    compare_observation_to_blocksize,
    resolve_blocksize_benchmark,
)
from src.rwa_market_expansion import build_futures_data_plan, build_market_expansion_plan
from src.rwa_oracle_streams import build_oracle_stream_coverage
from src.rwa_provider_catalog import build_provider_catalog
from src.rwa_consensus import build_consensus_source_plan, calculate_consensus_metric
from src.rwa_source_readiness import build_source_readiness
from src.rwa_store import RWAObservationStore, RWA_STORE_SCHEMA_VERSION
from src.security_config import (
    dashboard_token,
    install_sensitive_query_log_filter,
    is_production_environment,
    is_strong_secret,
    security_configuration_status,
    trusted_identity_configuration_status,
)
from src.payment_security import (
    FacilitatorAdapter,
    ParsedPayment,
    PaymentSecurityError,
    parse_payment_signature,
    payment_security_status,
)
from src.proxy_headers import TrustedProxyHeadersMiddleware
from src.rwa_models import RWAObservationEnvelope, RWAProbeRequest
from src.rwa_security import (
    RWARequestBodyLimitMiddleware,
    configured_rwa_observation_db_path,
    database_paths_collide,
    require_rwa_operator,
    rwa_database_collisions,
    rwa_security_status,
    rwa_store_lock_timeout_seconds,
)
from src.rwa_daily_feed_agent import build_daily_feed_agent_view
from src.rwa_xyz_monitor import build_rwa_xyz_monitor_view
from src.transaction_bridge import (
    economic_writes_locked,
    legacy_transaction_bridge_lock_status,
    transaction_bridge_readiness,
)
from src import anthropic_auth
from src import cursor_auth
from src import openai_auth
from src.anthropic_mcp_server import TOOL_COSTS as ANTHROPIC_TOOL_COSTS
from src.anthropic_mcp_server import anthropic_mcp
from src.cursor_mcp_server import TOOL_COSTS as CURSOR_TOOL_COSTS
from src.cursor_mcp_server import cursor_mcp
from src.openai_mcp_server import TOOL_COSTS as OPENAI_TOOL_COSTS
from src.openai_mcp_server import openai_mcp
from src.mcp_server import (
    DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
    DISCOVERY_INSTRUMENT_MAX_LIMIT,
    build_catalog_snapshot_metadata,
)
from src.public_mcp_server import public_mcp
from scripts.run_rwa_growth_pilot import (
    PILOT_STALE_AFTER_SECONDS,
    capture_pilot,
    evaluate_store,
    persist_capture,
)
from scripts.run_rwa_pilot_alignment_snapshot import (
    capture_blocksize_benchmarks,
    evaluate_alignment,
    persist_alignment_report,
)
from scripts.run_rwa_pilot_depth_snapshot import (
    capture_depth_inputs,
    evaluate_depth_evidence,
    load_depth_history,
    persist_depth_report,
)
from scripts.build_rwa_pilot_promotion_packet import (
    build_promotion_packet,
    persist_promotion_packet,
)

logger = logging.getLogger(__name__)
# httpx INFO records include full request URLs. Configured RPC URLs can contain
# credential material in their path, so retain only warning/error records.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
DOCS_DIR = resolve_data_directory("docs", override_env="BLOCKSIZE_DOCS_DIR")
SERVER_JSON_PATHS = (
    (PROJECT_ROOT / "server.json",)
    if SOURCE_CHECKOUT
    else (PACKAGED_DATA_ROOT / "server.json",)
)
READINESS_REQUIRED_DOC_PATHS = (
    "developer_portal.html",
    "remote_mcp_quickstart.html",
    "first_price_quickstart.html",
    "agent_framework_integrations.html",
    "prompt_examples.html",
    "privacy_policy.html",
    "support.html",
    "claude_connector.html",
    "assets/favicon.ico",
    "assets/favicon.png",
    "assets/logo-square.svg",
    "pdf/Blocksize_Agent_Manual.pdf",
    "evidence/rwa-coverage-index.html",
    "evidence/oracle-lineage-index.html",
)
READINESS_REQUIRED_RWA_REPORT_PATHS = REQUIRED_RWA_REPORT_FILENAMES


def _load_release_build() -> dict[str, Any]:
    """Load CI-stamped, non-secret release provenance."""
    path = Path(__file__).with_name("_release_build.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    railway_commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip().lower()
    release_commit = os.environ.get("RELEASE_COMMIT_SHA", "").strip().lower()
    env_commit = railway_commit or release_commit
    if re.fullmatch(r"[0-9a-f]{40}", env_commit):
        payload["commit_sha"] = env_commit
        payload["stamped"] = True
    railway_branch = os.environ.get("RAILWAY_GIT_BRANCH", "").strip()
    if railway_branch:
        payload["source_branch"] = railway_branch
    return payload


RELEASE_BUILD = _load_release_build()
PUBLIC_MCP_HTTP_APP = public_mcp.http_app(path="/", transport="streamable-http")
ANTHROPIC_MCP_HTTP_APP = anthropic_mcp.http_app(path="/", transport="streamable-http")
CURSOR_MCP_HTTP_APP = cursor_mcp.http_app(path="/", transport="streamable-http")
OPENAI_MCP_HTTP_APP = openai_mcp.http_app(path="/", transport="streamable-http")
OBSERVABILITY = (
    UsageEventStore(settings.server.observability_db_path)
    if settings.server.observability_enabled
    else None
)
configure_global_store(OBSERVABILITY)
SMITHERY_LISTING_URL = os.getenv(
    "SMITHERY_LISTING_URL",
    "https://smithery.ai/servers/blocksize/agentic-payments#performance",
)
SMITHERY_HOSTED_MCP_ENDPOINT = os.getenv(
    "SMITHERY_HOSTED_MCP_ENDPOINT",
    "https://agentic-payments--blocksize.run.tools",
)
SMITHERY_METRICS_API_URL = os.getenv("SMITHERY_METRICS_API_URL", "").strip()
PAY_SH_SERVICE_URL = os.getenv(
    "PAY_SH_SERVICE_URL",
    "https://pay.sh/services/blocksize/market-data",
)
PAY_SH_METRICS_API_URL = os.getenv("PAY_SH_METRICS_API_URL", "").strip()
OUTBOUND_DESTINATIONS = {
    "free-trial": "https://matrix.blocksize.capital/",
    "pricing": MAIN_WEBSITE_PRICING_URL.split("?", 1)[0],
    "contact": MAIN_WEBSITE_CONTACT_URL.split("?", 1)[0],
}
ATTRIBUTION_QUERY_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "selection_source",
)
ATTRIBUTION_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+ -]{0,95}$")
SELECTION_SOURCE_VALUES = {
    "public_http_resolver",
    "public_mcp_resolver",
    "authenticated_resolver",
    "published_example_path",
    "direct_http",
}


def _env_enabled(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _rwa_growth_pilot_stale_after_seconds() -> float:
    try:
        configured = float(
            os.environ.get(
                "RWA_GROWTH_PILOT_STALE_AFTER_SECONDS",
                str(PILOT_STALE_AFTER_SECONDS),
            )
        )
    except ValueError:
        configured = PILOT_STALE_AFTER_SECONDS
    return min(604_800.0, max(1.0, configured))


def _rwa_growth_pilot_alignment_paths() -> tuple[Path, Path]:
    return (
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_ALIGNMENT_HISTORY_PATH",
                "/data/rwa_growth_pilot_alignment_history.jsonl",
            )
        ),
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_ALIGNMENT_STATUS_PATH",
                "/data/rwa_growth_pilot_alignment_latest.json",
            )
        ),
    )


def _rwa_growth_pilot_depth_paths() -> tuple[Path, Path]:
    return (
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_DEPTH_HISTORY_PATH",
                "/data/rwa_growth_pilot_depth_history.jsonl",
            )
        ),
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_DEPTH_STATUS_PATH",
                "/data/rwa_growth_pilot_depth_latest.json",
            )
        ),
    )


def _rwa_growth_pilot_promotion_paths() -> tuple[Path, Path]:
    return (
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_PROMOTION_HISTORY_PATH",
                "/data/rwa_growth_pilot_promotion_history.jsonl",
            )
        ),
        Path(
            os.environ.get(
                "RWA_GROWTH_PILOT_PROMOTION_STATUS_PATH",
                "/data/rwa_growth_pilot_promotion_latest.json",
            )
        ),
    )


def _rwa_growth_pilot_dashboard_status(target_app: FastAPI) -> dict[str, Any]:
    """Derive core readiness from SQLite and attach optional evidence reports."""
    _, alignment_status_path = _rwa_growth_pilot_alignment_paths()
    _, depth_status_path = _rwa_growth_pilot_depth_paths()
    _, promotion_status_path = _rwa_growth_pilot_promotion_paths()
    enabled = _env_enabled("RWA_GROWTH_PILOT_ENABLED")
    store = getattr(target_app.state, "rwa_store", None)
    if not isinstance(store, RWAObservationStore):
        return {
            "status": "ledger_unavailable",
            "enabled": enabled,
            "source_of_truth": "rwa_observation_store",
            "production_promoted_feed_count": 0,
            "feeds": [],
        }
    try:
        report = evaluate_store(
            store,
            stale_after_seconds=_rwa_growth_pilot_stale_after_seconds(),
        )
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("RWA growth pilot ledger status is unavailable")
        return {
            "status": "ledger_unavailable",
            "enabled": enabled,
            "source_of_truth": "rwa_observation_store",
            "production_promoted_feed_count": 0,
            "feeds": [],
        }
    alignment = report.get("benchmark_alignment_latest", {})
    if alignment_status_path.exists():
        try:
            alignment = json.loads(alignment_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            alignment = {"status": "status_unreadable"}
    depth = report.get("depth_and_manipulation_latest", {})
    if depth_status_path.exists():
        try:
            depth = json.loads(depth_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            depth = {"status": "status_unreadable"}
    promotion = {}
    if promotion_status_path.exists():
        try:
            promotion = json.loads(promotion_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            promotion = {"status": "status_unreadable"}
    freshness_feeds = report.get("freshness", {}).get("feeds", [])
    latest_outcomes = [
        row for row in freshness_feeds if row.get("last_status") != "missing"
    ]
    succeeded = sum(row.get("last_status") == "ok" for row in latest_outcomes)
    report["current_capture"] = {
        "attempted": len(latest_outcomes),
        "succeeded": succeeded,
        "failed": len(latest_outcomes) - succeeded,
        "derived_from": "latest_ledger_outcome_per_feed",
    }
    supplemental = {
        "benchmark_alignment": {
            "generated_at": alignment.get("generated_at"),
            "status": alignment.get("status"),
            "summary": alignment.get("summary", {}),
            "gate_assessment": alignment.get("gate_assessment", {}),
        },
        "depth_and_manipulation_evidence": {
            "generated_at": depth.get("generated_at"),
            "status": depth.get("status"),
            "summary": depth.get("summary", {}),
            "gate_assessment": depth.get("gate_assessment", {}),
        },
        "promotion_packet": {
            "generated_at": promotion.get("generated_at"),
            "status": promotion.get("status", "not_started"),
            "summary": promotion.get("summary", {}),
            "feeds": promotion.get("feeds", []),
            "policy": promotion.get("policy", {}),
        },
    }
    return {**report, **supplemental, "enabled": enabled}


async def _run_rwa_growth_pilot_loop(app: FastAPI) -> None:
    initial_delay = max(1.0, float(os.environ.get("RWA_GROWTH_PILOT_INITIAL_DELAY_SECONDS", "15")))
    interval = max(300.0, float(os.environ.get("RWA_GROWTH_PILOT_INTERVAL_SECONDS", "1800")))
    timeout = max(1.0, float(os.environ.get("RWA_GROWTH_PILOT_TIMEOUT_SECONDS", "20")))
    alignment_history_path, alignment_status_path = _rwa_growth_pilot_alignment_paths()
    depth_history_path, depth_status_path = _rwa_growth_pilot_depth_paths()
    promotion_history_path, promotion_status_path = _rwa_growth_pilot_promotion_paths()
    await asyncio.sleep(initial_delay)
    while True:
        try:
            registry = getattr(app.state, "rwa_adapter_registry", RWA_ADAPTER_REGISTRY)
            captures = await capture_pilot(registry, timeout_seconds=timeout)
            store = getattr(app.state, "rwa_store", None)
            if not isinstance(store, RWAObservationStore):
                raise RuntimeError("RWA growth pilot ledger is unavailable")
            alignment: dict[str, Any] | None = None
            depth: dict[str, Any] | None = None
            blocksize = getattr(app.state, "blocksize", None)
            if blocksize is not None:
                benchmarks = await capture_blocksize_benchmarks(blocksize)
                depth_inputs = await capture_depth_inputs(
                    registry,
                    captures,
                    timeout_seconds=timeout,
                )
                depth_history = await asyncio.to_thread(
                    load_depth_history,
                    depth_history_path,
                )
                depth = evaluate_depth_evidence(
                    captures,
                    depth_inputs,
                    history=depth_history,
                )
                alignment = evaluate_alignment(captures, benchmarks)
            report = await asyncio.to_thread(
                persist_capture,
                store,
                captures,
                observation_store=store,
                alignment_report=alignment,
                depth_report=depth,
                stale_after_seconds=_rwa_growth_pilot_stale_after_seconds(),
            )
            if alignment is not None and depth is not None:
                promotion_packet = build_promotion_packet(report, alignment, depth)
                await asyncio.to_thread(
                    persist_depth_report,
                    depth,
                    history_path=depth_history_path,
                    latest_path=depth_status_path,
                )
                await asyncio.to_thread(
                    persist_alignment_report,
                    alignment,
                    history_path=alignment_history_path,
                    latest_path=alignment_status_path,
                )
                await asyncio.to_thread(
                    persist_promotion_packet,
                    promotion_packet,
                    history_path=promotion_history_path,
                    latest_path=promotion_status_path,
                )
                logger.info(
                    "RWA growth pilot captured %s/%s feeds; persisted=%s; aligned=%s/%s; depth_or_state=%s/%s; promotion_ready=%s; promotion_blockers=%s",
                    report["current_capture"]["succeeded"],
                    report["current_capture"]["attempted"],
                    report["current_capture"].get("ledger_persisted", 0),
                    alignment["summary"]["timestamp_aligned_comparisons"],
                    alignment["summary"]["feeds_attempted"],
                    (
                        depth["summary"]["native_l2_point_in_time_depth_observed"]
                        + depth["summary"]["pool_state_observed"]
                    ),
                    depth["summary"]["feeds_attempted"],
                    report["promotion_ready"],
                    promotion_packet["summary"]["blocking_gate_count"],
                )
            else:
                logger.info(
                    "RWA growth pilot captured %s/%s feeds in the authoritative ledger",
                    report["current_capture"]["succeeded"],
                    report["current_capture"]["attempted"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("RWA growth pilot capture failed")
        await asyncio.sleep(interval)


def _marketplace_metrics_feed_overrides() -> dict[str, str]:
    raw = os.getenv("MARKETPLACE_METRICS_FEEDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid MARKETPLACE_METRICS_FEEDS_JSON; expected object mapping platform ids to URLs")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(platform_id): str(url).strip()
        for platform_id, url in parsed.items()
        if str(platform_id).strip() and str(url).strip()
    }


MARKETPLACE_METRICS_FEEDS = _marketplace_metrics_feed_overrides()
DISTRIBUTION_PLATFORMS = [
    {
        "id": "pay_sh",
        "name": "Pay.sh / pay-skills",
        "source_label": "Pay.sh",
        "listing_url": PAY_SH_SERVICE_URL,
        "metric_status": "external_metrics_not_ingested",
        "release_status": "verified_live_baseline",
        "observed_version": "4-route x402 catalog",
        "audited_at": "2026-07-30",
        "note": "Pay.sh catalog and pay-skills validation are tracked locally only when traffic reaches this service with Pay.sh attribution.",
    },
    {
        "id": "smithery",
        "name": "Smithery",
        "source_label": "Smithery",
        "listing_url": SMITHERY_LISTING_URL,
        "hosted_endpoint": SMITHERY_HOSTED_MCP_ENDPOINT,
        "metric_status": "external_metrics_not_ingested",
        "release_status": "external_claims_stale",
        "observed_version": None,
        "audited_at": "2026-07-30",
        "note": "Smithery hosted performance is separate unless a metrics feed or hosted endpoint logs are wired into this observability database.",
    },
    {
        "id": "glama",
        "name": "Glama",
        "source_label": "Glama",
        "listing_url": "https://glama.ai/mcp/connectors/info.blocksize.mcp/agentic-payments",
        "metric_status": "local_attribution_only",
        "release_status": "external_claims_stale",
        "observed_version": None,
        "audited_at": "2026-07-30",
        "note": "Glama claim and connector traffic are recorded when they hit instrumented registry or MCP surfaces.",
    },
    {
        "id": "mcp_registry",
        "name": "Official MCP Registry",
        "source_label": "MCP Registry",
        "listing_url": "https://registry.modelcontextprotocol.io/v0/servers?search=blocksize",
        "metric_status": "local_attribution_only",
        "release_status": "version_behind_candidate",
        "observed_version": "0.6.3",
        "audited_at": "2026-07-30",
        "note": "Official registry discovery is recorded through /server.json and domain-verification traffic.",
    },
    {
        "id": "x402scan",
        "name": "x402scan",
        "source_label": "x402scan",
        "secondary_source_label": "x402 Directory",
        "listing_url": "https://www.x402scan.com/server/3d0ad7cd-9e98-473a-8409-25813530df66",
        "metric_status": "local_attribution_only",
        "release_status": "catalog_stale",
        "observed_version": "4 routes; last updated 2026-04-29",
        "audited_at": "2026-07-30",
        "note": "x402scan and x402 discovery calls are tracked when they request /.well-known/x402 or send an identifiable referrer.",
    },
    {
        "id": "github_package",
        "name": "GitHub Package",
        "source_label": "GitHub",
        "listing_url": REPOSITORY_URL,
        "metric_status": "repository_referral_only",
        "release_status": "release_source_v0_6_9",
        "observed_version": "0.6.9 candidate",
        "audited_at": "2026-08-31",
        "note": "GitHub activity is visible here only when it sends traffic to instrumented Blocksize surfaces.",
    },
    {
        "id": "gitlab_mirror",
        "name": "GitLab Mirror",
        "source_label": "GitLab",
        "listing_url": "https://gitlab.com/jfocke/agentic-payments",
        "metric_status": "repository_referral_only",
        "release_status": "stale_mirror_not_install_source",
        "observed_version": "server 0.6.2 / project 0.6.1",
        "audited_at": "2026-07-30",
        "note": "Historical stale mirror; it is not a release or package-install source. Referral activity is recorded only when it reaches instrumented Blocksize surfaces.",
    },
    {
        "id": "awesome_mcp",
        "name": "Awesome MCP",
        "source_label": "Awesome MCP",
        "listing_url": "https://github.com/punkpeye/awesome-mcp-servers/pull/7790",
        "metric_status": "listing_referral_only",
        "release_status": "merged_listing_stale_wrapper",
        "observed_version": "wrapper 0.1.1",
        "audited_at": "2026-07-30",
        "note": "The listing is merged, but its wrapper repository still carries stale product claims. Identifiable referral traffic is tracked locally.",
    },
]


class _SlashlessMountEndpoint:
    """Forward mount-root requests into a mounted app without a slash redirect."""

    def __init__(self, mounted_app: Any, mount_path: str) -> None:
        self.mounted_app = mounted_app
        self.mount_path = mount_path.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        root_path = scope.get("root_path", "")
        mounted_root_path = f"{root_path}{self.mount_path}"
        child_scope = dict(scope)
        child_scope["app_root_path"] = scope.get("app_root_path", root_path)
        child_scope["root_path"] = mounted_root_path
        child_scope["path"] = f"{mounted_root_path}/"
        if "raw_path" in child_scope:
            child_scope["raw_path"] = child_scope["path"].encode()
        await self.mounted_app(child_scope, receive, send)


def _facilitator_support_required() -> bool:
    """Return whether this process must prove facilitator capabilities."""
    hosted = any(
        os.environ.get(name)
        for name in (
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
        )
    )
    production = bool(security_configuration_status()["production"])
    anthropic_only = _env_enabled("ANTHROPIC_ONLY_MODE")
    return not anthropic_only and (hosted or production)


def _facilitator_configuration_fingerprint() -> str:
    """Fingerprint payment capability inputs without exposing credentials."""
    x402 = settings.x402
    material = {
        "facilitator_url": x402.facilitator_url,
        "facilitator_bearer_token": x402.facilitator_bearer_token,
        "cdp_api_key_id": x402.cdp_api_key_id,
        "cdp_api_key_secret": x402.cdp_api_key_secret,
        "solana_network": x402.solana_network,
        "solana_asset": x402.solana_usdc_address,
        "solana_recipient": x402.solana_wallet_address,
        "base_network": x402.base_network,
        "base_asset": x402.base_usdc_address,
        "base_asset_name": x402.base_usdc_name,
        "base_asset_version": x402.base_usdc_version,
        "base_recipient": x402.evm_wallet_address,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _facilitator_probe_timeout_seconds() -> float:
    try:
        configured = float(
            os.environ.get("X402_FACILITATOR_READINESS_TIMEOUT_SECONDS", "5")
        )
    except ValueError:
        configured = 5.0
    return min(15.0, max(0.5, configured))


def _facilitator_probe_max_age_seconds() -> float:
    try:
        configured = float(
            os.environ.get("X402_FACILITATOR_READINESS_MAX_AGE_SECONDS", "180")
        )
    except ValueError:
        configured = 180.0
    return min(900.0, max(30.0, configured))


async def _probe_facilitator_support() -> dict[str, Any]:
    """Fetch a safe capability snapshot when payments are deployment-critical."""
    fingerprint = _facilitator_configuration_fingerprint()
    if not _facilitator_support_required():
        return {
            "checked": False,
            "available": False,
            "required": False,
            "reason": "not_required",
            "kinds": [],
            "checked_at": None,
            "configuration_fingerprint": fingerprint,
        }
    try:
        adapter = FacilitatorAdapter(
            settings.x402.facilitator_url,
            bearer_token=settings.x402.facilitator_bearer_token or None,
            cdp_api_key_id=settings.x402.cdp_api_key_id or None,
            cdp_api_key_secret=settings.x402.cdp_api_key_secret or None,
            timeout_seconds=_facilitator_probe_timeout_seconds(),
            production=bool(security_configuration_status()["production"]),
        )
    except PaymentSecurityError:
        return {
            "checked": True,
            "available": False,
            "required": True,
            "reason": "facilitator_configuration_unsafe",
            "kinds": [],
            "checked_at": time.time(),
            "configuration_fingerprint": fingerprint,
        }

    try:
        result = await asyncio.wait_for(
            adapter.supported(),
            timeout=_facilitator_probe_timeout_seconds(),
        )
    except (TimeoutError, asyncio.TimeoutError):
        result = {
            "checked": True,
            "available": False,
            "reason": "timeout",
            "kinds": [],
        }
    result["required"] = True
    result["checked_at"] = time.time()
    result["configuration_fingerprint"] = fingerprint
    if result.get("available") is True:
        for kind in result.get("kinds", []):
            if (
                kind.get("x402Version") == 2
                and kind.get("scheme") == "exact"
                and kind.get("network") == settings.x402.solana_network
            ):
                fee_payer = (kind.get("extra") or {}).get("feePayer")
                if isinstance(fee_payer, str) and fee_payer:
                    settings.x402.solana_fee_payer = fee_payer
                    break
    return result


def _facilitator_support_readiness(
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    required = _facilitator_support_required()
    if not required:
        return {
            "ready": True,
            "required": False,
            "checked": bool(snapshot and snapshot.get("checked")),
            "available": bool(snapshot and snapshot.get("available")),
            "age_seconds": None,
            "reason": "not_required",
        }
    if not isinstance(snapshot, dict):
        return {
            "ready": False,
            "required": True,
            "checked": False,
            "available": False,
            "age_seconds": None,
            "max_age_seconds": _facilitator_probe_max_age_seconds(),
            "reason": "not_checked",
        }
    checked_at = snapshot.get("checked_at")
    try:
        age_seconds = max(0.0, time.time() - float(checked_at)) if checked_at else None
    except (TypeError, ValueError):
        age_seconds = None
    reason = snapshot.get("reason")
    if snapshot.get("configuration_fingerprint") != _facilitator_configuration_fingerprint():
        reason = "configuration_changed_since_probe"
    elif age_seconds is None or age_seconds > _facilitator_probe_max_age_seconds():
        reason = "probe_stale"
    ready = (
        snapshot.get("checked") is True
        and snapshot.get("available") is True
        and reason is None
    )
    return {
        "ready": ready,
        "required": True,
        "checked": snapshot.get("checked") is True,
        "available": snapshot.get("available") is True,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "max_age_seconds": _facilitator_probe_max_age_seconds(),
        "reason": reason,
    }


async def _run_facilitator_support_probe_loop(app: FastAPI) -> None:
    """Keep the cached payment capability snapshot bounded and fresh."""
    interval = max(15.0, _facilitator_probe_max_age_seconds() / 3)
    while True:
        await asyncio.sleep(interval)
        app.state.facilitator_support = await _probe_facilitator_support()


def _hosted_environment() -> bool:
    return any(
        os.environ.get(name)
        for name in (
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
        )
    )


def _blocksize_dependency_required() -> bool:
    return _hosted_environment() or is_production_environment()


def _blocksize_configuration_fingerprint(client: BlocksizeClient) -> str:
    """Fingerprint dependency configuration without exposing credentials."""
    material = f"{getattr(client, '_rest_url', '')}\0{getattr(client, '_api_key', '')}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _blocksize_probe_timeout_seconds() -> float:
    try:
        configured = float(os.environ.get("BLOCKSIZE_READINESS_TIMEOUT_SECONDS", "5"))
    except ValueError:
        configured = 5.0
    return min(15.0, max(0.5, configured))


def _blocksize_probe_max_age_seconds() -> float:
    try:
        configured = float(os.environ.get("BLOCKSIZE_READINESS_MAX_AGE_SECONDS", "180"))
    except ValueError:
        configured = 180.0
    return min(900.0, max(30.0, configured))


async def _probe_blocksize_dependency(client: BlocksizeClient) -> dict[str, Any]:
    """Run one bounded authenticated upstream probe and retain no response data."""
    required = _blocksize_dependency_required()
    base = {
        "required": required,
        "checked": False,
        "available": False,
        "checked_at": None,
        "reason": "not_required",
        "configuration_fingerprint": _blocksize_configuration_fingerprint(client),
    }
    if not required:
        return base
    if not str(getattr(client, "_api_key", "")).strip():
        return {
            **base,
            "checked": True,
            "checked_at": time.time(),
            "reason": "api_key_missing",
        }
    try:
        instruments = await asyncio.wait_for(
            client.list_vwap_instruments(),
            timeout=_blocksize_probe_timeout_seconds(),
        )
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        reason = "timeout"
    except httpx.HTTPStatusError as exc:
        reason = "authentication_failed" if exc.response.status_code in {401, 403} else "http_error"
    except (httpx.HTTPError, BlocksizeAPIError, OSError, ValueError, TypeError):
        reason = "upstream_error"
    else:
        if instruments:
            return {
                **base,
                "checked": True,
                "available": True,
                "checked_at": time.time(),
                "reason": None,
            }
        reason = "empty_catalog"
    return {
        **base,
        "checked": True,
        "checked_at": time.time(),
        "reason": reason,
    }


async def _run_blocksize_dependency_probe_loop(app: FastAPI) -> None:
    """Refresh the cached dependency result without probing on each readiness call."""
    interval = max(15.0, _blocksize_probe_max_age_seconds() / 3)
    while True:
        await asyncio.sleep(interval)
        app.state.blocksize_dependency = await _probe_blocksize_dependency(app.state.blocksize)


def _blocksize_dependency_readiness(client: BlocksizeClient | None) -> dict[str, Any]:
    required = _blocksize_dependency_required()
    snapshot = getattr(app.state, "blocksize_dependency", None)
    if not required:
        return {
            "ready": True,
            "required": False,
            "checked": bool(snapshot and snapshot.get("checked")),
            "available": bool(snapshot and snapshot.get("available")),
            "age_seconds": None,
            "reason": "not_required",
        }
    if client is None or not isinstance(snapshot, dict):
        return {
            "ready": False,
            "required": True,
            "checked": False,
            "available": False,
            "age_seconds": None,
            "reason": "not_checked",
        }
    checked_at = snapshot.get("checked_at")
    age_seconds = max(0.0, time.time() - float(checked_at)) if checked_at else None
    reason = snapshot.get("reason")
    if snapshot.get("configuration_fingerprint") != _blocksize_configuration_fingerprint(client):
        reason = "configuration_changed_since_probe"
    elif age_seconds is None or age_seconds > _blocksize_probe_max_age_seconds():
        reason = "probe_stale"
    ready = (
        snapshot.get("checked") is True
        and snapshot.get("available") is True
        and reason is None
    )
    return {
        "ready": ready,
        "required": True,
        "checked": snapshot.get("checked") is True,
        "available": snapshot.get("available") is True,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "max_age_seconds": _blocksize_probe_max_age_seconds(),
        "reason": reason,
    }


def _facilitator_supported_requirements(
    requirements: list[dict],
    support: dict[str, Any] | None,
) -> list[dict]:
    """Return only requirements backed by an exact v2 facilitator kind."""
    snapshot = support or {}
    if _facilitator_support_required() and not _facilitator_support_readiness(
        snapshot
    )["ready"]:
        return []
    if snapshot.get("checked") is not True:
        return requirements if not _facilitator_support_required() else []
    if snapshot.get("available") is not True:
        return []

    kinds = snapshot.get("kinds")
    if not isinstance(kinds, list):
        return []
    supported: list[dict] = []
    for requirement in requirements:
        matching_kind = next(
            (
                kind
                for kind in kinds
                if isinstance(kind, dict)
                and kind.get("x402Version") == 2
                and kind.get("scheme") == requirement.get("scheme")
                and kind.get("network") == requirement.get("network")
            ),
            None,
        )
        if matching_kind is None:
            continue
        candidate = {**requirement, "extra": dict(requirement.get("extra") or {})}
        if _network_kind(str(candidate.get("network") or "")) == "solana":
            fee_payer = (matching_kind.get("extra") or {}).get("feePayer")
            if not isinstance(fee_payer, str) or not fee_payer:
                continue
            candidate["extra"]["feePayer"] = fee_payer
        supported.append(candidate)
    return supported


# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the Blocksize client and Credit manager lifecycle."""
    install_sensitive_query_log_filter()
    app.state.security_configuration = security_configuration_status()
    if not app.state.security_configuration["ready"]:
        logger.error(
            "Security configuration is not ready; protected observability remains fail-closed"
        )
    hosted = _hosted_environment()
    production = is_production_environment()
    connector_prefixes = ["ANTHROPIC"]
    if not _anthropic_only_mode():
        connector_prefixes.extend(["CURSOR", "OPENAI"])
    app.state.connector_entitlement_statuses = {
        prefix: _connector_entitlement_readiness(
            prefix,
            hosted=hosted,
            production=production,
            initialize=hosted or production,
        )
        for prefix in connector_prefixes
    }
    app.state.facilitator_support = await _probe_facilitator_support()
    app.state.facilitator_support_task = None
    if (
        app.state.facilitator_support.get("required")
        and not app.state.facilitator_support.get("available")
    ):
        logger.error("Payment facilitator capabilities are unavailable")
    app.state.blocksize = BlocksizeClient()
    app.state.instrument_catalog_cache = {}
    app.state.instrument_catalog_cache_lock = asyncio.Lock()
    app.state.blocksize_dependency = await _probe_blocksize_dependency(app.state.blocksize)
    app.state.blocksize_dependency_task = None
    if (
        app.state.blocksize_dependency.get("required")
        and not app.state.blocksize_dependency.get("available")
    ):
        logger.error(
            "Blocksize upstream dependency is unavailable: %s",
            app.state.blocksize_dependency.get("reason"),
        )
    app.state.stream_cache = BlocksizeStreamCache(rest_client=app.state.blocksize)
    kraken_symbols = [
        item.strip()
        for item in os.environ.get("KRAKEN_XSTOCKS_WS_SYMBOLS", "").split(",")
        if item.strip()
    ]
    kraken_spot_symbols = [
        item.strip()
        for item in os.environ.get("KRAKEN_SPOT_WS_SYMBOLS", "").split(",")
        if item.strip()
    ]
    app.state.cex_book_cache = CEXBookCache(
        ttl_seconds=float(os.environ.get("CEX_STREAM_TTL_SECONDS", "10"))
    )
    app.state.kraken_book_stream = None
    app.state.kraken_spot_stream = None
    app.state.rwa_adapter_registry = build_default_registry()
    warm_sourcing_job_cache()
    if kraken_symbols:
        app.state.rwa_adapter_registry.register(
            KrakenXStocksAdapter(stream_cache=app.state.cex_book_cache)
        )
        app.state.kraken_book_stream = KrakenV2BookStream(
            app.state.cex_book_cache,
            symbols=kraken_symbols,
            depth=int(os.environ.get("KRAKEN_XSTOCKS_WS_DEPTH", "100")),
        )
    if kraken_spot_symbols:
        app.state.rwa_adapter_registry.register(
            KrakenSpotAdapter(stream_cache=app.state.cex_book_cache)
        )
        app.state.kraken_spot_stream = KrakenV2BookStream(
            app.state.cex_book_cache,
            symbols=kraken_spot_symbols,
            venue_id="kraken_spot",
            depth=int(os.environ.get("KRAKEN_SPOT_WS_DEPTH", "100")),
        )
    app.state.credits = CreditManager()
    rwa_db_path = configured_rwa_observation_db_path()
    if rwa_database_collisions(
        rwa_db_path,
        settings.server.observability_db_path,
        {"credits_runtime": app.state.credits.db_path},
    ):
        app.state.rwa_store = None
        logger.error("RWA evidence store is disabled because its database path is not isolated")
    else:
        app.state.rwa_store = RWAObservationStore(rwa_db_path)
    app.state.store_readiness_snapshots = {}
    app.state.store_readiness_task = None
    await _refresh_store_readiness_snapshots(app)
    app.state.rwa_growth_pilot_task = None
    logger.info("Blocksize MCP Resource Server starting (with Credit Drawdown engine)")
    logger.info("Solana wallet configured: %s", bool(settings.x402.solana_wallet_address))
    logger.info("Base wallet configured: %s", bool(settings.x402.evm_wallet_address))
    await app.state.stream_cache.start()
    if _facilitator_support_required():
        app.state.facilitator_support_task = asyncio.create_task(
            _run_facilitator_support_probe_loop(app),
            name="facilitator-readiness-probe",
        )
    if _blocksize_dependency_required():
        app.state.blocksize_dependency_task = asyncio.create_task(
            _run_blocksize_dependency_probe_loop(app),
            name="blocksize-readiness-probe",
        )
    app.state.store_readiness_task = asyncio.create_task(
        _run_store_readiness_probe_loop(app),
        name="store-readiness-probe",
    )
    if app.state.kraken_book_stream is not None:
        await app.state.kraken_book_stream.start()
    if app.state.kraken_spot_stream is not None:
        await app.state.kraken_spot_stream.start()
    if _env_enabled("RWA_GROWTH_PILOT_ENABLED"):
        app.state.rwa_growth_pilot_task = asyncio.create_task(_run_rwa_growth_pilot_loop(app))
    try:
        async with PUBLIC_MCP_HTTP_APP.lifespan(PUBLIC_MCP_HTTP_APP):
            async with ANTHROPIC_MCP_HTTP_APP.lifespan(ANTHROPIC_MCP_HTTP_APP):
                async with CURSOR_MCP_HTTP_APP.lifespan(CURSOR_MCP_HTTP_APP):
                    async with OPENAI_MCP_HTTP_APP.lifespan(OPENAI_MCP_HTTP_APP):
                        yield
    finally:
        if app.state.facilitator_support_task is not None:
            app.state.facilitator_support_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.facilitator_support_task
        if app.state.blocksize_dependency_task is not None:
            app.state.blocksize_dependency_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.blocksize_dependency_task
        if app.state.store_readiness_task is not None:
            app.state.store_readiness_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.store_readiness_task
        if app.state.rwa_growth_pilot_task is not None:
            app.state.rwa_growth_pilot_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.rwa_growth_pilot_task
    if app.state.kraken_book_stream is not None:
        await app.state.kraken_book_stream.stop()
    if app.state.kraken_spot_stream is not None:
        await app.state.kraken_spot_stream.stop()
    await app.state.stream_cache.stop()
    await app.state.blocksize.close()
    logger.info("Blocksize MCP Resource Server shut down")


app = FastAPI(
    title=PUBLIC_DISPLAY_NAME,
    version=APP_VERSION,
    description=f"""
Institutional-grade real-time market data gateway for autonomous AI agents.
Supports direct x402 USDC settlement, authenticated connector starter credits,
and a public read-only remote MCP discovery surface for directory listings and
client onboarding.

### Public Integration Surfaces
- **Developer Portal**: [Homepage]({PUBLIC_BASE_URL}/)
- **Remote MCP URL**: [Streamable HTTP]({REMOTE_MCP_URL})
- **MCP Manifest**: [Listing metadata]({MCP_MANIFEST_URL})
- **OpenAPI**: [JSON schema]({OPENAPI_URL})
- **Swagger UI**: [Interactive docs]({SWAGGER_URL})
- **Quickstart**: [Remote MCP install guide]({QUICKSTART_URL})
- **Prompt Examples**: [Example prompts]({PROMPT_EXAMPLES_URL})
- **Privacy Policy**: [Privacy]({PRIVACY_POLICY_URL})
- **Support**: [Contact and troubleshooting]({SUPPORT_URL})
    """,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "PAYMENT-REQUIRED",
        "PAYMENT-RESPONSE",
        "X-PAYMENT-RESPONSE",
        "X-Blocksize-Provider",
        "X-Blocksize-Citation",
        "X-Blocksize-Activation",
        "X-Blocksize-Credits-Remaining",
        "X-Blocksize-Credits-Refunded",
        "X-Blocksize-Delivery-Status",
        "X-Blocksize-Retry-Safe",
    ],
)
app.add_middleware(RWARequestBodyLimitMiddleware)


X402_EXPOSE_HEADERS = (
    "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE, "
    "X-Blocksize-Provider, X-Blocksize-Citation, X-Blocksize-Activation, "
    "X-Blocksize-Credits-Remaining, X-Blocksize-Credits-Refunded, "
    "X-Blocksize-Delivery-Status, X-Blocksize-Retry-Safe"
)
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

_INDEXABLE_PUBLIC_PATHS = {
    "/",
    "/docs",
    "/openapi.json",
    "/mcp/manifest.json",
    "/server.json",
    "/llms.txt",
    "/data-packages.json",
    "/category-hubs.json",
    "/instruments",
    "/instruments/crypto",
    "/instruments/equities",
    "/instruments/fx",
    "/instruments/metals",
    "/quickstart/remote-mcp",
    "/quickstart/first-price",
    "/prompt-examples",
    "/claude-connector",
    "/support",
    "/privacy",
    "/evidence/rwa-coverage-index.html",
    "/evidence/oracle-lineage-index.html",
    "/pdf/Blocksize_RWA_Coverage_Index.pdf",
    "/pdf/Blocksize_Oracle_Lineage_Index.pdf",
    "/pdf/Blocksize_Pricing_Guide.pdf",
    "/pdf/Blocksize_Data_Catalog.pdf",
    "/pdf/Blocksize_Agent_Manual.pdf",
    *{f"/{slug}" for slug in SEO_LANDING_PAGES},
}
_NOINDEX_PATH_PREFIXES = (
    "/v1/",
    "/internal/",
    "/mcp/server",
    "/anthropic/mcp",
    "/cursor/mcp",
    "/openai/mcp",
    "/.well-known/",
    "/go/",
    "/og/",
)


def _apply_security_headers(response: Any) -> Any:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("X-Blocksize-Provider", "Blocksize")
    response.headers.setdefault("X-Blocksize-Citation", CATEGORY_HUBS_JSON_URL)
    response.headers.setdefault(
        "Link",
        f'<{CATEGORY_HUBS_JSON_URL}>; rel="describedby"; type="application/json"',
    )
    return response


def _apply_x402_cors_headers(request: Request, response: Response) -> Response:
    """Expose payment challenge details on early paid-route responses."""
    _apply_security_headers(response)
    response.headers.setdefault("Cache-Control", "no-store")

    origin = request.headers.get("origin")
    if not origin:
        return response

    allowed_origins = settings.server.cors_origins
    if "*" in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        return response

    response.headers["Access-Control-Expose-Headers"] = X402_EXPOSE_HEADERS
    vary = response.headers.get("Vary")
    if vary:
        vary_items = {item.strip().lower() for item in vary.split(",")}
        if "origin" not in vary_items:
            response.headers["Vary"] = f"{vary}, Origin"
    else:
        response.headers["Vary"] = "Origin"
    return response


def _request_client_ip(request: Request) -> str:
    """Use only the trusted-proxy-normalized peer address.

    TrustedProxyHeadersMiddleware rewrites request.client only when the raw
    direct peer is in FORWARDED_ALLOW_IPS. Re-reading forwarding headers here
    would bypass that decision and let callers select their own identity.
    """
    return request.client.host if request.client else "unknown"


def _wallet_hash(wallet: str | None) -> str | None:
    return fingerprint(wallet.strip()) if wallet else None


def _endpoint_label(path: str) -> str:
    if path.startswith("/v1/vwap/"):
        return "/v1/vwap/{pair}"
    if path.startswith("/v1/bidask/"):
        return "/v1/bidask/{pair}"
    if path.startswith("/v1/fx/"):
        return "/v1/fx/{pair}"
    if path.startswith("/v1/metal/"):
        return "/v1/metal/{ticker}"
    if path.startswith("/v1/instruments/"):
        return "/v1/instruments/{service}"
    if path.startswith("/v1/credits/balance/"):
        return "/v1/credits/balance/{wallet}"
    if path == "/mcp/server" or path.startswith("/mcp/server/"):
        return "/mcp/server"
    if path == "/anthropic/mcp" or path.startswith("/anthropic/mcp/"):
        return "/anthropic/mcp"
    if path == "/cursor/mcp" or path.startswith("/cursor/mcp/"):
        return "/cursor/mcp"
    if path == "/openai/mcp" or path.startswith("/openai/mcp/"):
        return "/openai/mcp"
    return path


def _subject_for_request(request: Request) -> str | None:
    path = request.url.path
    if path == "/v1/search":
        return request.query_params.get("q")
    if path == "/v1/batch":
        return request.query_params.get("reqs")
    if path.startswith("/v1/instruments/"):
        return path.rstrip("/").rsplit("/", 1)[-1]
    if path.startswith(("/v1/vwap/", "/v1/bidask/", "/v1/fx/", "/v1/metal/")):
        return path.rstrip("/").rsplit("/", 1)[-1].upper()
    if path.startswith("/v1/credits/purchase"):
        return request.query_params.get("tier")
    return None


def _asset_class_for_request(request: Request) -> str | None:
    path = request.url.path
    if path.startswith("/v1/vwap/"):
        return "crypto"
    if path.startswith("/v1/bidask/"):
        subject = _subject_for_request(request) or ""
        return "equity" if _looks_like_equity_bidask_symbol(subject) else "multi_asset"
    if path.startswith("/v1/fx/"):
        return "fx"
    if path.startswith("/v1/metal/"):
        return "metal"
    if path == "/v1/search":
        return request.query_params.get("asset_class") or "all"
    return None


def _request_event_fields(
    request: Request,
    *,
    status_code: int | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "surface": surface_for_path(request.url.path),
        "endpoint": _endpoint_label(request.url.path),
        "method": request.method.upper(),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "ip_hash": fingerprint(_request_client_ip(request)),
        "user_agent": request.headers.get("user-agent"),
        "referrer": request.headers.get("referer") or request.headers.get("referrer"),
        "wallet_hash": getattr(request.state, "trusted_wallet_hash", None),
        "subject": _subject_for_request(request),
        "asset_class": _asset_class_for_request(request),
    }


def _request_attribution_metadata(request: Request) -> dict[str, str]:
    """Retain bounded campaign labels without storing arbitrary query strings."""
    metadata: dict[str, str] = {}
    for key in ATTRIBUTION_QUERY_KEYS:
        value = (request.query_params.get(key) or "").strip()
        if (
            value
            and ATTRIBUTION_VALUE_RE.fullmatch(value)
            and (key != "selection_source" or value in SELECTION_SOURCE_VALUES)
        ):
            metadata[key] = value
    if _is_live_price_delivery_path(request.url.path) and "selection_source" not in metadata:
        metadata["selection_source"] = "direct_http"
    return metadata


def _activation_identity_hash(request: Request) -> tuple[str, str] | None:
    """Resolve only an identity asserted by a trusted server-side verifier."""
    identity_hash = getattr(request.state, "trusted_identity_hash", None)
    identity_type = getattr(request.state, "trusted_identity_type", None)
    identity_trust = getattr(request.state, "trusted_identity_trust", None)
    if (
        isinstance(identity_hash, str)
        and isinstance(identity_type, str)
        and identity_trust == "verified_x402"
    ):
        return identity_hash, identity_type
    return None


def _growth_identity_metadata(request: Request) -> dict[str, str]:
    identity = _activation_identity_hash(request)
    if identity is None:
        return {}
    identity_hash, identity_type = identity
    metadata = {
        "identity_hash": identity_hash,
        "identity_type": identity_type,
        "identity_trust": "verified_x402",
    }
    activation_source = request.headers.get("X-BLOCKSIZE-ACTIVATION-SOURCE", "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", activation_source):
        metadata["activation_source"] = activation_source
    return metadata


def _anonymous_growth_identity_metadata(request: Request) -> dict[str, str]:
    """Attribute anonymous discovery only to the trusted client-IP fingerprint."""
    identity_hash = fingerprint(f"ip:{_request_client_ip(request)}")
    if not identity_hash:
        return {}
    return {
        "identity_hash": identity_hash,
        "identity_type": "ip",
        "identity_trust": "anonymous_ip",
    }


def _is_live_price_delivery_path(path: str) -> bool:
    return path.startswith(
        (
            "/v1/vwap/",
            "/v1/bidask/",
            "/v1/fx/",
            "/v1/metal/",
            "/v1/state/",
            "/v1/vwap30m/",
            "/v1/vwap24h/",
        )
    )


def _record_http_usage(request: Request, status_code: int, latency_ms: float) -> None:
    if request.url.path.startswith("/internal/observability"):
        return

    fields = _request_event_fields(
        request,
        status_code=status_code,
        latency_ms=latency_ms,
    )
    attribution = _request_attribution_metadata(request)
    record_usage_event("http_request", **fields, metadata=attribution)

    registry_name = registry_name_for_path(request.url.path)
    if registry_name:
        record_usage_event(
            "registry_request",
            **fields,
            metadata={"registry": registry_name, **attribution},
        )
    elif _is_discovery_rate_limited_path(request.url.path):
        record_usage_event(
            "free_discovery_call",
            **fields,
            metadata={**attribution, **_anonymous_growth_identity_metadata(request)},
        )


def _record_product_event(
    event: str,
    request: Request,
    *,
    price_usdc: Decimal | float | str | None = None,
    network: str | None = None,
    reason: str | None = None,
    wallet_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    fields = _request_event_fields(request)
    if wallet_hash is not None:
        fields["wallet_hash"] = wallet_hash
    event_metadata = {
        **_request_attribution_metadata(request),
        **_growth_identity_metadata(request),
        **(metadata or {}),
    }
    record_usage_event(
        event,
        **fields,
        price_usdc=str(price_usdc) if price_usdc is not None else None,
        network=network,
        reason=reason,
        metadata=event_metadata,
    )


def _delivery_event_for_response(
    response: Response,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    if response.status_code < 400:
        return "data_delivered"
    if (metadata or {}).get("refund_status") == "refunded":
        return "refunded_delivery_failed"
    return "charged_delivery_failed"


def _record_charged_delivery_outcome(
    request: Request,
    response: Response,
    *,
    price_usdc: Decimal | float | str | None,
    payment_mode: str,
    network: str | None = None,
    wallet_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    event = _delivery_event_for_response(response, metadata=metadata)
    outcome_metadata = {
        "payment_mode": payment_mode,
        "response_status_code": response.status_code,
        **(metadata or {}),
    }
    _record_product_event(
        event,
        request,
        price_usdc=price_usdc,
        network=network,
        reason=None if event == "data_delivered" else f"http_{response.status_code}",
        wallet_hash=wallet_hash,
        metadata=outcome_metadata,
    )
    if event == "data_delivered" and _is_live_price_delivery_path(request.url.path):
        activation_identity = _activation_identity_hash(request)
        if activation_identity is not None:
            identity_hash, identity_type = activation_identity
            return record_usage_event_once(
                "first_live_price_delivered",
                identity_hash,
                **_request_event_fields(request, status_code=response.status_code),
                price_usdc=str(price_usdc) if price_usdc is not None else None,
                network=network,
                metadata={
                    **_request_attribution_metadata(request),
                    **_growth_identity_metadata(request),
                    "payment_mode": payment_mode,
                },
            )
    return False


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response = _apply_security_headers(response)
    path = request.url.path.rstrip("/") or "/"
    if path in _INDEXABLE_PUBLIC_PATHS:
        canonical_url = f"{PUBLIC_BASE_URL.rstrip('/')}{path if path != '/' else '/'}"
        canonical_link = f'<{canonical_url}>; rel="canonical"'
        existing_link = response.headers.get("Link", "")
        if canonical_link not in existing_link:
            response.headers["Link"] = (
                f"{existing_link}, {canonical_link}"
                if existing_link
                else canonical_link
            )
        response.headers.setdefault("X-Robots-Tag", "index, follow")
    elif path in {"/health", "/readyz"} or path.startswith(
        _NOINDEX_PATH_PREFIXES
    ):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    return response


def _anthropic_only_mode() -> bool:
    value = os.environ.get("ANTHROPIC_ONLY_MODE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _root_oauth_connector() -> str:
    """Choose the connector advertised by the root OAuth metadata endpoint."""
    value = os.environ.get("ROOT_OAUTH_CONNECTOR", "anthropic").strip().lower()
    if value in {"anthropic", "claude"}:
        return "anthropic"
    if value == "cursor":
        return "cursor"
    if value in {"openai", "chatgpt"}:
        return "openai"
    logger.warning(
        "Invalid ROOT_OAUTH_CONNECTOR=%r; defaulting root OAuth metadata to Anthropic",
        value,
    )
    return "anthropic"


def _anthropic_only_allowed_path(path: str) -> bool:
    clean_path = path.rstrip("/") or "/"
    allowed_exact_paths = {
        "/health",
        "/readyz",
        "/privacy",
        "/prompt-examples",
        "/quickstart/first-price",
        "/integrations/agent-frameworks",
        "/support",
        "/claude-connector",
        "/robots.txt",
        "/sitemap.xml",
        "/llms.txt",
        "/data-packages.json",
        "/category-hubs.json",
        "/server.json",
        "/mcp/manifest.json",
        "/favicon.ico",
        "/favicon.svg",
        "/apple-touch-icon.png",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server/anthropic/mcp",
        "/.well-known/openid-configuration/anthropic/mcp",
        "/.well-known/oauth-protected-resource/anthropic/mcp",
    }
    seo_paths = {f"/{slug}" for slug in SEO_LANDING_PAGES}
    return (
        clean_path in allowed_exact_paths
        or clean_path in seo_paths
        or clean_path.startswith("/anthropic/mcp")
        or clean_path.startswith("/go/")
        or clean_path.startswith("/og/")
    )


def _anthropic_only_block_response() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "status": "error",
            "error_code": "ANTHROPIC_ONLY_MODE",
            "message": "This deployment exposes only the Anthropic-safe MCP endpoint.",
        },
    )

# Serve the Developer Portal as the main landing page
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def get_portal():
    """Serve the institutional developer portal."""
    portal_path = DOCS_DIR / "developer_portal.html"
    if portal_path.exists():
        return FileResponse(
            portal_path,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    raise HTTPException(status_code=404, detail="Developer Portal not found")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def get_favicon_ico():
    """Serve the root favicon for browser and directory crawlers."""
    return FileResponse(DOCS_DIR / "assets" / "favicon.ico", media_type="image/x-icon")


@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
async def get_favicon_svg():
    """Serve the square Blocksize mark as an SVG favicon."""
    return FileResponse(DOCS_DIR / "assets" / "logo-square.svg", media_type="image/svg+xml")


@app.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"], include_in_schema=False)
async def get_apple_touch_icon():
    """Serve the square Blocksize mark for touch icons."""
    return FileResponse(DOCS_DIR / "assets" / "favicon.png", media_type="image/png")


def _serve_doc(filename: str, description: str) -> FileResponse:
    """Serve a static documentation page from the docs directory."""
    path = DOCS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{description} not found")
    return FileResponse(path)


@app.api_route("/quickstart/remote-mcp", methods=["GET", "HEAD"], include_in_schema=False)
async def get_remote_quickstart():
    """Serve the public remote MCP quickstart page."""
    return _serve_doc("remote_mcp_quickstart.html", "Remote MCP quickstart")


@app.api_route("/quickstart/first-price", methods=["GET", "HEAD"], include_in_schema=False)
async def get_first_price_quickstart():
    """Serve the shortest honest path to a first live Blocksize price."""
    return _serve_doc("first_price_quickstart.html", "First price quickstart")


@app.api_route(
    "/integrations/agent-frameworks",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_agent_framework_integrations():
    """Serve the public agent-framework integration guide."""
    return _serve_doc("agent_framework_integrations.html", "Agent framework integrations")


@app.api_route("/prompt-examples", methods=["GET", "HEAD"], include_in_schema=False)
async def get_prompt_examples():
    """Serve prompt examples for reviewers and users."""
    return _serve_doc("prompt_examples.html", "Prompt examples")


@app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
async def get_privacy_policy():
    """Serve the privacy policy page."""
    return _serve_doc("privacy_policy.html", "Privacy policy")


@app.api_route("/support", methods=["GET", "HEAD"], include_in_schema=False)
async def get_support_page():
    """Serve the support and troubleshooting page."""
    return _serve_doc("support.html", "Support page")


@app.api_route("/instruments", methods=["GET", "HEAD"], include_in_schema=False)
async def get_instrument_explorer() -> HTMLResponse:
    """Serve the canonical human-and-agent instrument search surface."""
    return HTMLResponse(build_instrument_explorer_html())


@app.api_route(
    "/instruments/{asset_class}", methods=["GET", "HEAD"], include_in_schema=False
)
async def get_instrument_explorer_category(asset_class: str) -> HTMLResponse:
    """Serve one curated instrument category without generating thin symbol pages."""
    if asset_class not in {"crypto", "equities", "fx", "metals"}:
        raise HTTPException(status_code=404, detail="Instrument category not found")
    return HTMLResponse(build_instrument_explorer_html(asset_class))


@app.get("/terms", include_in_schema=False)
async def get_terms_page():
    """Send reviewers to the published Blocksize data terms."""
    return RedirectResponse("https://blocksize.info/terms-conditions-data/")


@app.api_route("/claude-connector", methods=["GET", "HEAD"], include_in_schema=False)
async def get_claude_connector_page():
    """Serve Claude connector setup and review documentation."""
    return _serve_doc("claude_connector.html", "Claude connector page")


@app.api_route("/server.json", methods=["GET", "HEAD"], include_in_schema=False)
async def get_server_json():
    """Serve the official MCP Registry metadata file."""
    return JSONResponse(build_server_json())


@app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def get_robots_txt() -> PlainTextResponse:
    """Serve crawler guidance and sitemap discovery."""
    return PlainTextResponse(build_robots_txt(), media_type="text/plain; charset=utf-8")


@app.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def get_sitemap_xml() -> PlainTextResponse:
    """Serve canonical public URL discovery for search engines."""
    return PlainTextResponse(
        build_sitemap_xml(),
        media_type="application/xml; charset=utf-8",
    )


@app.api_route("/llms.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def get_llms_txt() -> PlainTextResponse:
    """Serve a compact AI-reader brief for retrieval and citation systems."""
    return PlainTextResponse(build_llms_txt(), media_type="text/plain; charset=utf-8")


@app.api_route("/data-packages.json", methods=["GET", "HEAD"], include_in_schema=False)
async def get_data_packages_json() -> JSONResponse:
    """Serve the machine-readable Blocksize data package catalog."""
    return JSONResponse(build_data_packages_json())


@app.api_route("/category-hubs.json", methods=["GET", "HEAD"], include_in_schema=False)
async def get_category_hubs_json() -> JSONResponse:
    """Serve machine-readable category definitions, evidence, and claims boundaries."""
    return JSONResponse(build_category_hubs_json())


@app.get("/v1/products")
async def get_products() -> dict[str, Any]:
    """Serve raw data and premium workflow product catalog. FREE."""
    catalog = build_data_packages_json()
    return {
        "status": "ok",
        "starter_allowance": {
            "positioning": "Authenticated connectors receive up to 50 live data credits.",
            "eligibility": "authenticated_connector_only",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "not_free_forever": True,
            "direct_public_http": "Signed x402 payment is required per live-data request.",
            "upgrade_path": "Contact sales for sustained access through an authenticated account plan.",
        },
        "credit_costs": CREDIT_COSTS,
        "catalog": catalog,
    }


async def get_open_graph_svg(request: Request) -> Response:
    """Serve lightweight social preview artwork for high-intent pages."""
    filename = request.path_params.get("filename", "")
    slug = str(filename).removesuffix(".svg")
    if slug not in SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="OpenGraph image not found")
    return Response(
        build_open_graph_svg(slug),
        media_type="image/svg+xml; charset=utf-8",
    )


app.add_api_route(
    "/og/{filename}",
    get_open_graph_svg,
    methods=["GET", "HEAD"],
    include_in_schema=False,
)


async def get_seo_landing_page(request: Request) -> HTMLResponse:
    """Serve an additive high-intent landing page without changing the homepage."""
    slug = request.url.path.strip("/")
    if slug not in SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="SEO landing page not found")
    return HTMLResponse(build_seo_landing_page(slug))


for _seo_slug in SEO_LANDING_PAGES:
    app.add_api_route(
        f"/{_seo_slug}",
        get_seo_landing_page,
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )


@app.api_route("/go/{destination}", methods=["GET", "HEAD"], include_in_schema=False)
async def tracked_outbound_redirect(destination: str, request: Request) -> RedirectResponse:
    """Record an allowlisted conversion click and forward bounded campaign labels."""
    target = OUTBOUND_DESTINATIONS.get(destination)
    if target is None:
        raise HTTPException(status_code=404, detail="Outbound destination not found")
    attribution = _request_attribution_metadata(request)
    _record_product_event(
        "outbound_conversion_click",
        request,
        metadata={"destination": destination, **attribution},
    )
    parsed = urlsplit(target)
    location = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(attribution),
            parsed.fragment,
        )
    )
    return RedirectResponse(location, status_code=307)


def _cacheable_metadata_response(
    request: Request,
    payload: dict[str, object],
    *,
    max_age_seconds: int = 300,
) -> Response:
    """Return deterministic public metadata with bounded caching and ETag support."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    etag = f'"{hashlib.sha256(canonical).hexdigest()}"'
    headers = {
        "Cache-Control": f"public, max-age={max_age_seconds}",
        "ETag": etag,
    }
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


def _cacheable_text_metadata_response(
    request: Request,
    payload: str,
    *,
    max_age_seconds: int = 300,
) -> Response:
    encoded = payload.encode("utf-8")
    etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    headers = {
        "Cache-Control": f"public, max-age={max_age_seconds}",
        "ETag": etag,
    }
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return PlainTextResponse(payload, headers=headers)


@app.api_route(
    "/.well-known/glama.json",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_glama_well_known(request: Request) -> Response:
    """Serve the Glama connector claim file."""
    return _cacheable_metadata_response(
        request,
        {
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "maintainers": [{"email": GLAMA_MAINTAINER_EMAIL}],
        },
    )


@app.api_route(
    "/.well-known/mcp-registry-auth",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_mcp_registry_auth(request: Request) -> Response:
    """Serve the MCP Registry HTTP domain verification file."""
    return _cacheable_text_metadata_response(request, MCP_REGISTRY_AUTH_CONTENT)


@app.api_route(
    "/.well-known/x402",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_x402_well_known(request: Request) -> Response:
    """Serve x402scan-compatible paid resource discovery."""
    return _cacheable_metadata_response(
        request,
        {
            "version": 1,
            "resources": X402_WELL_KNOWN_RESOURCES,
            "instructions": (
                "Register the listed paid HTTP endpoints individually. "
                "Public MCP discovery remains available at /mcp/server/."
            ),
        },
    )


def _connector_mcp_url(env_var: str, default_path: str) -> str:
    return os.environ.get(
        env_var,
        f"{PUBLIC_BASE_URL.rstrip('/')}{default_path}",
    ).rstrip("/")


def _oauth_protected_resource_metadata(
    *,
    mcp_url: str,
    scopes: list[str],
    oauth_available: bool,
) -> dict[str, object]:
    return {
        "resource": f"{mcp_url}/",
        "authorization_servers": [mcp_url] if oauth_available else [],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
        "oauth_available": oauth_available,
    }


def _oauth_authorization_server_metadata(
    *,
    mcp_url: str,
    scopes: list[str],
    oauth_available: bool,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "issuer": mcp_url,
        "oauth_available": oauth_available,
        "scopes_supported": scopes,
    }
    if not oauth_available:
        return metadata
    metadata.update({
        "authorization_endpoint": f"{mcp_url}/authorize",
        "token_endpoint": f"{mcp_url}/token",
        "registration_endpoint": f"{mcp_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    })
    return metadata


def _connector_local_oauth_available(prefix: str, connector: Any) -> bool:
    provider = os.environ.get(f"{prefix}_AUTH_PROVIDER", "none").strip().lower()
    return provider in {"clerk", "auth0"} and getattr(connector, "auth", None) is not None


def _anthropic_mcp_url() -> str:
    return _connector_mcp_url("ANTHROPIC_MCP_PUBLIC_URL", "/anthropic/mcp")


def _cursor_mcp_url() -> str:
    return _connector_mcp_url("CURSOR_MCP_PUBLIC_URL", "/cursor/mcp")


def _openai_mcp_url() -> str:
    return _connector_mcp_url("OPENAI_MCP_PUBLIC_URL", "/openai/mcp")


@app.api_route(
    "/.well-known/oauth-protected-resource/anthropic/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-protected-resource/anthropic/mcp/",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_anthropic_oauth_protected_resource_metadata(
    request: Request,
) -> Response:
    """Serve Claude MCP OAuth protected-resource metadata at the challenged URL."""
    return _cacheable_metadata_response(
        request,
        _oauth_protected_resource_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available(
                "ANTHROPIC", anthropic_mcp
            ),
        ),
    )


@app.api_route(
    "/.well-known/oauth-protected-resource",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_root_oauth_protected_resource_metadata(request: Request) -> Response:
    """Serve root protected-resource metadata for clients that ignore path scope."""
    if _anthropic_only_mode() or _root_oauth_connector() == "anthropic":
        payload = _oauth_protected_resource_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available(
                "ANTHROPIC", anthropic_mcp
            ),
        )
    elif _root_oauth_connector() == "cursor":
        payload = _oauth_protected_resource_metadata(
            mcp_url=_cursor_mcp_url(),
            scopes=cursor_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("CURSOR", cursor_mcp),
        )
    else:
        payload = _oauth_protected_resource_metadata(
            mcp_url=_openai_mcp_url(),
            scopes=openai_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("OPENAI", openai_mcp),
        )
    return _cacheable_metadata_response(request, payload)


@app.api_route(
    "/.well-known/oauth-protected-resource/cursor/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-protected-resource/cursor/mcp/",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_cursor_oauth_protected_resource_metadata(request: Request) -> Response:
    """Serve Cursor MCP OAuth protected-resource metadata at the challenged URL."""
    return _cacheable_metadata_response(
        request,
        _oauth_protected_resource_metadata(
            mcp_url=_cursor_mcp_url(),
            scopes=cursor_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("CURSOR", cursor_mcp),
        ),
    )


@app.api_route(
    "/.well-known/oauth-protected-resource/openai/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-protected-resource/openai/mcp/",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_openai_oauth_protected_resource_metadata(request: Request) -> Response:
    """Serve OpenAI MCP OAuth protected-resource metadata."""
    return _cacheable_metadata_response(
        request,
        _oauth_protected_resource_metadata(
            mcp_url=_openai_mcp_url(),
            scopes=openai_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("OPENAI", openai_mcp),
        ),
    )


@app.api_route(
    "/anthropic/mcp/.well-known/openid-configuration",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/openid-configuration/anthropic/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-authorization-server/anthropic/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_anthropic_oauth_authorization_server_metadata(
    request: Request,
) -> Response:
    """Serve Claude MCP OAuth server metadata for path-scoped discovery."""
    return _cacheable_metadata_response(
        request,
        _oauth_authorization_server_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available(
                "ANTHROPIC", anthropic_mcp
            ),
        ),
    )


@app.api_route(
    "/.well-known/oauth-authorization-server",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_root_oauth_authorization_server_metadata(request: Request) -> Response:
    """Serve root OAuth metadata for clients that ignore path-scoped discovery."""
    if _anthropic_only_mode() or _root_oauth_connector() == "anthropic":
        payload = _oauth_authorization_server_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("ANTHROPIC", anthropic_mcp),
        )
    elif _root_oauth_connector() == "cursor":
        payload = _oauth_authorization_server_metadata(
            mcp_url=_cursor_mcp_url(),
            scopes=cursor_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("CURSOR", cursor_mcp),
        )
    else:
        payload = _oauth_authorization_server_metadata(
            mcp_url=_openai_mcp_url(),
            scopes=openai_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("OPENAI", openai_mcp),
        )
    return _cacheable_metadata_response(request, payload)


@app.api_route(
    "/cursor/mcp/.well-known/openid-configuration",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/openid-configuration/cursor/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-authorization-server/cursor/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_cursor_oauth_authorization_server_metadata(request: Request) -> Response:
    """Serve Cursor MCP OAuth server metadata for clients probing the root path."""
    return _cacheable_metadata_response(
        request,
        _oauth_authorization_server_metadata(
            mcp_url=_cursor_mcp_url(),
            scopes=cursor_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("CURSOR", cursor_mcp),
        ),
    )


@app.api_route(
    "/openai/mcp/.well-known/openid-configuration",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/openid-configuration/openai/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
@app.api_route(
    "/.well-known/oauth-authorization-server/openai/mcp",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def get_openai_oauth_authorization_server_metadata(request: Request) -> Response:
    """Serve OpenAI MCP OAuth authorization-server metadata."""
    return _cacheable_metadata_response(
        request,
        _oauth_authorization_server_metadata(
            mcp_url=_openai_mcp_url(),
            scopes=openai_auth.oauth_scopes(),
            oauth_available=_connector_local_oauth_available("OPENAI", openai_mcp),
        ),
    )


# Mount assets, PDFs, and the public remote MCP discovery server
app.mount(
    "/assets",
    StaticFiles(directory=str(DOCS_DIR / "assets"), check_dir=False),
    name="assets",
)
app.mount(
    "/pdf",
    StaticFiles(directory=str(DOCS_DIR / "pdf"), check_dir=False),
    name="pdf",
)
app.mount(
    "/evidence",
    StaticFiles(directory=str(DOCS_DIR / "evidence"), html=True, check_dir=False),
    name="evidence",
)
app.add_route(
    REMOTE_MCP_PATH.rstrip("/"),
    _SlashlessMountEndpoint(PUBLIC_MCP_HTTP_APP, REMOTE_MCP_PATH),
    include_in_schema=False,
)
app.mount(REMOTE_MCP_PATH, PUBLIC_MCP_HTTP_APP, name="public-mcp")
app.add_route(
    "/anthropic/mcp",
    _SlashlessMountEndpoint(ANTHROPIC_MCP_HTTP_APP, "/anthropic/mcp"),
    include_in_schema=False,
)
app.mount("/anthropic/mcp", ANTHROPIC_MCP_HTTP_APP, name="anthropic-mcp")
app.add_route(
    "/cursor/mcp",
    _SlashlessMountEndpoint(CURSOR_MCP_HTTP_APP, "/cursor/mcp"),
    include_in_schema=False,
)
app.mount("/cursor/mcp", CURSOR_MCP_HTTP_APP, name="cursor-mcp")
app.add_route(
    "/openai/mcp",
    _SlashlessMountEndpoint(OPENAI_MCP_HTTP_APP, "/openai/mcp"),
    include_in_schema=False,
)
app.mount("/openai/mcp", OPENAI_MCP_HTTP_APP, name="openai-mcp")


# ---------------------------------------------------------------------------
# x402 Payment Middleware — Tiered Pricing
# ---------------------------------------------------------------------------

# Route → price mapping (None = free)
ROUTE_PRICING: dict[str, Decimal | None] = {
    # Crypto — dynamic pricing based on asset tier (handled separately)
    "/v1/vwap/": None,  # set dynamically
    "/v1/bidask/": None,  # set dynamically
    "/v1/state/": None,  # set dynamically
    "/v1/vwap30m/": None,  # set dynamically
    "/v1/vwap24h/": None,  # set dynamically
    # TradFi
    "/v1/fx/": settings.pricing.tradfi,
    "/v1/metal/": settings.pricing.tradfi,
    "/v1/briefs/market": Decimal("0.25"),
    "/v1/checks/pre-trade": Decimal("0.10"),
    "/v1/receipts/price": Decimal("0.25"),
    "/v1/snapshots/macro": Decimal("1.00"),
    "/v1/monitors/evaluate": Decimal("0.25"),
    "/v1/indicators/token-quality": Decimal("0.50"),
    "/v1/indicators/state-divergence": Decimal("0.50"),
    "/v1/signals/solana-token-brief": Decimal("1.00"),
    "/v1/signals/trader-alpha-pack": Decimal("2.50"),
    "/v1/rwa/benchmark/blocksize": Decimal("0.25"),
    # Free
    "/v1/coverage": None,
    "/v1/search": None,
    "/v1/instruments/": None,
    "/v1/rwa/coverage": None,
    "/v1/rwa/assets": None,
    "/v1/rwa/oracle-parity": None,
    "/v1/rwa/dex-venues": None,
    "/v1/rwa/derivative-venues": None,
    "/v1/rwa/rwa-xyz-monitor": None,
    "/v1/rwa/daily-feed-agent": None,
    "/v1/rwa/dex-allowlist": None,
    "/v1/rwa/non-crypto-feeds": None,
    "/v1/rwa/discovery": None,
    "/v1/rwa/discovery/mitigation-plan": None,
    "/v1/rwa/blocker-resolution": None,
    "/v1/rwa/source-rights": None,
    "/v1/rwa/replay-inventory": None,
    "/v1/rwa/equity-universes": None,
    "/v1/rwa/market-expansion": None,
    "/v1/rwa/futures-data-plan": None,
    "/v1/rwa/oracle-streams": None,
    "/v1/rwa/provider-catalog": None,
    "/v1/rwa/source-readiness": None,
    "/v1/rwa/blocksize-state-methodology": None,
    "/v1/rwa/consensus/sources": None,
    "/v1/rwa/consensus/calculate": None,
    "/v1/rwa/registry": None,
    "/v1/rwa/registry/venues": None,
    "/v1/rwa/resolve": None,
    "/v1/rwa/sourcing/jobs": None,
    "/v1/rwa/sourcing/probe": None,
    "/v1/rwa/build-plan": None,
    "/v1/rwa/vwap/calculate": None,
    "/v1/rwa/bidask/calculate": None,
    "/v1/rwa/quality/check": None,
    "/v1/rwa/feeds": None,
    "/v1/rwa/feeds/promotion-check": None,
    "/v1/rwa/aggregate": None,
    "/v1/rwa/realtime/requirements": None,
    "/v1/rwa/realtime/quality": None,
    "/v1/rwa/observations/store": None,
    "/v1/rwa/observations": None,
    "/v1/rwa/observations/summary": None,
    "/v1/cache/status": None,
    "/v1/provenance/": None,
    "/health": None,
    "/readyz": None,
    "/v1/credits/": None,  # Credit endpoints define their own x402 challenges
}

SUPPORTED_BATCH_SERVICES = {"vwap", "bidask", "fx", "metal", "state", "vwap30m", "vwap24h"}
QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "BTC", "ETH")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
WALLET_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{20,128}$")
STARTER_ID_RE = re.compile(r"^[A-Za-z0-9:._@-]{8,160}$")
EVM_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EVM_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SOLANA_SIGNATURE_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
DISCOVERY_RATE_LIMIT_PATHS = (
    "/v1/coverage",
    "/v1/search",
    "/v1/instruments/",
    "/v1/rwa/",
)
# ---------------------------------------------------------------------------
# Documentation & Schemas
# ---------------------------------------------------------------------------

X402_RESPONSE = {
    "402": {
        "description": "Payment Required. Returns a PAYMENT-REQUIRED header with an x402 PaymentRequired challenge.",
        "headers": {
            "PAYMENT-REQUIRED": {
                "description": "Base64 encoded x402 PaymentRequired object.",
                "schema": {"type": "string"}
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "x402Version": 2,
                    "error": "Payment Required",
                    "message": "This endpoint requires a payment of $0.002 USDC.",
                    "price_usdc": "0.002",
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                            "amount": "2000",
                            "asset": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                            "payTo": "recipient-wallet",
                            "maxTimeoutSeconds": 30,
                        }
                    ],
                    "networks": [
                        {"name": "Solana", "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
                        {"name": "Base", "caip2": "eip155:8453"}
                    ]
                }
            }
        }
    }
}


X402_PROTOCOLS = [{"x402": {}}]
X402_CRYPTO_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(settings.pricing.extended_crypto),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_BIDASK_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(max(settings.pricing.extended_crypto, settings.pricing.equities)),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_TRADFI_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "fixed",
            "currency": "USD",
            "amount": str(settings.pricing.tradfi),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_BATCH_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(
                max(
                    settings.pricing.extended_crypto,
                    settings.pricing.tradfi,
                    settings.pricing.equities,
                )
                * Decimal(settings.server.max_batch_size)
            ),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_WELL_KNOWN_RESOURCES = [
    f"{PUBLIC_BASE_URL}/v1/vwap/BTC-USD",
    f"{PUBLIC_BASE_URL}/v1/bidask/BTC-USD",
    f"{PUBLIC_BASE_URL}/v1/state/MSOLUSD",
    f"{PUBLIC_BASE_URL}/v1/vwap30m/SOLUSD",
    f"{PUBLIC_BASE_URL}/v1/vwap24h/BTCUSD",
    f"{PUBLIC_BASE_URL}/v1/bidask/AAPL",
    f"{PUBLIC_BASE_URL}/v1/fx/EURUSD",
    f"{PUBLIC_BASE_URL}/v1/metal/XAUUSD",
    f"{PUBLIC_BASE_URL}/v1/briefs/market",
    f"{PUBLIC_BASE_URL}/v1/checks/pre-trade",
    f"{PUBLIC_BASE_URL}/v1/receipts/price",
    f"{PUBLIC_BASE_URL}/v1/snapshots/macro",
    f"{PUBLIC_BASE_URL}/v1/indicators/token-quality",
    f"{PUBLIC_BASE_URL}/v1/indicators/state-divergence",
    f"{PUBLIC_BASE_URL}/v1/signals/solana-token-brief",
    f"{PUBLIC_BASE_URL}/v1/signals/trader-alpha-pack",
]


def _x402_endpoint_description(path: str) -> str:
    if path.startswith("/v1/vwap/"):
        return "Real-time crypto VWAP market data from Blocksize Capital."
    if path.startswith("/v1/bidask/"):
        return "Real-time shared bid/ask snapshot data, including crypto pairs and supported equity tickers, from Blocksize Capital."
    if path.startswith("/v1/fx/"):
        return "Real-time FX market data from Blocksize Capital."
    if path.startswith("/v1/metal/"):
        return "Real-time precious metals market data from Blocksize Capital."
    if path.startswith("/v1/batch"):
        return "Batch real-time market data queries from Blocksize Capital."
    if path.startswith("/v1/briefs/market"):
        return "Agent Market Brief: decision-ready market package with provenance."
    if path.startswith("/v1/checks/pre-trade"):
        return "Pre-Trade Sanity Check: spread, freshness, and reference-price guardrails."
    if path.startswith("/v1/receipts/price"):
        return "Audit-grade price receipt with source inputs and reproducibility metadata."
    if path.startswith("/v1/snapshots/macro"):
        return "Multi-asset macro snapshot across crypto, FX, metals, and risk context."
    if path.startswith("/v1/indicators/token-quality"):
        return "Token Market Quality Score: trader-grade freshness, spread, state, and VWAP-window indicator."
    if path.startswith("/v1/indicators/state-divergence"):
        return "Oracle/state price divergence indicator using live Blocksize market and state prices."
    if path.startswith("/v1/signals/solana-token-brief"):
        return "Solana token brief for supported token symbols, with explicit feed coverage and trader signals."
    if path.startswith("/v1/signals/trader-alpha-pack"):
        return "Trader alpha-style signal pack built from Blocksize price, bid/ask, state, and VWAP-window data."
    if path.startswith("/v1/credits/purchase"):
        return "Local-QA-only legacy wallet credit purchase; unavailable in production."
    return "Blocksize Capital x402-protected market data."


def _x402_query_schema_for_request(request: Request) -> dict[str, Any]:
    if request.url.path.startswith("/v1/batch"):
        return {
            "type": "object",
            "properties": {
                "reqs": {
                    "type": "string",
                    "description": "Comma-separated service:symbol items, for example vwap:BTCUSD,fx:EURUSD.",
                }
            },
            "required": ["reqs"],
            "additionalProperties": False,
        }
    if request.url.path.startswith("/v1/credits/purchase"):
        return {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["starter", "pro", "institutional"],
                }
            },
            "required": ["tier"],
            "additionalProperties": False,
        }
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _x402_bazaar_extension(request: Request) -> dict[str, Any]:
    query_schema = _x402_query_schema_for_request(request)
    query_example: dict[str, Any] = {}
    if "reqs" in query_schema.get("properties", {}):
        query_example["reqs"] = "vwap:BTCUSD,fx:EURUSD"
    if "tier" in query_schema.get("properties", {}):
        query_example["tier"] = "starter"

    output_example = {
        "status": "ok",
        "data": {},
        "meta": {"provider": "Blocksize Capital"},
    }
    method = request.method.upper()
    body_method = method in {"POST", "PUT", "PATCH"}
    if body_method:
        input_info = {
            "type": "http",
            "method": method,
            "bodyType": "json",
            "body": {},
        }
        input_properties = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
            "bodyType": {
                "type": "string",
                "enum": ["json", "form-data", "text"],
            },
            "body": {"type": "object"},
        }
        input_required = ["type", "method", "bodyType", "body"]
    else:
        input_info = {
            "type": "http",
            "method": method,
            "queryParams": query_example,
        }
        input_properties = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": ["GET", "HEAD", "DELETE"]},
            "queryParams": query_schema,
        }
        input_required = ["type", "method"]
    return {
        "info": {
            "input": input_info,
            "output": {"type": "json", "example": output_example},
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": input_properties,
                    "required": input_required,
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": {"type": "object"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


def _x402_v2_accepts(
    payment_requirements: list[dict],
    resource_url: str | None = None,
) -> list[dict[str, Any]]:
    accepts: list[dict[str, Any]] = []
    for requirement in payment_requirements:
        extra = requirement.get("extra")
        accept_extra = dict(extra) if isinstance(extra, dict) else {}
        if resource_url:
            accept_extra["resource"] = resource_url
        fallback_asset = (
            settings.x402.solana_usdc_address
            if _network_kind(str(requirement.get("network", ""))) == "solana"
            else settings.x402.base_usdc_address
        )
        accept = {
            "scheme": str(requirement.get("scheme") or "exact"),
            "network": str(requirement.get("network") or ""),
            "amount": str(
                requirement.get("amount")
                or requirement.get("maxAmountRequired")
                or "0"
            ),
            "asset": _requirement_asset(requirement, fallback_asset),
            "payTo": str(requirement.get("payTo") or requirement.get("resource") or ""),
            "maxTimeoutSeconds": int(requirement.get("maxTimeoutSeconds") or 60),
            "extra": accept_extra,
        }
        accepts.append(accept)
    return accepts


def _public_request_url(request: Request) -> str:
    """Build the canonical public URL used inside payment challenges."""
    base = PUBLIC_BASE_URL.rstrip("/")
    path = request.url.path
    query = request.url.query
    url = f"{base}{path}"
    return f"{url}?{query}" if query else url


def _x402_payment_required(
    request: Request,
    payment_requirements: list[dict],
) -> dict[str, Any]:
    resource_url = _public_request_url(request)
    return {
        "x402Version": 2,
        "error": "Payment Required",
        "resource": {
            "url": resource_url,
            "description": _x402_endpoint_description(request.url.path),
            "mimeType": "application/json",
        },
        "accepts": _x402_v2_accepts(payment_requirements, resource_url),
        "extensions": {"bazaar": _x402_bazaar_extension(request)},
    }


def _encode_payment_required(payment_required: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payment_required).encode()).decode()


def _normalise_symbol(value: str, field_name: str = "symbol") -> str:
    """Return an upstream-safe alphanumeric symbol."""
    raw = value.strip()
    if len(raw) > 64:
        raise ValueError(f"{field_name} is too long")
    clean = raw.replace("-", "").replace("/", "").replace("_", "").upper()
    if not SYMBOL_RE.fullmatch(clean):
        raise ValueError(f"Invalid {field_name}; use 2-32 letters or digits")
    return clean


def _parse_batch_reqs(reqs: str) -> list[tuple[str, str, str]]:
    """Parse and validate batch requests as (service, clean_symbol, original)."""
    raw_queries = [item.strip() for item in reqs.split(",") if item.strip()]
    if not raw_queries:
        raise ValueError("Batch request must include at least one service:symbol item")
    if len(raw_queries) > settings.server.max_batch_size:
        raise ValueError(
            f"Batch request exceeds MAX_BATCH_SIZE={settings.server.max_batch_size}"
        )

    parsed: list[tuple[str, str, str]] = []
    for raw_query in raw_queries:
        if ":" not in raw_query:
            raise ValueError("Batch items must use service:symbol format")
        svc, ticker = raw_query.split(":", 1)
        svc = svc.strip().lower()
        if svc not in SUPPORTED_BATCH_SERVICES:
            raise ValueError(f"Unsupported batch service: {svc}")
        parsed.append((svc, _normalise_symbol(ticker, "batch symbol"), raw_query))

    return parsed


def _base_from_symbol(symbol: str) -> str:
    """Extract a likely base asset from a compact pair symbol."""
    for quote in QUOTE_SUFFIXES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol[: len(symbol) // 2].upper()


def _quote_from_symbol(symbol: str) -> str:
    """Extract a supported quote suffix from a compact symbol, when present."""
    for quote in QUOTE_SUFFIXES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    return ""


def _looks_like_equity_bidask_symbol(symbol: str) -> bool:
    """Heuristically identify equity tickers routed through /v1/bidask."""
    clean = symbol.upper()
    quote = _quote_from_symbol(clean)
    base = clean[: -len(quote)] if quote else clean

    if clean in METAL_TICKERS or base in FIAT_CURRENCIES:
        return False

    if quote in {"USD", "USDT", "USDC"}:
        return base.endswith("X")

    return clean.isalpha() and 1 <= len(clean) <= 5 and clean not in TOP_250_CRYPTO


def _bidask_price_for_symbol(symbol: str) -> Decimal:
    """Price shared bid/ask calls across crypto pairs and equity tickers."""
    if _looks_like_equity_bidask_symbol(symbol):
        return settings.pricing.equities
    return settings.pricing.get_crypto_price(_base_from_symbol(symbol))


class InMemoryRateLimiter:
    """Small fixed-window limiter for public discovery traffic."""

    def __init__(self) -> None:
        self._minute_hits: dict[str, Deque[float]] = defaultdict(deque)
        self._day_hits: dict[str, Deque[float]] = defaultdict(deque)

    def clear(self) -> None:
        self._minute_hits.clear()
        self._day_hits.clear()

    @staticmethod
    def _prune(hits: Deque[float], now: float, window_seconds: int) -> None:
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

    @staticmethod
    def _retry_after(hits: Deque[float], now: float, window_seconds: int) -> int:
        if not hits:
            return window_seconds
        return max(1, int(hits[0] + window_seconds - now) + 1)

    def check(
        self,
        key: str,
        *,
        per_minute: int,
        per_day: int,
        now: float | None = None,
    ) -> tuple[bool, int | None, str | None]:
        """Return whether the request is allowed, retry delay, and limit label."""
        if per_minute <= 0 and per_day <= 0:
            return True, None, None

        now = now or time.time()
        minute_hits = self._minute_hits[key]
        day_hits = self._day_hits[key]

        self._prune(minute_hits, now, 60)
        self._prune(day_hits, now, 86_400)

        if per_minute > 0 and len(minute_hits) >= per_minute:
            return False, self._retry_after(minute_hits, now, 60), "minute"
        if per_day > 0 and len(day_hits) >= per_day:
            return False, self._retry_after(day_hits, now, 86_400), "day"

        if per_minute > 0:
            minute_hits.append(now)
        if per_day > 0:
            day_hits.append(now)
        return True, None, None


_DISCOVERY_RATE_LIMITER = InMemoryRateLimiter()


def _client_ip(request: Request) -> str:
    """Best-effort client identifier for soft public discovery limits."""
    return _request_client_ip(request)


def _is_discovery_rate_limited_path(path: str) -> bool:
    """Limit public discovery calls without throttling docs, manifests, or paid routes."""
    remote_mcp_path = REMOTE_MCP_PATH.rstrip("/")
    for discovery_path in DISCOVERY_RATE_LIMIT_PATHS:
        if discovery_path.endswith("/"):
            if path.startswith(discovery_path):
                return True
        elif path == discovery_path:
            return True
    return path == remote_mcp_path or path.startswith(f"{remote_mcp_path}/")


def _discovery_rate_limit_response(request: Request) -> JSONResponse | None:
    """Return a 429 response when public discovery traffic exceeds fair-use limits."""
    if not settings.server.discovery_rate_limit_enabled:
        return None

    path = request.url.path
    if not _is_discovery_rate_limited_path(path):
        return None

    client_ip = _client_ip(request)
    manager = getattr(request.app.state, "credits", None)
    try:
        if isinstance(manager, CreditManager):
            allowed, retry_after, limit_window = manager.check_rate_limit(
                scope="discovery",
                key=client_ip,
                per_minute=settings.server.discovery_rate_limit_per_minute,
                per_day=settings.server.discovery_rate_limit_per_day,
            )
        else:
            allowed, retry_after, limit_window = _DISCOVERY_RATE_LIMITER.check(
                f"discovery:{client_ip}",
                per_minute=settings.server.discovery_rate_limit_per_minute,
                per_day=settings.server.discovery_rate_limit_per_day,
            )
    except sqlite3.Error:
        logger.exception("Persistent discovery rate limiter is unavailable")
        return JSONResponse(
            status_code=503,
            content={
                "error": "Service Unavailable",
                "message": "Discovery rate-limit state is temporarily unavailable.",
            },
        )
    if allowed:
        return None

    retry_after = retry_after or 60
    logger.warning(
        "Discovery rate limit exceeded for %s on %s (%s limit)",
        client_ip,
        path,
        limit_window,
    )
    return JSONResponse(
        status_code=429,
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Policy": (
                f"{settings.server.discovery_rate_limit_per_minute}/minute; "
                f"{settings.server.discovery_rate_limit_per_day}/day"
            ),
        },
        content={
            "error": "Too Many Requests",
            "message": (
                "Free discovery traffic is temporarily rate limited. "
                "Please retry later or use paid data routes for production traffic."
            ),
            "retry_after_seconds": retry_after,
            "limit_window": limit_window,
        },
    )


def _get_price_for_request(request: Request) -> Decimal | None:
    """Determine the price for a given request."""
    path = request.url.path
    paid_get_prefixes = (
        "/v1/batch",
        "/v1/vwap/",
        "/v1/bidask/",
        "/v1/state/",
        "/v1/vwap30m/",
        "/v1/vwap24h/",
        "/v1/fx/",
        "/v1/metal/",
    )
    paid_post_paths = {
        "/v1/briefs/market",
        "/v1/checks/pre-trade",
        "/v1/receipts/price",
        "/v1/snapshots/macro",
        "/v1/monitors/evaluate",
        "/v1/indicators/token-quality",
        "/v1/indicators/state-divergence",
        "/v1/signals/solana-token-brief",
        "/v1/signals/trader-alpha-pack",
        "/v1/rwa/benchmark/blocksize",
    }
    method = request.method.upper()
    if path.startswith(paid_get_prefixes) and method not in {"GET", "HEAD"}:
        return None
    if path in paid_post_paths and method != "POST":
        return None
    
    # Handle Batch endpoint dynamically
    if path.startswith("/v1/batch"):
        reqs = request.query_params.get("reqs", "")
        if not reqs:
            return None
        
        total = Decimal("0.0")
        for svc, pair, _raw_query in _parse_batch_reqs(reqs):
            if svc in {"vwap", "state", "vwap30m", "vwap24h"}:
                base = _base_from_symbol(pair)
                svc_price = settings.pricing.get_crypto_price(base)
                total += svc_price
            elif svc == "bidask":
                total += _bidask_price_for_symbol(pair)
            else:
                mock_path = f"/v1/{svc}/{pair}"
                for prefix, price in ROUTE_PRICING.items():
                    if mock_path.startswith(prefix) and price is not None:
                        total += price
                        break
        return total if total > 0 else None

    # Crypto uses dynamic tier pricing
    if (
        path.startswith("/v1/vwap/")
        or path.startswith("/v1/bidask/")
        or path.startswith("/v1/state/")
        or path.startswith("/v1/vwap30m/")
        or path.startswith("/v1/vwap24h/")
    ):
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            pair = _normalise_symbol(parts[3], "pair")
            if path.startswith("/v1/bidask/"):
                return _bidask_price_for_symbol(pair)
            base = _base_from_symbol(pair)
            return settings.pricing.get_crypto_price(base)
        return settings.pricing.core_crypto

    # Check static route pricing
    for route_prefix, price in ROUTE_PRICING.items():
        if path == route_prefix or (route_prefix.endswith("/") and path.startswith(route_prefix)):
            return price

    return None  # Free endpoint


def _credit_cost_for_request(request: Request) -> float | None:
    """Return starter-credit cost for a paid request, independent of USDC price."""
    path = request.url.path
    if path.startswith("/v1/batch"):
        reqs = request.query_params.get("reqs", "")
        if not reqs:
            return None
        total = 0.0
        for svc, _pair, _raw_query in _parse_batch_reqs(reqs):
            if svc == "vwap":
                total += CREDIT_COSTS["raw_vwap"]
            elif svc == "bidask":
                total += CREDIT_COSTS["raw_bidask"]
            elif svc == "state":
                total += CREDIT_COSTS["raw_state"]
            elif svc == "vwap30m":
                total += CREDIT_COSTS["raw_vwap_30m"]
            elif svc == "vwap24h":
                total += CREDIT_COSTS["raw_vwap_24h"]
            elif svc == "fx":
                total += CREDIT_COSTS["fx"]
            elif svc == "metal":
                total += CREDIT_COSTS["metals"]
        return total
    if path.startswith("/v1/vwap/"):
        return CREDIT_COSTS["raw_vwap"]
    if path.startswith("/v1/bidask/"):
        return CREDIT_COSTS["raw_bidask"]
    if path.startswith("/v1/state/"):
        return CREDIT_COSTS["raw_state"]
    if path.startswith("/v1/vwap30m/"):
        return CREDIT_COSTS["raw_vwap_30m"]
    if path.startswith("/v1/vwap24h/"):
        return CREDIT_COSTS["raw_vwap_24h"]
    if path.startswith("/v1/fx/"):
        return CREDIT_COSTS["fx"]
    if path.startswith("/v1/metal/"):
        return CREDIT_COSTS["metals"]
    if path.startswith("/v1/briefs/market"):
        return CREDIT_COSTS["market_brief"]
    if path.startswith("/v1/checks/pre-trade"):
        return CREDIT_COSTS["pre_trade_check"]
    if path.startswith("/v1/receipts/price"):
        return CREDIT_COSTS["audit_receipt"]
    if path.startswith("/v1/snapshots/macro"):
        return CREDIT_COSTS["macro_snapshot"]
    if path.startswith("/v1/monitors/evaluate"):
        return CREDIT_COSTS["market_brief"]
    if path.startswith("/v1/indicators/token-quality"):
        return CREDIT_COSTS["token_quality_indicator"]
    if path.startswith("/v1/indicators/state-divergence"):
        return CREDIT_COSTS["state_divergence_indicator"]
    if path.startswith("/v1/signals/solana-token-brief"):
        return CREDIT_COSTS["solana_token_brief"]
    if path.startswith("/v1/signals/trader-alpha-pack"):
        return CREDIT_COSTS["trader_alpha_pack"]
    if path.startswith("/v1/rwa/benchmark/blocksize"):
        return CREDIT_COSTS["rwa_blocksize_benchmark"]
    if path.startswith("/v1/provenance/"):
        return CREDIT_COSTS["provenance_lookup"]
    return None


def _starter_credit_subject(request: Request) -> tuple[str, str, bool] | None:
    """Resolve the best starter-credit subject from wallet/user/agent hints."""
    if (
        is_production_environment()
        or not settings.server.unverified_http_credits_enabled
    ):
        return None
    wallet = request.headers.get("X-AGENT-WALLET")
    if wallet:
        clean_wallet = wallet.strip()
        if not WALLET_ID_RE.fullmatch(clean_wallet):
            raise ValueError("Invalid X-AGENT-WALLET header")
        return clean_wallet, "wallet", True

    for header_name, subject_type in (
        ("X-AUTHENTICATED-USER", "user"),
        ("X-USER-ID", "user"),
        ("X-AGENT-ID", "agent"),
        ("X-DEVICE-ID", "device"),
        ("X-SESSION-ID", "session"),
    ):
        value = request.headers.get(header_name)
        if value:
            clean_value = value.strip()
            if not STARTER_ID_RE.fullmatch(clean_value):
                raise ValueError(f"Invalid {header_name} header")
            return clean_value, subject_type, False

    return None


def _credit_meta_for_request(request: Request) -> dict[str, Any] | None:
    context = getattr(request.state, "starter_credit_context", None)
    if not isinstance(context, dict):
        return None
    return {
        "credit_mode": "starter_allowance",
        "credit_cost": context["credits_spent"],
        "credits_remaining": context["credits_remaining"],
        "starter_allowance_credits": STARTER_CREDIT_ALLOWANCE,
        "upgrade_path": "After authenticated connector credits are exhausted, use signed x402 or contact sales for an account plan.",
    }


def _apply_credit_response_headers(response: Response, request: Request) -> Response:
    context = getattr(request.state, "starter_credit_context", None)
    if isinstance(context, dict):
        response.headers["X-Blocksize-Credit-Mode"] = "starter-allowance"
        response.headers["X-Blocksize-Credits-Spent"] = str(context["credits_spent"])
        response.headers["X-Blocksize-Credits-Remaining"] = str(context["credits_remaining"])
        response.headers["X-Blocksize-Starter-Allowance"] = str(STARTER_CREDIT_ALLOWANCE)
        response.headers["X-Blocksize-Upgrade-Path"] = "x402-or-authenticated-account-plan"
        if context.get("credits_refunded"):
            response.headers["X-Blocksize-Credits-Refunded"] = str(context["credits_refunded"])
            response.headers["X-Blocksize-Delivery-Status"] = "failed-refunded"
            response.headers["X-Blocksize-Retry-Safe"] = "true"
    return response


PAID_CATALOG_CACHE_TTL_SECONDS = 60.0
PAID_PREFLIGHT_SERVICE_PREFIXES = {
    "/v1/vwap/": "vwap",
    "/v1/bidask/": "bidask",
    "/v1/state/": "state",
    "/v1/vwap30m/": "vwap",
    "/v1/vwap24h/": "vwap",
    "/v1/fx/": "fx",
    "/v1/metal/": "metal",
}
PAID_PRODUCT_SYMBOL_MODES = {
    "/v1/briefs/market": "current",
    "/v1/checks/pre-trade": "current",
    "/v1/receipts/price": "current",
    "/v1/monitors/evaluate": "current",
    "/v1/indicators/token-quality": "current",
    "/v1/indicators/state-divergence": "state_and_current",
    "/v1/signals/solana-token-brief": "current",
    "/v1/signals/trader-alpha-pack": "current",
}


def _purchase_ready_pair(pair: PairInfo) -> PairInfo:
    """Attach an attributed purchase URL and a copyable unsigned request."""
    if not pair.endpoint_path:
        return pair
    query = urlencode(
        {
            "selection_source": "public_http_resolver",
            "utm_source": "instrument_search",
            "utm_medium": "free_discovery",
            "utm_campaign": "resolver_handoff",
        }
    )
    url = f"{PUBLIC_BASE_URL}{pair.endpoint_path}?{query}"
    return pair.model_copy(
        update={
            "purchase_url": url,
            "copy_request": f"curl -i '{url}'",
        }
    )


async def _paid_catalog_symbols(request: Request, service: str) -> set[str]:
    """Return a short-lived normalized catalog for payment preflight."""
    cache = getattr(request.app.state, "instrument_catalog_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        request.app.state.instrument_catalog_cache = cache
    now = time.monotonic()
    cached = cache.get(service)
    if isinstance(cached, tuple) and len(cached) == 2 and now - cached[0] < PAID_CATALOG_CACHE_TTL_SECONDS:
        return set(cached[1])
    if service == "current":
        vwap, bidask = await asyncio.gather(
            _paid_catalog_symbols(request, "vwap"),
            _paid_catalog_symbols(request, "bidask"),
        )
        symbols = vwap | bidask
        cache[service] = (now, frozenset(symbols))
        return symbols

    lock = getattr(request.app.state, "instrument_catalog_cache_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.instrument_catalog_cache_lock = lock
    async with lock:
        cached = cache.get(service)
        now = time.monotonic()
        if isinstance(cached, tuple) and len(cached) == 2 and now - cached[0] < PAID_CATALOG_CACHE_TTL_SECONDS:
            return set(cached[1])

        client: BlocksizeClient = request.app.state.blocksize
        if service == "vwap":
            records: list[Any] = await client.list_vwap_instruments()
        elif service == "bidask":
            records = await client.list_bidask_instruments()
        elif service == "fx":
            records = await client.list_fx_instruments()
        elif service == "metal":
            records = await client.list_metal_instruments()
        elif service == "state":
            records = await client.list_state_instruments()
        else:
            raise ValueError(f"Unsupported preflight service: {service}")

        symbols: set[str] = set()
        for record in records:
            value = record.get("symbol") if isinstance(record, dict) else record
            if value is None:
                continue
            try:
                symbols.add(_normalise_symbol(str(value), "catalog symbol"))
            except ValueError:
                continue
        cache[service] = (now, frozenset(symbols))
        return symbols


async def _paid_catalog_alternatives(
    request: Request,
    symbol: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    client: BlocksizeClient = request.app.state.blocksize
    search_term = _base_from_symbol(symbol) or symbol
    try:
        pairs, _total = await client.search_pairs_page(
            search_term,
            "all",
            limit=limit,
            offset=0,
        )
    except (BlocksizeAPIError, httpx.HTTPError, TypeError, ValueError):
        return []
    return [
        _purchase_ready_pair(pair).model_dump()
        for pair in pairs[:limit]
        if pair.endpoint_path
    ]


async def _unsupported_paid_instrument_response(
    request: Request,
    *,
    symbol: str,
    service: str,
) -> JSONResponse:
    alternatives = await _paid_catalog_alternatives(request, symbol)
    return JSONResponse(
        status_code=404,
        content={
            "error": "Unsupported Instrument",
            "error_code": "UNSUPPORTED_INSTRUMENT",
            "message": (
                f"{symbol} is not currently catalog-confirmed for {service}; "
                "no payment challenge was created and no payment was used."
            ),
            "symbol": symbol,
            "service": service,
            "alternatives": alternatives,
            "search_url": (
                f"{PUBLIC_BASE_URL}/v1/search?"
                + urlencode({"q": _base_from_symbol(symbol) or symbol})
            ),
        },
    )


async def _validate_catalog_support(
    request: Request,
    *,
    symbol: str,
    service: str,
) -> JSONResponse | None:
    try:
        catalog = await _paid_catalog_symbols(request, service)
    except (BlocksizeAPIError, httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Paid catalog preflight unavailable for %s: %s", service, exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "30", "Cache-Control": "no-store"},
            content={
                "error": "Instrument Readiness Unavailable",
                "error_code": "INSTRUMENT_PREFLIGHT_UNAVAILABLE",
                "message": (
                    "Instrument support could not be confirmed, so no payment "
                    "challenge was created and no payment was used."
                ),
                "symbol": symbol,
                "service": service,
            },
        )
    if symbol in catalog:
        return None
    return await _unsupported_paid_instrument_response(
        request,
        symbol=symbol,
        service=service,
    )


def _payload_symbols(payload: dict[str, Any]) -> list[str]:
    value = payload.get("symbols") or payload.get("watchlist") or payload.get("symbol")
    if value is None:
        return []
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        return []
    return [_normalise_symbol(str(item), "symbol") for item in value]


async def _validate_paid_request_before_charge(request: Request) -> JSONResponse | None:
    """Reject malformed or unsupported requests before payment is requested."""
    method = request.method.upper()
    path = request.url.path

    if method in {"GET", "HEAD"}:
        if path.startswith("/v1/batch"):
            try:
                queries = _parse_batch_reqs(request.query_params.get("reqs", ""))
            except ValueError as exc:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Bad Request", "message": str(exc)},
                )
            for service, symbol, _raw in queries:
                catalog_service = {
                    "vwap30m": "vwap",
                    "vwap24h": "vwap",
                }.get(service, service)
                if catalog_service not in {"vwap", "bidask", "state", "fx", "metal"}:
                    continue
                unsupported = await _validate_catalog_support(
                    request,
                    symbol=symbol,
                    service=catalog_service,
                )
                if unsupported is not None:
                    return unsupported
            return None
        for prefix, service in PAID_PREFLIGHT_SERVICE_PREFIXES.items():
            if path.startswith(prefix):
                symbol = _normalise_symbol(path[len(prefix):].split("/", 1)[0], "symbol")
                return await _validate_catalog_support(
                    request,
                    symbol=symbol,
                    service=service,
                )
        return None

    if method not in {"POST", "PUT", "PATCH"}:
        return None

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "error": "Bad Request",
                "message": "A valid JSON request body is required; no credits or payment were used.",
            },
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=422,
            content={
                "error": "Unprocessable Entity",
                "message": "The JSON request body must be an object; no credits or payment were used.",
            },
        )

    required_symbol_paths = {
        "/v1/checks/pre-trade",
        "/v1/receipts/price",
        "/v1/indicators/token-quality",
        "/v1/indicators/state-divergence",
    }
    if path in required_symbol_paths:
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Bad Request",
                    "message": "symbol is required; no credits or payment were used.",
                },
            )
        try:
            _normalise_symbol(symbol, "symbol")
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Bad Request",
                    "message": f"{exc}; no credits or payment were used.",
                },
            )
    mode = PAID_PRODUCT_SYMBOL_MODES.get(path)
    if mode:
        try:
            symbols = _payload_symbols(payload)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Bad Request",
                    "message": f"{exc}; no credits or payment were used.",
                },
            )
        for symbol in symbols:
            if mode == "state_and_current":
                current = await _validate_catalog_support(
                    request,
                    symbol=symbol,
                    service="current",
                )
                if current is not None:
                    return current
                state = await _validate_catalog_support(
                    request,
                    symbol=symbol,
                    service="state",
                )
                if state is not None:
                    return state
            else:
                unsupported = await _validate_catalog_support(
                    request,
                    symbol=symbol,
                    service="current",
                )
                if unsupported is not None:
                    return unsupported
    return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _citation_metadata(
    *,
    methodology_path: str,
    product_path: str,
    timestamp: str | None = None,
    lineage: dict[str, Any] | None = None,
    receipt_url: str | None = None,
) -> dict[str, Any]:
    """Return a stable citation envelope for safe public response metadata."""
    methodology_url = f"{PUBLIC_BASE_URL}/{methodology_path.lstrip('/')}"
    product_url = f"{PUBLIC_BASE_URL}/{product_path.lstrip('/')}"
    citation = {
        "provider": "Blocksize",
        "methodology_url": methodology_url,
        "product_url": product_url,
        "timestamp": timestamp or _utc_now_iso(),
        "lineage": lineage or {},
        "attribution": "Powered by Blocksize",
        "instructions": (
            "Cite Blocksize as provider, link the product and methodology URLs, "
            "and preserve the timestamp and lineage fields."
        ),
    }
    if receipt_url:
        citation["receipt_url"] = receipt_url
    return citation


def _json_hash(payload: Any) -> str:
    stable = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return f"sha256:{fingerprint(stable, salt_env='RECEIPT_HASH_SALT')}"


def _rwa_observation_store(request: Request) -> RWAObservationStore:
    db_path = configured_rwa_observation_db_path()
    credit_manager = getattr(request.app.state, "credits", None)
    runtime_paths = (
        {"credits_runtime": str(credit_manager.db_path)}
        if isinstance(credit_manager, CreditManager)
        else None
    )
    if rwa_database_collisions(
        db_path,
        settings.server.observability_db_path,
        runtime_paths,
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "RWA_DATABASE_NOT_ISOLATED",
                "message": "RWA evidence storage is unavailable until its database is isolated.",
            },
        )
    store = getattr(request.app.state, "rwa_store", None)
    if isinstance(store, RWAObservationStore) and database_paths_collide(
        store.db_path,
        db_path,
    ):
        return store
    store = RWAObservationStore(db_path)
    request.app.state.rwa_store = store
    return store


async def _store_rwa_observation_without_blocking(
    store: RWAObservationStore,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run a bounded SQLite writer off-loop and never abandon an in-flight transaction."""
    task = asyncio.create_task(
        asyncio.to_thread(
            store.store_observation,
            payload,
            lock_timeout_seconds=rwa_store_lock_timeout_seconds(),
            ingestion_source="operator_api",
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


def _response_receipt(
    request: Request,
    *,
    product: str,
    subject: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    source_endpoints: list[str],
) -> dict[str, Any]:
    created_at = _utc_now_iso()
    receipt_basis = {
        "product": product,
        "subject": subject,
        "created_at": created_at,
        "request": request_payload,
        "response": response_payload,
    }
    receipt_id = f"rcpt_{str(fingerprint(json.dumps(receipt_basis, default=str, sort_keys=True), salt_env='RECEIPT_ID_SALT'))[:24]}"
    receipt = {
        "receipt_id": receipt_id,
        "product": product,
        "created_at": created_at,
        "provider": "Blocksize Capital",
        "subject": subject,
        "request_hash": _json_hash(request_payload),
        "response_hash": _json_hash(response_payload),
        "source_endpoints": source_endpoints,
        "lookup_url": f"{PUBLIC_BASE_URL}/v1/provenance/{receipt_id}",
    }
    receipt["citation"] = _citation_metadata(
        methodology_path="signed-oracle-feeds",
        product_path="audit-grade-price-receipt-api",
        timestamp=created_at,
        lineage={"source_endpoints": source_endpoints},
        receipt_url=receipt["lookup_url"],
    )
    request.app.state.credits.store_price_receipt(
        receipt_id=receipt_id,
        product=product,
        subject=subject,
        payload={
            "status": "ok",
            "product": "agent_data_provenance",
            "credit_cost": CREDIT_COSTS["provenance_lookup"],
            "receipt": receipt,
            "request": request_payload,
            "response": response_payload,
            "payment": _credit_meta_for_request(request) or {
                "mode": "x402_or_external",
                "credits_spent": None,
            },
        },
    )
    return receipt


def _service_for_symbol(symbol: str, requested_service: str | None = None) -> str:
    if requested_service:
        service = requested_service.strip().lower()
        if service not in SUPPORTED_BATCH_SERVICES:
            raise ValueError(f"Unsupported service: {requested_service}")
        return service
    clean = _normalise_symbol(symbol)
    if clean in METAL_TICKERS:
        return "metal"
    base = _base_from_symbol(clean)
    quote = _quote_from_symbol(clean)
    if base in FIAT_CURRENCIES and quote in FIAT_CURRENCIES:
        return "fx"
    if _looks_like_equity_bidask_symbol(clean):
        return "bidask"
    return "vwap"


async def _fetch_service_snapshot(
    client: BlocksizeClient,
    *,
    service: str,
    symbol: str,
    stream_cache: BlocksizeStreamCache | None = None,
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol)
    if service == "vwap":
        data = await client.get_vwap_latest(clean)
        return {
            "service": "vwap",
            "symbol": clean,
            "asset_class": "crypto",
            "endpoint": f"/v1/vwap/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": data.vwap,
        }
    if service == "bidask":
        data = await client.get_bidask_snapshot(clean)
        mid = (data.bid + data.ask) / 2 if data.ask else data.bid
        return {
            "service": "bidask",
            "symbol": clean,
            "asset_class": "equity" if _looks_like_equity_bidask_symbol(clean) else "multi_asset",
            "endpoint": f"/v1/bidask/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": mid,
            "spread_bps": (data.spread / mid * 10000) if mid else None,
        }
    if service == "fx":
        data = await client.get_fx_rate(clean)
        value = data.mid or data.bid or data.ask or 0
        return {
            "service": "fx",
            "symbol": clean,
            "asset_class": "fx",
            "endpoint": f"/v1/fx/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": value,
        }
    if service == "metal":
        data = await client.get_metal_price(clean)
        return {
            "service": "metal",
            "symbol": clean,
            "asset_class": "metal",
            "endpoint": f"/v1/metal/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": data.price,
        }
    if service == "state":
        cache_error: str | None = None
        try:
            if stream_cache and stream_cache.enabled:
                data = await stream_cache.get_state_price(clean)
            else:
                raise BlocksizeAPIError(-32004, "state stream cache disabled")
        except BlocksizeAPIError as exc:
            cache_error = exc.message
            data = await client.get_state_price(clean)
        source_method = (
            "state_subscribe_cache"
            if data.source.endswith("state_subscribe_cache")
            else "state_instruments+state_pool"
        )
        snapshot = {
            "service": "state",
            "symbol": clean,
            "asset_class": "crypto_state",
            "endpoint": f"/v1/state/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": data.price,
            "source_method": source_method,
            "source_type": "blocksize_state_reference",
        }
        if cache_error:
            snapshot["cache_note"] = cache_error
        return snapshot
    raise ValueError(f"Unsupported service: {service}")


def _freshness_ms(timestamp_value: str | None) -> int | None:
    if not timestamp_value:
        return None
    try:
        ts = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0, int((datetime.now(UTC) - ts).total_seconds() * 1000))
    except ValueError:
        return None


def _quality_flags(snapshot: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    freshness = _freshness_ms(str(snapshot.get("timestamp") or ""))
    if freshness is not None and freshness > 60_000:
        flags.append("stale_over_60s")
    spread_bps = snapshot.get("spread_bps")
    if isinstance(spread_bps, (int, float)) and spread_bps > 50:
        flags.append("wide_spread")
    if not snapshot.get("value"):
        flags.append("missing_value")
    return flags


def _bps_delta(value: float | None, reference: float | None) -> float | None:
    if not value or not reference:
        return None
    return (value - reference) / reference * 10000


def _abs_bps_delta(value: float | None, reference: float | None) -> float | None:
    delta = _bps_delta(value, reference)
    return abs(delta) if delta is not None else None


def _base_ticker_from_symbol(symbol: str) -> str:
    return _base_from_symbol(_normalise_symbol(symbol, "symbol"))


def _indicator_signal_from_score(score: int) -> str:
    if score >= 85:
        return "strong_quality"
    if score >= 70:
        return "usable"
    if score >= 50:
        return "caution"
    return "avoid_or_verify"


def _coverage_status(components: dict[str, dict[str, Any]]) -> str:
    ok_count = sum(1 for item in components.values() if item.get("status") == "ok")
    if ok_count == len(components):
        return "full"
    if ok_count:
        return "partial"
    return "none"


def _has_current_market_component(components: dict[str, dict[str, Any]]) -> bool:
    return any(
        components.get(name, {}).get("status") == "ok"
        and components.get(name, {}).get("value") is not None
        for name in ("vwap", "bidask")
    )


def _component_source_endpoints(
    symbol: str,
    components: dict[str, dict[str, Any]],
) -> list[str]:
    endpoints: list[str] = []
    for name, item in components.items():
        if item.get("status") != "ok":
            continue
        if endpoint := item.get("endpoint"):
            endpoints.append(str(endpoint))
            continue
        if name == "state":
            endpoints.append(f"state_pool:{symbol}")
        elif name == "state_coverage":
            endpoints.append("state_instruments")
        elif name == "vwap_30m":
            endpoints.append(f"closingprice_list:{_base_ticker_from_symbol(symbol)}")
        elif name == "vwap_24h":
            endpoints.append(f"fixedvwap_subscribe_cache:{symbol}")
    return endpoints


def _state_coverage_from_instruments(
    *,
    symbol: str,
    instruments: list[Any],
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol, "symbol")
    base = _base_ticker_from_symbol(clean)
    target_symbols = {clean, f"{base}USD", f"{base}USDC", f"{base}USDT"}
    matches: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        item_symbol = str(item.get("symbol") or "").upper()
        if item_symbol in target_symbols:
            pools = item.get("pools") if isinstance(item.get("pools"), list) else []
            networks = sorted({
                str(pool.get("network")).lower()
                for pool in pools
                if isinstance(pool, dict) and pool.get("network")
            })
            matches.append(
                {
                    "symbol": item_symbol,
                    "pool_count": len(pools),
                    "networks": networks,
                    "solana_pool_count": sum(
                        1
                        for pool in pools
                        if isinstance(pool, dict)
                        and str(pool.get("network", "")).lower() == "solana"
                    ),
                    "pools": pools[:10],
                    "truncated": len(pools) > 10,
                }
            )
    return {
        "matched_count": len(matches),
        "matches": matches[:10],
        "truncated": len(matches) > 10,
        "pool_count": sum(item["pool_count"] for item in matches),
        "solana_pool_count": sum(item["solana_pool_count"] for item in matches),
        "networks": sorted({network for item in matches for network in item["networks"]}),
    }


async def _fetch_state_instrument_coverage(
    client: BlocksizeClient,
    *,
    symbol: str,
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol, "symbol")
    try:
        instruments = await client.list_state_instruments()
    except BlocksizeAPIError as exc:
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "BLOCKSIZE_ERROR",
            "message": str(exc),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "FEED_UNAVAILABLE",
            "message": str(exc),
        }
    if not isinstance(instruments, list):
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "UNEXPECTED_STATE_INSTRUMENTS",
            "message": "state_instruments did not return a list",
        }

    coverage = _state_coverage_from_instruments(symbol=clean, instruments=instruments)
    return {
        "status": "ok",
        "endpoint": "state_instruments",
        "instrument_count": len(instruments),
        **coverage,
    }


async def _fetch_indicator_components(
    client: BlocksizeClient,
    *,
    symbol: str,
    stream_cache: BlocksizeStreamCache | None = None,
    include_state_price: bool = False,
    include_windows: bool = False,
    include_state_coverage: bool = False,
) -> dict[str, dict[str, Any]]:
    import asyncio

    clean = _normalise_symbol(symbol, "symbol")
    base = _base_ticker_from_symbol(clean)
    tasks: dict[str, Any] = {
        "vwap": client.get_vwap_latest(clean),
        "bidask": client.get_bidask_snapshot(clean),
    }
    if include_state_price:
        tasks["state"] = (
            stream_cache.get_state_price(clean)
            if stream_cache and stream_cache.enabled
            else client.get_state_price(clean)
        )
    if include_windows:
        tasks["vwap_30m"] = client.get_vwap_30min(base)
        tasks["vwap_24h"] = (
            stream_cache.get_vwap_24h(clean)
            if stream_cache and stream_cache.enabled
            else client.get_vwap_24hr(clean)
        )
    if include_state_coverage:
        tasks["state_coverage"] = _fetch_state_instrument_coverage(client, symbol=clean)

    async def collect(name: str, coro: Any) -> tuple[str, dict[str, Any]]:
        try:
            data = await coro
        except BlocksizeAPIError as exc:
            return (
                name,
                {
                    "status": "unavailable",
                    "error_code": "BLOCKSIZE_ERROR",
                    "message": str(exc),
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            return (
                name,
                {
                    "status": "unavailable",
                    "error_code": "FEED_UNAVAILABLE",
                    "message": str(exc),
                },
            )

        if name == "vwap":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": f"/v1/vwap/{clean}",
                    "value": data.vwap,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "bidask":
            mid = (data.bid + data.ask) / 2 if data.ask else data.bid
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": f"/v1/bidask/{clean}",
                    "value": mid,
                    "bid": data.bid,
                    "ask": data.ask,
                    "spread": data.spread,
                    "spread_bps": (data.spread / mid * 10000) if mid else None,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "state":
            endpoint = (
                "state_subscribe_cache"
                if data.source.endswith("state_subscribe_cache")
                else "state_pool"
            )
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": endpoint,
                    "value": data.price,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "vwap_30m":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": "closingprice_list",
                    "value": data.vwap,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "vwap_24h":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": "fixedvwap_subscribe_cache"
                    if data.source.endswith("fixedvwap_subscribe_cache")
                    else "vwap_24h_latest",
                    "value": data.vwap,
                    "volume": data.volume,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "state_coverage" and isinstance(data, dict):
            return name, data
        return name, {"status": "unavailable", "error_code": "UNKNOWN_COMPONENT"}

    results = await asyncio.gather(
        *(collect(name, coro) for name, coro in tasks.items())
    )
    return dict(results)


def _build_token_quality_indicator(
    *,
    symbol: str,
    components: dict[str, dict[str, Any]],
    max_spread_bps: float = 50,
    max_state_divergence_bps: float = 75,
) -> dict[str, Any]:
    vwap = components.get("vwap", {})
    bidask = components.get("bidask", {})
    state = components.get("state", {})
    state_coverage = components.get("state_coverage", {})
    vwap_30m = components.get("vwap_30m", {})
    vwap_24h = components.get("vwap_24h", {})

    current_value = (
        vwap.get("value")
        if vwap.get("status") == "ok"
        else bidask.get("value") if bidask.get("status") == "ok" else None
    )
    state_value = state.get("value") if state.get("status") == "ok" else None
    vwap_30m_value = vwap_30m.get("value") if vwap_30m.get("status") == "ok" else None
    vwap_24h_value = vwap_24h.get("value") if vwap_24h.get("status") == "ok" else None
    spread_bps = bidask.get("spread_bps") if bidask.get("status") == "ok" else None
    state_divergence_bps = _abs_bps_delta(current_value, state_value)
    vwap_30m_drift_bps = _bps_delta(current_value, vwap_30m_value)
    vwap_24h_drift_bps = _bps_delta(current_value, vwap_24h_value)

    score = 100
    flags: list[str] = []
    if current_value is None:
        score -= 45
        flags.append("missing_current_market_price")
    if spread_bps is None:
        score -= 12
        flags.append("missing_bidask_spread")
    elif spread_bps > max_spread_bps:
        score -= min(30, int((spread_bps - max_spread_bps) / max_spread_bps * 30))
        flags.append("wide_spread")
    if "state" in components and state_divergence_bps is None:
        score -= 10
        flags.append("state_price_unavailable")
    elif state_divergence_bps is not None and state_divergence_bps > max_state_divergence_bps:
        score -= min(
            30,
            int(
                (state_divergence_bps - max_state_divergence_bps)
                / max_state_divergence_bps
                * 30
            ),
        )
        flags.append("state_price_divergence")
    if "state_coverage" in components:
        if state_coverage.get("status") != "ok":
            score -= 5
            flags.append("state_instrument_coverage_unavailable")
        elif not state_coverage.get("matched_count"):
            score -= 5
            flags.append("state_instrument_not_listed")
    if "vwap_30m" in components and vwap_30m_value is None:
        score -= 5
        flags.append("vwap_30m_unavailable")
    if "vwap_24h" in components and vwap_24h_value is None:
        score -= 5
        flags.append("vwap_24h_unavailable")

    freshness_values = [
        _freshness_ms(str(item.get("timestamp") or ""))
        for item in components.values()
        if item.get("status") == "ok"
    ]
    stale_count = sum(1 for item in freshness_values if item is not None and item > 60_000)
    if stale_count:
        score -= min(25, stale_count * 8)
        flags.append("stale_component")

    clean_score = max(0, min(100, score))
    return {
        "symbol": symbol,
        "network_hint": "solana" if _base_ticker_from_symbol(symbol) in {"SOL", "JUP", "PYTH", "JTO", "RAY", "ORCA", "BONK", "WIF"} else None,
        "score": clean_score,
        "signal": _indicator_signal_from_score(clean_score),
        "metrics": {
            "current_value": current_value,
            "spread_bps": spread_bps,
            "state_price": state_value,
            "state_divergence_bps": state_divergence_bps,
            "vwap_30m": vwap_30m_value,
            "vwap_30m_drift_bps": vwap_30m_drift_bps,
            "vwap_24h": vwap_24h_value,
            "vwap_24h_drift_bps": vwap_24h_drift_bps,
            "state_instrument_matched_count": (
                state_coverage.get("matched_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_pool_count": (
                state_coverage.get("pool_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_solana_pool_count": (
                state_coverage.get("solana_pool_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_networks": (
                state_coverage.get("networks")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "freshness_ms_max": max(
                (item for item in freshness_values if item is not None),
                default=None,
            ),
        },
        "coverage": {
            "status": _coverage_status(components),
            "feeds": {
                name: item.get("status", "unknown")
                for name, item in components.items()
            },
        },
        "flags": sorted(set(flags)),
    }


def _decode_payment_payload(payment_payload: str) -> dict[str, Any]:
    """Decode and lightly validate the x402 proof header payload."""
    if len(payment_payload) > 4096:
        raise ValueError("Payment payload is too large")
    try:
        decoded = base64.b64decode(payment_payload, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Payment payload must be base64-encoded JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Payment payload must be a JSON object")
    return payload


def _network_kind(network: str) -> str | None:
    network_lower = network.strip().lower()
    if network_lower.startswith("solana:"):
        return "solana"
    if network_lower.startswith("eip155:"):
        return "evm"
    return None


def _select_requirement(network: str, payment_requirements: list[dict]) -> dict[str, Any] | None:
    """Find the payment requirement matching the proof network."""
    clean_network = network.strip()
    for requirement in payment_requirements:
        req_network = str(requirement.get("network", "")).strip()
        if clean_network and clean_network == req_network:
            return requirement
    return None


def _requirement_amount_atomic(requirement: dict[str, Any]) -> int:
    """Read the required amount as USDC atomic units."""
    raw = requirement.get("maxAmountRequired")
    if raw is None and "amount" in requirement:
        return int(str(requirement["amount"]))
    return int(Decimal(str(raw or "0")))


def _requirement_recipient(requirement: dict[str, Any]) -> str:
    """Read the configured recipient from an x402 requirement."""
    return str(
        requirement.get("payTo")
        or requirement.get("recipient")
        or requirement.get("resource")
        or ""
    )


def _requirement_asset(requirement: dict[str, Any], fallback: str) -> str:
    """Read the token mint/contract address from an x402 requirement."""
    asset = str(requirement.get("asset") or "")
    if "/" in asset:
        return asset.rsplit("/", 1)[-1]
    return fallback


def _token_amount_atomic(balance: dict[str, Any]) -> int:
    amount = balance.get("uiTokenAmount", {}).get("amount")
    return int(amount or 0)


def _solana_transfer_satisfies_requirement(
    transaction: dict[str, Any],
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Validate a Solana USDC payment by recipient owner, mint, and net amount."""
    meta = transaction.get("meta") or {}
    expected_recipient = _requirement_recipient(requirement)
    expected_mint = _requirement_asset(requirement, settings.x402.solana_usdc_address)
    required_amount = _requirement_amount_atomic(requirement)

    if not expected_recipient:
        return False, "Solana payment recipient is not configured"
    if required_amount <= 0:
        return False, "Solana payment amount is not configured"

    pre_balances = {
        (item.get("accountIndex"), item.get("mint")): _token_amount_atomic(item)
        for item in meta.get("preTokenBalances", [])
    }

    for post in meta.get("postTokenBalances", []):
        if post.get("mint") != expected_mint:
            continue
        if post.get("owner") != expected_recipient:
            continue
        if int(post.get("uiTokenAmount", {}).get("decimals", -1)) != 6:
            continue
        key = (post.get("accountIndex"), post.get("mint"))
        delta = _token_amount_atomic(post) - pre_balances.get(key, 0)
        if delta >= required_amount:
            return True, "ok"

    return (
        False,
        "No Solana USDC transfer matched the configured recipient and required amount",
    )


def _evm_transfer_satisfies_requirement(
    receipt: dict[str, Any],
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Validate an EVM USDC Transfer log by contract, recipient, and amount."""
    expected_recipient = _requirement_recipient(requirement).lower()
    expected_token = _requirement_asset(requirement, settings.x402.base_usdc_address).lower()
    required_amount = _requirement_amount_atomic(requirement)

    if not expected_recipient.startswith("0x") or len(expected_recipient) != 42:
        return False, "EVM payment recipient is not configured"
    if not re.fullmatch(r"0x[0-9a-f]{40}", expected_recipient):
        return False, "EVM payment recipient is invalid"
    if not re.fullmatch(r"0x[0-9a-f]{40}", expected_token):
        return False, "EVM payment asset is invalid"
    if required_amount <= 0:
        return False, "EVM payment amount is not configured"

    recipient_topic = "0x" + expected_recipient.removeprefix("0x").rjust(64, "0")
    for log_item in receipt.get("logs", []):
        if str(log_item.get("address", "")).lower() != expected_token:
            continue
        topics = [str(topic).lower() for topic in log_item.get("topics", [])]
        if len(topics) < 3 or topics[0] != EVM_TRANSFER_TOPIC:
            continue
        if topics[2] != recipient_topic:
            continue
        try:
            amount = int(str(log_item.get("data", "0x0")), 16)
        except ValueError:
            continue
        if amount >= required_amount:
            return True, "ok"

    return False, "No Base USDC Transfer matched the configured recipient and required amount"


def _transaction_is_recent(block_time: int | None) -> bool:
    max_age = settings.server.x402_payment_max_age_seconds
    future_skew = settings.server.x402_payment_future_skew_seconds
    if max_age <= 0 or future_skew < 0 or block_time is None:
        return False
    try:
        timestamp = int(block_time)
    except (TypeError, ValueError):
        return False
    now = time.time()
    return now - max_age <= timestamp <= now + future_skew


async def _rpc_result(
    client: httpx.AsyncClient,
    rpc_url: str,
    method: str,
    params: list[Any],
) -> Any:
    """Call one JSON-RPC method and fail closed on protocol-level errors."""
    response = await client.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise ValueError(f"RPC method {method} failed")
    if "result" not in payload:
        raise ValueError(f"RPC method {method} returned no result")
    return payload["result"]


def _hex_quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ValueError(f"Invalid EVM {field}")
    return int(value, 16)


async def _verify_legacy_solana_payment(
    tx_hash: str,
    network: str,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Verify a legacy Solana transaction against exact mainnet chain state."""
    if not SOLANA_SIGNATURE_RE.fullmatch(tx_hash):
        return False, "Malformed Solana transaction signature"
    expected_genesis = network.partition(":")[2]
    if not expected_genesis:
        return False, "Malformed Solana network identifier"
    rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    async with httpx.AsyncClient(timeout=10.0) as client:
        genesis_hash = await _rpc_result(client, rpc_url, "getGenesisHash", [])
        if genesis_hash != expected_genesis:
            return False, "Solana RPC genesis does not match requested network"
        result = await _rpc_result(
            client,
            rpc_url,
            "getTransaction",
            [
                tx_hash,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "finalized",
                },
            ],
        )
    if not isinstance(result, dict):
        return False, "Transaction not found on chain or not finalized"
    signatures = (result.get("transaction") or {}).get("signatures") or []
    if tx_hash not in signatures:
        return False, "Solana RPC transaction signature mismatch"
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return False, "Solana transaction failed on chain"
    if not _transaction_is_recent(result.get("blockTime")):
        return False, "Solana transaction timestamp is missing, stale, or in the future"
    return _solana_transfer_satisfies_requirement(result, requirement)


async def _verify_legacy_evm_payment(
    tx_hash: str,
    network: str,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Verify a legacy EVM transfer with chain identity, time, and finality checks."""
    if not EVM_TX_HASH_RE.fullmatch(tx_hash):
        return False, "Malformed EVM transaction hash"
    try:
        expected_chain_id = int(network.partition(":")[2])
    except ValueError:
        return False, "Malformed EVM network identifier"
    rpc_url = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    token = _requirement_asset(requirement, settings.x402.base_usdc_address)
    async with httpx.AsyncClient(timeout=10.0) as client:
        chain_id = _hex_quantity(
            await _rpc_result(client, rpc_url, "eth_chainId", []),
            "chain id",
        )
        if chain_id != expected_chain_id:
            return False, "EVM RPC chain does not match requested network"
        receipt = await _rpc_result(
            client,
            rpc_url,
            "eth_getTransactionReceipt",
            [tx_hash],
        )
        if not isinstance(receipt, dict):
            return False, "EVM transaction not found or not finalized"
        returned_hash = str(receipt.get("transactionHash") or "")
        if returned_hash.lower() != tx_hash.lower():
            return False, "EVM RPC transaction hash mismatch"
        if receipt.get("status") not in ("0x1", 1):
            return False, "EVM transaction reverted on chain"
        block_hash = str(receipt.get("blockHash") or "")
        if not EVM_TX_HASH_RE.fullmatch(block_hash):
            return False, "EVM receipt is missing a canonical block hash"
        receipt_block_number = _hex_quantity(receipt.get("blockNumber"), "block number")
        latest_block_number = _hex_quantity(
            await _rpc_result(client, rpc_url, "eth_blockNumber", []),
            "latest block number",
        )
        confirmations = latest_block_number - receipt_block_number + 1
        if confirmations < settings.server.x402_payment_min_confirmations:
            return False, "EVM transaction has insufficient confirmations"
        block = await _rpc_result(
            client,
            rpc_url,
            "eth_getBlockByHash",
            [block_hash, False],
        )
        if not isinstance(block, dict):
            return False, "EVM canonical block is unavailable"
        if str(block.get("hash") or "").lower() != block_hash.lower():
            return False, "EVM canonical block hash mismatch"
        if _hex_quantity(block.get("number"), "canonical block number") != receipt_block_number:
            return False, "EVM canonical block number mismatch"
        block_time = _hex_quantity(block.get("timestamp"), "block timestamp")
        if not _transaction_is_recent(block_time):
            return False, "EVM transaction timestamp is missing, stale, or in the future"
        decimals_value = await _rpc_result(
            client,
            rpc_url,
            "eth_call",
            [{"to": token, "data": "0x313ce567"}, "latest"],
        )
        if _hex_quantity(decimals_value, "USDC decimals") != 6:
            return False, "EVM payment asset does not use six decimals"
    return _evm_transfer_satisfies_requirement(receipt, requirement)


def _reserve_payment_use(
    payment_id: str,
    network: str,
    requirement: dict[str, Any],
    credit_manager: CreditManager | None,
    purpose: str,
    request_binding: str,
    attempt_id: str,
    *,
    existing_only: bool = False,
) -> dict[str, Any]:
    """Acquire a durable proof lease before invoking a paid handler."""
    if credit_manager is None:
        return {"valid": False, "reason": "Payment ledger is unavailable"}
    amount_atomic = _requirement_amount_atomic(requirement)
    recipient = _requirement_recipient(requirement)
    reservation = credit_manager.reserve_payment_proof(
        payment_id=payment_id,
        network=network,
        amount_atomic=amount_atomic,
        recipient=recipient,
        purpose=purpose,
        request_binding=request_binding,
        attempt_id=attempt_id,
        lease_seconds=settings.server.x402_payment_verification_lease_seconds,
        existing_only=existing_only,
    )
    if not reservation.acquired:
        if reservation.reason == "payment_already_finalized":
            replay = credit_manager.finalized_payment_response(
                payment_id=reservation.payment_id,
                request_binding=request_binding,
                max_age_seconds=settings.server.x402_payment_replay_ttl_seconds,
            )
            if replay is not None:
                return {
                    "valid": True,
                    "replay": True,
                    "payment_id": reservation.payment_id,
                    "reservation_id": None,
                    "request_binding": request_binding,
                    "attempt_id": attempt_id,
                    "cached_response": replay,
                }
        return {"valid": False, "reason": reservation.reason}
    return {
        "valid": True,
        "payment_id": reservation.payment_id,
        "reservation_id": reservation.reservation_id,
        "request_binding": request_binding,
        "attempt_id": attempt_id,
    }


async def _verify_payment(
    payment_payload: str,
    payment_requirements: list[dict],
    credit_manager: CreditManager | None = None,
    purpose: str = "data",
    request_method: str = "GET",
    resource_url: str | None = None,
    request_body: bytes = b"",
    attempt_id: str | None = None,
    replay_only: bool = False,
) -> dict:
    """Verify an official x402 v2 signature and reserve it for delivery.

    Legacy public-transaction proofs are available only behind the explicit
    local/test migration flag and still receive strict native chain checks.
    """
    effective_attempt_id = attempt_id or secrets.token_hex(16)
    official_error = "Payment payload is not a valid bound x402 v2 signature"
    if resource_url:
        accepts = _x402_v2_accepts(payment_requirements, resource_url)
        for requirement in accepts:
            try:
                parsed = parse_payment_signature(
                    payment_payload,
                    accepted_requirement=requirement,
                    method=request_method,
                    resource_url=resource_url,
                    body=request_body,
                )
            except PaymentSecurityError:
                continue
            if credit_manager is not None:
                replay = credit_manager.finalized_payment_response(
                    payment_id=parsed.payment_id,
                    request_binding=parsed.request_binding,
                    max_age_seconds=settings.server.x402_payment_replay_ttl_seconds,
                )
                if replay is not None:
                    return {
                        "valid": True,
                        "replay": True,
                        "mode": "replay",
                        "network": str(requirement["network"]),
                        "payment_id": parsed.payment_id,
                        "reservation_id": None,
                        "request_binding": parsed.request_binding,
                        "attempt_id": effective_attempt_id,
                        "cached_response": replay,
                    }
            if replay_only:
                return {"valid": False, "reason": "economic_writes_locked"}
            try:
                facilitator = FacilitatorAdapter(
                    settings.x402.facilitator_url,
                    bearer_token=settings.x402.facilitator_bearer_token or None,
                    cdp_api_key_id=settings.x402.cdp_api_key_id or None,
                    cdp_api_key_secret=settings.x402.cdp_api_key_secret or None,
                    production=security_configuration_status()["production"],
                )
            except PaymentSecurityError:
                return {"valid": False, "reason": "Payment facilitator is not configured safely"}
            existing = _reserve_payment_use(
                parsed.payment_id,
                str(requirement["network"]),
                requirement,
                credit_manager,
                purpose,
                parsed.request_binding,
                effective_attempt_id,
                existing_only=True,
            )
            if existing.get("valid") is True:
                return {
                    **existing,
                    "valid": True,
                    "mode": "facilitator",
                    "network": str(requirement["network"]),
                    "_parsed_payment": parsed,
                    "_requirement": requirement,
                    "_facilitator": facilitator,
                    "verification_reused": True,
                }
            if existing.get("reason") != "payment_reservation_missing":
                return existing
            verification = await facilitator.verify(parsed, requirement)
            if verification.get("isValid") is not True:
                reason = str(verification.get("invalidReason") or "payment_invalid")
                return {"valid": False, "reason": reason}
            network = str(requirement["network"])
            reserved = _reserve_payment_use(
                parsed.payment_id,
                network,
                requirement,
                credit_manager,
                purpose,
                parsed.request_binding,
                effective_attempt_id,
            )
            if not reserved["valid"]:
                return reserved
            return {
                **reserved,
                "valid": True,
                "mode": "facilitator",
                "network": network,
                "payer": verification.get("payer"),
                "_parsed_payment": parsed,
                "_requirement": requirement,
                "_facilitator": facilitator,
            }

    if replay_only:
        return {"valid": False, "reason": "economic_writes_locked"}

    allow_mock = settings.server.x402_allow_mock_payments
    allow_legacy = settings.server.x402_allow_legacy_payments
    if not allow_legacy and not allow_mock:
        return {"valid": False, "reason": official_error}

    try:
        payload = _decode_payment_payload(payment_payload)
        tx_hash = str(payload.get("proof") or payload.get("tx_hash") or "").strip()
        network = str(payload.get("network") or "").strip()
        if not tx_hash:
            return {"valid": False, "reason": "Missing tx_hash/proof in payload"}
        if not network:
            return {"valid": False, "reason": "Missing exact CAIP-2 network in payload"}

        requirement = _select_requirement(network, payment_requirements)
        if requirement is None:
            return {"valid": False, "reason": f"No payment requirement configured for {network}"}

        request_binding = hashlib.sha256(
            (resource_url or purpose).encode("utf-8") + b"\0" + request_body
        ).hexdigest()
        if allow_mock and tx_hash.startswith(("mock_", "test_")):
            reserved = _reserve_payment_use(
                tx_hash,
                network,
                requirement,
                credit_manager,
                purpose,
                request_binding,
                effective_attempt_id,
            )
            if not reserved["valid"]:
                return reserved
            logger.warning("Accepted mock x402 proof for local/demo mode only: %s", tx_hash)
            return {
                **reserved,
                "valid": True,
                "mode": "mock",
                "mock": True,
                "network": network,
                "transaction": reserved["payment_id"],
                "_requirement": requirement,
            }

        if not allow_legacy:
            return {"valid": False, "reason": "Legacy transaction proofs are disabled"}
        kind = _network_kind(network)
        if kind == "solana":
            matched, reason = await _verify_legacy_solana_payment(
                tx_hash,
                network,
                requirement,
            )
        elif kind == "evm":
            matched, reason = await _verify_legacy_evm_payment(
                tx_hash,
                network,
                requirement,
            )
        else:
            return {"valid": False, "reason": f"Unsupported network: {network}"}
        if not matched:
            return {"valid": False, "reason": reason}
        reserved = _reserve_payment_use(
            tx_hash,
            network,
            requirement,
            credit_manager,
            purpose,
            request_binding,
            effective_attempt_id,
        )
        if not reserved["valid"]:
            return reserved
        logger.info("Natively verified legacy payment on %s", network)
        return {
            **reserved,
            "valid": True,
            "mode": "legacy",
            "network": network,
            "transaction": reserved["payment_id"],
            "_requirement": requirement,
        }
    except ValueError as e:
        return {"valid": False, "reason": str(e)}
    except Exception:
        logger.error("Native RPC verification failed")
        return {"valid": False, "reason": "Native RPC verification unavailable"}


async def _settle_payment(
    payment_payload: str,
    payment_requirements: list[dict],
    verification: dict[str, Any] | None = None,
) -> dict:
    """Settle an official authorization or attest an already-final native proof."""
    context = verification or {}
    if context.get("mode") == "facilitator":
        facilitator = context.get("_facilitator")
        parsed = context.get("_parsed_payment")
        requirement = context.get("_requirement")
        if (
            not isinstance(facilitator, FacilitatorAdapter)
            or not isinstance(parsed, ParsedPayment)
            or not isinstance(requirement, dict)
        ):
            return {"success": False, "errorReason": "missing_settlement_context"}
        return await facilitator.settle(parsed, requirement)
    if context.get("mode") in {"legacy", "mock"}:
        requirement = context.get("_requirement")
        if not isinstance(requirement, dict):
            requirement = _select_requirement(
                str(context.get("network") or ""),
                payment_requirements,
            )
        if not isinstance(requirement, dict):
            return {"success": False, "errorReason": "missing_settlement_context"}
        return {
            "success": True,
            "transaction": str(context.get("transaction") or context.get("payment_id") or ""),
            "network": str(context.get("network") or ""),
            "amount": str(_requirement_amount_atomic(requirement)),
        }
    return {"success": False, "errorReason": "missing_settlement_context"}


async def _buffer_payment_response(response: Response) -> tuple[Response, bytes] | None:
    """Materialize a successful paid response for bounded durable replay."""
    body = bytearray()
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is not None:
        async for chunk in body_iterator:
            encoded = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
            body.extend(encoded)
            if len(body) > MAX_CACHED_PAYMENT_RESPONSE_BYTES:
                return None
    else:
        raw_body = getattr(response, "body", b"")
        body.extend(raw_body.encode() if isinstance(raw_body, str) else bytes(raw_body))
        if len(body) > MAX_CACHED_PAYMENT_RESPONSE_BYTES:
            return None

    buffered = Response(
        content=bytes(body),
        status_code=response.status_code,
        headers=dict(response.headers),
        background=response.background,
    )
    return buffered, bytes(body)


def _cached_payment_response(cached: dict[str, Any]) -> Response:
    """Reconstruct a previously finalized response without recharging or settling."""
    response = Response(
        content=bytes(cached["body"]),
        status_code=int(cached["status_code"]),
        headers={str(key): str(value) for key, value in cached.get("headers", {}).items()},
    )
    settlement = cached.get("settlement")
    if isinstance(settlement, dict) and settlement:
        settlement_b64 = base64.b64encode(
            json.dumps(settlement, sort_keys=True).encode()
        ).decode()
        response.headers["PAYMENT-RESPONSE"] = settlement_b64
        response.headers["X-PAYMENT-RESPONSE"] = settlement_b64
    response.headers["X-Payment-Replayed"] = "true"
    response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _paid_request_preflight_response(request: Request) -> Response | None:
    """Reject known-invalid paid mutations before credits or payment handling."""
    if (
        request.method.upper() != "POST"
        or request.url.path != "/v1/rwa/benchmark/blocksize"
    ):
        return None
    body = await request.body()
    if len(body) > 1_048_576:
        return JSONResponse(
            status_code=413,
            content={"error": "Payload Too Large", "message": "Paid request body exceeds 1 MiB."},
        )
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("persist"):
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "error_code": "RWA_PUBLIC_PERSISTENCE_FORBIDDEN",
                    "message": "The public benchmark endpoint is stateless.",
                }
            },
        )
    return None


@app.middleware("http")
async def x402_payment_middleware(request: Request, call_next):
    """
    x402 payment gate middleware with tiered pricing.

    Flow:
    1. Check if the route is priced (None = free, pass through)
    2. Check for PAYMENT-SIGNATURE header
    3. If missing → 402 with payment requirements for BOTH networks
    4. If present → verify → proceed or reject
    5. After response → settle payment
    """
    if _anthropic_only_mode() and not _anthropic_only_allowed_path(request.url.path):
        return _anthropic_only_block_response()

    discovery_limit = _discovery_rate_limit_response(request)
    if discovery_limit is not None:
        return discovery_limit

    preflight_response = await _paid_request_preflight_response(request)
    if preflight_response is not None:
        return _apply_x402_cors_headers(request, preflight_response)

    path = request.url.path
    bridge_locked = economic_writes_locked()
    if bridge_locked and path == "/v1/credits/claim":
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=503,
                headers={"Retry-After": "300", "Cache-Control": "no-store"},
                content={
                    "error": "Economic Writes Locked",
                    "error_code": "ECONOMIC_WRITES_LOCKED",
                    "message": (
                        "Credit and payment writes are temporarily disabled during "
                        "a transaction-continuity maintenance release."
                    ),
                },
            ),
        )
    try:
        price = _get_price_for_request(request)
    except ValueError as e:
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=400,
                content={"error": "Bad Request", "message": str(e)},
            ),
        )

    # Free endpoints pass through
    if price is None:
        return await call_next(request)

    invalid_request = await _validate_paid_request_before_charge(request)
    if invalid_request is not None:
        _record_product_event(
            "paid_request_rejected_preflight",
            request,
            price_usdc=price,
            reason=f"http_{invalid_request.status_code}",
        )
        return _apply_x402_cors_headers(request, invalid_request)

    try:
        credit_subject = None if bridge_locked else _starter_credit_subject(request)
    except ValueError as e:
        _record_product_event(
            "credit_drawdown_failed",
            request,
            price_usdc=price,
            reason="invalid_starter_identity_header",
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=400,
                content={
                    "error": "Bad Request",
                    "message": str(e),
                },
            ),
        )

    if credit_subject is not None:
        subject, subject_type, require_wallet_history = credit_subject
        credit_cost = _credit_cost_for_request(request)
        if credit_cost is None:
            _record_product_event(
                "credit_drawdown_failed",
                request,
                price_usdc=price,
                reason="missing_credit_cost",
            )
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=500,
                    content={
                        "error": "Internal Error",
                        "message": "Credit cost is not configured for this paid route.",
                    },
                ),
            )

        mgr: CreditManager = request.app.state.credits
        client_ip = _request_client_ip(request)
        starter = await mgr.ensure_starter_allowance(
            subject=subject,
            subject_type=subject_type,
            ip=client_ip,
            device_id=request.headers.get("X-DEVICE-ID"),
            session_id=request.headers.get("X-SESSION-ID"),
            user_agent=request.headers.get("user-agent"),
            require_wallet_history=require_wallet_history,
        )

        charge_id = secrets.token_hex(16)
        if starter.eligible and mgr.spend_credits(
            subject,
            credit_cost,
            charge_id=charge_id,
            purpose=f"{request.method} {path}",
        ):
            credits_remaining = mgr.get_balance(subject)
            subject_wallet_hash = _wallet_hash(subject)
            request.state.starter_credit_context = {
                "subject_type": subject_type,
                "credits_spent": credit_cost,
                "credits_remaining": credits_remaining,
                "starter_granted": starter.granted_credits,
                "charge_id": charge_id,
            }
            logger.info(
                "CREDIT DRAWDOWN: Spent %.1f from %s:%s for %s",
                credit_cost,
                subject_type,
                subject,
                path,
            )
            _record_product_event(
                "credit_drawdown_success",
                request,
                price_usdc=price,
                wallet_hash=subject_wallet_hash,
                metadata={
                    "credits_spent": credit_cost,
                    "credits_remaining": credits_remaining,
                    "starter_subject_type": subject_type,
                    "starter_granted": starter.granted_credits,
                    "charge_id": charge_id,
                },
            )
            try:
                response = await call_next(request)
            except asyncio.CancelledError:
                mgr.refund_credits(
                    subject,
                    credit_cost,
                    charge_id=charge_id,
                )
                _record_product_event(
                    "charged_delivery_failed",
                    request,
                    price_usdc=price,
                    reason="request_cancelled",
                    wallet_hash=subject_wallet_hash,
                    metadata={
                        "attempt_id": charge_id,
                        "charge_id": charge_id,
                        "payment_mode": "starter_credit",
                        "refund_status": "refunded",
                    },
                )
                raise
            except Exception:
                mgr.refund_credits(
                    subject,
                    credit_cost,
                    charge_id=charge_id,
                )
                _record_product_event(
                    "charged_delivery_failed",
                    request,
                    price_usdc=price,
                    reason="handler_exception",
                    wallet_hash=subject_wallet_hash,
                    metadata={
                        "attempt_id": charge_id,
                        "charge_id": charge_id,
                        "payment_mode": "starter_credit",
                        "refund_status": "refunded",
                    },
                )
                raise
            refund_metadata: dict[str, Any] = {}
            if response.status_code >= 400:
                refunded = mgr.refund_credits(
                    subject,
                    credit_cost,
                    charge_id=charge_id,
                )
                refund_metadata = {
                    "credits_refunded": credit_cost if refunded else 0.0,
                    "refund_status": "refunded" if refunded else "refund_failed",
                    "credits_remaining_after_refund": mgr.get_balance(subject),
                }
                if refunded:
                    request.state.starter_credit_context.update(
                        {
                            "credits_refunded": credit_cost,
                            "credits_remaining": mgr.get_balance(subject),
                        }
                    )
            first_activation = _record_charged_delivery_outcome(
                request,
                response,
                price_usdc=price,
                payment_mode="starter_credit",
                wallet_hash=subject_wallet_hash,
                metadata={
                    "credits_spent": credit_cost,
                    "credits_remaining": credits_remaining,
                    "starter_subject_type": subject_type,
                    "charge_id": charge_id,
                    **refund_metadata,
                },
            )
            if first_activation:
                response.headers["X-Blocksize-Activation"] = "first-live-price"
            return _apply_credit_response_headers(response, request)
        else:
            logger.warning("INSUFFICIENT CREDITS: %s:%s for %s", subject_type, subject, path)
            _record_product_event(
                "credit_drawdown_failed",
                request,
                price_usdc=price,
                reason="insufficient_credits",
                wallet_hash=_wallet_hash(subject),
                metadata={
                    "credits_required": credit_cost,
                    "credits_remaining": mgr.get_balance(subject),
                    "starter_subject_type": subject_type,
                    "starter_reason": starter.reason,
                },
            )
            # Proceed to normal 402 challenge below

    # Build multi-network payment requirements
    payment_reqs = _facilitator_supported_requirements(
        settings.payment_requirements(price),
        getattr(request.app.state, "facilitator_support", None),
    )

    # Never advertise an empty or structurally unusable payment challenge.
    if not payment_reqs:
        _record_product_event(
            "payment_configuration_unavailable",
            request,
            price_usdc=price,
            reason="no_operational_payment_rail",
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=503,
                content={
                    "error": "Payment Configuration Unavailable",
                    "message": "No operational payment rail is currently configured.",
                },
            ),
        )

    # PAYMENT-SIGNATURE is the x402 v2 header. X-PAYMENT remains a temporary
    # transport alias, but its contents must pass the same signed-v2 parser.
    payment_header = (
        request.headers.get("PAYMENT-SIGNATURE")
        or request.headers.get("X-PAYMENT")
    )

    if not payment_header:
        payment_required = _x402_payment_required(request, payment_reqs)
        requirements_b64 = _encode_payment_required(payment_required)
        network_labels = {
            "solana": "Solana",
            "evm": "Base L2",
        }
        accepted_networks = [
            {
                "name": network_labels.get(
                    _network_kind(str(requirement.get("network") or "")),
                    str(requirement.get("network") or "Unknown"),
                ),
                "caip2": str(requirement.get("network") or ""),
            }
            for requirement in payment_reqs
        ]
        accepted_network_names = ", ".join(
            str(item["name"]) for item in accepted_networks
        )
        _record_product_event(
            "payment_required",
            request,
            price_usdc=price,
            metadata={
                "networks_offered": [
                    str(requirement.get("network"))
                    for requirement in payment_reqs
                    if requirement.get("network")
                ],
            },
        )

        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=402,
                content={
                    **payment_required,
                    "error": "Payment Required",
                    "message": (
                        f"This endpoint requires a payment of ${price} USDC. "
                        f"Send a signed x402 v2 payment in the PAYMENT-SIGNATURE header. "
                        f"Accepted networks: {accepted_network_names}."
                    ),
                    "price_usdc": str(price),
                    "starter_credits": {
                        "positioning": "Up to 50 live data credits are for authenticated connectors only.",
                        "eligibility": "authenticated_connector_only",
                        "available_on_this_surface": False,
                        "allowance_credits": STARTER_CREDIT_ALLOWANCE,
                        "credit_cost": _credit_cost_for_request(request),
                        "unverified_http_identity_enabled": (
                            settings.server.unverified_http_credits_enabled
                        ),
                        "identity_headers": (
                            [
                                "X-AGENT-WALLET",
                                "X-AUTHENTICATED-USER",
                                "X-USER-ID",
                                "X-AGENT-ID",
                                "X-DEVICE-ID",
                                "X-SESSION-ID",
                            ]
                            if settings.server.unverified_http_credits_enabled
                            else []
                        ),
                        "direct_public_http": "This request requires the signed x402 payment shown above.",
                        "upgrade_path": "Contact sales for sustained access through an authenticated account plan.",
                    },
                    "networks": accepted_networks,
                    "legacy_requirements": payment_reqs,
                },
                headers={
                    "PAYMENT-REQUIRED": requirements_b64,
                },
            ),
        )

    request_body = await request.body()
    if len(request_body) > 1_048_576:
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=413,
                content={"error": "Payload Too Large", "message": "Paid request body exceeds 1 MiB."},
            ),
        )
    attempt_id = secrets.token_hex(16)
    resource_url = _public_request_url(request)

    # Verify and reserve the payment before invoking the paid handler.
    _record_product_event(
        "payment_proof_submitted",
        request,
        price_usdc=price,
        metadata={"proof_hash": fingerprint(payment_header), "attempt_id": attempt_id},
    )
    try:
        verification = await _verify_payment(
            payment_header,
            payment_reqs,
            request.app.state.credits,
            purpose=f"{request.method} {path}",
            request_method=request.method,
            resource_url=resource_url,
            request_body=request_body,
            attempt_id=attempt_id,
            replay_only=bridge_locked,
        )
        if not verification.get("valid", False):
            reason = str(verification.get("reason", "unknown"))
            _record_product_event(
                "payment_failed",
                request,
                price_usdc=price,
                reason=reason,
                metadata={"attempt_id": attempt_id},
            )
            if reason == "economic_writes_locked":
                return _apply_x402_cors_headers(
                    request,
                    JSONResponse(
                        status_code=503,
                        headers={
                            "Retry-After": "300",
                            "Cache-Control": "no-store",
                        },
                        content={
                            "error": "Economic Writes Locked",
                            "error_code": "ECONOMIC_WRITES_LOCKED",
                            "message": (
                                "Payment-proof consumption is temporarily disabled "
                                "during a transaction-continuity maintenance release. "
                                "The proof was not verified, reserved, or settled."
                            ),
                        },
                    ),
                )
            unavailable = reason in {
                "facilitator_unavailable",
                "x402_sdk_unavailable",
                "Payment facilitator is not configured safely",
                "Payment ledger is unavailable",
            }
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=502 if unavailable else 402,
                    content={
                        "error": (
                            "Payment Verification Unavailable"
                            if unavailable
                            else "Payment Invalid"
                        ),
                        "message": (
                            "Payment verification is temporarily unavailable."
                            if unavailable
                            else "Payment verification failed."
                        ),
                        "details": reason,
                    },
                ),
            )
    except httpx.HTTPError as e:
        logger.error("Facilitator verification failed: %s", e)
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            reason="verification_unavailable",
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=502,
                content={
                    "error": "Payment Verification Unavailable",
                    "message": "Could not reach payment facilitator.",
                },
            ),
        )

    # Payment verified — serve the request
    network = str(verification.get("network") or "")
    payment_id = str(verification.get("payment_id") or "")
    reservation_id = str(verification.get("reservation_id") or "")
    payer = verification.get("payer")
    if isinstance(payer, str) and payer:
        request.state.trusted_wallet_hash = _wallet_hash(payer)
        request.state.trusted_identity_hash = fingerprint(f"x402:{network}:{payer}")
        request.state.trusted_identity_type = "wallet"
        request.state.trusted_identity_trust = "verified_x402"
    if verification.get("replay") is True:
        cached = verification.get("cached_response")
        if not isinstance(cached, dict):
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=503,
                    content={
                        "error": "Payment Replay Unavailable",
                        "message": "The finalized response could not be restored.",
                    },
                ),
            )
        _record_product_event(
            "payment_response_replayed",
            request,
            price_usdc=price,
            network=network,
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
            },
        )
        return _apply_x402_cors_headers(request, _cached_payment_response(cached))

    _record_product_event(
        "payment_authorization_verified",
        request,
        price_usdc=price,
        network=network,
        metadata={
            "mock": bool(verification.get("mock")),
            "attempt_id": attempt_id,
            "payment_id": payment_id or None,
        },
    )
    try:
        response = await call_next(request)
    except asyncio.CancelledError:
        if payment_id and reservation_id:
            request.app.state.credits.release_payment_proof(
                payment_id=payment_id,
                reservation_id=reservation_id,
            )
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            network=network,
            reason="request_cancelled_before_settlement",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "released",
            },
        )
        raise
    except Exception:
        if payment_id and reservation_id:
            request.app.state.credits.release_payment_proof(
                payment_id=payment_id,
                reservation_id=reservation_id,
            )
        raise

    if response.status_code >= 400:
        if payment_id and reservation_id:
            request.app.state.credits.release_payment_proof(
                payment_id=payment_id,
                reservation_id=reservation_id,
            )
        _record_charged_delivery_outcome(
            request,
            response,
            price_usdc=price,
            payment_mode="x402",
            network=network,
            metadata={
                "mock": bool(verification.get("mock")),
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "released",
            },
        )
        return response

    try:
        buffered_response = await _buffer_payment_response(response)
    except asyncio.CancelledError:
        if payment_id and reservation_id:
            request.app.state.credits.release_payment_proof(
                payment_id=payment_id,
                reservation_id=reservation_id,
            )
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            network=network,
            reason="request_cancelled_before_settlement",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "released",
            },
        )
        raise
    if buffered_response is None:
        if payment_id and reservation_id:
            request.app.state.credits.release_payment_proof(
                payment_id=payment_id,
                reservation_id=reservation_id,
            )
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            network=network,
            reason="response_replay_cache_limit_exceeded",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "released",
            },
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=502,
                content={
                    "error": "Paid Response Unavailable",
                    "message": "The response exceeded the protected delivery limit; no data was delivered.",
                },
            ),
        )
    response, response_body = buffered_response

    try:
        settlement = await _settle_payment(payment_header, payment_reqs, verification)
    except asyncio.CancelledError:
        try:
            quarantined = bool(
                payment_id
                and reservation_id
                and request.app.state.credits.mark_payment_settlement_unknown(
                    payment_id=payment_id,
                    reservation_id=reservation_id,
                )
            )
        except Exception:
            quarantined = False
        _record_product_event(
            "payment_settlement_unreconciled",
            request,
            price_usdc=price,
            network=network,
            reason="settlement_cancelled_with_unknown_remote_outcome",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": (
                    "settlement_unknown"
                    if quarantined
                    else "settlement_unknown_unpersisted"
                ),
            },
        )
        raise
    except Exception:
        logger.error("Payment settlement call failed")
        settlement = {
            "success": False,
            "errorReason": "facilitator_unavailable",
            "outcomeUnknown": True,
        }
    if settlement.get("success") is not True:
        reason = str(settlement.get("errorReason") or "settlement_failed")
        if settlement.get("outcomeUnknown") is True:
            try:
                quarantined = bool(
                    payment_id
                    and reservation_id
                    and request.app.state.credits.mark_payment_settlement_unknown(
                        payment_id=payment_id,
                        reservation_id=reservation_id,
                    )
                )
            except Exception:
                quarantined = False
            _record_product_event(
                "payment_settlement_unreconciled",
                request,
                price_usdc=price,
                network=network,
                reason=reason,
                metadata={
                    "attempt_id": attempt_id,
                    "payment_id": payment_id or None,
                    "payment_state": (
                        "settlement_unknown"
                        if quarantined
                        else "settlement_unknown_unpersisted"
                    ),
                },
            )
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=503,
                    content={
                        "error": "Payment Settlement Outcome Unknown",
                        "message": "The remote settlement outcome is unknown; this proof is quarantined from automatic retry pending reconciliation.",
                    },
                ),
            )
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            network=network,
            reason=reason,
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "pending",
            },
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=502,
                content={
                    "error": "Payment Settlement Unavailable",
                    "message": "The payment could not be finalized; no data was delivered.",
                    "details": reason,
                },
            ),
        )

    settlement_tx_hash = fingerprint(str(settlement.get("transaction") or ""))
    try:
        checkpointed = bool(
            payment_id
            and reservation_id
            and request.app.state.credits.checkpoint_settled_payment(
                payment_id=payment_id,
                reservation_id=reservation_id,
                settlement=settlement,
                response_status=response.status_code,
                response_headers=dict(response.headers),
                response_body=response_body,
                replay_ttl_seconds=settings.server.x402_payment_replay_ttl_seconds,
                replay_max_entries=settings.server.x402_payment_replay_max_entries,
            )
        )
    except Exception:
        checkpointed = False
    if not checkpointed:
        logger.error("Remote settlement succeeded but its local checkpoint failed")
        _record_product_event(
            "payment_settlement_unreconciled",
            request,
            price_usdc=price,
            network=network,
            reason="settlement_checkpoint_failed_after_remote_settlement",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id or None,
                "payment_state": "settlement_unreconciled",
                "transaction_hash": settlement_tx_hash,
            },
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=503,
                content={
                    "error": "Payment Reconciliation Required",
                    "message": "The payment settled remotely, but its recovery checkpoint could not be stored; no data was delivered.",
                },
            ),
        )

    _record_product_event(
        "payment_settled",
        request,
        price_usdc=price,
        network=network,
        metadata={
            "attempt_id": attempt_id,
            "payment_id": payment_id,
            "payment_state": "settled",
            "transaction_hash": settlement_tx_hash,
        },
    )

    try:
        finalized = request.app.state.credits.finalize_payment_proof(
            payment_id=payment_id,
            reservation_id=reservation_id,
            settlement=settlement,
            response_status=response.status_code,
            response_headers=dict(response.headers),
            response_body=response_body,
            replay_ttl_seconds=settings.server.x402_payment_replay_ttl_seconds,
            replay_max_entries=settings.server.x402_payment_replay_max_entries,
        )
    except Exception:
        finalized = False
    if not finalized:
        try:
            recovered = request.app.state.credits.finalized_payment_response(
                payment_id=payment_id,
                request_binding=str(verification.get("request_binding") or ""),
                max_age_seconds=settings.server.x402_payment_replay_ttl_seconds,
            )
            finalized = recovered is not None
        except Exception:
            finalized = False
    if not finalized:
        logger.error("Settlement checkpointed but local delivery finalization is deferred")
        _record_product_event(
            "charged_delivery_failed",
            request,
            status_code=503,
            price_usdc=price,
            network=network,
            reason="local_finalization_deferred_after_settlement",
            metadata={
                "attempt_id": attempt_id,
                "payment_id": payment_id,
                "payment_mode": "x402",
                "payment_state": "settled",
                "transaction_hash": settlement_tx_hash,
            },
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=503,
                content={
                    "error": "Payment Delivery Deferred",
                    "message": "The payment is safely checkpointed; retry the exact request to recover the response without paying again.",
                },
            ),
        )

    first_activation = _record_charged_delivery_outcome(
        request,
        response,
        price_usdc=price,
        payment_mode="x402",
        network=network,
        metadata={
            "mock": bool(verification.get("mock")),
            "attempt_id": attempt_id,
            "payment_id": payment_id or None,
            "payment_state": "finalized",
        },
    )
    if first_activation:
        response.headers["X-Blocksize-Activation"] = "first-live-price"
    settlement_b64 = base64.b64encode(
        json.dumps(settlement, sort_keys=True).encode()
    ).decode()
    response.headers["PAYMENT-RESPONSE"] = settlement_b64
    response.headers["X-PAYMENT-RESPONSE"] = settlement_b64
    logger.info("Payment settled and finalized: %s USDC for %s", price, path)

    return response


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        latency_ms = (time.perf_counter() - started) * 1000
        _record_http_usage(request, status_code, latency_ms)


# ---------------------------------------------------------------------------
# Data Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/vwap/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap(pair: str, request: Request) -> dict[str, Any]:
    """Get real-time VWAP for a crypto pair. Cost: $0.002–$0.004 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        vwap_data = await client.get_vwap_latest(clean)
        resp = VWAPResponse(data=vwap_data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Real-Time VWAP", "asset_class": "crypto"}
        resp["meta"]["citation"] = _citation_metadata(
            methodology_path="crypto-vwap-api",
            product_path="crypto-vwap-api",
            lineage={"upstream_method": "vwap_latest", "symbol": clean},
        )
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/bidask/{pair}", responses=X402_RESPONSE, openapi_extra=X402_BIDASK_PAYMENT_INFO)
async def get_bidask(pair: str, request: Request) -> dict[str, Any]:
    """Get bid/ask snapshot for a shared symbol. Cost: $0.002–$0.008 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        bidask_data = await client.get_bidask_snapshot(clean)
        resp = BidAskResponse(data=bidask_data).model_dump()
        is_equity = _looks_like_equity_bidask_symbol(clean)
        resp["meta"] = {
            "provider": "Blocksize Capital",
            "endpoint": "Bid/Ask Snapshot",
            "asset_class": "equity" if is_equity else "multi_asset",
            "route_family": "shared_bidask",
        }
        if is_equity:
            resp["meta"]["equity_ticker"] = clean
        resp["meta"]["citation"] = _citation_metadata(
            methodology_path=("equities-bidask-api" if is_equity else "bid-ask-price-api"),
            product_path=("equities-bidask-api" if is_equity else "bid-ask-price-api"),
            lineage={"upstream_method": "bidask_getSnapshot", "symbol": clean},
        )
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve bid/ask for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/state/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_state_price_endpoint(pair: str, request: Request) -> dict[str, Any]:
    """Get pool-derived state price for a covered crypto/protocol pair."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
        clean = _normalise_symbol(pair, "pair")
        cache_error: str | None = None
        try:
            if stream_cache and stream_cache.enabled:
                data = await stream_cache.get_state_price(clean)
            else:
                raise BlocksizeAPIError(-32004, "state stream cache disabled")
        except BlocksizeAPIError as exc:
            cache_error = exc.message
            data = await client.get_state_price(clean)
        source_method = (
            "state_subscribe_cache"
            if data.source.endswith("state_subscribe_cache")
            else "state_instruments+state_pool"
        )
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_state_price_from_pool_v1",
                "steps": [
                    "Normalize the requested symbol.",
                    "Read aggregate state price from state_subscribe cache when available.",
                    "If the cache does not cover the symbol, resolve matching pool instruments through state_instruments.",
                    "Fetch documented state_pool snapshots for matching pools and return a weighted state price.",
                ],
                "source_method": source_method,
                "limitations": [
                    "Not every market symbol has state-pool coverage; plain SOLUSD is distinct from pool symbols such as MSOLUSD or JUPSOLUSD.",
                ],
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "AMM State Price",
                "asset_class": "crypto_state",
                "upstream_methods": (
                    ["state_subscribe"]
                    if source_method == "state_subscribe_cache"
                    else ["state_instruments", "state_pool"]
                ),
                "citation": _citation_metadata(
                    methodology_path="signed-oracle-feeds",
                    product_path="state-price-api",
                    lineage={"upstream_method": source_method, "symbol": clean},
                ),
            },
        }
        if cache_error:
            resp["meta"]["cache_note"] = cache_error
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve state price for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/vwap30m/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap_30m_endpoint(
    pair: str,
    request: Request,
    include_trades: bool = Query(False, description="Include closingprice_trades evidence"),
) -> dict[str, Any]:
    """Get latest completed 30-minute close via Blocksize closingprice_list."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        data = await client.get_vwap_30min(clean)
        evidence: dict[str, Any] = {"included": False}
        if include_trades:
            trades = await client.get_vwap_30min_trades(clean)
            evidence = {
                "included": True,
                "upstream_method": "closingprice_trades",
                "trade_count": len(trades),
                "trades": trades,
            }
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_latest_30m_close_v1",
                "steps": [
                    "Normalize requested symbol into base and quote.",
                    "Request closingprice_list for the latest completed 30-minute UTC boundary.",
                    "Return the matching base/quote close from the upstream prices list.",
                    "When include_trades=true, attach closingprice_trades evidence for the same base/quote/window.",
                ],
                "upstream_method": "closingprice_list",
                "evidence": evidence,
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "30-Minute VWAP Close",
                "asset_class": "crypto",
                "citation": _citation_metadata(
                    methodology_path="vwap-30m-api",
                    product_path="vwap-30m-api",
                    lineage={"upstream_method": "closingprice_list", "symbol": clean},
                ),
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve 30-minute VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/vwap24h/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap_24h_endpoint(pair: str, request: Request) -> dict[str, Any]:
    """Get true 24-hour fixed VWAP from the websocket-backed cache."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
        clean = _normalise_symbol(pair, "pair")
        if stream_cache and stream_cache.enabled:
            data = await stream_cache.get_vwap_24h(clean)
        else:
            data = await client.get_vwap_24hr(clean)
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_fixed_24h_vwap_stream_cache_v1",
                "steps": [
                    "Maintain an authenticated Blocksize websocket subscription to fixedvwap_subscribe.",
                    "Store snapshot and update messages in a bounded in-memory cache.",
                    "Serve paid HTTP reads from the fresh cache entry for the requested pair.",
                ],
                "upstream_method": "fixedvwap_subscribe"
                if data.source.endswith("fixedvwap_subscribe_cache")
                else "vwap_24h_latest",
                "fallback": False,
                "limitations": [],
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "24-Hour Fixed VWAP",
                "asset_class": "crypto",
                "citation": _citation_metadata(
                    methodology_path="vwap-24h-api",
                    product_path="vwap-24h-api",
                    lineage={
                        "upstream_method": (
                            "fixedvwap_subscribe"
                            if data.source.endswith("fixedvwap_subscribe_cache")
                            else "vwap_24h_latest"
                        ),
                        "symbol": clean,
                    },
                ),
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve 24-hour VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/fx/{pair}", responses=X402_RESPONSE, openapi_extra=X402_TRADFI_PAYMENT_INFO)
async def get_fx(pair: str, request: Request) -> dict[str, Any]:
    """Get FX rate. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        data = await client.get_fx_rate(clean)
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "FX Rate",
                "asset_class": "fx",
                "citation": _citation_metadata(
                    methodology_path="fx-rates-api",
                    product_path="fx-rates-api",
                    lineage={"upstream_method": "fx_latest", "symbol": clean},
                ),
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve FX for {pair}", details=str(e),
        ).model_dump())

@app.get("/v1/metal/{ticker}", responses=X402_RESPONSE, openapi_extra=X402_TRADFI_PAYMENT_INFO)
async def get_metal(ticker: str, request: Request) -> dict[str, Any]:
    """Get metal spot price. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(ticker, "ticker")
        data = await client.get_metal_price(clean)
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "Metal Price",
                "asset_class": "metal",
                "citation": _citation_metadata(
                    methodology_path="metals-price-api",
                    product_path="metals-price-api",
                    lineage={"upstream_method": "metal_latest", "symbol": clean},
                ),
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve metal for {ticker}", details=str(e),
        ).model_dump())


@app.get("/v1/batch", responses=X402_RESPONSE, openapi_extra=X402_BATCH_PAYMENT_INFO)
async def batch_request(reqs: str, request: Request) -> dict[str, Any]:
    """
    Execute a batch of data queries.
    Pass a comma separated list of svc:pair in the `reqs` query parameter.
    Example: /v1/batch?reqs=vwap:BTCUSD,bidask:ETHUSD,fx:EURUSD
    """
    import asyncio
    
    try:
        queries = _parse_batch_reqs(reqs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    results = []
    
    # We execute requests concurrently for extreme speed
    tasks = []
    
    async def _safe_fetch(svc: str, ticker: str):
        try:
            if svc == "vwap":
                return await get_vwap(ticker, request)
            elif svc == "bidask":
                return await get_bidask(ticker, request)
            elif svc == "state":
                return await get_state_price_endpoint(ticker, request)
            elif svc == "vwap30m":
                return await get_vwap_30m_endpoint(ticker, request)
            elif svc == "vwap24h":
                return await get_vwap_24h_endpoint(ticker, request)
            elif svc == "fx":
                return await get_fx(ticker, request)
            elif svc == "metal":
                return await get_metal(ticker, request)
            else:
                return {
                    "status": "error",
                    "error_code": "UNSUPPORTED_SERVICE",
                    "message": f"Service type {svc} is not currently offered by this gateway",
                    "meta": {"endpoint": "Unsupported", "asset_class": "unknown"}
                }
        except HTTPException as he:
            return he.detail
        except Exception as e:
            return {"status": "error", "error_code": "BATCH_EXECUTION_ERROR", "message": str(e)}

    for svc, ticker, _raw_query in queries:
        tasks.append(_safe_fetch(svc, ticker))
        
    gathered_results = await asyncio.gather(*tasks)
    
    for (_svc, _ticker, raw_query), result in zip(queries, gathered_results):
        results.append({
            "request": raw_query,
            "response": result
        })
        
    return {
        "status": "ok",
        "batch_size": len(results),
        "results": results,
        "meta": {
            "credits": _credit_meta_for_request(request),
        },
    }


READINESS_PRODUCTS = {
    "token_market_quality_indicator",
    "state_divergence_indicator",
    "solana_token_brief",
    "trader_alpha_pack",
}


def _symbols_from_payload(payload: dict[str, Any]) -> list[str]:
    symbols = payload.get("symbols") or payload.get("watchlist") or payload.get("symbol")
    if symbols is None:
        symbols = ["SOLUSD"]
    if isinstance(symbols, str):
        if "," in symbols:
            symbols = [item.strip() for item in symbols.split(",") if item.strip()]
        else:
            symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols, watchlist, or symbol must include at least one symbol")
    if len(symbols) > 25:
        raise ValueError("Capability check supports up to 25 symbols")
    return [_normalise_symbol(str(symbol), "symbol") for symbol in symbols]


def _optional_feed_flags(payload: dict[str, Any]) -> dict[str, bool]:
    optional = payload.get("optional_feeds") if isinstance(payload.get("optional_feeds"), dict) else {}
    return {
        "state_coverage": bool(
            payload.get("include_state_coverage", optional.get("state_coverage", False))
        ),
        "state_price": bool(
            payload.get("include_state_price", optional.get("state_price", False))
        ),
        "vwap_windows": bool(
            payload.get("include_windows", optional.get("vwap_windows", False))
        ),
    }


@app.post("/v1/capabilities/check")
async def check_data_capabilities(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Check whether the current key has enough feed coverage for a product.

    This is a free readiness check. It uses instrument catalogs and known method
    availability, not paid price snapshots.
    """
    product = str(payload.get("product") or "token_market_quality_indicator")
    if product not in READINESS_PRODUCTS:
        raise HTTPException(status_code=400, detail=f"Unsupported product: {product}")
    try:
        symbols = _symbols_from_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    optional_flags = _optional_feed_flags(payload)
    client: BlocksizeClient = request.app.state.blocksize
    stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)

    try:
        vwap_symbols = {item.upper() for item in await client.list_vwap_instruments()}
    except BlocksizeAPIError as exc:
        vwap_symbols = set()
        vwap_catalog_error = str(exc)
    else:
        vwap_catalog_error = None

    try:
        bidask_symbols = {item.upper() for item in await client.list_bidask_instruments()}
    except BlocksizeAPIError as exc:
        bidask_symbols = set()
        bidask_catalog_error = str(exc)
    else:
        bidask_catalog_error = None

    state_instruments: list[Any] = []
    state_catalog_error = None
    state_catalog_required = (
        optional_flags["state_coverage"]
        or optional_flags["state_price"]
        or product == "state_divergence_indicator"
    )
    if state_catalog_required:
        try:
            state_instruments = await client.list_state_instruments()
        except BlocksizeAPIError as exc:
            state_catalog_error = str(exc)

    symbol_results: list[dict[str, Any]] = []
    for symbol in symbols:
        vwap_available = symbol in vwap_symbols
        bidask_available = symbol in bidask_symbols
        has_current_market = vwap_available or bidask_available
        state_coverage = (
            _state_coverage_from_instruments(symbol=symbol, instruments=state_instruments)
            if state_catalog_required and state_catalog_error is None
            else None
        )
        fixed_vwap_cached = bool(
            optional_flags["vwap_windows"]
            and stream_cache
            and stream_cache.enabled
            and stream_cache.has_vwap_24h(symbol)
        )

        required_feeds: dict[str, Any] = {
            "current_market_price": {
                "required": True,
                "available": has_current_market,
                "satisfied_by": [
                    name
                    for name, available in (
                        ("vwap_latest", vwap_available),
                        ("bidask_getSnapshot", bidask_available),
                    )
                    if available
                ],
            }
        }
        if product == "state_divergence_indicator":
            required_feeds["state_pool"] = {
                "required": True,
                "available": bool(
                    state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "satisfied_by": ["state_instruments", "state_pool"]
                if state_coverage is not None and state_coverage["matched_count"] > 0
                else [],
                "coverage": state_coverage,
                "reason": (
                    None
                    if state_coverage is not None and state_coverage["matched_count"] > 0
                    else "No matching state_instruments pools for this symbol."
                ),
            }

        optional_feeds = {
            "state_instruments": {
                "requested": optional_flags["state_coverage"],
                "available": bool(
                    optional_flags["state_coverage"]
                    and state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "coverage": state_coverage,
                "error": state_catalog_error,
            },
            "state_pool": {
                "requested": optional_flags["state_price"],
                "available": bool(
                    optional_flags["state_price"]
                    and state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "coverage": state_coverage,
                "reason": (
                    "state_pool is documented HTTP, resolved through state_instruments pools."
                    if state_coverage is not None and state_coverage["matched_count"] > 0
                    else "No matching state_instruments pools for this symbol."
                ),
            },
            "closingprice_list": {
                "requested": optional_flags["vwap_windows"],
                "available": optional_flags["vwap_windows"],
                "satisfied_by": ["closingprice_list"],
                "reason": "30-minute close is available through documented closingprice_list HTTP method.",
            },
            "fixedvwap_subscribe": {
                "requested": optional_flags["vwap_windows"],
                "available": fixed_vwap_cached,
                "satisfied_by": ["fixedvwap_subscribe_cache"] if fixed_vwap_cached else [],
                "reason": (
                    "24-hour fixed VWAP is available from the local fixedvwap_subscribe cache."
                    if fixed_vwap_cached
                    else "24-hour fixed VWAP requires the fixedvwap_subscribe stream cache to be enabled and populated for this symbol."
                ),
            },
        }
        missing_required = [
            name for name, item in required_feeds.items() if not item.get("available")
        ]
        symbol_results.append(
            {
                "symbol": symbol,
                "ready": not missing_required,
                "missing_required": missing_required,
                "required_feeds": required_feeds,
                "optional_feeds": optional_feeds,
            }
        )

    all_ready = all(item["ready"] for item in symbol_results)
    return {
        "status": "ok",
        "product": product,
        "ready": all_ready,
        "methodology": {
            "type": "blocksize_product_data_readiness_v1",
            "steps": [
                "Normalize requested symbols.",
                "Check VWAP and bid/ask instrument catalogs for current market coverage.",
                "Check state_instruments when state coverage, state price, or state-divergence products are requested.",
                "Resolve state price through documented state_pool pool snapshots rather than an undocumented ticker-level method.",
                "Mark 30-minute VWAP-window support available through closingprice_list and 24-hour fixed VWAP available when fixedvwap_subscribe cache is populated.",
                "Return missing required feeds before the caller spends credits on a paid workflow.",
            ],
        },
        "opt_in_policy": {
            "default_required": ["current_market_price"],
            "optional_default_off": [
                "state_instruments",
                "state_pool",
                "closingprice_list",
                "fixedvwap_subscribe_cache",
            ],
        },
        "catalog_errors": {
            "vwap_instruments": vwap_catalog_error,
            "bidask_instruments": bidask_catalog_error,
            "state_instruments": state_catalog_error,
        },
        "symbols": symbol_results,
    }


@app.post("/v1/briefs/market", responses=X402_RESPONSE)
async def agent_market_brief(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a decision-ready market brief. Cost: 10 credits or $0.25 USDC."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("symbol")
    if isinstance(symbols, str):
        symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 8:
        raise HTTPException(status_code=400, detail="Market brief supports up to 8 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    requested_service = payload.get("service")

    async def fetch_one(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        service = _service_for_symbol(symbol, str(requested_service) if requested_service else None)
        try:
            snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
            flags = _quality_flags(snapshot)
            return {
                "symbol": symbol,
                "asset_class": snapshot["asset_class"],
                "service": snapshot["service"],
                "latest": snapshot["data"],
                "value": snapshot.get("value"),
                "freshness_ms": _freshness_ms(snapshot.get("timestamp")),
                "spread_bps": snapshot.get("spread_bps"),
                "quality_flags": flags,
            }
        except BlocksizeAPIError as e:
            return {
                "symbol": symbol,
                "status": "error",
                "error_code": "BLOCKSIZE_ERROR",
                "message": str(e),
            }

    instruments = await asyncio.gather(*(fetch_one(symbol) for symbol in symbols))
    successful = [item for item in instruments if item.get("status") != "error"]
    if not successful:
        raise HTTPException(status_code=502, detail={"error_code": "NO_MARKET_DATA", "items": instruments})

    any_flags = sorted({flag for item in successful for flag in item.get("quality_flags", [])})
    market_state = "caution" if any_flags else "normal"
    response_core = {
        "methodology": {
            "type": "agent_market_brief_v1",
            "steps": [
                "Normalize and classify each requested symbol.",
                "Fetch one bounded live snapshot per symbol from the matching Blocksize raw-data service.",
                "Compute freshness, spread, and missing-value quality flags where available.",
                "Summarize actionability from returned data quality flags; no trade is executed.",
                "Persist a receipt with request hash, response hash, source endpoints, and lookup URL.",
            ],
            "limitations": [
                "This is market-data context, not investment advice.",
                "MVP brief uses current snapshots only; historical trend analytics are not included.",
            ],
        },
        "summary": {
            "headline": f"{len(successful)} of {len(instruments)} requested instruments returned live data.",
            "market_state": market_state,
            "actionability": "usable_with_caution" if any_flags else "usable_for_small_decision",
        },
        "instruments": instruments,
        "risks": [
            {
                "severity": "medium" if flag == "wide_spread" else "low",
                "code": flag,
                "message": flag.replace("_", " "),
            }
            for flag in any_flags
        ],
    }
    source_endpoints = [
        f"/v1/{item['service']}/{item['symbol']}"
        for item in successful
        if item.get("service") and item.get("symbol")
    ]
    receipt = _response_receipt(
        request,
        product="agent_market_brief",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=source_endpoints,
    )
    return {
        "status": "ok",
        "product": "agent_market_brief",
        "credit_cost": CREDIT_COSTS["market_brief"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.get("/v1/samples/pre-trade")
async def pre_trade_sample() -> dict[str, Any]:
    """Return a free, clearly labeled example of the paid pre-trade product."""
    paid_path = "/v1/checks/pre-trade"
    return {
        "status": "ok",
        "sample": True,
        "live_data": False,
        "product": "pre_trade_sanity_check",
        "purpose": (
            "Show the response contract and buyer outcome before an agent pays. "
            "Values below are illustrative and must not be used for a trade."
        ),
        "paid_endpoint": f"{PUBLIC_BASE_URL}{paid_path}",
        "price_usdc": str(ROUTE_PRICING[paid_path]),
        "example_request": {
            "symbol": "BTCUSD",
            "side": "buy",
            "notional_usd": 2500,
            "reference_price": 65000,
            "max_spread_bps": 50,
            "max_age_ms": 60000,
        },
        "example_response": {
            "decision": "pass",
            "checks": {
                "instrument_supported": True,
                "quote_fresh": True,
                "spread_within_limit": True,
                "reference_price_within_limit": True,
                "notional_supplied": True,
            },
            "recommendation": {
                "blocking": False,
                "message": "Market data passed configured sanity checks.",
            },
            "provenance": {
                "receipt_id": "rcpt_example_not_live",
                "request_hash": "illustrative",
                "response_hash": "illustrative",
            },
        },
        "limitations": [
            "Sample values are synthetic and intentionally not timestamped as live.",
            "The paid result is a read-only data-quality guardrail and never executes a trade.",
        ],
    }


@app.post("/v1/checks/pre-trade", responses=X402_RESPONSE)
async def pre_trade_sanity_check(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a read-only pre-trade sanity check. Cost: 5 credits or $0.10 USDC."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    side = str(payload.get("side") or "unknown").lower()
    notional_usd = float(payload.get("notional_usd") or 0)
    reference_price = payload.get("reference_price")
    reference_price = float(reference_price) if reference_price is not None else None
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_age_ms = int(payload.get("max_age_ms") or 60_000)

    client: BlocksizeClient = request.app.state.blocksize
    service = _service_for_symbol(symbol, str(payload.get("service")) if payload.get("service") else None)
    if service == "vwap":
        service = "bidask"
    snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
    freshness = _freshness_ms(snapshot.get("timestamp"))
    spread_bps = snapshot.get("spread_bps")
    market_value = float(snapshot.get("value") or 0)
    reference_drift_bps = (
        abs(market_value - reference_price) / reference_price * 10000
        if reference_price and market_value
        else None
    )
    checks = {
        "instrument_supported": True,
        "quote_fresh": freshness is None or freshness <= max_age_ms,
        "spread_within_limit": spread_bps is None or spread_bps <= max_spread_bps,
        "reference_price_drift_bps": reference_drift_bps,
        "reference_price_within_limit": reference_drift_bps is None or reference_drift_bps <= max_spread_bps,
        "notional_supplied": notional_usd > 0,
    }
    blocking = not checks["quote_fresh"] or not checks["spread_within_limit"]
    decision = "block" if blocking else ("caution" if not checks["reference_price_within_limit"] else "pass")
    response_core = {
        "methodology": {
            "type": "pre_trade_sanity_check_v1",
            "steps": [
                "Normalize the requested symbol and route to the relevant read-only market-data service.",
                "Fetch the latest bid/ask-style snapshot where possible.",
                "Compare quote age against max_age_ms.",
                "Compare spread_bps against max_spread_bps when spread is available.",
                "Compare supplied reference_price against current market value when provided.",
                "Return pass, caution, or block without executing or routing any trade.",
            ],
            "decision_rules": {
                "block": "Quote is stale or spread exceeds configured max_spread_bps.",
                "caution": "Reference price drift exceeds configured threshold.",
                "pass": "Configured freshness, spread, and reference checks pass.",
            },
        },
        "decision": decision,
        "side": side,
        "notional_usd": notional_usd,
        "checks": checks,
        "market": snapshot,
        "recommendation": {
            "blocking": blocking,
            "message": (
                "Do not proceed until a fresher/tighter quote is available."
                if blocking
                else "Market data passed configured sanity checks."
            ),
        },
    }
    receipt = _response_receipt(
        request,
        product="pre_trade_sanity_check",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=[snapshot["endpoint"]],
    )
    return {
        "status": "ok",
        "product": "pre_trade_sanity_check",
        "credit_cost": CREDIT_COSTS["pre_trade_check"],
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/receipts/price", responses=X402_RESPONSE)
async def audit_grade_price_receipt(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create an audit-grade receipt for one price lookup. Cost: 10 credits or $0.25 USDC."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    service = _service_for_symbol(symbol, str(payload.get("service")) if payload.get("service") else None)
    snapshot = await _fetch_service_snapshot(request.app.state.blocksize, service=service, symbol=symbol)
    response_core = {
        "methodology": {
            "type": "audit_grade_price_receipt_v1",
            "steps": [
                "Normalize symbol and requested source service.",
                "Fetch the corresponding live Blocksize raw-data snapshot.",
                "Capture the returned value, timestamp, raw payload, request hash, and response hash.",
                "Store receipt metadata in the credit ledger for later provenance lookup.",
            ],
            "hashing": "Stable JSON serialization with sha256 fingerprints; receipt ids are derived from product, subject, timestamp, request, and response.",
        },
        "price": {
            "service": service,
            "symbol": symbol,
            "value": snapshot.get("value"),
            "timestamp": snapshot.get("timestamp"),
            "raw": snapshot.get("data"),
        },
        "client_reference_id": payload.get("client_reference_id"),
        "purpose": payload.get("purpose"),
    }
    receipt = _response_receipt(
        request,
        product="audit_grade_price_receipt",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=[snapshot["endpoint"]],
    )
    return {
        "status": "ok",
        "product": "audit_grade_price_receipt",
        "credit_cost": CREDIT_COSTS["audit_receipt"],
        "receipt": receipt,
        **response_core,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/snapshots/macro", responses=X402_RESPONSE)
async def multi_asset_macro_snapshot(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded multi-asset macro snapshot. Cost: 25 credits or $1.00 USDC."""
    import asyncio

    universe = payload.get("universe") or payload.get("symbols") or ["BTCUSD", "ETHUSD", "EURUSD", "XAUUSD"]
    if isinstance(universe, str):
        universe = [item.strip() for item in universe.split(",") if item.strip()]
    if not isinstance(universe, list) or not universe:
        raise HTTPException(status_code=400, detail="universe must include at least one symbol")
    if len(universe) > 12:
        raise HTTPException(status_code=400, detail="Macro snapshot supports up to 12 symbols")

    client: BlocksizeClient = request.app.state.blocksize

    async def fetch_macro(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        service = _service_for_symbol(symbol)
        try:
            snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
            return {
                "symbol": symbol,
                "asset_class": snapshot["asset_class"],
                "service": service,
                "latest": snapshot["data"],
                "value": snapshot.get("value"),
                "quality_flags": _quality_flags(snapshot),
            }
        except BlocksizeAPIError as e:
            return {"symbol": symbol, "status": "error", "message": str(e)}

    assets = await asyncio.gather(*(fetch_macro(symbol) for symbol in universe))
    successful = [asset for asset in assets if asset.get("status") != "error"]
    if not successful:
        raise HTTPException(status_code=502, detail={"error_code": "NO_MARKET_DATA", "items": assets})
    flags = sorted({flag for asset in successful for flag in asset.get("quality_flags", [])})
    regime_label = "mixed" if flags else "orderly"
    response_core = {
        "methodology": {
            "type": "multi_asset_macro_snapshot_v1",
            "steps": [
                "Normalize and classify a bounded universe of up to 12 symbols.",
                "Fetch one current snapshot per symbol across crypto, FX, metals, or bid/ask routes.",
                "Evaluate data quality flags for freshness, spread, and missing values.",
                "Assign a simple MVP regime label from quality flags and coverage.",
                "Persist a receipt linking all source endpoints used in the snapshot.",
            ],
            "limitations": [
                "MVP market_regime is a quality/context heuristic, not a predictive macro model.",
                "No portfolio holdings, historical returns, or volatility model is applied.",
            ],
        },
        "market_regime": {
            "label": regime_label,
            "confidence": 0.68 if flags else 0.82,
            "drivers": flags or ["requested_assets_available", "no_quality_flags"],
        },
        "assets": assets,
        "brief": {
            "headline": f"Macro snapshot returned {len(successful)} of {len(assets)} requested assets.",
            "watch_items": flags,
        },
    }
    source_endpoints = [
        f"/v1/{asset['service']}/{asset['symbol']}"
        for asset in successful
        if asset.get("service") and asset.get("symbol")
    ]
    receipt = _response_receipt(
        request,
        product="multi_asset_macro_snapshot",
        subject=",".join(str(s).upper() for s in universe),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=source_endpoints,
    )
    return {
        "status": "ok",
        "product": "multi_asset_macro_snapshot",
        "credit_cost": CREDIT_COSTS["macro_snapshot"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/monitors/evaluate", responses=X402_RESPONSE)
async def spend_controlled_market_monitor(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a bounded market monitor immediately. Cost: 10 credits or $0.25 USDC."""
    brief = await agent_market_brief(request, payload)
    rules = payload.get("rules") or []
    matches: list[dict[str, Any]] = []
    for rule in rules if isinstance(rules, list) else []:
        metric = rule.get("metric")
        operator = rule.get("operator")
        value = float(rule.get("value") or 0)
        for item in brief.get("instruments", []):
            observed = item.get(metric)
            if not isinstance(observed, (int, float)):
                continue
            hit = (
                (operator == ">" and observed > value)
                or (operator == ">=" and observed >= value)
                or (operator == "<" and observed < value)
                or (operator == "<=" and observed <= value)
            )
            if hit:
                matches.append({"symbol": item.get("symbol"), "metric": metric, "observed": observed, "rule": rule})
    return {
        "status": "ok",
        "product": "spend_controlled_market_monitor",
        "credit_cost": CREDIT_COSTS["market_brief"],
        "methodology": {
            "type": "spend_controlled_market_monitor_v1",
            "steps": [
                "Reuse Agent Market Brief to fetch bounded current snapshots.",
                "Evaluate supplied numeric rules against returned instrument fields.",
                "Return matching triggers plus explicit credit budget metadata.",
                "Do not persist a scheduler or poll automatically in the MVP.",
            ],
            "limitations": [
                "This endpoint evaluates now only; recurring monitor scheduling is not enabled.",
            ],
        },
        "mode": "evaluate_now",
        "matches": matches,
        "brief": brief,
        "spend_control": {
            "max_credits": payload.get("max_credits"),
            "credits_spent": CREDIT_COSTS["market_brief"],
            "remaining_budget": (
                float(payload.get("max_credits")) - CREDIT_COSTS["market_brief"]
                if payload.get("max_credits") is not None
                else None
            ),
        },
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/indicators/token-quality", responses=X402_RESPONSE)
async def token_market_quality_indicator(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Score one token using live price, bid/ask, state, and VWAP-window feeds."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)
    components = await _fetch_indicator_components(
        request.app.state.blocksize,
        symbol=symbol,
        stream_cache=getattr(request.app.state, "stream_cache", None),
        include_state_price=bool(payload.get("include_state_price", False)),
        include_windows=bool(payload.get("include_windows", False)),
        include_state_coverage=bool(payload.get("include_state_coverage", False)),
    )
    if not _has_current_market_component(components):
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_INDICATOR_FEEDS", "components": components},
        )

    indicator = _build_token_quality_indicator(
        symbol=symbol,
        components=components,
        max_spread_bps=max_spread_bps,
        max_state_divergence_bps=max_state_divergence_bps,
    )
    response_core = {
        "methodology": {
            "type": "token_market_quality_indicator_v1",
            "steps": [
                "Normalize the token symbol and fetch current VWAP plus bid/ask from Blocksize raw-data feeds.",
                "Fetch available Blocksize state instrument coverage, including pool/network metadata, when enabled.",
                "Optionally resolve state/reference price through state_instruments plus state_pool, and fetch VWAP-window feeds if those upstream methods are enabled.",
                "Compute spread, freshness, state coverage, optional state divergence, optional VWAP-window drift, feed coverage, and transparent score penalties.",
                "Return a trader decision-support signal without executing or recommending a trade.",
                "Persist a provenance receipt with source endpoints and stable request/response hashes.",
            ],
            "score_rules": {
                "starts_at": 100,
                "penalties": [
                    "missing current market price",
                    "missing or wide bid/ask spread",
                    "missing or divergent state price when requested",
                    "missing VWAP windows when requested",
                    "missing state instrument coverage",
                    "stale returned components",
                ],
            },
            "limitations": [
                "Decision-support only; this is not investment advice or guaranteed alpha.",
                "State instrument and state_pool coverage is symbol-dependent; plain SOLUSD is not the same as protocol symbols such as MSOLUSD or JUPSOLUSD.",
                "Oracle confidence and VWAP-window latest methods are not available on the current deployed key surface.",
            ],
        },
        "indicator": indicator,
        "components": components,
    }
    receipt = _response_receipt(
        request,
        product="token_market_quality_indicator",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=_component_source_endpoints(symbol, components),
    )
    return {
        "status": "ok",
        "product": "token_market_quality_indicator",
        "credit_cost": CREDIT_COSTS["token_quality_indicator"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/indicators/state-divergence", responses=X402_RESPONSE)
async def state_divergence_indicator(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Compare Blocksize market prices with state/reference prices."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    max_divergence_bps = float(payload.get("max_divergence_bps") or 75)
    components = await _fetch_indicator_components(
        request.app.state.blocksize,
        symbol=symbol,
        stream_cache=getattr(request.app.state, "stream_cache", None),
        include_state_price=True,
        include_windows=False,
        include_state_coverage=bool(payload.get("include_state_coverage", False)),
    )
    state = components.get("state", {})
    if state.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "STATE_PRICE_UNAVAILABLE",
                "message": "Blocksize state_pool did not return a usable value for this symbol. Check state_instruments coverage first.",
                "components": components,
            },
        )

    vwap = components.get("vwap", {})
    bidask = components.get("bidask", {})
    state_value = state.get("value")
    vwap_vs_state_bps = _bps_delta(
        vwap.get("value") if vwap.get("status") == "ok" else None,
        state_value,
    )
    mid_vs_state_bps = _bps_delta(
        bidask.get("value") if bidask.get("status") == "ok" else None,
        state_value,
    )
    max_observed = max(
        (
            abs(item)
            for item in (vwap_vs_state_bps, mid_vs_state_bps)
            if item is not None
        ),
        default=None,
    )
    state_label = (
        "normal"
        if max_observed is not None and max_observed <= max_divergence_bps
        else "alert"
        if max_observed is not None
        else "insufficient_market_price"
    )
    response_core = {
        "methodology": {
            "type": "state_divergence_indicator_v1",
            "steps": [
                "Fetch current VWAP and bid/ask market prices for the symbol.",
                "Resolve the symbol through state_instruments and fetch Blocksize state_pool snapshots for matching pools.",
                "Compute signed basis in basis points from VWAP and bid/ask mid versus state price.",
                "Classify the result as normal or alert against max_divergence_bps.",
                "Persist receipt provenance for audit and replay context.",
            ],
            "decision_rules": {
                "normal": "Absolute observed divergence is within max_divergence_bps.",
                "alert": "At least one observed market-vs-state basis exceeds max_divergence_bps.",
                "insufficient_market_price": "State price exists but no current market price was returned.",
            },
            "limitations": [
                "State divergence can reflect stale, paused, or unsupported feeds as well as real market dislocation.",
                "Decision-support only; no trade is executed.",
            ],
        },
        "symbol": symbol,
        "state": {
            "label": state_label,
            "max_divergence_bps": max_divergence_bps,
            "max_observed_abs_bps": max_observed,
        },
        "basis": {
            "vwap_vs_state_bps": vwap_vs_state_bps,
            "bidask_mid_vs_state_bps": mid_vs_state_bps,
        },
        "components": components,
    }
    receipt = _response_receipt(
        request,
        product="state_divergence_indicator",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=_component_source_endpoints(symbol, components),
    )
    return {
        "status": "ok",
        "product": "state_divergence_indicator",
        "credit_cost": CREDIT_COSTS["state_divergence_indicator"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/signals/solana-token-brief", responses=X402_RESPONSE)
async def solana_token_brief(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a Solana-oriented token signal brief for supported price symbols."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("symbol") or ["SOLUSD"]
    if isinstance(symbols, str):
        symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 10:
        raise HTTPException(status_code=400, detail="Solana token brief supports up to 10 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)

    async def score_symbol(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        components = await _fetch_indicator_components(
            client,
            symbol=symbol,
            stream_cache=getattr(request.app.state, "stream_cache", None),
            include_state_price=bool(payload.get("include_state_price", False)),
            include_windows=bool(payload.get("include_windows", False)),
            include_state_coverage=bool(payload.get("include_state_coverage", False)),
        )
        if not _has_current_market_component(components):
            return {
                "symbol": symbol,
                "status": "unsupported_or_unavailable",
                "coverage": {"status": "none"},
                "components": components,
            }
        return {
            "symbol": symbol,
            "status": "ok",
            "indicator": _build_token_quality_indicator(
                symbol=symbol,
                components=components,
                max_spread_bps=max_spread_bps,
                max_state_divergence_bps=max_state_divergence_bps,
            ),
            "components": components,
        }

    token_results = await asyncio.gather(*(score_symbol(symbol) for symbol in symbols))
    successful = [item for item in token_results if item.get("status") == "ok"]
    if not successful:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_SOLANA_TOKEN_FEEDS", "tokens": token_results},
        )

    ranked = sorted(
        successful,
        key=lambda item: item["indicator"]["score"],
        reverse=True,
    )
    response_core = {
        "methodology": {
            "type": "solana_token_brief_v1",
            "steps": [
                "Treat requested symbols as a Solana-oriented token watchlist.",
                "Fetch only actual Blocksize feed data available for each symbol.",
                "Compute token market quality indicators from live VWAP, bid/ask, and available state instrument/pool coverage.",
                "Return unsupported protocol, DEX, oracle-price, or pool-price symbols as explicit coverage misses instead of synthetic data.",
                "Persist receipt provenance for the successful source endpoints.",
            ],
            "solana_scope": [
                "Supported now: token symbols available in Blocksize VWAP and bid/ask feeds, plus state_instruments pool/network metadata.",
                "Not synthesized in this MVP: DEX pool liquidity, oracle account confidence, state price, protocol TVL, perps funding, or VWAP windows unless Blocksize exposes those feeds.",
            ],
        },
        "network": "solana",
        "summary": {
            "headline": f"{len(successful)} of {len(token_results)} requested Solana-oriented symbols returned usable feed data.",
            "top_symbol": ranked[0]["symbol"],
            "top_score": ranked[0]["indicator"]["score"],
            "coverage_status": "partial" if len(successful) < len(token_results) else "full",
        },
        "tokens": token_results,
        "ranked_symbols": [
            {
                "symbol": item["symbol"],
                "score": item["indicator"]["score"],
                "signal": item["indicator"]["signal"],
            }
            for item in ranked
        ],
    }
    source_endpoints: list[str] = []
    for item in successful:
        source_endpoints.extend(
            _component_source_endpoints(item["symbol"], item.get("components", {}))
        )
    receipt = _response_receipt(
        request,
        product="solana_token_brief",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=sorted(set(source_endpoints)),
    )
    return {
        "status": "ok",
        "product": "solana_token_brief",
        "credit_cost": CREDIT_COSTS["solana_token_brief"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/signals/trader-alpha-pack", responses=X402_RESPONSE)
async def trader_alpha_pack(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Bundle token quality indicators and macro context into a trader signal pack."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("watchlist") or ["BTCUSD", "ETHUSD", "SOLUSD"]
    if isinstance(symbols, str):
        symbols = [item.strip() for item in symbols.split(",") if item.strip()]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 12:
        raise HTTPException(status_code=400, detail="Trader alpha pack supports up to 12 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)

    async def build_one(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        components = await _fetch_indicator_components(
            client,
            symbol=symbol,
            stream_cache=getattr(request.app.state, "stream_cache", None),
            include_state_price=bool(payload.get("include_state_price", False)),
            include_windows=bool(payload.get("include_windows", False)),
            include_state_coverage=bool(payload.get("include_state_coverage", False)),
        )
        if not _has_current_market_component(components):
            return {
                "symbol": symbol,
                "status": "unsupported_or_unavailable",
                "components": components,
            }
        return {
            "symbol": symbol,
            "status": "ok",
            "indicator": _build_token_quality_indicator(
                symbol=symbol,
                components=components,
                max_spread_bps=max_spread_bps,
                max_state_divergence_bps=max_state_divergence_bps,
            ),
            "components": components,
        }

    items = await asyncio.gather(*(build_one(symbol) for symbol in symbols))
    successful = [item for item in items if item.get("status") == "ok"]
    if not successful:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_TRADER_SIGNAL_FEEDS", "signals": items},
        )
    ranked = sorted(
        successful,
        key=lambda item: item["indicator"]["score"],
        reverse=True,
    )
    alerts = [
        {
            "symbol": item["symbol"],
            "flags": item["indicator"]["flags"],
            "signal": item["indicator"]["signal"],
        }
        for item in successful
        if item["indicator"]["flags"]
    ]
    response_core = {
        "methodology": {
            "type": "trader_alpha_pack_v1",
            "steps": [
                "Evaluate a bounded watchlist with the Token Market Quality methodology.",
                "Rank symbols by transparent quality score and identify caution/alert flags.",
                "Highlight spread quality, stale components, state instrument/pool coverage, and optional state/window metrics as trader decision-support data.",
                "Return coverage misses explicitly; no synthetic protocol or pool data is invented.",
                "Persist provenance for later receipt lookup.",
            ],
            "alpha_definition": (
                "In this MVP, alpha means faster, auditable signal packaging from live Blocksize data, "
                "not a promise of predictive returns."
            ),
            "limitations": [
                "Not investment advice.",
                "State instrument/pool coverage is available through state_instruments; DEX pool depth, state price, oracle confidence intervals, protocol TVL, and perps funding require additional Blocksize feed connectors before they can be scored directly.",
            ],
        },
        "summary": {
            "headline": f"{len(successful)} of {len(items)} watchlist symbols produced trader indicators.",
            "best_quality_symbol": ranked[0]["symbol"],
            "best_quality_score": ranked[0]["indicator"]["score"],
            "alerts_count": len(alerts),
        },
        "ranked_signals": [
            {
                "symbol": item["symbol"],
                "score": item["indicator"]["score"],
                "signal": item["indicator"]["signal"],
                "metrics": item["indicator"]["metrics"],
            }
            for item in ranked
        ],
        "alerts": alerts,
        "signals": items,
    }
    source_endpoints: list[str] = []
    for item in successful:
        source_endpoints.extend(
            _component_source_endpoints(item["symbol"], item.get("components", {}))
        )
    receipt = _response_receipt(
        request,
        product="trader_alpha_pack",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=sorted(set(source_endpoints)),
    )
    return {
        "status": "ok",
        "product": "trader_alpha_pack",
        "credit_cost": CREDIT_COSTS["trader_alpha_pack"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.get("/v1/provenance/{receipt_id}")
async def lookup_price_receipt(request: Request, receipt_id: str) -> dict[str, Any]:
    """Lookup provenance for a prior paid or credited call. FREE when receipt exists."""
    if not re.fullmatch(r"rcpt_[A-Za-z0-9]{12,64}", receipt_id):
        raise HTTPException(status_code=400, detail="Invalid receipt id")
    receipt = request.app.state.credits.get_price_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt.setdefault(
        "methodology",
        {
            "type": "agent_data_provenance_lookup_v1",
            "steps": [
                "Validate receipt id format.",
                "Fetch stored receipt payload from the Blocksize receipt ledger.",
                "Return original request, response, receipt hashes, source endpoints, and payment context.",
            ],
        },
    )
    return receipt


@app.get("/v1/search")
async def search_pairs(
    q: str = Query(..., min_length=1, max_length=64, description="Search query"),
    asset_class: str = Query(
        "all",
        pattern="^(all|crypto|equity|equities|fx|metal)$",
        description="Asset class filter",
    ),
    limit: int = Query(
        50,
        ge=1,
        le=DISCOVERY_INSTRUMENT_MAX_LIMIT,
        description="Maximum matching instruments to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based offset into the deterministic search results",
    ),
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Search instruments. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        pairs, total = await client.search_pairs_page(
            q,
            asset_class,
            limit=limit,
            offset=offset,
        )
        pairs = [_purchase_ready_pair(pair) for pair in pairs]
        next_offset = offset + len(pairs)
        has_more = next_offset < total
        opportunity = normalize_symbol_opportunity(q)
        _record_product_event(
            "catalog_search_completed",
            request,
            metadata={
                "normalized_query": opportunity,
                "total_matches": total,
                "returned_matches": len(pairs),
                "limit": limit,
                "offset": offset,
                "has_more": has_more,
            },
        )
        if not pairs and total == 0 and opportunity is not None:
            _record_product_event(
                "unsupported_symbol_request",
                request,
                metadata={"normalized_query": opportunity, "result_count": 0},
            )
        elif pairs and offset == 0 and pairs[0].match_type in {
            "exact_symbol",
            "exact_base",
            "alias",
        }:
            selected = pairs[0]
            _record_product_event(
                "instrument_resolved",
                request,
                metadata={
                    "query": q,
                    "canonical_symbol": selected.canonical_symbol,
                    "recommended_service": selected.recommended_service,
                    "selection_source": "public_http_resolver",
                    "match_type": selected.match_type,
                },
            )
        return PairSearchResponse(
            query=q,
            total_matches=total,
            returned_matches=len(pairs),
            offset=offset,
            limit=limit,
            has_more=has_more,
            next_offset=next_offset if has_more else None,
            pairs=pairs,
            meta={
                "provider": "Blocksize Capital",
                "snapshot_scope": "full_catalog_search_with_paginated_response",
                "total_coverage": (
                    "Enabled symbols across crypto, equities, FX, and metals"
                ),
                "ranking": (
                    "exact symbol, exact base asset, symbol prefix, base prefix, "
                    "exact quote asset, common quote currency, then deterministic "
                    "lexical order"
                ),
                "canonical_symbol_format": "compact uppercase without separators",
                "accepted_search_formats": ["BTCUSD", "BTC-USD", "BTC/USD", "BTC_USD"],
                "route_templates_by_service": {
                    "vwap": "/v1/vwap/{pair}",
                    "bidask": "/v1/bidask/{pair}",
                    "fx": "/v1/fx/{pair}",
                    "metal": "/v1/metal/{ticker}",
                },
                "purchase_handoff": (
                    "Each result includes its current price, attributed purchase URL, "
                    "and a copyable unsigned request. Unsupported symbols are rejected "
                    "before an x402 challenge is created."
                ),
            },
        ).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Search failed for '{q}'", details=str(e),
        ).model_dump())


@app.get("/v1/coverage")
async def unified_coverage(request: Request) -> dict[str, Any]:
    """Return one conservative coverage map for humans and agents. FREE."""
    client: BlocksizeClient = request.app.state.blocksize
    namespace_calls = {
        "vwap": client.list_vwap_instruments(),
        "bidask": client.list_bidask_instruments(),
        "fx": client.list_fx_instruments(),
        "metal": client.list_metal_instruments(),
    }
    results = await asyncio.gather(
        *namespace_calls.values(),
        return_exceptions=True,
    )
    live_namespaces: dict[str, dict[str, Any]] = {}
    for name, result in zip(namespace_calls, results, strict=True):
        if isinstance(result, Exception):
            live_namespaces[name] = {
                "status": "temporarily_unavailable",
                "enabled_instrument_count": None,
                "discovery_endpoint": f"/v1/instruments/{name}",
            }
        else:
            live_namespaces[name] = {
                "status": "available",
                "enabled_instrument_count": len(result),
                "discovery_endpoint": f"/v1/instruments/{name}",
            }

    registry = build_rwa_registry_overview(
        include_aliases=False,
        limit=1,
        offset=0,
        include_venue_instruments=False,
    )
    rwa = registry["summary"]
    writes_locked = economic_writes_locked()
    payment_rails = settings.x402.payment_rail_status()

    def rail_availability(rail: dict[str, object]) -> str:
        if not rail["ready"]:
            return "unavailable_configuration"
        if not rail["enabled"]:
            return "disabled_by_rail_control"
        if writes_locked:
            return "locked_pending_production_enablement"
        return "available"

    rail_access = {
        name: {
            "configured": bool(rail["configured"]),
            "configuration_ready": bool(rail["ready"]),
            "rail_enabled": bool(rail["enabled"]),
            "accepting_payments": bool(rail["operational"]) and not writes_locked,
            "availability": rail_availability(rail),
            "blockers": list(rail["blockers"]),
        }
        for name, rail in payment_rails.items()
    }
    _record_product_event(
        "coverage_catalog_view",
        request,
        metadata={
            "available_live_namespaces": sum(
                row["status"] == "available" for row in live_namespaces.values()
            ),
            "rwa_canonical_assets": rwa["canonical_asset_count"],
            "rwa_decision_grade_assets": rwa[
                "decision_grade_canonical_asset_count"
            ],
            "economic_writes_locked": writes_locked,
        },
    )
    return {
        "status": "ok",
        "product": "unified_data_coverage",
        "as_of": _utc_now_iso(),
        "definitions": {
            "enabled_live_namespace": (
                "An instrument is discoverable from the live upstream namespace; "
                "a usable observation still depends on source freshness and availability."
            ),
            "decision_grade": (
                "Canonical identity checks passed for the current RWA catalog. "
                "This is not a promise that every venue observation is live or licensed."
            ),
            "research_only": (
                "Catalog coverage that requires identity, source-rights, adapter, "
                "freshness, or quality verification before production decisions."
            ),
            "counting_note": (
                "Namespaces and RWA venue rows overlap. Do not add their counts to "
                "claim a unique instrument total."
            ),
        },
        "live_data": {
            "scope": "enabled_upstream_discovery_namespaces",
            "namespaces": live_namespaces,
            "search_endpoint": "/v1/search?q={query}&asset_class={asset_class}&limit=50&offset=0",
        },
        "rwa_registry": {
            "canonical_assets": rwa["canonical_asset_count"],
            "normalized_symbol_aliases": rwa["alias_count"],
            "venue_instrument_rows": rwa["coverage_row_count"],
            "venues": rwa["registry_venue_count"],
            "decision_grade_canonical_assets": rwa[
                "decision_grade_canonical_asset_count"
            ],
            "research_only_or_manual_verification_assets": rwa[
                "manual_verification_asset_count"
            ],
            "ambiguous_source_scoped_assets": rwa[
                "ambiguous_source_scoped_asset_count"
            ],
            "registry_endpoint": "/v1/rwa/registry",
            "venue_rows_endpoint": "/v1/rwa/registry/venues",
        },
        "access": {
            "free_discovery": [
                "/v1/search",
                "/v1/instruments/{service}",
                "/v1/rwa/registry",
                "/v1/rwa/coverage",
                "/data-packages.json",
                "/llms.txt",
            ],
            "live_http": {
                "mode": "signed_x402_per_call",
                "availability": (
                    "locked_pending_production_enablement"
                    if writes_locked
                    else (
                        "available"
                        if any(rail["accepting_payments"] for rail in rail_access.values())
                        else "unavailable_no_enabled_rail"
                    )
                ),
                "rails": rail_access,
                "templates": [
                    "/v1/vwap/{pair}",
                    "/v1/bidask/{pair}",
                    "/v1/fx/{pair}",
                    "/v1/metal/{ticker}",
                ],
            },
            "authenticated_connectors": {
                "surfaces": ["OpenAI", "Claude", "Cursor"],
                "starter_allowance_credits": STARTER_CREDIT_ALLOWANCE,
                "availability": (
                    "locked_pending_production_enablement"
                    if writes_locked
                    else "available_to_eligible_authenticated_users"
                ),
            },
            "public_mcp": REMOTE_MCP_URL,
            "pay_sh": "https://pay.sh/services/blocksize/market-data",
            "product_catalog": DATA_PACKAGES_JSON_URL,
        },
        "commercial_model": {
            "discovery_cost_credits": 0,
            "starter_credits_apply_to_products_not_symbols": True,
            "starter_credit_costs": CREDIT_COSTS,
            "conversion_path": [
                "discover coverage for free",
                "select a supported instrument and product",
                "receive a live result through starter credits or signed x402",
                "move repeat or high-volume usage to an authenticated account plan",
            ],
            "measurement_events": [
                "free_discovery_call",
                "catalog_search_completed",
                "coverage_catalog_view",
                "payment_required",
                "payment_settled",
                "data_delivered",
            ],
        },
    }


@app.get("/v1/instruments/{service}")
async def list_instruments(
    service: str,
    request: Request,
    limit: int = Query(
        DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
        ge=1,
        le=DISCOVERY_INSTRUMENT_MAX_LIMIT,
        description="Maximum instruments to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based offset into the sorted instrument catalog",
    ),
) -> dict[str, Any]:
    """List instruments for a service. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        elif service == "metal":
            instruments = await client.list_metal_instruments()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        instruments = sorted(str(instrument) for instrument in instruments)
        total = len(instruments)
        page = instruments[offset : offset + limit]
        next_offset = offset + len(page)
        has_more = next_offset < total
        return InstrumentListResponse(
            service=service,
            total_instruments=total,
            returned_instruments=len(page),
            offset=offset,
            limit=limit,
            has_more=has_more,
            next_offset=next_offset if has_more else None,
            instruments=page,
            meta={
                **build_catalog_snapshot_metadata(
                    source=f"Blocksize {service} instrument catalog",
                    records=instruments,
                    grain="instrument",
                    snapshot_scope="full_upstream_catalog",
                ),
                "ordering": "lexicographic_ascending",
            },
        ).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to list for {service}", details=str(e),
        ).model_dump())


@app.get("/v1/rwa/build-plan")
async def get_rwa_build_plan() -> dict[str, Any]:
    """Return the recommended RWA VWAP and bid/ask build plan. FREE."""
    return {
        "status": "ok",
        "product": "rwa_market_data_build_plan",
        "as_of": _utc_now_iso(),
        **build_rwa_build_plan(),
    }


@app.get("/v1/rwa/coverage")
async def get_rwa_coverage(
    asset_class: str = Query(
        "all",
        max_length=64,
        description="RWA asset-class filter",
    ),
    venue: str = Query(
        "all",
        max_length=64,
        description="Venue filter, e.g. kraken_xstocks, ostium, gains, jupiter_xstocks",
    ),
    include_symbols: bool = Query(True, description="Include per-symbol coverage rows"),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum symbol rows to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based symbol-row offset",
    ),
) -> dict[str, Any]:
    """Return filterable RWA symbol and venue coverage. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_market_data_coverage",
            "as_of": _utc_now_iso(),
            "filters": {
                "asset_class": asset_class,
                "venue": venue,
                "include_symbols": include_symbols,
                "limit": limit,
                "offset": offset,
            },
            **build_rwa_coverage_overview(
                asset_class=asset_class,
                venue=venue,
                include_symbols=include_symbols,
                limit=limit,
                offset=offset,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/assets")
async def get_rwa_assets(
    asset_class: str = Query(
        "all",
        max_length=64,
        description="RWA asset-class filter",
    ),
    venue: str = Query(
        "all",
        max_length=64,
        description="Venue filter, e.g. kraken_xstocks, ostium, gains, ondo_stocks",
    ),
    limit: int = Query(
        RWA_ASSET_MATRIX_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum canonical asset rows to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based canonical-asset offset",
    ),
) -> dict[str, Any]:
    """Return sourceable assets grouped across all RWA venues. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_asset_sourcing_matrix",
            "as_of": _utc_now_iso(),
            "filters": {
                "asset_class": asset_class,
                "venue": venue,
                "limit": limit,
                "offset": offset,
            },
            **build_rwa_asset_matrix(
                asset_class=asset_class,
                venue=venue,
                limit=limit,
                offset=offset,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/identity-audit")
async def get_rwa_identity_audit(
    asset_id: str = Query(
        "all",
        max_length=512,
        description="Optional comma-separated exact canonical asset ids",
    ),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum identity rows to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based identity-row offset",
    ),
) -> dict[str, Any]:
    """Return verified ticker identities and taxonomy corrections. FREE."""
    audit = build_rwa_ticker_identity_audit()
    requested_asset_ids = {
        item.strip().upper()
        for item in asset_id.split(",")
        if item.strip()
    }
    matching_rows = [
        row
        for row in audit["rows"]
        if (
            asset_id.strip().lower() == "all"
            or str(row.get("asset_id", "")).upper() in requested_asset_ids
        )
    ]
    rows, pagination = paginate_rows(
        matching_rows,
        limit=limit,
        offset=offset,
    )
    return {
        "status": "ok",
        "product": "rwa_ticker_identity_audit",
        "as_of": _utc_now_iso(),
        "filters": {
            "asset_id": asset_id,
            "limit": limit,
            "offset": offset,
        },
        **audit,
        "summary": {
            **audit["summary"],
            "matching_asset_count": len(matching_rows),
            "returned_asset_count": len(rows),
        },
        "rows": rows,
        "pagination": pagination,
    }


@app.get("/v1/rwa/oracle-parity")
async def get_rwa_oracle_parity() -> dict[str, Any]:
    """Return Pyth/Chainlink-style oracle parity sourcing gaps. FREE."""
    return {
        "status": "ok",
        "product": "rwa_oracle_parity_matrix",
        "as_of": _utc_now_iso(),
        **build_oracle_parity_matrix(),
    }


@app.get("/v1/rwa/dex-venues")
async def get_rwa_dex_venues() -> dict[str, Any]:
    """Return high-quality DEX venue candidates and promotion gates. FREE."""
    return {
        "status": "ok",
        "product": "rwa_dex_venue_quality_plan",
        "as_of": _utc_now_iso(),
        **build_dex_venue_quality_plan(),
    }


@app.get("/v1/rwa/derivative-venues")
async def get_rwa_derivative_venues(
    venue: str = Query(
        "all",
        max_length=96,
        description="Derivative venue filter, e.g. aevo, aster, dydx, orderly, drift",
    ),
    asset_class: str = Query(
        "all",
        max_length=64,
        description="Asset class filter, e.g. crypto, equity, commodity, option, yield_token",
    ),
    status: str = Query(
        "all",
        max_length=96,
        description="Discovery or ingestion status filter, e.g. catalog_fetched, ready_to_probe, blocked_by_auth_or_license",
    ),
    include_market_rows: bool = Query(
        False,
        description="Include inactive/settled and raw market rows from the generated report",
    ),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum rows per derivative collection to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based derivative-row offset",
    ),
) -> dict[str, Any]:
    """Return derivative/perp/futures/yield venue discovery and fair-value methodology. FREE."""
    report = build_derivative_venue_report(
        venue=venue,
        asset_class=asset_class,
        status=status,
        include_market_rows=include_market_rows,
    )
    coverage_rows, coverage_pagination = paginate_rows(
        report["coverage_rows"],
        limit=limit,
        offset=offset,
    )
    matching_coverage_rows = len(report["coverage_rows"])
    pagination: dict[str, Any] = {"coverage_rows": coverage_pagination}
    response = {
        **report,
        "filters": {
            **report["filters"],
            "limit": limit,
            "offset": offset,
        },
        "summary": {
            **report["summary"],
            "matching_coverage_row_count": matching_coverage_rows,
            "returned_coverage_rows": len(coverage_rows),
        },
        "coverage_rows": coverage_rows,
        "pagination": pagination,
    }
    if include_market_rows:
        market_rows, market_pagination = paginate_rows(
            report.get("market_rows", []),
            limit=limit,
            offset=offset,
        )
        response["market_rows"] = market_rows
        response["summary"] = {
            **response["summary"],
            "matching_market_row_count": len(report.get("market_rows", [])),
            "returned_market_rows": len(market_rows),
        }
        pagination["market_rows"] = market_pagination
    return {
        "status": "ok",
        "product": "rwa_derivative_venue_discovery",
        "as_of": _utc_now_iso(),
        **response,
    }


@app.get("/v1/rwa/rwa-xyz-monitor")
async def get_rwa_xyz_monitor(
    include_asset_rows: bool = Query(
        False,
        description="Include normalized RWA.xyz asset/product rows",
    ),
    include_token_rows: bool = Query(
        False,
        description="Include normalized RWA.xyz token contract rows",
    ),
    include_coverage_rows: bool = Query(
        False,
        description="Include aggregator coverage rows derived from the monitor",
    ),
    row_limit: int = Query(
        100,
        ge=0,
        le=1000,
        description="Maximum rows to return for each requested row set",
    ),
) -> dict[str, Any]:
    """Return RWA.xyz New Asset Monitor catalog and source assessment. FREE."""
    return {
        "status": "ok",
        "product": "rwa_xyz_new_asset_monitor",
        "as_of": _utc_now_iso(),
        "filters": {
            "include_asset_rows": include_asset_rows,
            "include_token_rows": include_token_rows,
            "include_coverage_rows": include_coverage_rows,
            "row_limit": row_limit,
        },
        **build_rwa_xyz_monitor_view(
            include_asset_rows=include_asset_rows,
            include_token_rows=include_token_rows,
            include_coverage_rows=include_coverage_rows,
            row_limit=row_limit,
        ),
    }


@app.get("/v1/rwa/daily-feed-agent")
async def get_rwa_daily_feed_agent(
    include_rows: bool = Query(
        False,
        description="Include new asset, token, and sourcing action rows",
    ),
    row_limit: int = Query(
        100,
        ge=0,
        le=1000,
        description="Maximum rows to return for each requested row set",
    ),
) -> dict[str, Any]:
    """Return the latest daily RWA new-feed discovery diff. FREE."""
    return {
        "status": "ok",
        "product": "rwa_daily_feed_discovery_agent",
        "as_of": _utc_now_iso(),
        "filters": {"include_rows": include_rows, "row_limit": row_limit},
        **build_daily_feed_agent_view(include_rows=include_rows, row_limit=row_limit),
    }


@app.get("/v1/rwa/dex-allowlist")
async def get_rwa_dex_allowlist(
    venue: str = Query(
        "all",
        max_length=64,
        description="DEX venue filter, e.g. jupiter_router, meteora_dlmm, uniswap_v3_v4",
    ),
    status: str = Query(
        "all",
        max_length=64,
        description="Allowlist status filter, e.g. planned_adapter, blocked_by_auth_or_rpc",
    ),
) -> dict[str, Any]:
    """Return executable DEX route/pool candidates and promotion jobs. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_dex_allowlist",
            "as_of": _utc_now_iso(),
            **build_dex_allowlist(venue=venue, status=status),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/non-crypto-feeds")
async def get_rwa_non_crypto_feeds(
    exclude_tokenized_stocks: bool = Query(
        True,
        description="Exclude tokenized stock/xStock-style rows by default",
    ),
    asset_class: str = Query(
        "all",
        pattern="^(all|equity|etf|index|fx|commodity|metal|treasury|treasury_fund|tokenized_fund)$",
        description="Optional non-crypto asset class filter",
    ),
    venue: str = Query(
        "all",
        max_length=96,
        description="Optional venue id filter",
    ),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum rows per non-crypto feed collection to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based non-crypto feed-row offset",
    ),
) -> dict[str, Any]:
    """Return generated non-crypto VWAP and bid/ask feed definitions. FREE."""
    try:
        catalog = build_non_crypto_feed_catalog(
            exclude_tokenized_stocks=exclude_tokenized_stocks,
            asset_class=None if asset_class == "all" else asset_class,
            venue=None if venue == "all" else venue,
        )
        vwap_feeds, vwap_pagination = paginate_rows(
            catalog["vwap_feeds"],
            limit=limit,
            offset=offset,
        )
        bidask_feeds, bidask_pagination = paginate_rows(
            catalog["bidask_feeds"],
            limit=limit,
            offset=offset,
        )
        excluded_rows, excluded_pagination = paginate_rows(
            catalog["excluded_rows"],
            limit=limit,
            offset=offset,
        )
        return {
            "status": "ok",
            "product": "rwa_non_crypto_feed_catalog",
            "as_of": _utc_now_iso(),
            **catalog,
            "filters": {
                **catalog["filters"],
                "limit": limit,
                "offset": offset,
            },
            "summary": {
                **catalog["summary"],
                "returned_vwap_feed_count": len(vwap_feeds),
                "returned_bidask_feed_count": len(bidask_feeds),
                "returned_excluded_row_count": len(excluded_rows),
            },
            "vwap_feeds": vwap_feeds,
            "bidask_feeds": bidask_feeds,
            "excluded_rows": excluded_rows,
            "pagination": {
                "vwap_feeds": vwap_pagination,
                "bidask_feeds": bidask_pagination,
                "excluded_rows": excluded_pagination,
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/discovery")
async def get_rwa_discovery(
    exclude_tokenized_stocks: bool = Query(
        False,
        description="Exclude tokenized stock/xStock-style rows from the discovery audit",
    ),
    asset_class: str = Query(
        "all",
        pattern="^(all|equity|etf|index|fx|commodity|metal|treasury|treasury_fund|tokenized_fund)$",
        description="Optional asset class filter",
    ),
    venue: str = Query(
        "all",
        max_length=96,
        description="Optional venue id filter",
    ),
    status: str = Query(
        "all",
        max_length=96,
        description="Optional promotion status filter",
    ),
    include_feed_details: bool = Query(
        True,
        description="Include per-feed gate details",
    ),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum feed-detail rows to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based feed-detail offset",
    ),
) -> dict[str, Any]:
    """Return discovery evidence and promotion blockers for every sourced RWA feed. FREE."""
    try:
        audit = build_feed_discovery_audit(
            exclude_tokenized_stocks=exclude_tokenized_stocks,
            asset_class=asset_class,
            venue=venue,
            status=status,
            include_feed_details=include_feed_details,
        )
        if include_feed_details:
            feeds, pagination = paginate_rows(
                audit["feeds"],
                limit=limit,
                offset=offset,
            )
            audit = {
                **audit,
                "summary": {
                    **audit["summary"],
                    "returned_feed_count": len(feeds),
                },
                "feeds": feeds,
                "pagination": pagination,
            }
        audit["filters"] = {
            **audit["filters"],
            "limit": limit,
            "offset": offset,
        }
        return {
            "status": "ok",
            "product": "rwa_feed_discovery_promotion_audit",
            "as_of": _utc_now_iso(),
            **audit,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/discovery/mitigation-plan")
async def get_rwa_discovery_mitigation_plan(
    exclude_tokenized_stocks: bool = Query(
        False,
        description="Exclude tokenized stock/xStock-style rows from mitigation counts",
    ),
) -> dict[str, Any]:
    """Return a research-backed mitigation plan for RWA discovery blockers. FREE."""
    return {
        "status": "ok",
        "product": "rwa_discovery_mitigation_plan",
        "as_of": _utc_now_iso(),
        **build_discovery_mitigation_plan(
            exclude_tokenized_stocks=exclude_tokenized_stocks,
        ),
    }


@app.get("/v1/rwa/blocker-resolution")
async def get_rwa_blocker_resolution() -> dict[str, Any]:
    """Return the RWA production blocker-resolution ledger. FREE."""
    return {
        "status": "ok",
        "product": "rwa_blocker_resolution_ledger",
        "as_of": _utc_now_iso(),
        **build_blocker_resolution_ledger(),
    }


@app.get("/v1/rwa/source-rights")
async def get_rwa_source_rights(
    venue: str = Query(
        "all",
        max_length=96,
        description="Optional venue id filter",
    ),
    status: str = Query(
        "all",
        max_length=96,
        description="Optional rights status filter",
    ),
) -> dict[str, Any]:
    """Return venue/provider rights-to-source and redistribution readiness. FREE."""
    return {
        "status": "ok",
        "product": "rwa_source_rights_registry",
        "as_of": _utc_now_iso(),
        **build_source_rights_registry(venue=venue, status=status),
    }


@app.get("/v1/rwa/replay-inventory")
async def get_rwa_replay_inventory(
    venue: str = Query(
        "all",
        max_length=96,
        description="Optional DEX venue filter",
    ),
    status: str = Query(
        "all",
        max_length=96,
        description="Optional replay status filter",
    ),
) -> dict[str, Any]:
    """Return route/pool identifiers and replayable payload evidence. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_route_pool_replay_inventory",
            "as_of": _utc_now_iso(),
            **build_route_pool_replay_inventory(venue=venue, status=status),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/equity-universes")
async def get_rwa_equity_universes(
    universe: str = Query(
        "all",
        max_length=64,
        description="Universe id filter, e.g. sp500, china_a_shares, hong_kong_equities, south_korea_equities",
    ),
    region: str = Query(
        "all",
        max_length=64,
        description="Optional region filter, e.g. United States, China, Hong Kong, South Korea",
    ),
) -> dict[str, Any]:
    """Return S&P 500 and APAC equity universe sourceability. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_equity_universe_sourcing_plan",
            "as_of": _utc_now_iso(),
            "filters": {"universe": universe, "region": region},
            **build_equity_universe_sourcing_plan(
                universe=None if universe == "all" else universe,
                region=None if region == "all" else region,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/market-expansion")
async def get_rwa_market_expansion() -> dict[str, Any]:
    """Return expanded RWA/traditional venue and ticker sourcing plan. FREE."""
    return {
        "status": "ok",
        "product": "rwa_market_expansion_plan",
        "as_of": _utc_now_iso(),
        **build_market_expansion_plan(),
    }


@app.get("/v1/rwa/futures-data-plan")
async def get_rwa_futures_data_plan() -> dict[str, Any]:
    """Return futures data and fair-value methodology for RWA replacement coverage. FREE."""
    return {
        "status": "ok",
        "product": "rwa_futures_data_plan",
        "as_of": _utc_now_iso(),
        **build_futures_data_plan(),
    }


@app.get("/v1/rwa/oracle-streams")
async def get_rwa_oracle_streams() -> dict[str, Any]:
    """Return oracle-streamed feed coverage for RWA/traditional assets. FREE."""
    return {
        "status": "ok",
        "product": "rwa_oracle_stream_coverage",
        "as_of": _utc_now_iso(),
        **build_oracle_stream_coverage(),
    }


@app.get("/v1/rwa/provider-catalog")
async def get_rwa_provider_catalog(
    category: str = Query(
        "all",
        max_length=64,
        description="Provider category filter, e.g. tokenized_security, dex_liquidity, licensed_exchange",
    ),
    status: str = Query(
        "all",
        max_length=64,
        description="Ingestion status filter, e.g. ready_to_probe, planned_adapter, blocked_by_auth_or_license",
    ),
) -> dict[str, Any]:
    """Return the provider catalog ingestion roadmap for expanded RWA coverage. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_provider_catalog_ingestion",
            "as_of": _utc_now_iso(),
            "filters": {"category": category, "status": status},
            **build_provider_catalog(category=category, status=status),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/source-readiness")
async def get_rwa_source_readiness(
    category: str = Query(
        "all",
        max_length=64,
        description="Dependency category filter, e.g. dex_liquidity, oracle_reference, licensed_exchange",
    ),
    status: str = Query(
        "all",
        max_length=64,
        description="Readiness status filter, e.g. missing_identifier_mapping, blocked_by_license_or_contract",
    ),
) -> dict[str, Any]:
    """Return credential, identifier, licensing, and ops dependency readiness. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_source_readiness",
            "as_of": _utc_now_iso(),
            "filters": {"category": category, "status": status},
            **build_source_readiness(category=category, status=status),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/blocksize-state-methodology")
async def get_rwa_blocksize_state_methodology() -> dict[str, Any]:
    """Return the Blocksize state reference methodology for RWA consensus. FREE."""
    methodology = build_blocksize_state_methodology()
    return {
        "status": "ok",
        "product": "rwa_blocksize_state_methodology",
        "as_of": _utc_now_iso(),
        "methodology": methodology,
        "usage": {
            "benchmark": {
                "endpoint": "/v1/rwa/benchmark/blocksize",
                "required_observation_fields": ["symbol", "value", "timestamp"],
                "recommended_fields": [
                    "venue",
                    "provider",
                    "source_type",
                    "benchmark_service",
                    "benchmark_symbol",
                ],
                "example": methodology["observation_shape"],
            },
            "consensus": {
                "endpoint": "/v1/rwa/consensus/calculate",
                "source_type": methodology["source_type"],
                "venue": methodology["venue"],
                "role": "supplemental_reference",
            },
        },
        "thresholds": {
            "benchmark_drift_bps": QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"],
            "freshness_ms": QUALITY_ALIGNMENT["thresholds"]["max_age_ms"],
        },
    }


@app.get("/v1/rwa/consensus/sources")
async def get_rwa_consensus_sources(
    exclude_tokenized_stocks: bool = Query(
        False,
        description="Exclude tokenized stock/xStock-style rows from primary market feed counts",
    ),
) -> dict[str, Any]:
    """Return all source layers needed for RWA consensus metrics. FREE."""
    return {
        "status": "ok",
        "product": "rwa_consensus_source_plan",
        "as_of": _utc_now_iso(),
        **build_consensus_source_plan(exclude_tokenized_stocks=exclude_tokenized_stocks),
    }


@app.post("/v1/rwa/consensus/calculate")
async def calculate_rwa_consensus(payload: dict[str, Any]) -> dict[str, Any]:
    """Calculate a quality-weighted RWA consensus metric from submitted observations. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_consensus_metric",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_weighted_consensus_v1",
                "steps": [
                    "Normalize submitted source observations into one price value per row.",
                    "Apply real-time timestamp, spread/depth, confidence, benchmark drift, and source-family gates.",
                    "Use MAD to exclude cross-source outliers.",
                    "Calculate a quality-weighted consensus value and basis for each source.",
                    "Keep supplemental oracle, futures, NAV, issuer, and proof rows visibly labeled.",
                ],
            },
            "result": calculate_consensus_metric(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/registry")
async def get_rwa_registry(
    symbol: str | None = Query(None, max_length=96, description="Optional symbol or alias, e.g. AAPL, AAPLx/USD, EURUSD"),
    venue: str | None = Query(None, max_length=96, description="Optional venue alias, e.g. Meteora DLMM, uniswap, jupiter"),
    include_aliases: bool = Query(False, description="Include generated symbol and venue aliases"),
    limit: int = Query(
        RWA_ASSET_MATRIX_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum canonical assets to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Canonical asset offset in deterministic registry order",
    ),
) -> dict[str, Any]:
    """Return canonical RWA assets and venue coverage with alias handling. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_canonical_registry",
            "as_of": _utc_now_iso(),
            "filters": {
                "symbol": symbol,
                "venue": venue,
                "include_aliases": include_aliases,
                "limit": limit,
                "offset": offset,
            },
            **build_rwa_registry_overview(
                symbol=symbol,
                venue=venue,
                include_aliases=include_aliases,
                limit=limit,
                offset=offset,
                include_venue_instruments=False,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/resolve")
async def resolve_rwa_symbol_endpoint(
    symbol: str = Query(..., max_length=96, description="Symbol or alias to resolve"),
    venue: str | None = Query(None, max_length=96, description="Optional venue alias"),
) -> dict[str, Any]:
    """Resolve any supported naming convention into canonical RWA coverage. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_symbol_resolution",
            "as_of": _utc_now_iso(),
            **resolve_rwa_symbol(symbol, venue=venue),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/registry/venues")
async def get_rwa_registry_venues(
    venue: str | None = Query(None, max_length=96, description="Optional venue alias"),
    asset_id: str | None = Query(
        None,
        max_length=256,
        description="Optional canonical asset_id returned by the RWA registry",
    ),
    include_aliases: bool = Query(False, description="Include generated venue aliases"),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum venue-instrument rows to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Venue-instrument offset in deterministic registry order",
    ),
) -> dict[str, Any]:
    """Return what each venue covers and how it should be interpreted. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_venue_coverage_registry",
            "as_of": _utc_now_iso(),
            **build_rwa_venue_registry_page(
                venue=venue,
                asset_id=asset_id,
                include_aliases=include_aliases,
                limit=limit,
                offset=offset,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/sourcing/jobs")
async def get_rwa_sourcing_jobs(
    include_completed_targets: bool = Query(
        False,
        description="Include jobs for targets already marked covered",
    ),
    venue: str = Query(
        "all",
        max_length=96,
        description="Optional exact venue id filter",
    ),
    status: str = Query(
        "all",
        max_length=96,
        description="Optional exact sourcing status filter",
    ),
    category: str = Query(
        "all",
        max_length=128,
        description="Optional exact sourcing category filter",
    ),
    limit: int = Query(
        RWA_COLLECTION_DEFAULT_LIMIT,
        ge=1,
        le=RWA_COLLECTION_MAX_LIMIT,
        description="Maximum sourcing jobs to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Zero-based sourcing-job offset",
    ),
) -> dict[str, Any]:
    """Return per-symbol sourcing jobs needed for oracle-parity coverage. FREE."""
    sourcing = build_sourcing_jobs(
        include_completed_targets=include_completed_targets,
    )
    venue_filter = venue.strip().lower()
    status_filter = status.strip().lower()
    category_filter = category.strip().lower()
    matching_jobs = [
        job
        for job in sourcing["jobs"]
        if (
            venue_filter == "all"
            or str(job.get("venue", "")).lower() == venue_filter
        )
        and (
            status_filter == "all"
            or str(job.get("status", "")).lower() == status_filter
        )
        and (
            category_filter == "all"
            or str(job.get("category", "")).lower() == category_filter
        )
    ]
    jobs, pagination = paginate_rows(
        matching_jobs,
        limit=limit,
        offset=offset,
    )
    return {
        "status": "ok",
        "product": "rwa_sourcing_jobs",
        "as_of": _utc_now_iso(),
        "filters": {
            "include_completed_targets": include_completed_targets,
            "venue": venue,
            "status": status,
            "category": category,
            "limit": limit,
            "offset": offset,
        },
        **sourcing,
        "summary": {
            **sourcing["summary"],
            "matching_job_count": len(matching_jobs),
            "returned_job_count": len(jobs),
        },
        "jobs": jobs,
        "pagination": pagination,
    }


@app.post("/v1/rwa/sourcing/probe")
async def probe_rwa_sourcing_jobs(request: Request) -> dict[str, Any]:
    """Execute bounded ready-to-probe RWA sourcing jobs for an authenticated operator."""
    require_rwa_operator(request, require_mutations=True)
    try:
        payload = RWAProbeRequest.model_validate(await request.json()).as_probe_payload()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "RWA_PROBE_REQUEST_INVALID",
                "message": "The RWA probe request did not satisfy the bounded schema.",
            },
        ) from None
    registry = getattr(request.app.state, "rwa_adapter_registry", RWA_ADAPTER_REGISTRY)
    store = _rwa_observation_store(request) if payload.get("persist") else None
    try:
        result = await probe_sourcing_jobs(payload, registry=registry, store=store)
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail={
                "error_code": "RWA_PROBE_TIMEOUT",
                "message": "The bounded RWA probe exceeded its time limit.",
            },
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "RWA_PROBE_RESULT_INVALID",
                "message": "The probe result did not satisfy the evidence contract.",
            },
        ) from None
    return {
        "status": "ok",
        "product": "rwa_sourcing_probe",
        "as_of": _utc_now_iso(),
        "methodology": {
            "type": "rwa_sourcing_probe_v2",
            "steps": [
                "Authenticate an operator and enforce request, concurrency, and time ceilings.",
                "Select ready_to_probe jobs from the oracle-parity sourcing queue.",
                "Fetch normalized bid/ask and optional bounded L2 depth.",
                "Run the real-time quality gate over fetched observations.",
                "Commit persisted observations atomically when persist is explicitly true.",
            ],
        },
        **result,
    }


@app.post("/v1/rwa/vwap/calculate")
async def calculate_rwa_vwap(payload: dict[str, Any]) -> dict[str, Any]:
    """Calculate block-size RWA VWAP from normalized venue depth. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_block_vwap_calculation",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_block_vwap_v1",
                "steps": [
                    "Normalize venue order-book levels into price and USD notional depth.",
                    "Sort asks ascending for buys or bids descending for sells.",
                    "Walk depth until the requested block size is filled or venue depth is exhausted.",
                    "Return fillable_notional_usd and partial_fill status instead of extrapolating.",
                    "Score quality using source type, freshness, fill ratio, and optional benchmark drift.",
                ],
            },
            "result": calculate_block_vwap(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/rwa/bidask/calculate")
async def calculate_rwa_bidask(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize and score an RWA bid/ask observation. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_bidask_calculation",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_bidask_v1",
                "steps": [
                    "Normalize bid and ask into bid, ask, mid, spread, and spread_bps.",
                    "Apply source-type, freshness, spread, and optional benchmark-drift gates.",
                    "Keep source_type explicit so native L1, synthetic L1, quotes, and NAV are not blended blindly.",
                ],
            },
            "result": calculate_bidask(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/rwa/quality/check")
async def check_rwa_quality(payload: dict[str, Any]) -> dict[str, Any]:
    """Run RWA cross-venue outlier and quality checks. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_quality_check",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_quality_check_v1",
                "steps": [
                    "Compute median and median absolute deviation across venue observations.",
                    "Flag large robust-z outliers.",
                    "Apply optional benchmark drift gates against Blocksize/vendor reference prices.",
                    "Exclude stale, severe benchmark-drift, and MAD-outlier observations from consolidated value.",
                ],
            },
            "result": detect_outliers(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/feeds")
async def get_rwa_feeds() -> dict[str, Any]:
    """Return RWA adapter registry, readiness, and expansion todos. FREE."""
    return {
        "status": "ok",
        "product": "rwa_feed_registry",
        "as_of": _utc_now_iso(),
        **build_aggregator_status(),
    }


@app.post("/v1/rwa/feeds/promotion-check")
async def check_rwa_feed_promotion(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether a feed can be promoted to a stronger trust tier. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_feed_promotion_check",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_feed_promotion_gate_v1",
                "steps": [
                    "Check backtest window, uptime, sample size, excluded-observation rate, and benchmark drift.",
                    "Require legal approval, locked source-type labeling, and replayable receipts.",
                    "Return promote only when every gate passes.",
                ],
            },
            "result": evaluate_feed_promotion(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/rwa/aggregate")
async def aggregate_rwa_observations(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate normalized RWA observations into a quality-gated value. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_market_data_aggregation",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_aggregator_v1",
                "steps": [
                    "Normalize submitted bid/ask and order-book observations.",
                    "Calculate per-venue mid prices and block-size VWAPs.",
                    "Run cross-observation MAD and benchmark-drift checks.",
                    "Return consolidated value from included observations only.",
                ],
            },
            "result": aggregate_submitted_observations(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/rwa/realtime/requirements")
async def get_rwa_realtime_requirements() -> dict[str, Any]:
    """Return real-time freshness and cadence requirements. FREE."""
    return {
        "status": "ok",
        "product": "rwa_realtime_quality_requirements",
        "as_of": _utc_now_iso(),
        **build_realtime_requirements(),
    }


@app.post("/v1/rwa/realtime/quality")
async def check_rwa_realtime_quality(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether submitted observations are real-time usable. FREE."""
    try:
        return {
            "status": "ok",
            "product": "rwa_realtime_quality_check",
            "as_of": _utc_now_iso(),
            "methodology": {
                "type": "rwa_realtime_quality_v1",
                "steps": [
                    "Check every observation has a source timestamp.",
                    "Compare age against the stricter of asset-class and venue freshness thresholds.",
                    "Compare observed tick interval against venue cadence requirements.",
                    "Block reference/NAV observations from tick-by-tick real-time consolidation.",
                ],
            },
            "result": evaluate_realtime_quality(payload),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/v1/rwa/observations/store")
async def store_rwa_observation(request: Request) -> dict[str, Any]:
    """Store one bounded observation for an authenticated RWA operator."""
    require_rwa_operator(request, require_mutations=True)
    try:
        payload = RWAObservationEnvelope.model_validate(
            await request.json()
        ).as_store_payload()
        record = await _store_rwa_observation_without_blocking(
            _rwa_observation_store(request),
            payload,
        )
        return {
            "status": "ok",
            "product": "rwa_observation_store",
            "as_of": _utc_now_iso(),
            "record": record,
        }
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "RWA_OBSERVATION_INVALID",
                "message": "The observation did not satisfy the bounded evidence schema.",
            },
        ) from None
    except sqlite3.Error:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "RWA_OBSERVATION_STORE_BUSY",
                "message": "The RWA evidence store is temporarily unavailable.",
            },
        ) from None


@app.get("/v1/rwa/observations")
async def list_rwa_observations(
    request: Request,
    symbol: str | None = Query(None, max_length=64, description="Optional symbol filter"),
    venue: str | None = Query(None, max_length=64, description="Optional venue filter"),
    limit: int = Query(50, ge=1, le=200, description="Maximum observations to return"),
) -> dict[str, Any]:
    """List bounded RWA evidence summaries for an authenticated operator."""
    require_rwa_operator(request, require_mutations=False)
    return {
        "status": "ok",
        "product": "rwa_observation_ledger",
        "as_of": _utc_now_iso(),
        "filters": {"symbol": symbol, "venue": venue, "limit": limit},
        "observations": _rwa_observation_store(request).list_observations(
            symbol=symbol,
            venue=venue,
            limit=limit,
        ),
    }


@app.get("/v1/rwa/observations/summary")
async def summarize_rwa_observations(request: Request) -> dict[str, Any]:
    """Return compact RWA observation persistence stats. FREE."""
    return {
        "status": "ok",
        "product": "rwa_observation_ledger_summary",
        "as_of": _utc_now_iso(),
        "summary": _rwa_observation_store(request).summary(),
    }


@app.post("/v1/rwa/benchmark/blocksize", responses=X402_RESPONSE)
async def benchmark_rwa_against_blocksize(
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Benchmark sourced RWA observations without mutating operator evidence."""
    import asyncio

    if payload.get("persist"):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "RWA_PUBLIC_PERSISTENCE_FORBIDDEN",
                "message": "The public benchmark endpoint is stateless.",
            },
        )
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise HTTPException(status_code=400, detail="observations must include at least one row")
    if len(observations) > 20:
        raise HTTPException(status_code=400, detail="Benchmark supports up to 20 observations")

    client: BlocksizeClient = request.app.state.blocksize
    stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)

    async def benchmark_one(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"status": "error", "error_code": "INVALID_OBSERVATION"}
        try:
            resolved = resolve_blocksize_benchmark(raw)
            snapshot = await _fetch_service_snapshot(
                client,
                service=resolved["service"],
                symbol=resolved["symbol"],
                stream_cache=stream_cache,
            )
            comparison = compare_observation_to_blocksize(raw, snapshot)
            return {
                "status": "ok",
                "resolved_benchmark": resolved,
                **comparison,
            }
        except ValueError as exc:
            return {
                "status": "unsupported",
                "error_code": "UNSUPPORTED_BENCHMARK",
                "message": str(exc),
                "observation": raw,
            }
        except BlocksizeAPIError as exc:
            return {
                "status": "unavailable",
                "error_code": "BLOCKSIZE_ERROR",
                "message": str(exc),
                "resolved_benchmark": (
                    resolve_blocksize_benchmark(raw)
                    if isinstance(raw, dict)
                    else None
                ),
                "observation": raw,
            }

    results = await asyncio.gather(*(benchmark_one(item) for item in observations))
    ok_results = [item for item in results if item.get("status") == "ok"]
    decisions = [item["decision"] for item in ok_results]
    summary_decision = (
        "exclude"
        if "exclude" in decisions
        else "warn"
        if "warn" in decisions
        else "pass"
        if ok_results
        else "unavailable"
    )
    response_core = {
        "methodology": {
            "type": "rwa_blocksize_benchmark_v1",
            "steps": [
                "Resolve each RWA observation to the closest Blocksize benchmark service.",
                "Fetch one live Blocksize snapshot per observation through the existing Blocksize API client.",
                "Compare sourced observation value against the live Blocksize value in basis points.",
                "Return pass, warn, or exclude based on configured benchmark drift thresholds.",
                "Keep unsupported or unavailable benchmarks explicit instead of fabricating comparisons.",
            ],
            "thresholds": QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"],
        },
        "summary": {
            "decision": summary_decision,
            "observations": len(observations),
            "benchmarked": len(ok_results),
            "unavailable": len([item for item in results if item.get("status") == "unavailable"]),
            "unsupported": len([item for item in results if item.get("status") == "unsupported"]),
        },
        "benchmarks": results,
    }
    source_endpoints = [
        str(item["benchmark"]["endpoint"])
        for item in ok_results
        if isinstance(item.get("benchmark"), dict) and item["benchmark"].get("endpoint")
    ]
    receipt = _response_receipt(
        request,
        product="rwa_blocksize_benchmark",
        subject=",".join(str(item.get("symbol") or "") for item in observations if isinstance(item, dict)),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=sorted(set(source_endpoints)),
    )
    return {
        "status": "ok",
        "product": "rwa_blocksize_benchmark",
        "credit_cost": CREDIT_COSTS["rwa_blocksize_benchmark"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.get("/v1/cache/status")
async def get_cache_status(request: Request) -> dict[str, Any]:
    """Report stream-cache readiness for websocket-backed feeds. FREE."""
    stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
    status = stream_cache.status() if stream_cache else {"enabled": False, "ready": False}
    return {
        "status": "ok",
        "cache": status,
        "feeds": {
            "vwap24h": {
                "source": "fixedvwap_subscribe",
                "http_product": "/v1/vwap24h/{pair}",
                "ready": bool(status.get("enabled") and status.get("cached_24h_vwap", 0) > 0),
            },
            "state": {
                "source": "state_subscribe plus state_instruments/state_pool fallback",
                "http_product": "/v1/state/{pair}",
                "ready": bool(
                    status.get("enabled")
                    and (
                        status.get("cached_state", 0) > 0
                        or status.get("state_mode") == "configured"
                    )
                ),
            },
            "vwap30m": {
                "source": "closingprice_list",
                "http_product": "/v1/vwap30m/{pair}",
                "ready": True,
            },
        },
        "links": {
            "capability_check": "/v1/capabilities/check",
            "docs": "/docs",
        },
    }


# ---------------------------------------------------------------------------
# Credit Management
# ---------------------------------------------------------------------------

def _require_unverified_http_credits_enabled() -> None:
    """Expose legacy wallet-credit QA routes only outside production."""
    if (
        security_configuration_status()["production"]
        or not settings.server.unverified_http_credits_enabled
    ):
        raise HTTPException(status_code=404, detail="Not found")

@app.get("/v1/credits/balance/{wallet}", include_in_schema=False)
async def get_credit_balance(request: Request, wallet: str):
    """Local-QA-only view of a legacy unverified wallet credit balance."""
    _require_unverified_http_credits_enabled()
    mgr: CreditManager = request.app.state.credits
    balance = mgr.get_balance(wallet)
    return {
        "wallet": wallet,
        "balance_credits": balance,
        "credit_unit": "Blocksize service credit",
        "starter_allowance": {
            "positioning": "Legacy local-QA wallets may receive up to 50 test credits.",
            "eligibility": "local_qa_only",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "not_free_forever": True,
        },
        "upgrade_path": "Production direct HTTP uses signed x402; contact sales for an authenticated account plan.",
    }

@app.post("/v1/credits/purchase", include_in_schema=False)
async def purchase_credits_challenge(
    request: Request,
    tier: str = Query(..., pattern="^(starter|pro|institutional)$"),
):
    """
    Local-QA-only legacy challenge for unverified wallet credits.
    Tiers: starter ($0.90), pro ($8.00), institutional ($60.00)
    """
    _require_unverified_http_credits_enabled()
    tier_data = BULK_TIERS.get(tier)
    price = Decimal(str(tier_data["price"]))

    # Return 402 challenge
    requirements = _facilitator_supported_requirements(
        settings.payment_requirements(price),
        getattr(request.app.state, "facilitator_support", None),
    )
    if not requirements:
        raise HTTPException(status_code=503, detail="No operational payment rail")
    payment_required = _x402_payment_required(request, requirements)
    payload = _encode_payment_required(payment_required)
    _record_product_event(
        "bulk_credit_challenge",
        request,
        price_usdc=price,
        metadata={"tier": tier, "credits": tier_data["credits"]},
    )

    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": payload},
        content={
            **payment_required,
            "error": "Payment Required",
            "message": f"Purchase {tier_data['credits']} credits for ${price} USDC.",
            "tier": tier,
            "credits_to_add": tier_data["credits"],
            "legacy_requirements": requirements,
        }
    )

@app.post("/v1/credits/claim", include_in_schema=False)
async def claim_credits(request: Request, payload: dict):
    """Local-QA-only legacy proof claim for an unverified wallet balance."""
    _require_unverified_http_credits_enabled()
    mgr: CreditManager = request.app.state.credits
    tx_hash = payload.get("proof")
    network = payload.get("network", "solana")
    tier = payload.get("tier")
    wallet = payload.get("wallet") # The address to credit

    if not all([tx_hash, tier, wallet]):
        _record_product_event("bulk_credit_claim_failed", request, reason="missing_fields")
        raise HTTPException(status_code=400, detail="Missing tx_hash, tier, or wallet")
    wallet = str(wallet).strip()
    if not WALLET_ID_RE.fullmatch(wallet):
        _record_product_event("bulk_credit_claim_failed", request, reason="invalid_wallet")
        raise HTTPException(status_code=400, detail="Invalid wallet")

    tier_data = BULK_TIERS.get(tier)
    if not tier_data:
        _record_product_event("bulk_credit_claim_failed", request, reason="invalid_tier")
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Native RPC verification of the bulk payment
    payment_reqs = settings.payment_requirements(Decimal(str(tier_data["price"])))
    payment_header = base64.b64encode(json.dumps({
        "proof": tx_hash,
        "network": network
    }).encode()).decode()
    verification = await _verify_payment(
        payment_header,
        payment_reqs,
        mgr,
        purpose=f"credits:{tier}",
        attempt_id=secrets.token_hex(16),
    )

    if not verification.get("valid"):
        _record_product_event(
            "bulk_credit_claim_failed",
            request,
            price_usdc=tier_data["price"],
            network=str(network),
            reason=str(verification.get("reason", "unknown")),
            metadata={"tier": tier},
        )
        raise HTTPException(status_code=402, detail=f"Bulk payment verification failed: {verification.get('reason')}")
    
    settlement = await _settle_payment(payment_header, payment_reqs, verification)
    if settlement.get("success") is not True:
        raise HTTPException(status_code=502, detail="Bulk payment settlement failed")
    payment_id = str(verification.get("payment_id") or "")
    reservation_id = str(verification.get("reservation_id") or "")
    if not mgr.finalize_payment_and_add_credits(
        payment_id=payment_id,
        reservation_id=reservation_id,
        address=wallet,
        credits=tier_data["credits"],
        amount_usdc=tier_data["price"],
        settlement=settlement,
    ):
        raise HTTPException(status_code=409, detail="Bulk payment was already claimed")
    _record_product_event(
        "bulk_credit_claimed",
        request,
        price_usdc=tier_data["price"],
        network=str(network),
        wallet_hash=_wallet_hash(wallet),
        metadata={
            "tier": tier,
            "credits_added": tier_data["credits"],
            "proof_hash": fingerprint(payment_id),
        },
    )
    
    return {
        "status": "success",
        "added": tier_data["credits"],
        "new_balance": mgr.get_balance(wallet),
        "message": f"Successfully credited {tier_data['credits']} to {wallet}"
    }

# ---------------------------------------------------------------------------
# Discovery & MCP
# ---------------------------------------------------------------------------

@app.head("/mcp/manifest.json", include_in_schema=False)
@app.get("/mcp/manifest.json")
async def mcp_manifest():
    """
    Model Context Protocol (MCP) Manifest.
    Provides listing metadata for the public remote discovery server.
    """
    manifest_tools: list[dict[str, object]] = []
    for tool in await public_mcp.list_tools():
        annotations = (
            tool.annotations.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            if tool.annotations is not None
            else {}
        )
        manifest_tool: dict[str, object] = {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters,
            "payment": {"required": False},
            "annotations": annotations,
        }
        if tool.title:
            manifest_tool["title"] = tool.title
        manifest_tools.append(manifest_tool)

    manifest: dict[str, object] = {
        "mcp_version": "1.0",
        "name": PUBLIC_DISPLAY_NAME,
        "description": PUBLIC_DESCRIPTION,
        "version": APP_VERSION,
        "transport": {
            "type": "streamable-http",
            "url": REMOTE_MCP_URL,
        },
        "capabilities": {
            "discovery_modes": ["instrument-search", "equity-search", "pricing-inspection", "document-search"],
            "paid_api_modes": [
                "real-time-x402",
                "authenticated-connector-credit-drawdown",
            ],
            "public_remote_server": "read-only and listing-safe",
            "ai_reader_brief": LLMS_TXT_URL,
            "sitemap": SITEMAP_URL,
            "data_package_catalog": DATA_PACKAGES_JSON_URL,
            "category_hubs": CATEGORY_HUBS_JSON_URL,
            "instrument_explorer": INSTRUMENT_EXPLORER_URL,
            "starter_allowance": "Authenticated connectors receive up to 50 live data credits; direct public HTTP uses signed x402.",
            "equities": "Supported stock tickers are discoverable with asset_class=equity and fetched through /v1/bidask/{ticker}.",
        },
        "links": {
            "homepage": PUBLIC_BASE_URL,
            "openapi": OPENAPI_URL,
            "swagger": SWAGGER_URL,
            "robots": ROBOTS_URL,
            "sitemap": SITEMAP_URL,
            "llms_txt": LLMS_TXT_URL,
            "data_packages_json": DATA_PACKAGES_JSON_URL,
            "category_hubs_json": CATEGORY_HUBS_JSON_URL,
            "instrument_explorer": INSTRUMENT_EXPLORER_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "agent_manual": AGENT_MANUAL_URL,
            "pricing_guide": PRICING_GUIDE_URL,
            "data_catalog": DATA_CATALOG_URL,
            "user_flow": USER_FLOW_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
        },
        "tools": manifest_tools,
        "paid_api": {
            "openapi_url": OPENAPI_URL,
            "swagger_url": SWAGGER_URL,
            "payment_model": "direct x402 or authenticated connector starter credits",
            "starter_allowance": {
                "positioning": "Authenticated connectors receive up to 50 live data credits.",
                "eligibility": "authenticated_connector_only",
                "allowance_credits": STARTER_CREDIT_ALLOWANCE,
                "applies_to": [
                    "raw_vwap",
                    "bid_ask",
                    "equity_bid_ask",
                    "fx",
                    "metals",
                    "batch",
                    "market_briefs",
                    "pre_trade_checks",
                    "audit_receipts",
                    "macro_snapshots",
                    "token_quality_indicators",
                    "state_divergence_indicators",
                    "solana_token_briefs",
                    "trader_alpha_packs",
                    "provenance",
                ],
                "direct_public_http": "Signed x402 payment is required per live-data request.",
                "upgrade_path": "Contact sales for sustained access through an authenticated account plan.",
            },
        },
    }
    if REPOSITORY_URL:
        links = manifest["links"]
        if isinstance(links, dict):
            links["repository"] = REPOSITORY_URL
    return manifest


# ---------------------------------------------------------------------------


def _observability_authorized(request: Request) -> bool:
    expected = dashboard_token()
    if not expected:
        return False
    header_token = request.headers.get("x-observability-token", "")
    cookie_token = request.cookies.get("observability_token", "")
    auth_header = request.headers.get("authorization", "")
    bearer_token = ""
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip()
    return any(
        secrets.compare_digest(expected, token)
        for token in (header_token, bearer_token, cookie_token)
        if token
    )


def _observability_unauthorized() -> JSONResponse:
    if not dashboard_token():
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={
                "error": "Observability authentication unavailable",
                "message": (
                    "Configure a strong OBSERVABILITY_DASHBOARD_TOKEN before "
                    "using internal observability endpoints."
                ),
            },
        )
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        content={
            "error": "Unauthorized",
            "message": "Set OBSERVABILITY_DASHBOARD_TOKEN and provide it as a bearer token.",
        },
    )


def _with_external_observability_context(summary: dict[str, Any]) -> dict[str, Any]:
    """Add marketplace context that is not stored in the local event database."""
    registry_sources = summary.get("registry_source_mix")
    if not isinstance(registry_sources, dict):
        registry_sources = {}
    overview = summary.get("overview")
    if not isinstance(overview, dict):
        overview = {}
    marketplace_metrics = summary.get("marketplace_metrics")
    if not isinstance(marketplace_metrics, dict):
        marketplace_metrics = {}
    latest_external_metrics = marketplace_metrics.get("latest_by_platform")
    if not isinstance(latest_external_metrics, dict):
        latest_external_metrics = {}

    platforms = []
    total_platform_calls = 0
    for platform in DISTRIBUTION_PLATFORMS:
        platform_id = str(platform["id"])
        source_label = str(platform.get("source_label") or "")
        secondary_label = str(platform.get("secondary_source_label") or "")
        local_calls = int(registry_sources.get(source_label) or 0)
        if secondary_label:
            local_calls += int(registry_sources.get(secondary_label) or 0)
        total_platform_calls += local_calls
        metrics_env = (
            PAY_SH_METRICS_API_URL
            if platform_id == "pay_sh"
            else SMITHERY_METRICS_API_URL
            if platform_id == "smithery"
            else ""
        )
        metrics_env = MARKETPLACE_METRICS_FEEDS.get(platform_id, metrics_env)
        latest_metrics = latest_external_metrics.get(platform_id)
        external_metrics_configured = bool(metrics_env or latest_metrics)
        external_metrics_state = (
            "ingested"
            if external_metrics_configured
            else "unavailable_no_reviewed_feed_or_export"
        )
        external_metrics_reason = (
            "A reviewed feed or imported marketplace snapshot is available."
            if external_metrics_configured
            else (
                "No reviewed marketplace API, export, or hosted-call log is configured; "
                "local server traffic cannot prove upstream views, installs, or hosted calls."
            )
        )
        platforms.append(
            {
                **platform,
                "local_recorded_calls": local_calls,
                "external_metrics_configured": external_metrics_configured,
                "external_metrics_state": external_metrics_state,
                "external_metrics_reason": external_metrics_reason,
                "metrics_owner": "Growth engineering",
                "last_metrics_review_date": summary.get("generated_at"),
                "metrics_api_url": metrics_env or None,
                "latest_external_metrics": latest_metrics,
                "status": (
                    "configured"
                    if external_metrics_configured
                    else "watching"
                    if local_calls
                    else "no_local_activity"
                ),
            }
        )

    summary["external_sources"] = {
        "platforms": platforms,
        "platform_count": len(platforms),
        "local_recorded_platform_calls": total_platform_calls,
        "smithery": {
            "name": "Smithery",
            "listing_url": SMITHERY_LISTING_URL,
            "performance_url": SMITHERY_LISTING_URL,
            "hosted_mcp_endpoint": SMITHERY_HOSTED_MCP_ENDPOINT,
            "local_recorded_registry_calls": int(registry_sources.get("Smithery") or 0),
            "local_recorded_mcp_tool_calls": 0,
            "all_recorded_mcp_tool_calls": int(overview.get("mcp_tool_calls") or 0),
            "metrics_ingestion_configured": bool(
                SMITHERY_METRICS_API_URL or latest_external_metrics.get("smithery")
            ),
            "metrics_api_url": MARKETPLACE_METRICS_FEEDS.get("smithery")
            or SMITHERY_METRICS_API_URL
            or None,
            "latest_external_metrics": latest_external_metrics.get("smithery"),
            "status": (
                "configured"
                if SMITHERY_METRICS_API_URL or latest_external_metrics.get("smithery")
                else "not_ingested"
            ),
            "note": (
                "Smithery marketplace performance is only shown here when a metrics feed "
                "or hosted endpoint logs are wired into this observability database."
            ),
        },
        "pay_sh": {
            "name": "Pay.sh / pay-skills",
            "listing_url": PAY_SH_SERVICE_URL,
            "local_recorded_calls": int(registry_sources.get("Pay.sh") or 0),
            "metrics_ingestion_configured": bool(
                PAY_SH_METRICS_API_URL or latest_external_metrics.get("pay_sh")
            ),
            "metrics_api_url": MARKETPLACE_METRICS_FEEDS.get("pay_sh")
            or PAY_SH_METRICS_API_URL
            or None,
            "latest_external_metrics": latest_external_metrics.get("pay_sh"),
            "status": (
                "configured"
                if PAY_SH_METRICS_API_URL or latest_external_metrics.get("pay_sh")
                else "not_ingested"
            ),
            "note": (
                "Pay.sh catalog performance and pay-skills marketplace activity are only "
                "shown here when a metrics feed, referral, or hosted logs reach this observability database."
            ),
        },
    }
    return summary


def _brief_ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)


def _brief_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{round(value * 100, 1)}%"


def _top_popularity_row(rows: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    ranked = [row for row in rows if int(row.get(field) or 0) > 0]
    if not ranked:
        return None
    return max(ranked, key=lambda row: (int(row.get(field) or 0), str(row.get("service") or "")))


def _build_revenue_reconciliation(summary: dict[str, Any]) -> dict[str, Any]:
    """Classify wallet movement without promoting legacy evidence to revenue."""
    wallet_inflows = summary.get("wallet_inflows")
    wallet_inflows = wallet_inflows if isinstance(wallet_inflows, dict) else {}
    overview = summary.get("overview")
    overview = overview if isinstance(overview, dict) else {}
    payment_funnel = summary.get("payment_funnel")
    payment_funnel = payment_funnel if isinstance(payment_funnel, dict) else {}
    event_counts = summary.get("event_counts")
    event_counts = event_counts if isinstance(event_counts, dict) else {}
    rows = wallet_inflows.get("rows")
    rows = rows if isinstance(rows, list) else []

    direct_rows = [row for row in rows if row.get("kind") == "direct_x402"]
    topup_rows = [row for row in rows if row.get("kind") == "credit_topup"]
    direct_total = round(
        sum(float(row.get("amount_usdc") or 0.0) for row in direct_rows), 6
    )
    topup_total = round(
        sum(float(row.get("amount_usdc") or 0.0) for row in topup_rows), 6
    )
    recognized_revenue = round(
        float(overview.get("estimated_revenue_usdc") or 0.0), 6
    )
    settled_attempts = int(payment_funnel.get("settled_attempts") or 0)
    correlated_x402_deliveries = int(payment_funnel.get("x402_deliveries") or 0)
    legacy_delivery_events = max(
        int(event_counts.get("data_delivered") or 0) - correlated_x402_deliveries,
        0,
    )
    recognized_direct_count = min(
        len(direct_rows), max(settled_attempts, correlated_x402_deliveries)
    )
    legacy_direct_count = max(len(direct_rows) - recognized_direct_count, 0)
    legacy_direct_amount = round(max(direct_total - recognized_revenue, 0.0), 6)
    rows_with_transaction_evidence = sum(
        1 for row in rows if str(row.get("tx_hash") or "").strip()
    )
    transaction_evidence_complete = rows_with_transaction_evidence == len(rows)
    status = "reconciled"
    if legacy_direct_count or legacy_direct_amount:
        status = "classified_pending_lifecycle_backfill"
    if not transaction_evidence_complete:
        status = "unclassified_inflows_present"
    return {
        "status": status,
        "window_days": summary.get("window_days"),
        "total_wallet_inflow_usdc": round(direct_total + topup_total, 6),
        "decision_grade_recognized_revenue_usdc": recognized_revenue,
        "legacy_transaction_backed_x402_usdc": legacy_direct_amount,
        "legacy_transaction_backed_x402_count": legacy_direct_count,
        "legacy_delivery_events": legacy_delivery_events,
        "local_qa_topup_usdc": topup_total,
        "local_qa_topup_count": len(topup_rows),
        "rows_with_transaction_evidence": rows_with_transaction_evidence,
        "total_inflow_rows": len(rows),
        "transaction_evidence_complete": transaction_evidence_complete,
        "classification_rows": [
            {
                "classification": "decision_grade_recognized_x402",
                "count": recognized_direct_count,
                "amount_usdc": recognized_revenue,
                "revenue_status": "recognized",
                "next_action": "Continue lifecycle correlation and deduplication.",
            },
            {
                "classification": "legacy_transaction_backed_x402",
                "count": legacy_direct_count,
                "amount_usdc": legacy_direct_amount,
                "revenue_status": "excluded_pending_lifecycle_backfill",
                "next_action": "Retain as historical evidence; backfill only after reviewed settlement-to-delivery joins.",
            },
            {
                "classification": "local_qa_credit_topup",
                "count": len(topup_rows),
                "amount_usdc": topup_total,
                "revenue_status": "not_revenue",
                "next_action": "Keep excluded from production revenue.",
            },
        ],
        "definitions": {
            "decision_grade_recognized_revenue": "Finalized, deduplicated x402 settlements joined to durable delivery lifecycle evidence.",
            "legacy_transaction_backed_x402": "Direct x402 inflows with transaction evidence that predate or lack complete lifecycle correlation.",
            "reconciliation_boundary": "Classification explains wallet movement; it does not rewrite historical payment or delivery records.",
        },
    }


def _build_conversion_experiment(summary: dict[str, Any]) -> dict[str, Any]:
    performance = summary.get("product_performance")
    performance = performance if isinstance(performance, dict) else {}
    rows = performance.get("rows")
    rows = rows if isinstance(rows, list) else []
    quality = summary.get("request_quality")
    quality = quality if isinstance(quality, dict) else {}
    candidates = [row for row in rows if int(row.get("non_monitor_attempts") or 0) > 0]
    candidates.sort(
        key=lambda row: (
            -int(row.get("non_monitor_attempts") or 0),
            -int(row.get("payment_prompts") or 0),
            str(row.get("product_id") or ""),
        )
    )
    package_candidates = [
        row
        for row in candidates
        if row.get("product_family") == "market_intelligence_package"
    ]
    selected = candidates[0] if candidates else None
    selected_package = package_candidates[0] if package_candidates else None
    gross = int(quality.get("gross_live_data_requests") or 0)
    selection_mix = quality.get("selection_source_mix")
    selection_mix = selection_mix if isinstance(selection_mix, dict) else {}
    attributed = sum(
        int(value or 0)
        for source, value in selection_mix.items()
        if source != "unattributed"
    )
    attribution_rate = min(attributed / gross, 1.0) if gross else None
    monitor_share = quality.get("known_monitor_share")
    monitor_share = float(monitor_share) if monitor_share is not None else None
    blockers: list[str] = []
    if selected:
        if attribution_rate is None or attribution_rate < 0.8:
            blockers.append("selection_attribution_below_80_percent")
        if monitor_share is not None and monitor_share >= 0.5:
            blockers.append("known_monitor_share_at_or_above_50_percent")
        if int(selected.get("non_monitor_attempts") or 0) < 30:
            blockers.append("fewer_than_30_non_monitor_candidate_attempts")
    status = (
        "no_candidate_demand"
        if selected is None
        else "collecting_clean_baseline"
        if blockers
        else "ready_to_run"
    )
    return {
        "experiment_id": "official_x402_handoff_for_top_non_monitor_product",
        "status": status,
        "selected_product_id": selected.get("product_id") if selected else None,
        "selected_product_family": selected.get("product_family") if selected else None,
        "selected_non_monitor_attempts": int(selected.get("non_monitor_attempts") or 0)
        if selected
        else 0,
        "selected_payment_prompts": int(selected.get("payment_prompts") or 0)
        if selected
        else 0,
        "selected_validated_deliveries": int(selected.get("validated_deliveries") or 0)
        if selected
        else 0,
        "market_intelligence_candidate_product_id": (
            selected_package.get("product_id") if selected_package else None
        ),
        "selection_attribution_rate": attribution_rate,
        "known_monitor_share": monitor_share,
        "blockers": blockers,
        "hypothesis": "An explicit resolver-selected handoff using the official x402 v2 client will convert more non-monitor prompts to verified authorization and delivery than the generic path.",
        "control": "Current resolver or direct endpoint handoff and purchase instructions.",
        "treatment": "Resolver-selected endpoint plus official x402 v2 preflight, client example, and failure-specific recovery guidance.",
        "primary_metric": "correlated_proof_attempts / non-monitor payment prompts",
        "secondary_metrics": [
            "verified_authorizations / correlated_proof_attempts",
            "validated_deliveries / non-monitor payment prompts",
            "recognized_revenue_usdc per non-monitor attempt",
            "repeat_7d_rate for trusted identities",
        ],
        "guardrails": [
            "0 payment_settlement_unreconciled events",
            "0 charged delivery failures",
            "exclude known monitors, tagged tests, and published examples from the primary result",
        ],
        "launch_gate": "At least 80% selection attribution, known-monitor share below 50%, and 30 non-monitor candidate attempts.",
        "decision_rule": "Run a seven-day controlled cohort and ship only if conversion improves without a payment-integrity guardrail failure.",
    }


def _build_operational_alerts(summary: dict[str, Any]) -> dict[str, Any]:
    funnel = summary.get("payment_funnel")
    funnel = funnel if isinstance(funnel, dict) else {}
    quality = summary.get("request_quality")
    quality = quality if isinstance(quality, dict) else {}
    reliability = summary.get("reliability")
    reliability = reliability if isinstance(reliability, dict) else {}
    marketplace = summary.get("marketplace_metrics")
    marketplace = marketplace if isinstance(marketplace, dict) else {}
    economic = summary.get("economic_correlation")
    economic = economic if isinstance(economic, dict) else {}
    reconciliation = summary.get("revenue_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    resolver = summary.get("resolver_funnel")
    resolver = resolver if isinstance(resolver, dict) else {}
    alerts: list[dict[str, str]] = []

    def add(
        alert_id: str,
        severity: str,
        title: str,
        detail: str,
        owner: str,
        metric: str,
        value: str,
        threshold: str,
        runbook: str,
    ) -> None:
        alerts.append(
            {
                "id": alert_id,
                "severity": severity,
                "status": "active",
                "title": title,
                "detail": detail,
                "owner": owner,
                "metric": metric,
                "value": value,
                "threshold": threshold,
                "runbook": runbook,
            }
        )

    unreconciled = int(economic.get("unreconciled_settlements") or 0)
    post_charge = int(reliability.get("charged_delivery_failures") or 0)
    correlated = int(funnel.get("correlated_proof_attempts") or 0)
    settled = int(funnel.get("settled_attempts") or 0)
    settlement_rate = funnel.get("proof_to_settlement_rate")
    raw_proofs = int(funnel.get("raw_proof_submission_events") or 0)
    failed_proofs = int(funnel.get("failed_or_rejected_proof_events") or 0)
    failed_rate = failed_proofs / raw_proofs if raw_proofs else None
    monitor_share = quality.get("known_monitor_share")
    if unreconciled:
        add("unreconciled-remote-settlement", "P0", "Remote settlement lacks a delivery or refund checkpoint", f"{unreconciled} outcome(s) require reconciliation.", "Payments engineering", "unreconciled_settlements", str(unreconciled), "0", "Join every transaction hash to one delivery or refund checkpoint.")
    if post_charge:
        add("post-charge-delivery-failure", "P0", "A charged request failed before value delivery", f"{post_charge} post-charge failure(s) are active.", "API engineering", "charged_delivery_failures", str(post_charge), "0", "Confirm replay delivery or refund with the durable lifecycle IDs.")
    if correlated and (settlement_rate is None or float(settlement_rate) < 0.9):
        add("proof-to-settlement-rate", "P0", "Correlated payment attempts are not settling reliably", f"{settled} of {correlated} correlated attempts settled.", "Payments engineering", "proof_to_settlement_rate", _brief_pct(float(settlement_rate) if settlement_rate is not None else None), ">= 90%", "Break failures down by parser, network, facilitator, finality, replay checkpoint, and delivery.")
    if failed_rate is not None and failed_rate >= 0.25:
        add("proof-payload-rejection-noise", "P1", "Most proof events are malformed or rejected", f"{failed_proofs} of {raw_proofs} raw proof events failed.", "Payments + growth engineering", "failed_proof_event_rate", _brief_pct(failed_rate), "< 25%", "Separate probes from genuine attempts and publish official x402 v2 examples.")
    if monitor_share is not None and float(monitor_share) >= 0.5:
        add("monitor-dominated-demand", "P1", "Known monitors dominate gross attempts", "Gross requests are not a safe demand denominator.", "Product analytics", "known_monitor_share", _brief_pct(float(monitor_share)), "< 50% or separate", "Use non-monitor attempts for demand decisions.")
    if not marketplace.get("platforms_configured"):
        add("marketplace-metrics-unavailable", "P1", "Marketplace-side metrics are unavailable", "Upstream views, installs, hosted calls, and conversion are not ingested.", "Growth engineering", "marketplace_platforms_configured", "0", "all ingested or explicitly unsupported", "Load reviewed APIs/exports and retain an unavailable reason where none exists.")
    if reconciliation.get("status") == "classified_pending_lifecycle_backfill":
        amount = float(reconciliation.get("legacy_transaction_backed_x402_usdc") or 0.0)
        add("legacy-x402-lifecycle-backfill", "P1", "Historical x402 inflows lack decision-grade joins", f"${amount:.4f} remains excluded from recognized revenue.", "Payments + finance", "legacy_transaction_backed_x402_usdc", f"${amount:.4f}", "$0 unresolved", "Backfill only reviewed settlement-to-delivery joins.")
    if resolver.get("status") == "collecting_after_instrumentation":
        add("resolver-funnel-collecting", "P2", "Resolver conversion is collecting its baseline", "Search and resolution are visible, but paid delivery has not matured.", "Agent experience", "resolver_to_delivery_rate", "collecting", "measured", "Run the fixed long-tail resolver QA suite.")
    severity_rank = {"P0": 0, "P1": 1, "P2": 2}
    alerts.sort(key=lambda alert: (severity_rank.get(alert["severity"], 9), alert["id"]))
    return {
        "status": "needs_attention" if alerts else "clear",
        "active_count": len(alerts),
        "p0_count": sum(alert["severity"] == "P0" for alert in alerts),
        "p1_count": sum(alert["severity"] == "P1" for alert in alerts),
        "p2_count": sum(alert["severity"] == "P2" for alert in alerts),
        "alerts": alerts,
        "delivery": {
            "mode": "dashboard_and_authenticated_json",
            "external_webhook_configured": False,
            "note": "No external webhook is configured; incident tools can poll the authenticated alert feed.",
        },
    }


def _build_daily_observability_interpretation(summary: dict[str, Any]) -> dict[str, Any]:
    overview = summary.get("overview") if isinstance(summary.get("overview"), dict) else {}
    event_counts = summary.get("event_counts") if isinstance(summary.get("event_counts"), dict) else {}
    payment_funnel = summary.get("payment_funnel") if isinstance(summary.get("payment_funnel"), dict) else {}
    transport_requests = summary.get("transport_requests") if isinstance(summary.get("transport_requests"), dict) else {}
    reconciliation = summary.get("revenue_reconciliation") if isinstance(summary.get("revenue_reconciliation"), dict) else {}
    growth_funnel = summary.get("growth_funnel") if isinstance(summary.get("growth_funnel"), dict) else {}
    growth_summary = growth_funnel.get("summary") if isinstance(growth_funnel.get("summary"), dict) else {}
    popularity = summary.get("popularity") if isinstance(summary.get("popularity"), dict) else {}
    wallet_inflows = summary.get("wallet_inflows") if isinstance(summary.get("wallet_inflows"), dict) else {}
    external_sources = summary.get("external_sources") if isinstance(summary.get("external_sources"), dict) else {}
    source_evidence = summary.get("source_evidence") if isinstance(summary.get("source_evidence"), dict) else {}
    reliability = summary.get("reliability") if isinstance(summary.get("reliability"), dict) else {}
    platforms = external_sources.get("platforms") if isinstance(external_sources.get("platforms"), list) else []
    timeline = summary.get("timeline") if isinstance(summary.get("timeline"), list) else []

    rows = popularity.get("rows") if isinstance(popularity.get("rows"), list) else []
    requested = int(payment_funnel.get("live_data_requests") or 0)
    non_monitor_requested = int(payment_funnel.get("non_monitor_live_data_requests") or 0)
    known_monitor_requested = int(payment_funnel.get("known_monitor_live_data_requests") or 0)
    transport_count = int(
        transport_requests.get("public_mcp_transport_requests") or 0
    ) + int(transport_requests.get("authenticated_mcp_transport_requests") or 0)
    delivered = int(popularity.get("total_delivered") or 0)
    blocked = int(payment_funnel.get("x402_prompts") or 0)
    failed_after_credit = int(popularity.get("total_failed_after_credit") or 0)
    prompts = int(event_counts.get("payment_required") or 0)
    proof_submissions = int(payment_funnel.get("raw_proof_submission_events") or 0)
    correlated_proofs = int(payment_funnel.get("correlated_proof_attempts") or 0)
    verified_authorizations = int(payment_funnel.get("verified_authorizations") or 0)
    settled_payments = int(payment_funnel.get("settled_attempts") or 0)
    unreconciled_settlements = int(
        event_counts.get("payment_settlement_unreconciled") or 0
    )
    paid_calls = int(overview.get("paid_calls") or 0)
    registry_requests = int(overview.get("registry_requests") or 0)
    revenue_usdc = float(overview.get("estimated_revenue_usdc") or 0.0)
    inflow_count = int(wallet_inflows.get("total_inflows") or 0)
    inflow_usdc = float(wallet_inflows.get("total_usdc") or 0.0)
    legacy_x402_usdc = float(reconciliation.get("legacy_transaction_backed_x402_usdc") or 0.0)
    repeat_7d_eligible = int(growth_summary.get("repeat_7d_eligible_identities") or 0)
    error_rate = reliability.get("server_error_rate")
    if error_rate is None:
        error_rate = overview.get("server_error_rate")
    error_rate_float = float(error_rate) if error_rate is not None else 0.0
    post_credit_failures = int(reliability.get("charged_delivery_failures") or failed_after_credit)
    recent_post_credit_failures = int(
        reliability.get("charged_delivery_failures_last_24h") or 0
    )
    latest_post_credit_failure_at = reliability.get("latest_charged_delivery_failure_at")
    delivery_rate = _brief_ratio(delivered, requested)
    block_rate = _brief_ratio(blocked, requested)
    active_registry_sources = sum(1 for value in summary.get("registry_source_mix", {}).values() if value)
    evidence_events = int(source_evidence.get("events_reviewed") or 0)
    synthetic_evidence_events = int(source_evidence.get("synthetic_events") or 0)
    proof_hash_events = int(source_evidence.get("transaction_or_proof_hash_events") or 0)
    telemetry_scope = (
        summary.get("telemetry_scope")
        if isinstance(summary.get("telemetry_scope"), dict)
        else {}
    )
    excluded_synthetic_events = int(
        telemetry_scope.get("excluded_synthetic_events") or 0
    )
    evidence_scope_note = (
        f"{synthetic_evidence_events} synthetic/test events included"
        if telemetry_scope.get("include_synthetic")
        else f"{excluded_synthetic_events} tagged synthetic/test events excluded"
    )
    unconfigured_platforms = [
        str(platform.get("name") or platform.get("id") or "Unknown platform")
        for platform in platforms
        if not platform.get("external_metrics_configured")
    ]
    latest_day = timeline[-1] if timeline else {}
    top_requested = _top_popularity_row(rows, "requested")
    top_blocked = _top_popularity_row(rows, "blocked")
    top_delivered = _top_popularity_row(rows, "delivered")

    if unreconciled_settlements:
        status = "needs_attention"
        status_label = "Needs attention"
    elif requested and delivered == 0 and (blocked or prompts):
        status = "needs_attention"
        status_label = "Needs attention"
    elif requested and (delivery_rate is not None and delivery_rate < 0.5):
        status = "watch"
        status_label = "Watch closely"
    elif error_rate_float >= 0.01 or recent_post_credit_failures:
        status = "watch"
        status_label = "Watch closely"
    elif requested or registry_requests:
        status = "working"
        status_label = "Working"
    else:
        status = "quiet"
        status_label = "Quiet"

    executive_summary: list[str] = []
    if requested:
        leader = ""
        if top_requested:
            leader = (
                f" Top demand is {top_requested.get('service')} / {top_requested.get('subject')} "
                f"with {int(top_requested.get('requested') or 0)} requests."
            )
        executive_summary.append(
            f"{requested} commercial live-data attempts were observed: {non_monitor_requested} non-monitor demand candidates and "
            f"{known_monitor_requested} known-monitor attempts. {delivered} reached validated delivery and {blocked} were blocked or prompted. "
            f"Delivery rate is {_brief_pct(delivery_rate)} and block rate is {_brief_pct(block_rate)}.{leader}"
        )
    else:
        executive_summary.append("No paid-data demand was recorded in this window, so product usage is not yet proven by telemetry.")

    executive_summary.append(
        f"The payment funnel shows {prompts} x402 prompts, {proof_submissions} raw proof events, "
        f"{correlated_proofs} correlated attempts, {verified_authorizations} verified authorizations, "
        f"{settled_payments} settlements, and {paid_calls} paid/credit-backed deliveries."
    )
    executive_summary.append(
        f"Decision-grade recognized revenue is ${revenue_usdc:.4f}; ${legacy_x402_usdc:.4f} of transaction-backed legacy x402 inflows remains excluded pending reviewed lifecycle backfill. "
        f"Total wallet inflows are ${inflow_usdc:.4f}."
    )
    if transport_count:
        executive_summary.append(
            f"{transport_count} MCP transport requests were recorded separately and are not counted as ticker or package demand."
        )
    if unreconciled_settlements:
        executive_summary.append(
            f"P0: {unreconciled_settlements} remote settlement outcome(s) lack a conclusive local recovery checkpoint."
        )
    executive_summary.append(
        f"Acquisition telemetry recorded {registry_requests} registry requests across {active_registry_sources} attributed registry sources. "
        f"{len(unconfigured_platforms)} onboarded platform metric feed(s) are still not ingested."
    )
    executive_summary.append(
        f"Raw evidence review found {evidence_events} evidence-bearing events, "
        f"{evidence_scope_note}, and {proof_hash_events} transaction/proof-hash event(s)."
    )
    if repeat_7d_eligible == 0:
        executive_summary.append(
            "No activation cohort has matured for seven days, so consistent repeat demand cannot yet be claimed."
        )

    what_works: list[dict[str, str]] = []
    if delivered:
        detail = f"{delivered} calls returned data"
        if top_delivered:
            detail += f"; strongest delivered item is {top_delivered.get('service')} / {top_delivered.get('subject')}"
        what_works.append({"title": "Data delivery is producing usable output", "detail": detail, "tone": "good"})
    if settled_payments:
        what_works.append(
            {
                "title": "Settled paid usage path has activity",
                "detail": f"{proof_submissions} proof submissions, {settled_payments} finalized direct payments, and {paid_calls} successful paid/credit-backed deliveries.",
                "tone": "good",
            }
        )
    elif paid_calls:
        what_works.append(
            {
                "title": "Credit-backed delivery path has activity",
                "detail": f"{paid_calls} successful credit-backed deliveries are visible, but no direct x402 payment is settled and finalized.",
                "tone": "watch",
            }
        )
    if registry_requests or active_registry_sources:
        what_works.append(
            {
                "title": "Discovery telemetry is being captured",
                "detail": f"{registry_requests} registry requests and {active_registry_sources} attributed registry sources are visible.",
                "tone": "good",
            }
        )
    if inflow_count:
        what_works.append(
            {
                "title": "Wallet inflow tracking is working",
                "detail": f"{inflow_count} verified inflows totaling ${inflow_usdc:.4f} are tied to transaction hashes.",
                "tone": "good",
            }
        )
    if not what_works:
        what_works.append(
            {
                "title": "The dashboard is collecting baseline telemetry",
                "detail": "Events are being summarized, but usage has not yet crossed into successful paid data delivery.",
                "tone": "watch",
            }
        )

    what_does_not: list[dict[str, str]] = []
    if unreconciled_settlements:
        what_does_not.append(
            {
                "title": "Payment settlement outcomes require immediate reconciliation",
                "detail": f"{unreconciled_settlements} remote settlement outcome(s) are unresolved; use available payment evidence to reconcile delivery or refund before retrying.",
                "tone": "bad",
            }
        )
    if prompts and proof_submissions == 0:
        what_does_not.append(
            {
                "title": "Payment prompts are not converting",
                "detail": "Clients are seeing x402 challenges, but no payment proof is being submitted afterward.",
                "tone": "bad",
            }
        )
    if proof_submissions and settled_payments == 0:
        what_does_not.append(
            {
                "title": "Submitted payment proofs are not settling",
                "detail": f"{proof_submissions} proof submission(s) are recorded, but none reached durable payment_settled finalization.",
                "tone": "bad",
            }
        )
    if proof_submissions > correlated_proofs:
        what_does_not.append(
            {
                "title": "Raw proof traffic overstates genuine purchase attempts",
                "detail": f"Only {correlated_proofs} of {proof_submissions} raw proof events correlate to a challenge lifecycle; malformed and probe traffic must not be counted as users.",
                "tone": "bad",
            }
        )
    if requested and delivered == 0:
        what_does_not.append(
            {
                "title": "No paid-data delivery is visible",
                "detail": "Demand exists, but the telemetry does not show a successful data return after payment or credits.",
                "tone": "bad",
            }
        )
    if blocked:
        detail = f"{blocked} requests are blocked or payment-prompted"
        if top_blocked:
            detail += f"; biggest blocked item is {top_blocked.get('service')} / {top_blocked.get('subject')}"
        what_does_not.append({"title": "Demand is being stopped before value is delivered", "detail": detail, "tone": "warn"})
    if recent_post_credit_failures:
        what_does_not.append(
            {
                "title": "Some paid/credit-backed calls fail after charging",
                "detail": f"{recent_post_credit_failures} unrecovered charged call(s) failed during the trailing 24 hours and need refund or retry review.",
                "tone": "bad",
            }
        )
    elif post_credit_failures:
        what_does_not.append(
            {
                "title": "A historical charged failure remains in this reporting window",
                "detail": (
                    f"No unrecovered charged failure was observed in the trailing 24 hours; "
                    f"the latest selected-window failure was {latest_post_credit_failure_at or 'not timestamped'}."
                ),
                "tone": "warn",
            }
        )
    if prompts and inflow_count == 0:
        what_does_not.append(
            {
                "title": "No wallet inflows are tied to the payment prompts",
                "detail": "The prompts look like unpaid challenges rather than completed payments.",
                "tone": "warn",
            }
        )
    if evidence_events and proof_hash_events == 0 and (prompts or delivered):
        what_does_not.append(
            {
                "title": "Growth evidence is not payment-backed yet",
                "detail": "Recent evidence includes endpoints and user agents, but no transaction or proof-hash events for the claim.",
                "tone": "warn",
            }
        )
    if unconfigured_platforms:
        what_does_not.append(
            {
                "title": "Platform metrics are incomplete",
                "detail": "External metric feeds are not ingested for: " + ", ".join(unconfigured_platforms[:5]) + ".",
                "tone": "warn",
            }
        )
    if not what_does_not:
        what_does_not.append(
            {
                "title": "No critical breakage is visible in this window",
                "detail": "Continue watching conversion, delivery, inflows, and registry attribution for drift.",
                "tone": "good",
            }
        )

    improvement_steps: list[dict[str, str]] = []
    if unreconciled_settlements:
        improvement_steps.append(
            {
                "priority": "P0",
                "action": "Reconcile every remotely settled payment that lacks a local checkpoint before changing payment infrastructure.",
                "why": "The payer may have transferred irreversible funds without receiving the protected response.",
                "check": "Expect payment_settlement_unreconciled to return to 0 after each transaction is delivered or refunded.",
            }
        )
    if prompts and proof_submissions == 0:
        improvement_steps.append(
            {
                "priority": "P0",
                "action": "Run an end-to-end x402 payment smoke test from the same client surfaces that are prompting.",
                "why": "The current funnel stops at 402 challenge, so users may be unable or unwilling to complete payment.",
                "check": "Expect payment_proof_submitted > 0, payment_settled > 0, and wallet inflows with transaction hashes.",
            }
        )
    if requested and delivered == 0:
        improvement_steps.append(
            {
                "priority": "P0",
                "action": "Verify one protected data endpoint returns payload after a valid payment or credit drawdown.",
                "why": "Demand without delivery means the product value is not reaching users.",
                "check": "Expect Data Delivered to rise and Called Data Detail to show 'Data returned after payment or credits'.",
            }
        )
    if top_blocked:
        improvement_steps.append(
            {
                "priority": "P1",
                "action": f"Review the purchase path for {top_blocked.get('service')} / {top_blocked.get('subject')}.",
                "why": "This is the largest blocked demand signal and should be the first conversion target.",
                "check": "Expect blocked demand to fall or proof submissions to rise for that data item.",
            }
        )
    if unconfigured_platforms:
        improvement_steps.append(
            {
                "priority": "P1",
                "action": "Wire external marketplace metrics into the observability database.",
                "why": "Local logs only prove traffic that reaches our server; marketplace performance pages can include upstream views or health checks.",
                "check": "Expect platform coverage to show configured feeds for Pay.sh, Smithery, and other onboarded registries.",
            }
        )
    if error_rate_float >= 0.01 or recent_post_credit_failures:
        improvement_steps.append(
            {
                "priority": "P1",
                "action": "Audit recent failed/rejected events and add refunds or retries for charged failures.",
                "why": "Reliability problems after a user pays are high trust-risk events.",
                "check": "Expect server error rate below 1% and failed-after-credit count at 0.",
            }
        )
    improvement_steps.append(
        {
            "priority": "P2",
            "action": "Review this daily brief against the raw event trace before changing pricing or registry strategy.",
            "why": "The honest read depends on separating synthetic monitors, registry crawlers, and real paid users.",
            "check": "Expect source, user agent, endpoint, and transaction hash evidence to support any growth claim.",
        }
    )

    checks = [
        {
            "name": "Settlement reconciliation",
            "status": "fail" if unreconciled_settlements else "pass",
            "value": f"{unreconciled_settlements} unreconciled",
            "detail": "Remote settlements without a durable local recovery checkpoint are P0 operator alerts.",
        },
        {
            "name": "Data delivery",
            "status": "pass" if delivered else "fail" if requested else "watch",
            "value": f"{delivered}/{requested}",
            "detail": "Delivered paid/credit-backed data calls divided by requested data signals.",
        },
        {
            "name": "Payment proof submission",
            "status": "pass" if settled_payments else "fail" if proof_submissions or prompts else "watch",
            "value": f"{settled_payments} settled / {verified_authorizations} verified / {correlated_proofs} correlated / {proof_submissions} raw / {prompts} prompted",
            "detail": "Settlement conversion uses correlated purchase attempts; malformed raw proof events are diagnostic noise, not customers.",
        },
        {
            "name": "Revenue reconciliation",
            "status": "pass" if legacy_x402_usdc == 0 else "watch",
            "value": f"${revenue_usdc:.4f} recognized / ${legacy_x402_usdc:.4f} legacy excluded",
            "detail": "Legacy transaction-backed inflows remain outside recognized revenue until settlement-to-delivery joins are reviewed.",
        },
        {
            "name": "Seven-day repeat demand",
            "status": "watch" if repeat_7d_eligible == 0 else "pass" if growth_summary.get("repeat_7d_rate") else "fail",
            "value": f"{repeat_7d_eligible} matured cohort identity(ies)",
            "detail": "Consistent demand is only measured after trusted activation cohorts have had a complete seven-day observation window.",
        },
        {
            "name": "Wallet inflows",
            "status": "pass" if inflow_count else "fail" if prompts or settled_payments else "watch",
            "value": f"{inflow_count} inflow(s), ${inflow_usdc:.4f}",
            "detail": "Verified direct x402 payments and legacy local-QA top-ups with transaction hashes.",
        },
        {
            "name": "Registry attribution",
            "status": "pass" if active_registry_sources else "watch",
            "value": f"{active_registry_sources} source(s)",
            "detail": "Traffic attributed to Glama, Pay.sh, Smithery, MCP Registry, x402 directories, or similar sources.",
        },
        {
            "name": "External platform feeds",
            "status": "pass" if platforms and not unconfigured_platforms else "fail" if unconfigured_platforms else "watch",
            "value": f"{len(platforms) - len(unconfigured_platforms)}/{len(platforms)} configured",
            "detail": "Marketplace-side metrics configured for onboarded platforms.",
        },
        {
            "name": "HTTP reliability",
            "status": "pass" if error_rate_float < 0.01 and not recent_post_credit_failures else "fail",
            "value": (
                f"{_brief_pct(error_rate_float)} server; "
                f"{recent_post_credit_failures} trailing-24h / {post_credit_failures} selected-window post-credit failure(s)"
            ),
            "detail": "HTTP 5xx rate plus recent charged-delivery failures. Historical failures remain visible without making a repaired path look currently broken.",
        },
        {
            "name": "Raw evidence",
            "status": "pass" if settled_payments or not (prompts or delivered) else "watch",
            "value": f"{proof_hash_events} proof/tx event(s), {synthetic_evidence_events} synthetic",
            "detail": "Submitted proof hashes support debugging; settled and durably finalized payments are required for monetization claims.",
        },
    ]

    return {
        "title": "Daily Executive Brief",
        "as_of": summary.get("generated_at"),
        "window_days": summary.get("window_days"),
        "latest_day": latest_day.get("date"),
        "latest_day_events": int(latest_day.get("http_requests") or 0)
        + int(latest_day.get("mcp_tool_calls") or 0)
        + int(latest_day.get("registry_requests") or 0),
        "status": status,
        "status_label": status_label,
        "executive_summary": executive_summary,
        "what_works": what_works[:5],
        "what_does_not": what_does_not[:6],
        "improvement_steps": improvement_steps[:6],
        "checks": checks,
    }


@app.post("/internal/observability/marketplace-metrics", include_in_schema=False)
async def ingest_marketplace_metrics(request: Request) -> JSONResponse:
    """Store a marketplace-side metrics snapshot for Platform Coverage."""
    if not _observability_authorized(request):
        return _observability_unauthorized()
    if OBSERVABILITY is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"error": "Observability disabled"},
        )

    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "Expected JSON object"})

    platform_id = str(payload.get("platform_id") or "").strip()
    platform_ids = {str(platform["id"]) for platform in DISTRIBUTION_PLATFORMS}
    if platform_id not in platform_ids:
        return JSONResponse(
            status_code=400,
            content={
                "error": "Unknown platform_id",
                "allowed_platform_ids": sorted(platform_ids),
            },
        )

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return JSONResponse(status_code=400, content={"error": "metrics must be an object"})

    OBSERVABILITY.record_marketplace_metrics(
        platform_id=platform_id,
        metrics=metrics,
        source_url=str(payload.get("source_url") or "").strip() or None,
        status=str(payload.get("status") or "ok").strip() or "ok",
    )
    return JSONResponse(
        headers={"Cache-Control": "no-store"},
        content={
            "status": "ok",
            "platform_id": platform_id,
            "metrics_recorded": True,
        },
    )


@app.get("/internal/observability/stats", include_in_schema=False)
async def observability_stats(
    request: Request,
    days: int = Query(30, ge=1, le=180),
    include_synthetic: bool = Query(True),
) -> JSONResponse:
    """Return dashboard-ready usage, registry, MCP, and monetization rollups."""
    if not _observability_authorized(request):
        return _observability_unauthorized()
    if OBSERVABILITY is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"error": "Observability disabled"},
        )
    content = _with_external_observability_context(
        OBSERVABILITY.summarize(
            days=days,
            include_synthetic=include_synthetic,
        )
    )
    credits = getattr(request.app.state, "credits", None)
    if credits is not None and hasattr(credits, "wallet_inflow_summary"):
        content["wallet_inflows"] = credits.wallet_inflow_summary(days=days)
    else:
        content["wallet_inflows"] = {
            "window_days": days,
            "total_inflows": 0,
            "direct_x402_count": 0,
            "credit_topup_count": 0,
            "total_usdc": 0.0,
            "latest_timestamp": None,
            "rows": [],
        }
    content["revenue_reconciliation"] = _build_revenue_reconciliation(content)
    content["conversion_experiment"] = _build_conversion_experiment(content)
    content["operational_alerts"] = _build_operational_alerts(content)
    content["daily_interpretation"] = _build_daily_observability_interpretation(content)
    content["rwa_growth_pilot"] = _rwa_growth_pilot_dashboard_status(request.app)
    return JSONResponse(
        headers={"Cache-Control": "no-store"},
        content=content,
    )


@app.get("/internal/observability/alerts", include_in_schema=False)
async def observability_alerts(
    request: Request,
    days: int = Query(30, ge=1, le=180),
) -> JSONResponse:
    """Return the authenticated operator-alert feed without synthetic traffic."""
    response = await observability_stats(
        request=request,
        days=days,
        include_synthetic=False,
    )
    if response.status_code != 200:
        return response
    payload = json.loads(response.body)
    return JSONResponse(
        headers={"Cache-Control": "no-store"},
        content={
            "generated_at": payload.get("generated_at"),
            "window_days": payload.get("window_days"),
            "alerts": payload.get("operational_alerts"),
            "revenue_reconciliation": payload.get("revenue_reconciliation"),
            "conversion_experiment": payload.get("conversion_experiment"),
            "payment_funnel": payload.get("payment_funnel"),
            "request_quality": payload.get("request_quality"),
        },
    )


@app.get("/internal/observability", include_in_schema=False, response_model=None)
async def observability_dashboard(request: Request) -> Any:
    """Serve a lightweight internal product observability dashboard."""
    return await observability_command_center(request)


def _observability_login_page(
    request: Request,
    *,
    status_code: int = 401,
    login_failed: bool = False,
) -> HTMLResponse:
    del request
    error_message = (
        '<p role="alert">The supplied password was not accepted.</p>'
        if login_failed
        else ""
    )
    return HTMLResponse(
        status_code=status_code,
        headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability Access</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18201c;
      --muted: #68736d;
      --line: #d9ded9;
      --bg: #f5f7f2;
      --panel: #ffffff;
      --green: #1d6f45;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 24px;
    }}
    main {{
      width: min(440px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 28px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    p {{ margin: 0 0 20px; color: var(--muted); line-height: 1.5; }}
    label {{ display: grid; gap: 8px; color: var(--muted); font-size: 13px; }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }}
    button {{
      width: 100%;
      margin-top: 14px;
      border: 1px solid var(--green);
      border-radius: 6px;
      padding: 11px 12px;
      color: #fff;
      background: var(--green);
      font-weight: 700;
      cursor: pointer;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Internal Observability</h1>
    <p>This console is private. Enter the observability password configured for this environment.</p>
    {error_message}
    <form method="post" action="/internal/observability/login">
      <label>Password
        <input name="password" type="password" autocomplete="current-password" autofocus required />
      </label>
      <button type="submit">Open Dashboard</button>
    </form>
  </main>
</body>
</html>""",
    )


def _observability_command_center_html(*, stats_path: str) -> str:
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability Command Center</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #18201c;
      --muted: #66716b;
      --faint: #8b968f;
      --line: #d7ddd8;
      --soft-line: #e7ebe7;
      --bg: #f5f7f2;
      --panel: #ffffff;
      --panel-soft: #fbfcf9;
      --green: #1d6f45;
      --green-soft: #e7f4eb;
      --blue: #2f5f9d;
      --blue-soft: #e9f0fb;
      --amber: #9b6418;
      --amber-soft: #f7eedf;
      --red: #a33d34;
      --red-soft: #f8e8e6;
      --glama: #7c3aed;
      --pay-sh: #0f9f8a;
      --mcp-registry: #d97706;
      --smithery: #2563eb;
      --awesome-mcp: #be123c;
      --github-source: #24292f;
      --gitlab-source: #e24329;
      --openapi-source: #0891b2;
      --x402-directory: #65a30d;
      --listing-asset: #7c2d12;
      --registry-other: #8b5e34;
      --shadow: 0 1px 1px rgba(24, 32, 28, .04);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .layout {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      position: sticky;
      top: 0;
      align-self: start;
      height: 100vh;
      padding: 22px 16px;
      background: #ffffff;
      border-right: 1px solid var(--line);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 6px 20px;
      border-bottom: 1px solid var(--soft-line);
    }
    .mark {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 6px;
      color: #fff;
      background: var(--green);
      font-weight: 800;
    }
    .brand strong { display: block; font-size: 14px; }
    .brand span { color: var(--muted); font-size: 12px; }
    nav { display: grid; gap: 6px; padding-top: 18px; }
    nav a {
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 36px;
      padding: 8px 10px;
      border-radius: 6px;
      color: var(--muted);
      text-decoration: none;
      font-size: 13px;
    }
    nav a:hover { background: var(--panel-soft); color: var(--ink); }
    nav a.active { background: var(--green-soft); color: var(--green); font-weight: 700; }
    main {
      min-width: 0;
      padding: 26px 30px 48px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 20px;
    }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }
    h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .sub { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    button, select, input, .toolbar-link {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    button { padding: 8px 11px; cursor: pointer; }
    .toolbar-link { padding: 7px 11px; text-decoration: none; }
    button[aria-pressed="true"] { border-color: var(--green); color: var(--green); font-weight: 700; }
    select { padding: 8px 10px; }
    input { padding: 9px 10px; min-width: 220px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--panel);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .pill::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
    }
    .unprotected::before { background: var(--amber); }
    .confidence-banner {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      margin-bottom: 14px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--amber);
      border-radius: 8px;
      background: var(--amber-soft);
      color: var(--ink);
    }
    .confidence-banner.ready {
      border-left-color: var(--green);
      background: var(--green-soft);
    }
    .confidence-banner strong { font-size: 13px; white-space: nowrap; }
    .confidence-banner span { color: var(--muted); font-size: 13px; line-height: 1.4; }
    .grid { display: grid; gap: 14px; }
    .hero {
      grid-template-columns: 1fr;
      align-items: start;
      margin-bottom: 14px;
    }
    .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .two { grid-template-columns: minmax(0, 1fr) minmax(320px, .78fr); }
    .three { grid-template-columns: repeat(3, minmax(220px, 1fr)); }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .section { margin-top: 14px; }
    .metric {
      display: grid;
      gap: 8px;
      min-height: 116px;
    }
    .metric-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .metric-value { font-size: 28px; font-weight: 800; overflow-wrap: anywhere; }
    .metric-note { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .summary-strip {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 10px;
    }
    .summary-item {
      min-width: 112px;
      padding: 8px 10px;
      border: 1px solid var(--soft-line);
      border-radius: 6px;
      background: var(--panel-soft);
    }
    .summary-item span { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .summary-item strong { display: block; margin-top: 3px; font-size: 18px; }
    .headline {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
    }
    .headline-value { font-size: 42px; line-height: 1; font-weight: 850; margin: 10px 0 8px; }
    .status-list { display: grid; gap: 10px; }
    .status-item {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      gap: 9px;
      align-items: start;
    }
    .status-dot {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      margin-top: 4px;
      background: var(--green);
    }
    .status-dot.warn { background: var(--amber); }
    .status-dot.bad { background: var(--red); }
    .status-item strong { display: block; font-size: 13px; }
    .status-item span { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; line-height: 1.35; }
    .brief-header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: start;
      margin-bottom: 14px;
    }
    .brief-status {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border-radius: 999px;
      background: var(--green-soft);
      color: var(--green);
      font-weight: 800;
      font-size: 12px;
      white-space: nowrap;
    }
    .brief-status.watch, .brief-status.quiet { background: var(--amber-soft); color: var(--amber); }
    .brief-status.needs_attention { background: var(--red-soft); color: var(--red); }
    .brief-summary {
      display: grid;
      gap: 8px;
      margin-bottom: 16px;
      font-size: 14px;
      line-height: 1.45;
    }
    .brief-summary div {
      padding-left: 12px;
      border-left: 3px solid var(--line);
    }
    .brief-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }
    .brief-panel {
      display: grid;
      gap: 10px;
      padding-top: 4px;
    }
    .brief-panel h3 { margin-bottom: 0; }
    .brief-item {
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr);
      gap: 9px;
      font-size: 13px;
      line-height: 1.38;
    }
    .brief-item .status-dot { margin-top: 5px; width: 9px; height: 9px; }
    .brief-item strong { display: block; }
    .brief-item span { display: block; margin-top: 2px; color: var(--muted); }
    .action-list {
      display: grid;
      gap: 10px;
    }
    .action-item {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 10px;
      padding: 10px 0;
      border-top: 1px solid var(--soft-line);
      font-size: 13px;
      line-height: 1.4;
    }
    .priority {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 26px;
      border-radius: 999px;
      background: var(--blue-soft);
      color: var(--blue);
      font-weight: 800;
      font-size: 12px;
    }
    .action-item strong { display: block; }
    .action-item span { display: block; margin-top: 3px; color: var(--muted); }
    .check-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .check {
      border: 1px solid var(--soft-line);
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 5px;
      min-height: 112px;
      align-content: start;
    }
    .check strong { font-size: 13px; }
    .check span { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .check .value { font-size: 18px; font-weight: 800; color: var(--ink); }
    .bars { display: grid; gap: 9px; }
    .bar-row {
      display: grid;
      grid-template-columns: minmax(0, 140px) minmax(48px, 1fr) minmax(32px, auto);
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }
    .bar-row code {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .track { height: 10px; background: #eef1ee; border-radius: 999px; overflow: hidden; }
    .fill { height: 100%; background: var(--green); }
    .fill.blue { background: var(--blue); }
    .fill.amber { background: var(--amber); }
    .fill.red { background: var(--red); }
    .timeline {
      display: flex;
      align-items: end;
      gap: 4px;
      height: 168px;
      padding-top: 10px;
      border-bottom: 1px solid var(--line);
      overflow: visible;
    }
    .day { position: relative; flex: 1; min-width: 5px; display: flex; flex-direction: column; justify-content: end; gap: 2px; height: 100%; outline: none; }
    .day:hover::after, .day:focus-visible::after {
      content: attr(data-tip);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 10px);
      z-index: 20;
      width: max-content;
      max-width: 230px;
      transform: translateX(-50%);
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #18201c;
      color: #fff;
      box-shadow: 0 8px 22px rgba(24,32,28,.16);
      font-size: 12px;
      line-height: 1.35;
      text-align: left;
      white-space: pre-line;
      pointer-events: none;
    }
    .day:hover::before, .day:focus-visible::before {
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 4px);
      z-index: 21;
      width: 10px;
      height: 10px;
      transform: translateX(-50%) rotate(45deg);
      background: #18201c;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      pointer-events: none;
    }
    .seg { width: 100%; min-height: 1px; }
    .seg.http { background: var(--blue); }
    .seg.mcp { background: var(--green); }
    .seg.registry { background: var(--amber); }
    .seg.glama { background: var(--glama); }
    .seg.pay-sh { background: var(--pay-sh); }
    .seg.mcp-registry { background: var(--mcp-registry); }
    .seg.smithery { background: var(--smithery); }
    .seg.awesome-mcp { background: var(--awesome-mcp); }
    .seg.github-source { background: var(--github-source); }
    .seg.gitlab-source { background: var(--gitlab-source); }
    .seg.openapi-source { background: var(--openapi-source); }
    .seg.x402-directory { background: var(--x402-directory); }
    .seg.listing-asset { background: var(--listing-asset); }
    .seg.registry-other { background: var(--registry-other); }
    .legend { display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }
    .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
    .date-axis {
      display: flex;
      gap: 4px;
      min-height: 22px;
      padding-top: 6px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.1;
    }
    .date-tick {
      flex: 1;
      min-width: 5px;
      text-align: center;
      white-space: nowrap;
    }
    .filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
    .source-card {
      display: grid;
      gap: 10px;
      font-size: 13px;
    }
    .source-card .big {
      font-size: 30px;
      font-weight: 800;
      line-height: 1;
    }
    .source-row {
      display: grid;
      grid-template-columns: minmax(92px, 132px) minmax(0, 1fr);
      gap: 10px;
      align-items: start;
    }
    .source-row span:first-child { color: var(--muted); }
    .source-row a { color: var(--blue); overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--soft-line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 700; white-space: nowrap; }
    tbody tr:hover { background: var(--panel-soft); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--green-soft);
      color: var(--green);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .badge.warn { background: var(--amber-soft); color: var(--amber); }
    .badge.bad { background: var(--red-soft); color: var(--red); }
    .badge.neutral { background: var(--blue-soft); color: var(--blue); }
    .empty { color: var(--muted); font-size: 13px; padding: 14px 0; }
    .scroll { overflow-x: auto; }
    @media (max-width: 1160px) {
      .layout { grid-template-columns: 1fr; }
      aside { position: relative; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      nav { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .hero { grid-template-columns: 1fr; }
      .two, .three, .kpis { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 760px) {
      main { padding: 18px; }
      header, .headline { grid-template-columns: 1fr; display: grid; }
      .hero, .two, .three, .kpis, .brief-grid, .check-grid { grid-template-columns: 1fr; }
      .brief-header { grid-template-columns: 1fr; }
      nav { grid-template-columns: 1fr 1fr; }
      .bar-row { grid-template-columns: minmax(0, 1fr) minmax(48px, 1fr) 44px; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="brand">
        <div class="mark">B</div>
        <div><strong>Observability</strong><span>Internal console</span></div>
      </div>
      <nav>
        <a class="active" href="#overview">Overview</a>
        <a href="#growth-funnel">Growth Funnel</a>
        <a href="#daily-brief">Daily Brief</a>
        <a href="#operator-alerts">Operator Alerts</a>
        <a href="#product-performance">Products</a>
        <a href="#popularity">Popularity</a>
        <a href="#acquisition">Acquisition</a>
        <a href="#platforms">Platforms</a>
        <a href="#monetization">Monetization</a>
        <a href="#wallet-inflows">Wallet Inflows</a>
        <a href="#called-data">Called Data</a>
        <a href="#events">Event Trace</a>
      </nav>
    </aside>
    <main>
      <header id="overview">
        <div>
          <h1>Product Usage Command Center</h1>
          <div class="sub" id="freshness">Loading live telemetry...</div>
        </div>
        <div class="toolbar">
          <span id="security" class="pill">Protected</span>
          <button id="live" type="button" aria-pressed="true">Live 10s</button>
          <button id="refresh" type="button">Refresh</button>
          <label class="sub">Window
            <select id="window">
              <option value="1">24 hours</option>
              <option value="7">7 days</option>
              <option value="30" selected>30 days</option>
              <option value="90">90 days</option>
              <option value="180">180 days</option>
            </select>
          </label>
          <label class="sub">Telemetry
            <select id="telemetry-scope">
              <option value="false" selected>Exclude tagged tests</option>
              <option value="true">Include tests</option>
            </select>
          </label>
          <a class="toolbar-link" href="/internal/observability/logout">Log out</a>
        </div>
      </header>

      <section id="decision-confidence" class="confidence-banner" role="status" aria-live="polite">
        <strong id="confidence-label">Checking evidence</strong>
        <span id="confidence-summary">Evaluating whether this snapshot is suitable for operational, growth, and revenue decisions.</span>
      </section>

      <section class="grid hero">
        <div class="card headline">
          <div>
            <h3>Primary signal</h3>
            <div id="headline-value" class="headline-value">...</div>
            <div id="headline-note" class="sub">Waiting for telemetry.</div>
          </div>
          <div class="status-list" id="attention"></div>
        </div>
        <div class="grid kpis" id="kpis"></div>
      </section>

      <section class="card section" id="growth-funnel">
        <div class="headline">
          <div>
            <h2>Growth Funnel</h2>
            <div class="sub">Privacy-safe explicit identities from first discovery through live-price activation, seven-day repeat use, and paid conversion.</div>
          </div>
          <div class="summary-strip" id="growth-kpis"></div>
        </div>
        <div class="grid two">
          <div>
            <h3>Identity progression</h3>
            <div id="growth-stages" class="bars"></div>
          </div>
          <div>
            <h3>Operating targets</h3>
            <div class="check-grid" id="growth-targets"></div>
          </div>
        </div>
        <div class="metric-note" id="growth-boundary"></div>
        <div class="headline" style="margin-top:18px">
          <div>
            <h3>Three-feed RWA pilot</h3>
            <div class="sub">AAPL/USDC, PAXG/USDC and EURC/USDC candidate observations. Monitoring can never promote a feed automatically.</div>
          </div>
          <div class="summary-strip" id="rwa-pilot-kpis"></div>
        </div>
        <div class="scroll"><table id="rwa-pilot-table"></table></div>
      </section>

      <section class="card section" id="daily-brief">
        <div class="brief-header">
          <div>
            <h2>Daily Executive Brief</h2>
            <div class="sub" id="brief-meta">Critical interpretation of usage, payments, delivery, and platform coverage.</div>
          </div>
          <span class="brief-status quiet" id="brief-status">Waiting</span>
        </div>
        <div class="brief-summary" id="brief-summary"></div>
        <div class="brief-grid">
          <div class="brief-panel">
            <h3>What Works</h3>
            <div id="brief-works"></div>
          </div>
          <div class="brief-panel">
            <h3>What Does Not Work</h3>
            <div id="brief-gaps"></div>
          </div>
        </div>
        <div class="brief-panel" style="margin-top:16px">
          <h3>Improvement Steps</h3>
          <div class="action-list" id="brief-actions"></div>
        </div>
        <div class="brief-panel" style="margin-top:16px">
          <h3>Daily Checks</h3>
          <div class="check-grid" id="brief-checks"></div>
        </div>
      </section>

      <section class="card section" id="operator-alerts">
        <div class="headline">
          <div>
            <h2>Operator Alerts</h2>
            <div class="sub">Actionable integrity, conversion, demand-quality, marketplace, and resolver conditions with owners and runbooks.</div>
          </div>
          <div class="summary-strip" id="alert-kpis"></div>
        </div>
        <div class="scroll"><table id="alert-table"></table></div>
      </section>

      <section class="grid two section">
        <div class="card" id="revenue-reconciliation">
          <h2>Revenue Reconciliation</h2>
          <div class="sub">Recognized revenue is separated from legacy transaction-backed inflows and QA top-ups.</div>
          <div class="summary-strip" id="reconciliation-kpis"></div>
          <div class="scroll"><table id="reconciliation-table"></table></div>
        </div>
        <div class="card" id="resolver-funnel">
          <h2>Instrument Resolver Funnel</h2>
          <div class="sub">Search, canonical resolution, resolver-selected live attempts, and paid delivery for long-tail instruments.</div>
          <div class="summary-strip" id="resolver-kpis"></div>
          <div id="resolver-note" class="metric-note"></div>
        </div>
      </section>

      <section class="card section" id="product-performance">
        <div class="headline">
          <div>
            <h2>Product &amp; Package Performance</h2>
            <div class="sub">Raw market data and market-intelligence packages, with monitor traffic separated from non-monitor demand candidates.</div>
          </div>
          <div class="summary-strip" id="experiment-kpis"></div>
        </div>
        <div id="experiment-note" class="metric-note"></div>
        <div class="scroll"><table id="product-table"></table></div>
      </section>

      <section class="grid two section">
        <div class="card">
          <h2>Usage Trend</h2>
          <div class="timeline" id="timeline"></div>
          <div class="date-axis" id="timeline-dates" aria-label="Usage trend dates"></div>
          <div class="legend">
            <span><i style="background: var(--blue)"></i>HTTP</span>
            <span><i style="background: var(--green)"></i>MCP tools</span>
            <span><i style="background: var(--glama)"></i>Glama</span>
            <span><i style="background: var(--pay-sh)"></i>Pay.sh</span>
            <span><i style="background: var(--mcp-registry)"></i>MCP Registry</span>
            <span><i style="background: var(--smithery)"></i>Smithery</span>
            <span><i style="background: var(--awesome-mcp)"></i>Awesome MCP</span>
            <span><i style="background: var(--github-source)"></i>GitHub</span>
            <span><i style="background: var(--gitlab-source)"></i>GitLab</span>
            <span><i style="background: var(--openapi-source)"></i>OpenAPI</span>
            <span><i style="background: var(--x402-directory)"></i>x402 Directory</span>
            <span><i style="background: var(--listing-asset)"></i>Listing assets</span>
            <span><i style="background: var(--registry-other)"></i>Other registry</span>
          </div>
        </div>
        <div class="card" id="monetization">
          <h2>Payment Funnel</h2>
          <div id="funnel" class="bars"></div>
        </div>
      </section>

      <section class="card section" id="popularity">
        <div class="headline">
          <div>
            <h2>Data Popularity</h2>
            <div class="sub">What was requested most, what actually returned data, and where demand is blocked by payment, credits, or errors.</div>
          </div>
          <div class="summary-strip" id="popularity-kpis"></div>
        </div>
        <div class="grid three">
          <div><h3>Most Requested</h3><div id="popular-requested" class="bars"></div></div>
          <div><h3>Data Delivered</h3><div id="popular-delivered" class="bars"></div></div>
          <div><h3>Blocked Demand</h3><div id="popular-blocked" class="bars"></div></div>
        </div>
        <div class="scroll"><table id="popularity-table"></table></div>
      </section>

      <section class="card section" id="wallet-inflows">
        <div class="headline">
          <div>
            <h2>Wallet Inflows</h2>
            <div class="sub">Verified direct x402 payments and legacy local-QA top-ups with transaction hashes for payment follow-up.</div>
          </div>
          <div class="summary-strip" id="wallet-inflow-kpis"></div>
        </div>
        <div class="scroll"><table id="wallet-inflow-table"></table></div>
      </section>

      <section class="grid three section" id="acquisition">
        <div class="card"><h2>Platform Sources</h2><div id="registry-sources" class="bars"></div></div>
        <div class="card"><h2>Campaigns</h2><div id="campaigns" class="bars"></div></div>
        <div class="card"><h2>Conversion Destinations</h2><div id="outbound-destinations" class="bars"></div></div>
        <div class="card"><h2>Pay.sh Marketplace</h2><div id="pay-sh-source" class="source-card"></div></div>
        <div class="card"><h2>Smithery Hosted Activity</h2><div id="smithery-source" class="source-card"></div></div>
        <div class="card"><h2>Most Used Services</h2><div id="services" class="bars"></div></div>
        <div class="card"><h2>Origins and Clients</h2><div id="origins" class="bars"></div></div>
      </section>

      <section class="card section" id="platforms">
        <div class="headline">
          <div>
            <h2>Platform Coverage</h2>
            <div class="sub">Onboarded discovery and marketplace surfaces, what we observed locally, and which external metrics still require ingestion.</div>
          </div>
        </div>
        <div class="scroll"><table id="platform-coverage"></table></div>
      </section>

      <section class="grid three section">
        <div class="card"><h2>MCP Tool Mix</h2><div id="mcp" class="bars"></div></div>
        <div class="card"><h2>Registry Surfaces</h2><div id="registries" class="bars"></div></div>
        <div class="card"><h2>User Agents</h2><div id="agents" class="bars"></div></div>
      </section>

      <section class="card section" id="called-data">
        <div class="headline">
          <div>
            <h2>Called Data Detail</h2>
            <div class="sub">What was queried, when it was viewed, where it came from, and whether data was returned.</div>
          </div>
          <div class="filters">
            <input id="data-search" type="search" placeholder="Search service, symbol, outcome..." />
            <select id="outcome-filter">
              <option value="">All outcomes</option>
              <option value="Prompted">Prompted to pay</option>
              <option value="Data returned">Data returned</option>
              <option value="failed">Failed</option>
              <option value="rejected">Rejected</option>
            </select>
          </div>
        </div>
        <div class="scroll"><table id="data-called"></table></div>
      </section>

      <section class="card section" id="events">
        <div class="headline">
          <div>
            <h2>Recent Event Trace</h2>
            <div class="sub">Latest raw telemetry events for debugging acquisition, payment, and data access paths.</div>
          </div>
          <div class="filters">
            <input id="event-search" type="search" placeholder="Search event, endpoint, subject..." />
          </div>
        </div>
        <div class="scroll"><table id="recent"></table></div>
      </section>
    </main>
  </div>
  <script>
    const statsPath = __STATS_PATH__;
    const fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
    const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
    const pct = value => value == null ? "n/a" : `${Math.round(value * 1000) / 10}%`;
    const text = value => value == null || value === "" ? "n/a" : String(value);
    let live = true;
    let refreshTimer = null;
    let currentData = null;

    document.getElementById("security").textContent = "Password protected";

    function metric(label, value, note = "") {
      return `<div class="card metric"><div class="metric-label">${escapeAttr(label)}</div><div class="metric-value">${escapeAttr(value)}</div><div class="metric-note">${escapeAttr(note)}</div></div>`;
    }

    function rowBadge(value) {
      const label = text(value);
      const lower = label.toLowerCase();
      let cls = "neutral";
      if (lower.includes("returned") || lower.includes("verified") || lower.includes("success")) cls = "";
      if (lower.includes("prompted") || lower.includes("required")) cls = "warn";
      if (lower.includes("failed") || lower.includes("error") || lower.includes("rejected")) cls = "bad";
      return `<span class="badge ${cls}">${escapeAttr(label)}</span>`;
    }

    function toneClass(tone) {
      if (tone === "bad" || tone === "fail") return "bad";
      if (tone === "warn" || tone === "watch") return "warn";
      return "";
    }

    function briefItem(item) {
      const cls = toneClass(item.tone || item.status);
      return `<div class="brief-item">
        <span class="status-dot ${cls}"></span>
        <div><strong>${escapeAttr(item.title || item.name || "")}</strong><span>${escapeAttr(item.detail || "")}</span></div>
      </div>`;
    }

    function renderDailyInterpretation(data) {
      const brief = data.daily_interpretation || {};
      const status = text(brief.status || "quiet");
      const statusEl = document.getElementById("brief-status");
      statusEl.className = `brief-status ${status}`;
      statusEl.textContent = text(brief.status_label || "Quiet");
      document.getElementById("brief-meta").textContent =
        `Generated ${new Date(brief.as_of || data.generated_at).toLocaleString()} for latest day ${text(brief.latest_day)} inside the selected ${text(brief.window_days || data.window_days)} day window.`;
      document.getElementById("brief-summary").innerHTML = (brief.executive_summary || [])
        .map(line => `<div>${escapeAttr(line)}</div>`)
        .join("") || `<div>No executive interpretation is available yet.</div>`;
      document.getElementById("brief-works").innerHTML = (brief.what_works || [])
        .map(briefItem)
        .join("") || `<div class="empty">No working signals yet.</div>`;
      document.getElementById("brief-gaps").innerHTML = (brief.what_does_not || [])
        .map(briefItem)
        .join("") || `<div class="empty">No gaps detected.</div>`;
      document.getElementById("brief-actions").innerHTML = (brief.improvement_steps || [])
        .map(step => `<div class="action-item">
          <span class="priority">${escapeAttr(step.priority || "")}</span>
          <div>
            <strong>${escapeAttr(step.action || "")}</strong>
            <span>${escapeAttr(step.why || "")}</span>
            <span><strong>Check:</strong> ${escapeAttr(step.check || "")}</span>
          </div>
        </div>`)
        .join("") || `<div class="empty">No improvement steps generated.</div>`;
      document.getElementById("brief-checks").innerHTML = (brief.checks || [])
        .map(check => `<div class="check">
          ${rowBadge(check.status)}
          <strong>${escapeAttr(check.name || "")}</strong>
          <div class="value">${escapeAttr(check.value || "")}</div>
          <span>${escapeAttr(check.detail || "")}</span>
        </div>`)
        .join("") || `<div class="empty">No checks generated.</div>`;
    }

    function renderOperatorAlerts(data) {
      const feed = data.operational_alerts || {};
      const alerts = feed.alerts || [];
      document.getElementById("alert-kpis").innerHTML = [
        summaryItem("Active", fmt.format(feed.active_count || 0)),
        summaryItem("P0", fmt.format(feed.p0_count || 0)),
        summaryItem("P1", fmt.format(feed.p1_count || 0)),
        summaryItem("P2", fmt.format(feed.p2_count || 0)),
      ].join("");
      document.getElementById("alert-table").innerHTML =
        `<thead><tr><th>Priority</th><th>Condition</th><th>Metric</th><th>Owner</th><th>Runbook</th></tr></thead><tbody>` +
        (alerts.length ? alerts.map(alert => `<tr>
          <td>${rowBadge(alert.severity)}</td>
          <td><strong>${escapeAttr(alert.title || "")}</strong><div class="metric-note">${escapeAttr(alert.detail || "")}</div></td>
          <td><code>${escapeAttr(alert.metric || "")}</code><div>${escapeAttr(alert.value || "")} · threshold ${escapeAttr(alert.threshold || "")}</div></td>
          <td>${escapeAttr(alert.owner || "")}</td>
          <td>${escapeAttr(alert.runbook || "")}</td>
        </tr>`).join("") : `<tr><td colspan="5" class="empty">No active operator alerts.</td></tr>`) + `</tbody>`;
    }

    function renderRevenueReconciliation(data) {
      const reconciliation = data.revenue_reconciliation || {};
      const rows = reconciliation.classification_rows || [];
      document.getElementById("reconciliation-kpis").innerHTML = [
        summaryItem("Recognized", money.format(reconciliation.decision_grade_recognized_revenue_usdc || 0)),
        summaryItem("Legacy excluded", money.format(reconciliation.legacy_transaction_backed_x402_usdc || 0)),
        summaryItem("QA / not revenue", money.format(reconciliation.local_qa_topup_usdc || 0)),
        summaryItem("Evidence", reconciliation.transaction_evidence_complete ? "complete" : "incomplete"),
      ].join("");
      document.getElementById("reconciliation-table").innerHTML =
        `<thead><tr><th>Class</th><th>Count</th><th>Amount</th><th>Revenue status</th></tr></thead><tbody>` +
        (rows.length ? rows.map(row => `<tr>
          <td><code>${escapeAttr(row.classification || "")}</code></td>
          <td>${fmt.format(row.count || 0)}</td>
          <td>${money.format(row.amount_usdc || 0)}</td>
          <td>${rowBadge(String(row.revenue_status || "").replaceAll("_", " "))}</td>
        </tr>`).join("") : `<tr><td colspan="4" class="empty">No wallet classifications in this window.</td></tr>`) + `</tbody>`;
    }

    function renderResolverFunnel(data) {
      const resolver = data.resolver_funnel || {};
      document.getElementById("resolver-kpis").innerHTML = [
        summaryItem("Searches", fmt.format(resolver.search_events || 0)),
        summaryItem("Resolved", fmt.format(resolver.resolved_events || 0)),
        summaryItem("Distinct symbols", fmt.format(resolver.distinct_resolved_symbols || 0)),
        summaryItem("Resolver attempts", fmt.format(resolver.resolver_live_attempts || 0)),
        summaryItem("Deliveries", fmt.format(resolver.resolver_deliveries || 0)),
        summaryItem("Resolve rate", pct(resolver.search_to_resolution_rate)),
      ].join("");
      document.getElementById("resolver-note").textContent =
        `${fmt.format(resolver.zero_or_unsupported_events || 0)} zero/unsupported results. Resolver-to-delivery: ${pct(resolver.resolver_to_delivery_rate)}. Status: ${text(resolver.status).replaceAll("_", " ")}.`;
    }

    function renderProductPerformance(data) {
      const performance = data.product_performance || {};
      const experiment = data.conversion_experiment || {};
      const rows = performance.rows || [];
      document.getElementById("experiment-kpis").innerHTML = [
        summaryItem("Experiment", text(experiment.status).replaceAll("_", " ")),
        summaryItem("Candidate", text(experiment.selected_product_id)),
        summaryItem("Package candidate", text(experiment.market_intelligence_candidate_product_id)),
        summaryItem("Attribution", pct(experiment.selection_attribution_rate)),
      ].join("");
      const blockers = experiment.blockers || [];
      document.getElementById("experiment-note").textContent = blockers.length
        ? `Launch gates still open: ${blockers.map(value => String(value).replaceAll("_", " ")).join(", ")}.`
        : text(experiment.hypothesis);
      document.getElementById("product-table").innerHTML =
        `<thead><tr><th>Product</th><th>Family</th><th>Gross</th><th>Known monitors</th><th>Non-monitor</th><th>Prompts</th><th>Verified</th><th>Settled</th><th>Delivered</th><th>Revenue</th></tr></thead><tbody>` +
        (rows.length ? rows.map(row => `<tr>
          <td><code>${escapeAttr(row.product_id || "")}</code></td>
          <td>${escapeAttr(String(row.product_family || "").replaceAll("_", " "))}</td>
          <td>${fmt.format(row.gross_attempts || 0)}</td>
          <td>${fmt.format(row.known_monitor_attempts || 0)}</td>
          <td>${fmt.format(row.non_monitor_attempts || 0)}</td>
          <td>${fmt.format(row.payment_prompts || 0)}</td>
          <td>${fmt.format(row.verified_authorizations || 0)}</td>
          <td>${fmt.format(row.settled_attempts || 0)}</td>
          <td>${fmt.format(row.validated_deliveries || 0)}</td>
          <td>${money.format(row.recognized_revenue_usdc || 0)}</td>
        </tr>`).join("") : `<tr><td colspan="10" class="empty">No commercial product activity in this window.</td></tr>`) + `</tbody>`;
    }

    function renderGrowthFunnel(data) {
      const funnel = data.growth_funnel || {};
      const summary = funnel.summary || {};
      const targets = funnel.targets || {};
      document.getElementById("growth-kpis").innerHTML = [
        summaryItem("Activated", fmt.format(summary.activated_identities || 0)),
        summaryItem("Under 3 min", pct(summary.first_live_price_within_3m_rate)),
        summaryItem("7-day repeat", pct(summary.repeat_7d_rate)),
        summaryItem("Starter to paid", pct(summary.starter_to_paid_rate)),
        summaryItem("Credits exhausted", fmt.format(summary.credits_exhausted_identities || 0)),
      ].join("");
      bars(
        "growth-stages",
        Object.fromEntries((funnel.stages || []).map(row => [row.stage, Number(row.identities || 0)])),
        "blue",
      );
      const targetRows = [
        ["First price under 3 min", summary.first_live_price_within_3m_rate, targets.first_live_price_within_3m_rate],
        ["Seven-day repeat", summary.repeat_7d_rate, targets.repeat_7d_rate],
        ["Starter to paid", summary.starter_to_paid_rate, targets.starter_to_paid_rate],
      ];
      document.getElementById("growth-targets").innerHTML = targetRows.map(([label, actual, target]) => {
        const status = actual == null ? "watch" : Number(actual) >= Number(target) ? "pass" : "watch";
        return `<div class="check">
          ${rowBadge(status)}
          <strong>${escapeAttr(label)}</strong>
          <div class="value">${pct(actual)} / ${pct(target)}</div>
          <span>Observed rate versus the provisional operating target.</span>
        </div>`;
      }).join("");
      const boundary = funnel.definitions?.measurement_boundary || "Identity-attributed events in the selected window.";
      const unattributed = Number(summary.unattributed_activation_events || 0);
      document.getElementById("growth-boundary").textContent =
        `${boundary} ${fmt.format(unattributed)} activation event${unattributed === 1 ? " is" : "s are"} currently unattributed.`;
    }

    function renderRwaPilot(data) {
      const pilot = data.rwa_growth_pilot || {};
      const depth = pilot.depth_and_manipulation_evidence?.summary || {};
      const packet = pilot.promotion_packet || {};
      const packetByPilot = Object.fromEntries((packet.feeds || []).map(row => [row.pilot_id, row]));
      const freshness = pilot.freshness || {};
      const freshnessFeeds = freshness.feeds || [];
      const healthyFeeds = freshnessFeeds.filter(row => row.healthy).length;
      const latestCapture = pilot.current_capture || {};
      document.getElementById("rwa-pilot-kpis").innerHTML = [
        summaryItem("Status", text(pilot.status)),
        summaryItem("Ledger freshness", text(freshness.status || "not_started")),
        summaryItem("Latest outcomes", `${fmt.format(latestCapture.succeeded || 0)}/${fmt.format(latestCapture.attempted || 0)}`),
        summaryItem("Healthy feeds", `${fmt.format(healthyFeeds)}/${fmt.format(freshnessFeeds.length)}`),
        summaryItem("Monitoring ready", pilot.source_monitoring_ready ? "Yes" : "No"),
        summaryItem("Volume windows", fmt.format(depth.point_in_time_volume_window_observed || 0)),
        summaryItem("Tick replays", fmt.format(depth.point_in_time_tick_replay_observed || 0)),
        summaryItem("Promoted feeds", fmt.format(pilot.production_promoted_feed_count || 0)),
      ].join("");
      const rows = pilot.feeds || [];
      document.getElementById("rwa-pilot-table").innerHTML =
        `<thead><tr><th>Feed</th><th>Source</th><th>Samples</th><th>Window</th><th>Success</th><th>Freshness</th><th>Latest</th><th>Monitoring</th><th>Gate progress</th><th>Decision</th></tr></thead><tbody>` +
        (rows.length ? rows.map(row => {
          const readiness = packetByPilot[row.pilot_id] || {};
          return `<tr>
          <td><code>${escapeAttr(row.symbol || row.pilot_id)}</code></td>
          <td>${escapeAttr(row.source_lane || row.venue)}</td>
          <td>${fmt.format(row.sample_count || 0)}</td>
          <td>${fmt.format(row.window_days || 0)} days</td>
          <td>${pct(row.success_rate)}</td>
          <td>${pct(row.freshness_rate)}</td>
          <td>${rowBadge(row.stale ? "stale" : "fresh")}</td>
          <td>${rowBadge(row.source_monitoring_ready ? "pass" : "collecting")}</td>
          <td>${fmt.format(readiness.passed_gate_count || 0)}/${fmt.format(readiness.required_gate_count || 0)}</td>
          <td title="${escapeAttr((readiness.blocking_gates || []).join(", "))}">${rowBadge(readiness.decision || "hold")}</td>
        </tr>`;
        }).join("") : `<tr><td colspan="10" class="empty">The authoritative RWA ledger has no pilot outcomes yet.</td></tr>`) +
        `</tbody>`;
    }

    function bars(target, data, color = "") {
      const entries = Object.entries(data || {}).slice(0, 8);
      const max = Math.max(1, ...entries.map(([, v]) => Number(v)));
      document.getElementById(target).innerHTML = entries.length ? entries.map(([k, v]) => `
        <div class="bar-row">
          <code title="${escapeAttr(k)}">${escapeAttr(k)}</code>
          <div class="track"><div class="fill ${color}" style="width:${Math.max(3, Number(v) / max * 100)}%"></div></div>
          <strong>${fmt.format(v)}</strong>
        </div>`).join("") : `<div class="empty">No activity in this window.</div>`;
    }

    const registrySourceWatchlist = [
      "Glama",
      "Pay.sh",
      "MCP Registry",
      "Smithery",
      "x402scan",
      "x402 Directory",
      "Awesome MCP",
      "GitHub",
      "GitLab",
      "OpenAPI crawlers",
      "Listing asset crawler",
    ];

    function registrySourceBars(data) {
      const known = new Set(registrySourceWatchlist);
      const watched = registrySourceWatchlist.map(label => [label, Number(data?.[label] || 0)]);
      const extra = Object.entries(data || {}).filter(([label]) => !known.has(label));
      const entries = [...watched, ...extra].slice(0, 14);
      const max = Math.max(1, ...entries.map(([, value]) => Number(value)));
      document.getElementById("registry-sources").innerHTML = entries.map(([label, value]) => `
        <div class="bar-row">
          <code title="${escapeAttr(label)}">${escapeAttr(label)}</code>
          <div class="track"><div class="fill amber" style="width:${Number(value) > 0 ? Math.max(3, Number(value) / max * 100) : 0}%"></div></div>
          <strong>${fmt.format(value)}</strong>
        </div>`).join("");
    }

    function renderSmitherySource(data) {
      const smithery = data.external_sources?.smithery || {};
      const localRegistry = Number(smithery.local_recorded_registry_calls || 0);
      const localMcp = Number(smithery.local_recorded_mcp_tool_calls || 0);
      const allMcp = Number(smithery.all_recorded_mcp_tool_calls || 0);
      const configured = Boolean(smithery.metrics_ingestion_configured);
      const status = configured ? "Metrics feed configured" : "Hosted metrics not ingested";
      const statusClass = configured ? "neutral" : "warn";
      document.getElementById("smithery-source").innerHTML = `
        <div>
          <div class="big">${fmt.format(localRegistry + localMcp)}</div>
          <div class="metric-note">Smithery-attributed calls recorded locally in this window</div>
        </div>
        <div>${rowBadge(status).replace('class="badge neutral"', `class="badge ${statusClass}"`)}</div>
        <div class="source-row"><span>Listing</span><a href="${escapeAttr(smithery.performance_url || "")}" target="_blank" rel="noreferrer">Smithery performance</a></div>
        <div class="source-row"><span>Hosted MCP</span><code title="${escapeAttr(smithery.hosted_mcp_endpoint || "")}">${escapeAttr(text(smithery.hosted_mcp_endpoint))}</code></div>
        <div class="source-row"><span>All MCP tools</span><strong>${fmt.format(allMcp)}</strong></div>
        <div class="metric-note">${escapeAttr(text(smithery.note))}</div>
      `;
    }

    function renderPayShSource(data) {
      const pay = data.external_sources?.pay_sh || {};
      const localCalls = Number(pay.local_recorded_calls || 0);
      const configured = Boolean(pay.metrics_ingestion_configured);
      const prompts = Number(data.event_counts?.payment_required || 0);
      const paid = Number(data.overview?.paid_calls || 0);
      const status = configured ? "Metrics feed configured" : "Catalog metrics not ingested";
      const statusClass = configured ? "neutral" : "warn";
      document.getElementById("pay-sh-source").innerHTML = `
        <div>
          <div class="big">${fmt.format(localCalls)}</div>
          <div class="metric-note">Pay.sh-attributed calls recorded locally in this window</div>
        </div>
        <div>${rowBadge(status).replace('class="badge neutral"', `class="badge ${statusClass}"`)}</div>
        <div class="source-row"><span>Listing</span><a href="${escapeAttr(pay.listing_url || "")}" target="_blank" rel="noreferrer">Pay.sh service</a></div>
        <div class="source-row"><span>x402 prompts</span><strong>${fmt.format(prompts)}</strong></div>
        <div class="source-row"><span>Paid calls</span><strong>${fmt.format(paid)}</strong></div>
        <div class="metric-note">${escapeAttr(text(pay.note))}</div>
      `;
    }

    function renderPlatformCoverage(data) {
      const platforms = data.external_sources?.platforms || [];
      const table = document.getElementById("platform-coverage");
      table.innerHTML = `<thead><tr><th>Platform</th><th>Local Calls</th><th>External Metrics</th><th>Owner / Reason</th><th>Release Truth</th><th>Listing</th><th>Notes</th></tr></thead><tbody>` +
        (platforms.length ? platforms.map(platform => {
          const status = platform.external_metrics_configured ? "Ingested" : "Unavailable";
          const badgeClass = platform.external_metrics_configured ? "neutral" : "warn";
          const releaseStatus = text(platform.release_status || "not audited").replaceAll("_", " ");
          const releaseClass = platform.release_status === "verified_live_baseline" ? "neutral" : "warn";
          return `<tr>
            <td><strong>${escapeAttr(platform.name)}</strong></td>
            <td>${fmt.format(platform.local_recorded_calls || 0)}</td>
            <td><span class="badge ${badgeClass}">${status}</span></td>
            <td><strong>${escapeAttr(platform.metrics_owner || "Unassigned")}</strong><div class="metric-note">${escapeAttr(platform.external_metrics_reason || "No reason recorded")}</div></td>
            <td><span class="badge ${releaseClass}">${escapeAttr(releaseStatus)}</span><div class="metric-note">${escapeAttr(platform.observed_version || "Version not exposed")} · audited ${escapeAttr(platform.audited_at || "unknown")}</div></td>
            <td><a href="${escapeAttr(platform.listing_url || "")}" title="${escapeAttr(platform.listing_url || "")}" target="_blank" rel="noreferrer">Open listing ↗</a></td>
            <td>${escapeAttr(platform.note || "")}</td>
          </tr>`;
        }).join("") : `<tr><td colspan="7" class="empty">No platform inventory configured.</td></tr>`) + `</tbody>`;
    }

    function summaryItem(label, value) {
      return `<div class="summary-item"><span>${escapeAttr(label)}</span><strong>${escapeAttr(value)}</strong></div>`;
    }

    function txExplorerUrl(network, txHash) {
      const net = String(network || "").toLowerCase();
      const hash = String(txHash || "").trim();
      if (!hash) return "";
      if (net.includes("solana") || net.includes("mainnet")) return `https://solscan.io/tx/${encodeURIComponent(hash)}`;
      if (net.includes("base") || net.includes("8453")) return `https://basescan.org/tx/${encodeURIComponent(hash)}`;
      return "";
    }

    function inflowKindLabel(kind) {
      if (kind === "credit_topup") return "Legacy local-QA top-up";
      if (kind === "direct_x402") return "Direct x402";
      return text(kind);
    }

    function renderWalletInflows(data) {
      const inflows = data.wallet_inflows || {};
      const rows = inflows.rows || [];
      document.getElementById("wallet-inflow-kpis").innerHTML = [
        summaryItem("Total", money.format(inflows.total_usdc || 0)),
        summaryItem("Inflows", fmt.format(inflows.total_inflows || 0)),
        summaryItem("Legacy QA top-ups", fmt.format(inflows.credit_topup_count || 0)),
        summaryItem("Direct x402", fmt.format(inflows.direct_x402_count || 0)),
      ].join("");
      const table = document.getElementById("wallet-inflow-table");
      table.innerHTML = `<thead><tr><th>Time</th><th>Type</th><th>Network</th><th>Amount</th><th>Credits</th><th>Wallet / Recipient</th><th>Purpose</th><th>Transaction Hash</th></tr></thead><tbody>` +
        (rows.length ? rows.slice(0, 50).map(row => {
          const tx = text(row.tx_hash);
          const url = txExplorerUrl(row.network, row.tx_hash);
          const wallet = row.wallet || row.recipient || "n/a";
          const txHtml = url
            ? `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer"><code>${escapeAttr(tx)}</code></a>`
            : `<code>${escapeAttr(tx)}</code>`;
          return `<tr>
            <td><code>${escapeAttr(text(row.timestamp).slice(0, 19).replace("T", " "))}</code></td>
            <td>${rowBadge(inflowKindLabel(row.kind))}</td>
            <td>${escapeAttr(text(row.network))}</td>
            <td>${money.format(row.amount_usdc || 0)}</td>
            <td>${row.credits_added == null ? "n/a" : fmt.format(row.credits_added)}</td>
            <td><code>${escapeAttr(wallet)}</code></td>
            <td><code>${escapeAttr(row.purpose || "")}</code></td>
            <td>${txHtml}</td>
          </tr>`;
        }).join("") : `<tr><td colspan="8" class="empty">No verified wallet inflows in this window.</td></tr>`) + `</tbody>`;
    }

    function popularityLabel(row) {
      const subject = text(row.subject);
      const service = text(row.service);
      return subject === service ? service : `${service}:${subject}`;
    }

    function popularityMap(rows, field) {
      return Object.fromEntries((rows || [])
        .filter(row => Number(row[field] || 0) > 0)
        .slice(0, 8)
        .map(row => [popularityLabel(row), Number(row[field] || 0)]));
    }

    function renderPopularity(data) {
      const popularity = data.popularity || {};
      const rows = popularity.rows || [];
      document.getElementById("popularity-kpis").innerHTML = [
        summaryItem("Requested", fmt.format(popularity.total_requested || 0)),
        summaryItem("Delivered", fmt.format(popularity.total_delivered || 0)),
        summaryItem("Blocked", fmt.format(popularity.total_blocked || 0)),
        summaryItem("Credits Used", fmt.format(popularity.total_credits_spent || 0)),
      ].join("");
      bars("popular-requested", popularityMap(rows, "requested"), "blue");
      bars("popular-delivered", popularityMap(rows, "delivered"), "");
      bars("popular-blocked", popularityMap(rows, "blocked"), "amber");
      const table = document.getElementById("popularity-table");
      table.innerHTML = `<thead><tr><th>Last Seen</th><th>Service</th><th>Data</th><th>Surface</th><th>Requested</th><th>Delivered</th><th>Credits</th><th>Blocked</th><th>Failed After Credit</th><th>Outcome</th></tr></thead><tbody>` +
        (rows.length ? rows.slice(0, 30).map(row => `<tr>
          <td><code>${escapeAttr(text(row.last_seen).slice(0, 19).replace("T", " "))}</code></td>
          <td><code>${escapeAttr(row.service)}</code></td>
          <td><code>${escapeAttr(row.subject)}</code></td>
          <td>${escapeAttr(row.surface)}</td>
          <td>${fmt.format(row.requested || 0)}</td>
          <td>${fmt.format(row.delivered || 0)}</td>
          <td>${fmt.format(row.credits_spent || 0)}</td>
          <td>${fmt.format(row.blocked || 0)}</td>
          <td>${fmt.format(row.failed_after_credit || 0)}</td>
          <td>${rowBadge(row.leading_outcome)}</td>
        </tr>`).join("") : `<tr><td colspan="10" class="empty">No popularity signals in this window.</td></tr>`) + `</tbody>`;
    }

    function shortDate(isoDate) {
      const [year, month, day] = String(isoDate || "").split("-");
      if (!month || !day) return text(isoDate);
      return `${Number(month)}/${Number(day)}`;
    }

    function escapeAttr(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function registryBreakdown(sources) {
      const ordered = [
        ["Glama", "glama", Number(sources?.["Glama"] || 0)],
        ["Pay.sh", "pay-sh", Number(sources?.["Pay.sh"] || 0)],
        ["MCP Registry", "mcp-registry", Number(sources?.["MCP Registry"] || 0)],
        ["Smithery", "smithery", Number(sources?.["Smithery"] || 0)],
        ["x402scan", "x402-directory", Number(sources?.["x402scan"] || 0)],
        ["x402 Directory", "x402-directory", Number(sources?.["x402 Directory"] || 0)],
        ["Awesome MCP", "awesome-mcp", Number(sources?.["Awesome MCP"] || 0)],
        ["GitHub", "github-source", Number(sources?.["GitHub"] || 0)],
        ["GitLab", "gitlab-source", Number(sources?.["GitLab"] || 0)],
        ["OpenAPI crawlers", "openapi-source", Number(sources?.["OpenAPI crawlers"] || 0)],
        ["Listing asset crawler", "listing-asset", Number(sources?.["Listing asset crawler"] || 0)],
      ];
      const known = new Set(ordered.map(([label]) => label));
      const other = Object.entries(sources || {})
        .filter(([label]) => !known.has(label))
        .reduce((sum, [, value]) => sum + Number(value || 0), 0);
      return [...ordered, ["Other registry", "registry-other", other]];
    }

    function registrySourceSegments(sources, total, height) {
      return registryBreakdown(sources).filter(([, , value]) => value > 0).map(([label, cls, value]) => {
        const segmentHeight = Math.max(1, value / total * height);
        const labelText = `${label}: ${fmt.format(value)}`;
        return `<div class="seg ${cls}" style="height:${segmentHeight}%" title="${escapeAttr(labelText)}"></div>`;
      }).join("");
    }

    function timelineTip(d) {
      const total = Number(d.http_requests || 0) + Number(d.mcp_tool_calls || 0) + Number(d.registry_requests || 0);
      const registryRows = registryBreakdown(d.registry_sources || {})
        .map(([label, , value]) => `${label}: ${fmt.format(value)}`);
      return [
        text(d.date),
        `Total: ${fmt.format(total)}`,
        `HTTP: ${fmt.format(d.http_requests || 0)}`,
        `MCP tools: ${fmt.format(d.mcp_tool_calls || 0)}`,
        ...registryRows,
      ].join("\\n");
    }

    function timeline(rows) {
      const max = Math.max(1, ...rows.map(d => d.http_requests + d.mcp_tool_calls + d.registry_requests));
      const labelEvery = rows.length <= 10 ? 1 : rows.length <= 31 ? 7 : rows.length <= 90 ? 14 : 30;
      document.getElementById("timeline").innerHTML = rows.map(d => {
        const total = Math.max(1, d.http_requests + d.mcp_tool_calls + d.registry_requests);
        const h = Math.max(1, total / max * 100);
        const http = Math.max(1, d.http_requests / total * h);
        const mcp = d.mcp_tool_calls ? Math.max(1, d.mcp_tool_calls / total * h) : 0;
        const reg = registrySourceSegments(d.registry_sources || {}, total, h);
        const tip = timelineTip(d);
        return `<div class="day" tabindex="0" data-tip="${escapeAttr(tip)}" title="${escapeAttr(tip)}">
          ${reg}
          <div class="seg mcp" style="height:${mcp}%" title="MCP tools: ${fmt.format(d.mcp_tool_calls || 0)}"></div>
          <div class="seg http" style="height:${http}%" title="HTTP: ${fmt.format(d.http_requests || 0)}"></div>
        </div>`;
      }).join("");
      document.getElementById("timeline-dates").innerHTML = rows.map((d, index) => {
        const farEnoughFromEnd = rows.length - 1 - index >= Math.ceil(labelEvery / 2);
        const show = index === 0 || index === rows.length - 1 || (index % labelEvery === 0 && farEnoughFromEnd);
        return `<span class="date-tick" title="${escapeAttr(text(d.date))}">${show ? escapeAttr(shortDate(d.date)) : ""}</span>`;
      }).join("");
    }

    function renderAttention(data) {
      const o = data.overview || {};
      const prompts = Number(data.event_counts?.payment_required || 0);
      const settled = Number(data.event_counts?.payment_settled || 0);
      const abandoned = Math.max(0, prompts - settled);
      const errorRate = o.server_error_rate == null ? 0 : Number(o.server_error_rate);
      const registrySources = Object.keys(data.registry_source_mix || {}).length;
      const items = [
        {
          tone: abandoned > 0 ? "warn" : "",
          title: `${fmt.format(abandoned)} payment prompt${abandoned === 1 ? "" : "s"} without paid success`,
          note: abandoned > 0 ? "Follow up on pricing, wallet flow, or x402 client friction." : "No abandoned payment prompts in this window."
        },
        {
          tone: errorRate > 0 ? "bad" : "",
          title: `${pct(errorRate)} server error rate`,
          note: errorRate > 0 ? "Review HTTP 5xx and upstream failures in the trace." : "No HTTP 5xx responses observed in this window."
        },
        {
          tone: registrySources ? "" : "warn",
          title: `${fmt.format(registrySources)} registry source${registrySources === 1 ? "" : "s"} observed`,
          note: registrySources ? "Directory attribution is flowing into the dashboard." : "No Glama, Pay.sh, or MCP registry traffic observed."
        }
      ];
      document.getElementById("attention").innerHTML = items.map(item => `
        <div class="status-item"><span class="status-dot ${toneClass(item.tone)}"></span><div><strong>${escapeAttr(item.title)}</strong><span>${escapeAttr(item.note)}</span></div></div>
      `).join("");
    }

    function recent(rows) {
      const q = document.getElementById("event-search").value.trim().toLowerCase();
      const filtered = (rows || []).filter(row => !q || JSON.stringify(row).toLowerCase().includes(q)).slice(0, 18);
      const table = document.getElementById("recent");
      table.innerHTML = `<thead><tr><th>Time</th><th>Event</th><th>Surface</th><th>Endpoint</th><th>Subject</th><th>Status</th><th>Price</th></tr></thead><tbody>` +
        (filtered.length ? filtered.map(row => `<tr>
          <td><code>${escapeAttr(text(row.timestamp).slice(0, 19).replace("T", " "))}</code></td>
          <td>${rowBadge(row.event)}</td>
          <td>${escapeAttr(text(row.surface))}</td>
          <td><code>${escapeAttr(text(row.endpoint || row.tool_name))}</code></td>
          <td><code>${escapeAttr(text(row.subject || row.reason))}</code></td>
          <td>${escapeAttr(text(row.status_code))}</td>
          <td>${row.price_usdc == null ? "n/a" : money.format(row.price_usdc)}</td>
        </tr>`).join("") : `<tr><td colspan="7" class="empty">No matching events in this window.</td></tr>`) + `</tbody>`;
    }

    function dataCalled(rows) {
      const q = document.getElementById("data-search").value.trim().toLowerCase();
      const outcome = document.getElementById("outcome-filter").value.toLowerCase();
      const filtered = (rows || []).filter(row => {
        const haystack = `${row.service} ${row.subject} ${row.asset_class} ${row.surface} ${row.latest_outcome}`.toLowerCase();
        return (!q || haystack.includes(q)) && (!outcome || text(row.latest_outcome).toLowerCase().includes(outcome));
      }).slice(0, 40);
      const table = document.getElementById("data-called");
      table.innerHTML = `<thead><tr><th>Last Viewed</th><th>Service</th><th>Data</th><th>Asset Class</th><th>Origin</th><th>Outcome</th><th>Prompt Price</th><th>Paid</th><th>Revenue</th></tr></thead><tbody>` +
        (filtered.length ? filtered.map(row => `<tr>
          <td><code>${escapeAttr(text(row.last_seen).slice(0, 19).replace("T", " "))}</code></td>
          <td><code>${escapeAttr(text(row.service))}</code></td>
          <td><code>${escapeAttr(text(row.subject))}</code></td>
          <td>${escapeAttr(text(row.asset_class))}</td>
          <td>${escapeAttr(text(row.surface))}</td>
          <td>${rowBadge(row.latest_outcome)}</td>
          <td>${row.prompt_price_usdc == null ? "n/a" : money.format(row.prompt_price_usdc)}</td>
          <td>${fmt.format(row.paid_successes || 0)}</td>
          <td>${money.format(row.revenue_usdc || 0)}</td>
        </tr>`).join("") : `<tr><td colspan="9" class="empty">No matching called data in this window.</td></tr>`) + `</tbody>`;
    }

    async function load() {
      const days = document.getElementById("window").value;
      const includeSynthetic = document.getElementById("telemetry-scope").value;
      const statsUrl = new URL(statsPath, window.location.origin);
      statsUrl.searchParams.set("days", days);
      statsUrl.searchParams.set("include_synthetic", includeSynthetic);
      const res = await fetch(statsUrl.toString(), { cache: "no-store", credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.error || "Unable to load stats");
      currentData = data;
      const o = data.overview || {};
      const g = data.growth_funnel?.summary || {};
      const prompts = Number(data.event_counts?.payment_required || 0);
      const paid = Number(o.paid_calls || 0);
      const conversion = prompts ? paid / prompts : null;
      const scope = data.telemetry_scope || {};
      const scopeNote = scope.include_synthetic
        ? ` Includes ${fmt.format(scope.detected_synthetic_events || 0)} test/synthetic events.`
        : ` Excludes ${fmt.format(scope.excluded_synthetic_events || 0)} test/synthetic events.`;
      document.getElementById("freshness").textContent = `Generated ${new Date(data.generated_at).toLocaleString()} over the last ${data.window_days} day${data.window_days === 1 ? "" : "s"}.${scopeNote} ${live ? "Live refresh is on." : "Live refresh is paused."}`;
      const confidence = data.decision_confidence || {};
      const confidenceBanner = document.getElementById("decision-confidence");
      confidenceBanner.classList.toggle("ready", confidence.level === "decision_ready");
      document.getElementById("confidence-label").textContent = confidence.label || "Evidence status unavailable";
      document.getElementById("confidence-summary").textContent = confidence.summary || "Review the raw event trace before using this snapshot for decisions.";
      document.getElementById("headline-value").textContent = g.activated_identities ? `${fmt.format(g.activated_identities)} activated` : "No activation yet";
      document.getElementById("headline-note").textContent = g.activated_identities ? "Explicit identities that received their first live price in the selected window." : "No identity-attributed first live price has been recorded in this window.";
      document.getElementById("kpis").innerHTML = [
        metric("Activation", pct(g.activation_rate), "First live price / eligible explicit identities"),
        metric("Time to Value", g.median_time_to_first_live_price_seconds == null ? "n/a" : `${fmt.format(g.median_time_to_first_live_price_seconds)}s`, "Median discovery-to-first-live-price time"),
        metric("7-Day Repeat", pct(g.repeat_7d_rate), "Mature activated identities with repeat delivery"),
        metric("Starter to Paid", pct(g.starter_to_paid_rate), "Starter activations later tied to finalized settlement"),
        metric("Successful Deliveries", fmt.format(paid), "Completed x402, credit-backed HTTP, and authenticated MCP data returns"),
        metric("Revenue", money.format(o.estimated_revenue_usdc || 0), "Deduplicated finalized x402 settlements"),
        metric("Server Errors", pct(data.reliability?.server_error_rate), "HTTP 5xx responses / all HTTP requests"),
        metric("Unsupported Demand", fmt.format(o.unsupported_symbol_requests || 0), "Bounded zero-result symbol searches"),
      ].join("");
      renderAttention(data);
      renderGrowthFunnel(data);
      renderRwaPilot(data);
      renderDailyInterpretation(data);
      renderOperatorAlerts(data);
      renderRevenueReconciliation(data);
      renderResolverFunnel(data);
      renderProductPerformance(data);
      timeline(data.timeline || []);
      const payment = data.payment_funnel || {};
      bars("funnel", {
        "x402 prompts": payment.x402_prompts || prompts,
        "raw proof events": payment.raw_proof_submission_events || 0,
        "correlated attempts": payment.correlated_proof_attempts || 0,
        "authorization verified": payment.verified_authorizations || 0,
        "settled payments": payment.settled_attempts || 0,
        "credit drawdowns": data.event_counts?.credit_drawdown_success || 0,
        "validated deliveries": payment.successful_deliveries || paid,
      }, "amber");
      renderPopularity(data);
      renderWalletInflows(data);
      registrySourceBars(data.registry_source_mix);
      bars("campaigns", data.campaign_mix, "blue");
      bars("outbound-destinations", data.outbound_destination_mix, "amber");
      renderPayShSource(data);
      renderSmitherySource(data);
      renderPlatformCoverage(data);
      bars("services", data.service_mix);
      bars("origins", data.origin_mix, "blue");
      bars("mcp", data.mcp_tool_mix, "blue");
      bars("registries", data.registry_mix, "amber");
      bars("agents", data.user_agent_mix, "blue");
      dataCalled(data.data_called);
      recent(data.recent_events);
    }

    function rerenderTables() {
      if (!currentData) return;
      dataCalled(currentData.data_called);
      recent(currentData.recent_events);
    }

    function scheduleLive() {
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = live && !document.hidden ? setInterval(load, 10000) : null;
      document.getElementById("live").setAttribute("aria-pressed", String(live));
      document.getElementById("live").textContent = live ? "Live 10s" : "Live off";
    }

    document.getElementById("window").addEventListener("change", load);
    document.getElementById("telemetry-scope").addEventListener("change", load);
    document.getElementById("refresh").addEventListener("click", load);
    document.getElementById("data-search").addEventListener("input", rerenderTables);
    document.getElementById("outcome-filter").addEventListener("change", rerenderTables);
    document.getElementById("event-search").addEventListener("input", rerenderTables);
    document.getElementById("live").addEventListener("click", () => {
      live = !live;
      scheduleLive();
      load();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (refreshTimer) clearInterval(refreshTimer);
        refreshTimer = null;
        return;
      }
      scheduleLive();
      if (live) load();
    });
    scheduleLive();
    function syncActiveNav() {
      const activeHash = window.location.hash || "#overview";
      document.querySelectorAll("nav a").forEach(link => {
        link.classList.toggle("active", link.getAttribute("href") === activeHash);
      });
    }
    window.addEventListener("hashchange", syncActiveNav);
    syncActiveNav();
    load().catch(err => {
      document.getElementById("freshness").textContent = err.message;
    });
  </script>
</body>
</html>"""
    return html.replace("__STATS_PATH__", json.dumps(stats_path))


_OBSERVABILITY_COOKIE_NAME = "observability_token"
_OBSERVABILITY_COOKIE_PATH = "/internal/observability"
_OBSERVABILITY_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 12
_OBSERVABILITY_LOGIN_BODY_LIMIT = 4096


async def _read_observability_login_body(request: Request) -> bytes:
    """Read the login form with a hard ceiling for fixed or streamed bodies."""
    raw_content_length = request.headers.get("content-length")
    if raw_content_length:
        try:
            content_length = int(raw_content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if content_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if content_length > _OBSERVABILITY_LOGIN_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="Login form is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _OBSERVABILITY_LOGIN_BODY_LIMIT:
            raise HTTPException(status_code=413, detail="Login form is too large")
    return bytes(body)


@app.post("/internal/observability/login", include_in_schema=False, response_model=None)
async def observability_login(request: Request) -> Any:
    """Exchange a form password for a secure, narrowly scoped dashboard cookie."""
    expected = dashboard_token()
    if not expected:
        return _observability_unauthorized()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return JSONResponse(
            status_code=415,
            headers={"Cache-Control": "no-store"},
            content={"error": "Expected application/x-www-form-urlencoded"},
        )
    try:
        encoded_body = await _read_observability_login_body(request)
        decoded_body = encoded_body.decode("utf-8", errors="strict")
        fields = parse_qs(
            decoded_body,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=400,
            headers={"Cache-Control": "no-store"},
            content={"error": "Login form must be UTF-8"},
        )
    except ValueError:
        return JSONResponse(
            status_code=400,
            headers={"Cache-Control": "no-store"},
            content={"error": "Malformed login form"},
        )

    password_values = fields.get("password", [])
    if set(fields) != {"password"} or len(password_values) != 1:
        return JSONResponse(
            status_code=400,
            headers={"Cache-Control": "no-store"},
            content={"error": "Login form must contain one password field"},
        )
    supplied = password_values[0]
    if not supplied or not secrets.compare_digest(expected, supplied):
        return _observability_login_page(request, login_failed=True)

    response = RedirectResponse(
        "/internal/observability/command-center",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        _OBSERVABILITY_COOKIE_NAME,
        expected,
        max_age=_OBSERVABILITY_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path=_OBSERVABILITY_COOKIE_PATH,
    )
    return response


@app.get("/internal/observability/command-center", include_in_schema=False, response_model=None)
async def observability_command_center(request: Request) -> Any:
    """Serve the protected internal product usage command center."""
    if not _observability_authorized(request):
        if not dashboard_token():
            return _observability_unauthorized()
        return _observability_login_page(request)
    if OBSERVABILITY is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"error": "Observability disabled"},
        )

    return HTMLResponse(
        headers={"Cache-Control": "no-store"},
        content=_observability_command_center_html(
            stats_path="/internal/observability/stats",
        ),
    )


@app.get("/internal/observability/logout", include_in_schema=False, response_model=None)
async def observability_logout() -> RedirectResponse:
    """Clear local observability access and return to the login screen."""
    response = RedirectResponse("/internal/observability/command-center", status_code=303)
    response.delete_cookie(
        _OBSERVABILITY_COOKIE_NAME,
        path=_OBSERVABILITY_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/internal/observability/legacy", include_in_schema=False, response_model=None)
async def observability_legacy_dashboard(request: Request) -> Any:
    """Serve the original lightweight internal product observability dashboard."""
    if not _observability_authorized(request):
        if not dashboard_token():
            return _observability_unauthorized()
        return _observability_login_page(request)
    stats_path = "/internal/observability/stats"
    return HTMLResponse(
        headers={"Cache-Control": "no-store"},
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17201c;
      --muted: #637068;
      --line: #d7ddd8;
      --bg: #f7f8f5;
      --panel: #ffffff;
      --green: #1f7a4d;
      --blue: #315e9f;
      --amber: #9a6418;
      --red: #a53b32;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{ margin: 0; font-size: 26px; letter-spacing: 0; }}
    .sub {{ margin-top: 6px; color: var(--muted); font-size: 14px; }}
    main {{ padding: 24px 32px 40px; max-width: 1440px; margin: 0 auto; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 18px; flex-wrap: wrap; }}
    .controls {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    button {{ border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); padding: 8px 10px; cursor: pointer; }}
    button[aria-pressed="true"] {{ border-color: var(--green); color: var(--green); font-weight: 700; }}
    select {{ border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); padding: 8px 10px; }}
    .grid {{ display: grid; gap: 16px; }}
    .kpis {{ grid-template-columns: repeat(6, minmax(150px, 1fr)); }}
    .two {{ grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr); margin-top: 16px; }}
    .three {{ grid-template-columns: repeat(3, minmax(260px, 1fr)); margin-top: 16px; }}
    .wide {{ margin-top: 16px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }}
    .metric-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    .metric-value {{ font-size: 25px; font-weight: 700; margin-top: 8px; overflow-wrap: anywhere; }}
    .metric-note {{ color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 16px; }}
    h2 {{ font-size: 16px; margin: 0 0 12px; letter-spacing: 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .bars {{ display: grid; gap: 8px; }}
    .bar-row {{ display: grid; grid-template-columns: minmax(90px, 160px) 1fr 64px; gap: 10px; align-items: center; font-size: 13px; }}
    .track {{ height: 10px; background: #e8ece8; border-radius: 999px; overflow: hidden; }}
    .fill {{ height: 100%; background: var(--green); }}
    .fill.blue {{ background: var(--blue); }}
    .fill.amber {{ background: var(--amber); }}
    .timeline {{ display: flex; align-items: end; gap: 5px; height: 170px; padding-top: 8px; border-bottom: 1px solid var(--line); }}
    .day {{ flex: 1; min-width: 5px; display: flex; flex-direction: column; justify-content: end; gap: 2px; height: 100%; }}
    .seg {{ width: 100%; min-height: 1px; }}
    .seg.http {{ background: var(--blue); }}
    .seg.mcp {{ background: var(--green); }}
    .seg.registry {{ background: var(--amber); }}
    .legend {{ display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }}
    .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .empty {{ color: var(--muted); font-size: 13px; }}
    @media (max-width: 1000px) {{
      main, header {{ padding-left: 18px; padding-right: 18px; }}
      .kpis, .two, .three {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 680px) {{
      .kpis, .two, .three {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: minmax(90px, 1fr) 1fr 48px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Blocksize Observability</h1>
    <div class="sub" id="freshness">Loading usage telemetry...</div>
  </header>
  <main>
    <div class="toolbar">
      <div class="sub">Registry, MCP, discovery, payment, credit, and data usage.</div>
      <div class="controls">
        <button id="live" type="button" aria-pressed="true">Live 10s</button>
        <button id="refresh" type="button">Refresh</button>
        <label class="sub">Window
          <select id="window">
            <option value="7">7 days</option>
            <option value="30" selected>30 days</option>
            <option value="90">90 days</option>
            <option value="180">180 days</option>
          </select>
        </label>
      </div>
    </div>
    <section class="grid kpis" id="kpis"></section>
    <section class="grid two">
      <div class="card">
        <h2>Usage Trend</h2>
        <div class="timeline" id="timeline"></div>
        <div class="legend">
          <span><i class="dot" style="background: var(--blue)"></i>HTTP</span>
          <span><i class="dot" style="background: var(--green)"></i>MCP tools</span>
          <span><i class="dot" style="background: var(--amber)"></i>Registries</span>
        </div>
      </div>
      <div class="card">
        <h2>Monetization Funnel</h2>
        <div id="funnel" class="bars"></div>
      </div>
    </section>
    <section class="grid three">
      <div class="card"><h2>Most Used Services</h2><div id="services" class="bars"></div></div>
      <div class="card"><h2>Call Origins</h2><div id="origins" class="bars"></div></div>
      <div class="card"><h2>Data Called</h2><div id="subjects" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>Registry Sources</h2><div id="registry-sources" class="bars"></div></div>
      <div class="card"><h2>Registry Surfaces</h2><div id="registries" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>MCP Tool Mix</h2><div id="mcp" class="bars"></div></div>
      <div class="card"><h2>Referrers and Clients</h2><div id="clients" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>Paid Endpoint Mix</h2><div id="paid" class="bars"></div></div>
      <div class="card"><h2>User Agent Families</h2><div id="agents" class="bars"></div></div>
    </section>
    <section class="card wide">
      <h2>Called Data Detail</h2>
      <table id="data-called"></table>
    </section>
    <section class="grid two">
      <div class="card"><h2>Recent Events</h2><table id="recent"></table></div>
    </section>
  </main>
  <script>
    const statsPath = {json.dumps(stats_path)};
    const fmt = new Intl.NumberFormat(undefined, {{ maximumFractionDigits: 3 }});
    const money = new Intl.NumberFormat(undefined, {{ style: "currency", currency: "USD", maximumFractionDigits: 4 }});
    const pct = value => value == null ? "n/a" : `${{Math.round(value * 1000) / 10}}%`;
    const text = value => value == null || value === "" ? "n/a" : String(value);
    const htmlEscapes = Object.freeze({{
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }});
    const escapeHtml = value => String(value).replace(/[&<>"']/g, character => htmlEscapes[character]);
    const escapeAttr = escapeHtml;
    const finiteNumber = value => {{
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }};
    let live = true;
    let refreshTimer = null;

    function metric(label, value, note = "") {{
      return `<div class="card"><div class="metric-label">${{escapeHtml(label)}}</div><div class="metric-value">${{escapeHtml(value)}}</div><div class="metric-note">${{escapeHtml(note)}}</div></div>`;
    }}

    function bars(target, data, color = "") {{
      const entries = Object.entries(data || {{}}).slice(0, 10);
      const max = Math.max(1, ...entries.map(([, v]) => finiteNumber(v)));
      document.getElementById(target).innerHTML = entries.length ? entries.map(([k, v]) => `
        <div class="bar-row">
          <code title="${{escapeAttr(text(k))}}">${{escapeHtml(text(k))}}</code>
          <div class="track"><div class="fill ${{escapeAttr(color)}}" style="width:${{Math.max(2, finiteNumber(v) / max * 100)}}%"></div></div>
          <strong>${{fmt.format(finiteNumber(v))}}</strong>
        </div>`).join("") : `<div class="empty">No activity in this window.</div>`;
    }}

    function timeline(rows) {{
      const max = Math.max(1, ...rows.map(d => finiteNumber(d.http_requests) + finiteNumber(d.mcp_tool_calls) + finiteNumber(d.registry_requests)));
      document.getElementById("timeline").innerHTML = rows.map(d => {{
        const httpRequests = finiteNumber(d.http_requests);
        const mcpCalls = finiteNumber(d.mcp_tool_calls);
        const registryRequests = finiteNumber(d.registry_requests);
        const total = httpRequests + mcpCalls + registryRequests;
        const h = Math.max(1, total / max * 100);
        const http = Math.max(1, httpRequests / Math.max(1, total) * h);
        const mcp = mcpCalls ? Math.max(1, mcpCalls / Math.max(1, total) * h) : 0;
        const reg = registryRequests ? Math.max(1, registryRequests / Math.max(1, total) * h) : 0;
        return `<div class="day" title="${{escapeAttr(text(d.date))}}: ${{fmt.format(httpRequests)}} HTTP, ${{fmt.format(mcpCalls)}} MCP, ${{fmt.format(registryRequests)}} registry">
          <div class="seg registry" style="height:${{reg}}%"></div>
          <div class="seg mcp" style="height:${{mcp}}%"></div>
          <div class="seg http" style="height:${{http}}%"></div>
        </div>`;
      }}).join("");
    }}

    function recent(rows) {{
      const table = document.getElementById("recent");
      table.innerHTML = `<thead><tr><th>Time</th><th>Event</th><th>Surface</th><th>Subject</th></tr></thead><tbody>` +
        (rows || []).slice(0, 12).map(row => `<tr>
          <td><code>${{escapeHtml(text(row.timestamp).slice(0, 19).replace("T", " "))}}</code></td>
          <td>${{escapeHtml(text(row.event))}}</td>
          <td>${{escapeHtml(text(row.surface || row.endpoint))}}</td>
          <td><code>${{escapeHtml(text(row.tool_name || row.subject || row.reason))}}</code></td>
        </tr>`).join("") + `</tbody>`;
    }}

    function dataCalled(rows) {{
      const table = document.getElementById("data-called");
      const items = (rows || []).slice(0, 20);
      table.innerHTML = `<thead><tr><th>Last Viewed</th><th>Service</th><th>Data</th><th>Asset Class</th><th>Origin</th><th>Outcome</th><th>Prompt Price</th><th>Paid Success</th><th>Revenue</th></tr></thead><tbody>` +
        (items.length ? items.map(row => `<tr>
          <td><code>${{escapeHtml(text(row.last_seen).slice(0, 19).replace("T", " "))}}</code></td>
          <td><code>${{escapeHtml(text(row.service))}}</code></td>
          <td><code>${{escapeHtml(text(row.subject))}}</code></td>
          <td>${{escapeHtml(text(row.asset_class))}}</td>
          <td>${{escapeHtml(text(row.surface))}}</td>
          <td>${{escapeHtml(text(row.latest_outcome))}}</td>
          <td>${{row.prompt_price_usdc == null ? "n/a" : money.format(row.prompt_price_usdc)}}</td>
          <td>${{fmt.format(finiteNumber(row.paid_successes))}}</td>
          <td>${{money.format(finiteNumber(row.revenue_usdc))}}</td>
        </tr>`).join("") : `<tr><td colspan="9" class="empty">No called data in this window.</td></tr>`) + `</tbody>`;
    }}

    async function load() {{
      const days = document.getElementById("window").value;
      const sep = statsPath.includes("?") ? "&" : "?";
      const res = await fetch(`${{statsPath}}${{sep}}days=${{days}}`, {{ cache: "no-store" }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.error || "Unable to load stats");
      const o = data.overview;
      document.getElementById("freshness").textContent = `Generated ${{new Date(data.generated_at).toLocaleString()}} over the last ${{data.window_days}} days. ${{live ? "Live refresh is on." : "Live refresh is paused."}}`;
      document.getElementById("kpis").innerHTML = [
        metric("Paid Calls", fmt.format(o.paid_calls), "x402 + credit + MCP credit usage"),
        metric("Revenue", money.format(o.estimated_revenue_usdc), "deduplicated finalized x402 settlements"),
        metric("MCP Tools", fmt.format(o.mcp_tool_calls), "tool-level activity"),
        metric("Registry Hits", fmt.format(o.registry_requests), "directory and metadata discovery"),
        metric("Top Service", text(o.most_used_service), "highest-volume called service"),
        metric("Unique Clients", fmt.format(o.unique_client_fingerprints), "hashed IP fingerprints"),
        metric("Payment Success", pct(o.payment_success_rate), "settled / correlated submitted proofs"),
      ].join("");
      timeline(data.timeline || []);
      bars("funnel", {{
        "free discovery": o.free_discovery_calls,
        "402 challenges": data.event_counts.payment_required || 0,
        "proof submissions": data.event_counts.payment_proof_submitted || 0,
        "authorization verified": data.event_counts.payment_authorization_verified || 0,
        "settled payments": data.event_counts.payment_settled || 0,
        "credit drawdowns": data.event_counts.credit_drawdown_success || 0,
      }}, "amber");
      bars("services", data.service_mix);
      bars("origins", data.origin_mix, "blue");
      bars("registry-sources", data.registry_source_mix, "amber");
      bars("campaigns", data.campaign_mix, "blue");
      bars("outbound-destinations", data.outbound_destination_mix, "amber");
      bars("registries", data.registry_mix, "amber");
      bars("mcp", data.mcp_tool_mix, "blue");
      bars("paid", data.paid_endpoint_mix);
      bars("subjects", data.top_subjects, "blue");
      bars("clients", data.referrer_mix && Object.keys(data.referrer_mix).length ? data.referrer_mix : data.client_fingerprint_mix, "amber");
      bars("agents", data.user_agent_mix, "blue");
      dataCalled(data.data_called);
      recent(data.recent_events);
    }}
    function scheduleLive() {{
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = live ? setInterval(load, 10000) : null;
      document.getElementById("live").setAttribute("aria-pressed", String(live));
      document.getElementById("live").textContent = live ? "Live 10s" : "Live off";
    }}
    document.getElementById("window").addEventListener("change", load);
    document.getElementById("refresh").addEventListener("click", load);
    document.getElementById("live").addEventListener("click", () => {{
      live = !live;
      scheduleLive();
      load();
    }});
    scheduleLive();
    load().catch(err => {{
      document.getElementById("freshness").textContent = err.message;
    }});
  </script>
</body>
</html>""",
    )


def _database_path_is_writable(raw_path: str) -> bool:
    """Check a database target without creating or modifying it."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        return path.is_file() and os.access(path, os.W_OK)
    parent = path.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


_CREDIT_DB_SCHEMA = {
    "wallets": {"address", "balance_credits", "last_updated"},
    "credit_purchases": {"tx_hash", "address", "amount_usdc", "credits_added"},
    "trial_history": {"ip_hash", "subject_hash", "subject_type"},
    "payment_proofs": {
        "tx_hash",
        "state",
        "request_binding",
        "reservation_id",
        "settled_at",
        "finalized_at",
        "response_body",
    },
    "credit_charges": {"charge_id", "address", "credits", "state"},
    "rate_limit_events": {"scope", "key_hash", "occurred_at"},
    "price_receipts": {"receipt_id", "payload_json", "created_at"},
}
_OBSERVABILITY_DB_SCHEMA = {
    "usage_events": {"id", "timestamp", "event", "metadata_json"},
    "marketplace_metric_snapshots": {"id", "timestamp", "platform_id", "metrics_json"},
    "event_milestones": {"event", "identity_hash", "timestamp"},
}


def _resolved_runtime_path(raw_path: str | os.PathLike[str]) -> Path:
    path = Path(raw_path).expanduser()
    return (path if path.is_absolute() else Path.cwd() / path).resolve(strict=False)


def _path_on_railway_volume(path: Path) -> bool:
    try:
        path.relative_to(Path("/data"))
    except ValueError:
        return False
    return True


def _sqlite_database_readiness(
    raw_path: str | os.PathLike[str],
    *,
    expected_schema: dict[str, set[str]],
    hosted: bool,
    required: bool = True,
    configured_path: str | os.PathLike[str] | None = None,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate the exact runtime SQLite file, integrity, and critical schema."""
    path = _resolved_runtime_path(raw_path)
    blockers: list[str] = []
    if configured_path is not None and path != _resolved_runtime_path(configured_path):
        blockers.append("runtime_path_mismatch")
    if hosted and not _path_on_railway_volume(path):
        blockers.append("not_on_railway_volume")
    if not path.is_file():
        blockers.append("database_missing")
    elif not os.access(path, os.R_OK | os.W_OK):
        blockers.append("database_not_read_write")

    integrity = "unavailable"
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    if deadline_seconds is None:
        try:
            deadline_seconds = float(
                os.environ.get("STORE_READINESS_PROBE_DEADLINE_SECONDS", "2")
            )
        except ValueError:
            deadline_seconds = 2.0
    deadline_seconds = max(0.1, min(float(deadline_seconds), 10.0))
    deadline = time.monotonic() + deadline_seconds
    if not blockers or all(item == "not_on_railway_volume" for item in blockers):
        try:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                timeout=min(1.0, deadline_seconds),
            )
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
            try:
                # Readiness validates the small schema b-tree, not every page in
                # a potentially multi-gigabyte event ledger. Full-database
                # integrity belongs in an offline maintenance job; running it
                # on a health poll can make the health check the outage source.
                integrity_rows = connection.execute(
                    "PRAGMA quick_check('sqlite_schema')"
                ).fetchall()
                integrity = (
                    "ok"
                    if integrity_rows and all(str(row[0]).lower() == "ok" for row in integrity_rows)
                    else "failed"
                )
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                missing_tables = sorted(set(expected_schema) - tables)
                for table, required_columns in expected_schema.items():
                    if table not in tables:
                        continue
                    columns = {
                        str(row[1])
                        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    missing = sorted(required_columns - columns)
                    if missing:
                        missing_columns[table] = missing
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            if time.monotonic() >= deadline:
                integrity = "timeout"
                blockers.append("integrity_check_timeout")
            else:
                integrity = "unavailable"
                blockers.append("database_unreadable")
    if integrity != "ok":
        blockers.append("integrity_check_failed")
    if missing_tables:
        blockers.append("schema_tables_missing")
    if missing_columns:
        blockers.append("schema_columns_missing")
    blockers = sorted(set(blockers))
    return {
        "ready": not blockers if required else True,
        "required": required,
        "path": str(path),
        "absolute": Path(raw_path).expanduser().is_absolute(),
        "on_railway_volume": _path_on_railway_volume(path),
        "integrity": integrity,
        "integrity_scope": "sqlite_schema",
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "blockers": blockers,
    }


def _store_readiness_max_age_seconds() -> float:
    try:
        configured = float(
            os.environ.get("STORE_READINESS_PROBE_MAX_AGE_SECONDS", "180")
        )
    except ValueError:
        configured = 180.0
    return min(900.0, max(30.0, configured))


def _sqlite_readiness_configuration_fingerprint(
    raw_path: str | os.PathLike[str],
    *,
    expected_schema: dict[str, set[str]],
    hosted: bool,
    required: bool = True,
    configured_path: str | os.PathLike[str] | None = None,
) -> str:
    material = {
        "runtime_path": str(_resolved_runtime_path(raw_path)),
        "configured_path": (
            str(_resolved_runtime_path(configured_path))
            if configured_path is not None
            else None
        ),
        "hosted": hosted,
        "required": required,
        "schema": {
            table: sorted(columns)
            for table, columns in sorted(expected_schema.items())
        },
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _rwa_readiness_configuration_fingerprint(
    store: RWAObservationStore | None,
    *,
    configured_path: str | os.PathLike[str],
    hosted: bool,
) -> str:
    material = {
        "runtime_path": (
            str(_resolved_runtime_path(store.db_path))
            if isinstance(store, RWAObservationStore)
            else None
        ),
        "configured_path": str(_resolved_runtime_path(configured_path)),
        "hosted": hosted,
        "schema_version": RWA_STORE_SCHEMA_VERSION,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _transaction_bridge_configuration_fingerprint(
    credit_db_path: str | os.PathLike[str],
    connector_db_paths: dict[str, str | os.PathLike[str]],
) -> str:
    material = {
        "credit_db_path": str(_resolved_runtime_path(credit_db_path)),
        "connector_db_paths": {
            name: str(_resolved_runtime_path(path))
            for name, path in sorted(connector_db_paths.items())
        },
        "lock": legacy_transaction_bridge_lock_status(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cached_store_readiness(
    name: str,
    *,
    configuration_fingerprint: str,
    required: bool = True,
) -> dict[str, Any]:
    snapshots = getattr(app.state, "store_readiness_snapshots", {})
    snapshot = snapshots.get(name) if isinstance(snapshots, dict) else None
    max_age = _store_readiness_max_age_seconds()
    if not isinstance(snapshot, dict):
        return {
            "ready": not required,
            "required": required,
            "checked": False,
            "age_seconds": None,
            "max_age_seconds": max_age,
            "reason": "not_checked",
            "blockers": [] if not required else ["readiness_probe_not_checked"],
        }

    checked_at = snapshot.get("checked_at")
    try:
        age_seconds = max(0.0, time.time() - float(checked_at))
    except (TypeError, ValueError):
        age_seconds = None
    result = dict(snapshot.get("result") or {})
    blockers = set(result.get("blockers") or [])
    reason = result.get("reason")
    if snapshot.get("configuration_fingerprint") != configuration_fingerprint:
        reason = "configuration_changed_since_probe"
        blockers.add("configuration_changed_since_probe")
    elif age_seconds is None or age_seconds > max_age:
        reason = "probe_stale"
        blockers.add("readiness_probe_stale")
    ready = bool(result.get("ready")) and not blockers and reason is None
    return {
        **result,
        "ready": ready if required else True,
        "required": required,
        "checked": True,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "max_age_seconds": max_age,
        "reason": reason,
        "blockers": sorted(blockers),
    }


async def _refresh_store_readiness_snapshots(target_app: FastAPI) -> None:
    """Refresh bounded store probes once; callers serialize refresh cycles."""
    hosted = _hosted_environment()
    credit_manager = getattr(target_app.state, "credits", None)
    rwa_store = getattr(target_app.state, "rwa_store", None)
    rwa_db_path = configured_rwa_observation_db_path()
    observability_runtime_path = (
        str(OBSERVABILITY.db_path)
        if OBSERVABILITY is not None
        else settings.server.observability_db_path
    )

    probes: list[tuple[str, str, Any]] = []
    if isinstance(credit_manager, CreditManager):
        credit_kwargs = {
            "expected_schema": _CREDIT_DB_SCHEMA,
            "hosted": hosted,
            "configured_path": os.environ.get("CREDIT_DB_PATH", "credits.db"),
        }
        probes.append(
            (
                "credit_ledger",
                _sqlite_readiness_configuration_fingerprint(
                    credit_manager.db_path,
                    **credit_kwargs,
                ),
                asyncio.to_thread(
                    _sqlite_database_readiness,
                    credit_manager.db_path,
                    **credit_kwargs,
                ),
            )
        )
        connector_db_paths = {
            prefix.lower(): connector_entitlement_db_path(prefix)
            for prefix in (
                ["ANTHROPIC"]
                if _anthropic_only_mode()
                else ["ANTHROPIC", "CURSOR", "OPENAI"]
            )
        }
        probes.append(
            (
                "legacy_transaction_bridge",
                _transaction_bridge_configuration_fingerprint(
                    credit_manager.db_path,
                    connector_db_paths,
                ),
                asyncio.to_thread(
                    transaction_bridge_readiness,
                    credit_manager.db_path,
                    connector_db_paths,
                ),
            )
        )
    if settings.server.observability_enabled:
        observability_kwargs = {
            "expected_schema": _OBSERVABILITY_DB_SCHEMA,
            "hosted": hosted,
            "configured_path": settings.server.observability_db_path,
        }
        probes.append(
            (
                "observability_store",
                _sqlite_readiness_configuration_fingerprint(
                    observability_runtime_path,
                    **observability_kwargs,
                ),
                asyncio.to_thread(
                    _sqlite_database_readiness,
                    observability_runtime_path,
                    **observability_kwargs,
                ),
            )
        )
    if isinstance(rwa_store, RWAObservationStore):
        probes.append(
            (
                "rwa_operator_store",
                _rwa_readiness_configuration_fingerprint(
                    rwa_store,
                    configured_path=rwa_db_path,
                    hosted=hosted,
                ),
                asyncio.to_thread(rwa_store.schema_status, force=True),
            )
        )

    results = await asyncio.gather(
        *(probe[2] for probe in probes),
        return_exceptions=True,
    )
    checked_at = time.time()
    snapshots: dict[str, dict[str, Any]] = {}
    for (name, configuration_fingerprint, _), result in zip(probes, results):
        if isinstance(result, Exception):
            result = {
                "ready": False,
                "integrity": "unavailable",
                "reason": "readiness_probe_failed",
                "blockers": ["readiness_probe_failed"],
            }
        snapshots[name] = {
            "checked_at": checked_at,
            "configuration_fingerprint": configuration_fingerprint,
            "result": result,
        }
    target_app.state.store_readiness_snapshots = snapshots


async def _run_store_readiness_probe_loop(target_app: FastAPI) -> None:
    """Refresh store snapshots without overlapping large SQLite work."""
    interval = max(15.0, _store_readiness_max_age_seconds() / 3)
    while True:
        await asyncio.sleep(interval)
        await _refresh_store_readiness_snapshots(target_app)


def _oauth_storage_readiness(prefix: str, *, hosted: bool, production: bool) -> dict[str, Any]:
    """Require durable encrypted proxy-OAuth state for production connectors."""
    provider = os.environ.get(f"{prefix}_AUTH_PROVIDER", "none").strip().lower()
    provider_requires_auth = provider not in {"", "none", "dev", "beta-token"}
    required = (production or hosted) and provider_requires_auth
    raw_path = os.environ.get(f"{prefix}_OAUTH_STORAGE_DIR", "").strip()
    path = _resolved_runtime_path(raw_path) if raw_path else None
    jwt_ready = is_strong_secret(os.environ.get(f"{prefix}_OAUTH_JWT_SIGNING_KEY"))
    encryption_ready = is_strong_secret(
        os.environ.get(f"{prefix}_OAUTH_STORAGE_ENCRYPTION_KEY")
    )
    blockers: list[str] = []
    # Supabase validates bearer JWTs directly and does not construct the local
    # authorize/token/register/callback route surface advertised by this host.
    local_route_provider = provider in {"clerk", "auth0"}
    if required and not local_route_provider:
        blockers.append("provider_missing_local_oauth_routes")
    if required and not jwt_ready:
        blockers.append("jwt_signing_key_missing_or_weak")
    if required and not encryption_ready:
        blockers.append("storage_encryption_key_missing_or_weak")
    if required and path is None:
        blockers.append("storage_directory_missing")
    if required and path is not None:
        if not Path(raw_path).expanduser().is_absolute():
            blockers.append("storage_directory_not_absolute")
        if hosted and not _path_on_railway_volume(path):
            blockers.append("storage_directory_not_on_railway_volume")
        if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            blockers.append("storage_directory_unavailable")
    return {
        "ready": not blockers,
        "required": required,
        "backend": "encrypted_filetree" if raw_path else "none",
        "path": str(path) if path is not None else None,
        "absolute": bool(raw_path and Path(raw_path).expanduser().is_absolute()),
        "on_railway_volume": bool(path and _path_on_railway_volume(path)),
        "jwt_signing_key_strong": jwt_ready,
        "storage_encryption_key_strong": encryption_ready,
        "local_oauth_route_provider": local_route_provider,
        "blockers": sorted(set(blockers)),
    }


def _stream_cache_readiness(stream_cache: BlocksizeStreamCache | None) -> dict[str, Any]:
    required = _blocksize_dependency_required() and not _anthropic_only_mode()
    status = stream_cache.status() if isinstance(stream_cache, BlocksizeStreamCache) else {}
    blockers: list[str] = []
    if required and not status.get("enabled"):
        blockers.append("stream_cache_disabled")
    if required and not status.get("ready"):
        blockers.append("stream_not_connected")
    if required and int(status.get("fixed_vwap_tickers") or 0) < 1:
        blockers.append("no_fixed_vwap_subscriptions")
    configured_tickers = int(status.get("fixed_vwap_tickers") or 0)
    fresh_tickers = int(status.get("fresh_configured_24h_vwap") or 0)
    if required and fresh_tickers < configured_tickers:
        blockers.append("configured_fixed_vwap_cache_not_fully_seeded")
    return {
        "ready": not blockers,
        "required": required,
        "enabled": bool(status.get("enabled")),
        "connected": bool(status.get("ready")),
        "fixed_vwap_tickers": configured_tickers,
        "cached_24h_vwap": int(status.get("cached_24h_vwap") or 0),
        "fresh_configured_24h_vwap": fresh_tickers,
        "blockers": blockers,
    }


def _state_store_isolation(paths: dict[str, str | os.PathLike[str] | None]) -> dict[str, Any]:
    resolved_paths: list[tuple[str, Path]] = []
    for label, raw_path in paths.items():
        if raw_path:
            resolved_paths.append((label, _resolved_runtime_path(raw_path)))
    collision_pairs: set[tuple[str, str]] = set()
    for index, (left_label, left_path) in enumerate(resolved_paths):
        for right_label, right_path in resolved_paths[index + 1 :]:
            overlaps = (
                left_path == right_path
                or left_path in right_path.parents
                or right_path in left_path.parents
            )
            if overlaps:
                collision_pairs.add(tuple(sorted((left_label, right_label))))
    collisions = [list(labels) for labels in sorted(collision_pairs)]
    return {
        "ready": not collisions,
        "collisions": collisions,
    }


def _release_manifest_check() -> dict[str, Any]:
    """Validate the packaged/tracked MCP manifest against the running version."""
    manifest_path = next((path for path in SERVER_JSON_PATHS if path.is_file()), None)
    if manifest_path is None:
        return {"ready": False, "reason": "server.json is not packaged"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ready": False, "reason": "server.json is unreadable or invalid"}
    description = str(manifest.get("description", ""))
    version_matches = manifest.get("version") == APP_VERSION
    description_valid = 1 <= len(description) <= 100
    return {
        "ready": version_matches and description_valid,
        "version_matches": version_matches,
        "description_length": len(description),
        "description_valid": description_valid,
    }


def _connector_entitlement_readiness(
    prefix: str,
    *,
    hosted: bool,
    production: bool,
    initialize: bool,
) -> dict[str, Any]:
    raw_path = connector_entitlement_db_path(prefix)
    path = Path(raw_path).expanduser()
    blockers: list[str] = []
    if production and not path.is_absolute():
        blockers.append("entitlement_db_not_durable")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(Path("/data"))
        on_railway_volume = True
    except ValueError:
        on_railway_volume = False
    if hosted and not on_railway_volume:
        blockers.append("entitlement_db_not_on_railway_volume")
    if (production or hosted) and not _database_path_is_writable(str(path)):
        blockers.append("entitlement_db_not_writable")

    schema: dict[str, object] = {
        "ready": not (production or hosted),
        "integrity": "not_required",
        "missing_tables": [],
    }
    if initialize and not blockers:
        try:
            manager = connector_entitlement_manager(prefix)
            schema = manager.schema_status()
        except (OSError, sqlite3.Error, ValueError):
            schema = {
                "ready": False,
                "integrity": "unavailable",
                "missing_tables": [],
            }
    elif production or hosted:
        cached = getattr(app.state, "connector_entitlement_statuses", {}).get(prefix)
        if isinstance(cached, dict) and cached.get("path") == str(resolved):
            cached_schema = cached.get("schema")
            if isinstance(cached_schema, dict):
                schema = cached_schema
        # A path/configuration mismatch must fail closed. Never instantiate or
        # scan an entitlement database from the readiness request path: hosted
        # health polling must remain constant-time even when state is large or
        # damaged. The next controlled process start refreshes this snapshot.
    if (production or hosted) and not schema.get("ready"):
        blockers.append("entitlement_schema_unavailable")
    return {
        "ready": not blockers,
        "required": production or hosted,
        "path": str(resolved),
        "absolute": path.is_absolute(),
        "on_railway_volume": on_railway_volume,
        "schema": schema,
        "blockers": sorted(set(blockers)),
    }


def _connector_readiness(
    prefix: str,
    connector: Any,
    entitlement: dict[str, Any],
) -> dict[str, Any]:
    provider = os.environ.get(f"{prefix}_AUTH_PROVIDER", "none").strip().lower()
    provider_requires_auth = provider not in {"", "none", "dev", "beta-token"}
    auth_constructed = getattr(connector, "auth", None) is not None
    production = is_production_environment()
    hosted = _hosted_environment()
    strict_auth = production or hosted
    beta_enabled = _env_enabled(f"{prefix}_ENABLE_BETA_TOKENS")
    connector_slug = prefix.lower()
    connector_url = os.environ.get(
        f"{prefix}_MCP_PUBLIC_URL",
        f"{PUBLIC_BASE_URL.rstrip('/')}/{connector_slug}/mcp",
    ).rstrip("/")
    connector_parsed = urlsplit(connector_url)
    base_parsed = urlsplit(PUBLIC_BASE_URL)
    connector_url_ready = (
        connector_parsed.scheme in {"http", "https"}
        and bool(connector_parsed.netloc)
        and (connector_parsed.scheme, connector_parsed.netloc)
        == (base_parsed.scheme, base_parsed.netloc)
        and connector_parsed.path.rstrip("/") == f"/{connector_slug}/mcp"
        and not connector_parsed.query
        and not connector_parsed.fragment
    )
    oauth_storage = _oauth_storage_readiness(
        prefix,
        hosted=hosted,
        production=production,
    )
    auth_ready = (
        auth_constructed and provider_requires_auth and not beta_enabled
        if strict_auth
        else not provider_requires_auth or auth_constructed
    )
    return {
        "ready": (
            auth_ready
            and bool(entitlement.get("ready"))
            and bool(oauth_storage.get("ready"))
            and (connector_url_ready or not strict_auth)
        ),
        "provider": provider or "none",
        "auth_constructed": auth_constructed,
        "beta_tokens_enabled": beta_enabled,
        "production_auth_required": strict_auth,
        "public_url": {
            "ready": connector_url_ready or not strict_auth,
            "url": connector_url,
            "expected_origin": f"{base_parsed.scheme}://{base_parsed.netloc}",
            "expected_path": f"/{connector_slug}/mcp",
            "reason": (
                None
                if connector_url_ready or not strict_auth
                else "connector_public_url_must_match_PUBLIC_BASE_URL"
            ),
        },
        "oauth_storage": oauth_storage,
        "entitlement_ledger": entitlement,
    }


def _mark_connector_entitlement_collisions(
    entitlement_statuses: dict[str, dict[str, Any]],
    protected_paths: dict[str, str],
) -> None:
    """Fail readiness when a connector ledger shares any other state store."""
    connector_paths = [str(status.get("path") or "") for status in entitlement_statuses.values()]
    connector_paths = [path for path in connector_paths if path]
    connectors_share_path = len(connector_paths) != len(set(connector_paths))
    for status in entitlement_statuses.values():
        path = str(status.get("path") or "")
        blockers = set(status.get("blockers") or [])
        collisions: list[str] = []
        if connectors_share_path and connector_paths.count(path) > 1:
            blockers.add("entitlement_db_shared_between_connectors")
            collisions.append("connector_entitlements")
        for label, protected_path in protected_paths.items():
            if path and protected_path and database_paths_collide(path, protected_path):
                blockers.add(f"entitlement_db_shared_with_{label}")
                collisions.append(label)
        if collisions:
            status["ready"] = False
        status["blockers"] = sorted(blockers)
        status["database_collisions"] = sorted(set(collisions))


def _rwa_runtime_reports_readiness() -> dict[str, Any]:
    """Validate the exact override-aware reports consumed by the runtime."""
    effective_paths = effective_rwa_report_paths(reports_dir=RWA_REPORTS_DIR)
    failures = {
        filename: list(errors)
        for filename, path in effective_paths.items()
        if (errors := inspect_required_rwa_report(filename, path))
    }
    if not failures:
        cross_report_errors = inspect_daily_xyz_reconciliation(
            effective_paths["rwa_daily_feed_agent.json"],
            effective_paths["rwa_xyz_new_asset_monitor.json"],
            reports_dir=RWA_REPORTS_DIR,
        )
        if cross_report_errors:
            failures["rwa_daily_feed_agent.json"] = list(cross_report_errors)
    return {
        "ready": not failures,
        "required_report_count": len(READINESS_REQUIRED_RWA_REPORT_PATHS),
        "checked_report_count": len(effective_paths),
        "failures": failures,
    }


def _readiness_report() -> dict[str, Any]:
    missing_docs = [
        relative_path
        for relative_path in READINESS_REQUIRED_DOC_PATHS
        if not (DOCS_DIR / relative_path).is_file()
    ]
    rwa_runtime_reports = _rwa_runtime_reports_readiness()
    runtime_ready = all(
        getattr(app.state, name, None) is not None
        for name in ("blocksize", "stream_cache", "credits", "rwa_store")
    )
    hosted = _hosted_environment()
    require_payment_wallet = _env_enabled(
        "READINESS_REQUIRE_PAYMENT_WALLET",
        "true" if hosted and not _anthropic_only_mode() else "false",
    )
    payment_rails = settings.x402.payment_rail_status()
    facilitator_support = getattr(app.state, "facilitator_support", None)
    facilitator_readiness = _facilitator_support_readiness(facilitator_support)
    operational_payment_requirements = _facilitator_supported_requirements(
        settings.payment_requirements(Decimal("0.000001")),
        facilitator_support,
    )
    payment_wallet_ready = bool(operational_payment_requirements) or not require_payment_wallet
    rwa_db_path = configured_rwa_observation_db_path()
    credit_manager = getattr(app.state, "credits", None)
    credit_store = (
        _cached_store_readiness(
            "credit_ledger",
            configuration_fingerprint=_sqlite_readiness_configuration_fingerprint(
                credit_manager.db_path,
                expected_schema=_CREDIT_DB_SCHEMA,
                hosted=hosted,
                configured_path=os.environ.get("CREDIT_DB_PATH", "credits.db"),
            ),
        )
        if isinstance(credit_manager, CreditManager)
        else {
            "ready": False,
            "required": True,
            "integrity": "unavailable",
            "blockers": ["runtime_store_missing"],
        }
    )
    bridge_lock = legacy_transaction_bridge_lock_status()
    bridge_connector_paths = {
        prefix.lower(): connector_entitlement_db_path(prefix)
        for prefix in (
            ["ANTHROPIC"]
            if _anthropic_only_mode()
            else ["ANTHROPIC", "CURSOR", "OPENAI"]
        )
    }
    legacy_transaction_bridge = (
        _cached_store_readiness(
            "legacy_transaction_bridge",
            configuration_fingerprint=_transaction_bridge_configuration_fingerprint(
                credit_manager.db_path,
                bridge_connector_paths,
            ),
            required=bool(bridge_lock["economic_writes_locked"]),
        )
        if isinstance(credit_manager, CreditManager)
        else {
            **bridge_lock,
            "ready": not bridge_lock["economic_writes_locked"],
            "required": bool(bridge_lock["economic_writes_locked"]),
            "checked": False,
            "direct_counts": None,
            "blockers": ["runtime_store_missing"],
        }
    )
    observability_runtime_path = (
        str(OBSERVABILITY.db_path)
        if OBSERVABILITY is not None
        else settings.server.observability_db_path
    )
    observability_store = (
        _cached_store_readiness(
            "observability_store",
            configuration_fingerprint=_sqlite_readiness_configuration_fingerprint(
                observability_runtime_path,
                expected_schema=_OBSERVABILITY_DB_SCHEMA,
                hosted=hosted,
                configured_path=settings.server.observability_db_path,
            ),
        )
        if settings.server.observability_enabled
        else {
            "ready": True,
            "required": False,
            "enabled": False,
            "integrity": "not_required",
            "blockers": [],
        }
    )
    observability_store["enabled"] = settings.server.observability_enabled
    runtime_state_paths = (
        {"credits_runtime": str(credit_manager.db_path)}
        if isinstance(credit_manager, CreditManager)
        else None
    )
    rwa_boundary = rwa_security_status(
        settings.server.observability_db_path,
        runtime_state_paths,
    )
    rwa_store = getattr(app.state, "rwa_store", None)
    rwa_schema = (
        _cached_store_readiness(
            "rwa_operator_store",
            configuration_fingerprint=_rwa_readiness_configuration_fingerprint(
                rwa_store,
                configured_path=rwa_db_path,
                hosted=hosted,
            ),
        )
        if isinstance(rwa_store, RWAObservationStore)
        else {
            "ready": False,
            "schema_version": 0,
            "integrity": "unavailable",
            "blockers": ["runtime_store_missing"],
        }
    )
    rwa_runtime_path = (
        _resolved_runtime_path(rwa_store.db_path)
        if isinstance(rwa_store, RWAObservationStore)
        else _resolved_runtime_path(rwa_db_path)
    )
    rwa_blockers: list[str] = []
    if rwa_runtime_path != _resolved_runtime_path(rwa_db_path):
        rwa_blockers.append("runtime_path_mismatch")
    if hosted and not _path_on_railway_volume(rwa_runtime_path):
        rwa_blockers.append("not_on_railway_volume")
    if not _database_path_is_writable(str(rwa_runtime_path)):
        rwa_blockers.append("database_not_writable")
    if not rwa_schema.get("ready"):
        rwa_blockers.append("schema_or_integrity_check_failed")
    rwa_blockers.extend(rwa_schema.get("blockers") or [])
    if not rwa_boundary.get("ready"):
        rwa_blockers.append("operator_boundary_not_ready")

    entitlement_statuses = {
        prefix: _connector_entitlement_readiness(
            prefix,
            hosted=hosted,
            production=is_production_environment(),
            initialize=False,
        )
        for prefix in (
            ["ANTHROPIC"]
            if _anthropic_only_mode()
            else ["ANTHROPIC", "CURSOR", "OPENAI"]
        )
    }
    _mark_connector_entitlement_collisions(
        entitlement_statuses,
        {
            "credit_ledger": (
                str(credit_manager.db_path)
                if isinstance(credit_manager, CreditManager)
                else os.environ.get("CREDIT_DB_PATH", "credits.db")
            ),
            "observability_store": settings.server.observability_db_path,
            "rwa_store": rwa_db_path,
        },
    )
    connectors = {
        "anthropic": _connector_readiness(
            "ANTHROPIC",
            anthropic_mcp,
            entitlement_statuses["ANTHROPIC"],
        ),
    }
    if not _anthropic_only_mode():
        connectors.update(
            {
                "cursor": _connector_readiness(
                    "CURSOR", cursor_mcp, entitlement_statuses["CURSOR"]
                ),
                "openai": _connector_readiness(
                    "OPENAI", openai_mcp, entitlement_statuses["OPENAI"]
                ),
            }
        )

    state_paths: dict[str, str | os.PathLike[str] | None] = {
        "credit_ledger": (
            credit_manager.db_path if isinstance(credit_manager, CreditManager) else None
        ),
        "observability_store": (
            observability_runtime_path if settings.server.observability_enabled else None
        ),
        "rwa_store": (
            rwa_store.db_path if isinstance(rwa_store, RWAObservationStore) else rwa_db_path
        ),
    }
    for prefix, entitlement in entitlement_statuses.items():
        state_paths[f"{prefix.lower()}_entitlements"] = str(entitlement.get("path") or "")
    for name, connector_status in connectors.items():
        oauth_path = connector_status.get("oauth_storage", {}).get("path")
        state_paths[f"{name}_oauth_storage"] = str(oauth_path or "")
    state_store_isolation = _state_store_isolation(state_paths)

    checks: dict[str, Any] = {
        "static_product": {
            "ready": not missing_docs,
            "missing": missing_docs,
        },
        "rwa_runtime_reports": rwa_runtime_reports,
        "release_manifest": _release_manifest_check(),
        "release_provenance": {
            "ready": bool(RELEASE_BUILD.get("commit_sha")) or not hosted,
            "required": hosted,
            "commit_sha": RELEASE_BUILD.get("commit_sha"),
            "source_branch": RELEASE_BUILD.get("source_branch"),
        },
        "deployment_security_policy": {
            "ready": not hosted or is_production_environment(),
            "required": hosted,
            "strict_environment": is_production_environment(),
            "reason": (
                None
                if not hosted or is_production_environment()
                else "hosted_runtime_requires_APP_ENV_production"
            ),
        },
        "runtime": {"ready": runtime_ready},
        "blocksize_upstream": _blocksize_dependency_readiness(
            getattr(app.state, "blocksize", None)
        ),
        "stream_cache": _stream_cache_readiness(
            getattr(app.state, "stream_cache", None)
        ),
        "credit_ledger": credit_store,
        "legacy_transaction_bridge": legacy_transaction_bridge,
        "payment_wallet": {
            "ready": payment_wallet_ready,
            "required": require_payment_wallet,
            "configured_rails": sum(
                bool(status["configured"])
                for status in payment_rails.values()
            ),
            "operational_rails": len(operational_payment_requirements),
            "rails": payment_rails,
        },
        "observability_store": observability_store,
        "state_store_isolation": state_store_isolation,
        "privacy_security": security_configuration_status(),
        "trusted_identity": trusted_identity_configuration_status(),
        "payment_security": payment_security_status(
            production=security_configuration_status()["production"],
            railway_hosted=hosted,
            facilitator_url=settings.x402.facilitator_url,
            facilitator_bearer_configured=bool(
                settings.x402.facilitator_bearer_token
            ),
            cdp_api_key_id_configured=bool(settings.x402.cdp_api_key_id),
            cdp_api_key_secret_configured=bool(settings.x402.cdp_api_key_secret),
            mock_enabled=settings.server.x402_allow_mock_payments,
            legacy_enabled=settings.server.x402_allow_legacy_payments,
            networks=[
                str(requirement["network"])
                for requirement in operational_payment_requirements
            ],
            trusted_proxies=settings.server.forwarded_allow_ips,
            freshness_seconds=settings.server.x402_payment_max_age_seconds,
            finality_confirmations=settings.server.x402_payment_min_confirmations,
            verification_lease_seconds=(
                settings.server.x402_payment_verification_lease_seconds
            ),
            replay_ttl_seconds=settings.server.x402_payment_replay_ttl_seconds,
            replay_max_entries=settings.server.x402_payment_replay_max_entries,
            credit_db_path=(credit_manager.db_path if isinstance(credit_manager, CreditManager) else None),
        ),
        "facilitator_support": {
            "ready": (
                facilitator_readiness["ready"]
                and (
                    not facilitator_readiness["required"]
                    or bool(operational_payment_requirements)
                )
            ),
            "required": facilitator_readiness["required"],
            "checked": facilitator_readiness["checked"],
            "available": facilitator_readiness["available"],
            "age_seconds": facilitator_readiness.get("age_seconds"),
            "max_age_seconds": facilitator_readiness.get("max_age_seconds"),
            "supported_networks": sorted(
                {
                    str(kind.get("network"))
                    for kind in (facilitator_support or {}).get("kinds", [])
                    if isinstance(kind, dict) and kind.get("network")
                }
            ),
            "advertised_networks": [
                str(requirement["network"])
                for requirement in operational_payment_requirements
            ],
            "reason": (
                facilitator_readiness.get("reason")
                or (
                    "no_supported_configured_rail"
                    if facilitator_readiness["required"]
                    and not operational_payment_requirements
                    else None
                )
            ),
        },
        "rwa_operator_store": {
            **rwa_boundary,
            "ready": not rwa_blockers,
            "path": str(rwa_runtime_path),
            "on_railway_volume": _path_on_railway_volume(rwa_runtime_path),
            "blockers": sorted(set(rwa_blockers)),
            "schema_version": rwa_schema.get("schema_version", 0),
            "integrity": rwa_schema.get("integrity", "unavailable"),
            "integrity_scope": rwa_schema.get("integrity_scope", "rwa_store_metadata"),
            "checked": rwa_schema.get("checked", False),
            "age_seconds": rwa_schema.get("age_seconds"),
            "max_age_seconds": rwa_schema.get("max_age_seconds"),
            "reason": rwa_schema.get("reason"),
        },
        "connectors": {
            "ready": all(item["ready"] for item in connectors.values()),
            "providers": connectors,
        },
    }
    ready = all(bool(check.get("ready")) for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "service": "blocksize-mcp-x402",
        "version": APP_VERSION,
        "commit_sha": RELEASE_BUILD.get("commit_sha"),
        "checks": checks,
    }


_PUBLIC_READINESS_REDACTED_KEYS = frozenset({"path", "database_path"})


def _public_readiness_report(value: Any) -> Any:
    """Copy readiness data without exposing internal filesystem locations."""
    if isinstance(value, dict):
        return {
            key: _public_readiness_report(item)
            for key, item in value.items()
            if key not in _PUBLIC_READINESS_REDACTED_KEYS
        }
    if isinstance(value, list):
        return [_public_readiness_report(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_readiness_report(item) for item in value)
    return value


@app.get("/readyz", include_in_schema=False)
async def readiness_check() -> JSONResponse:
    """Return dependency-aware release readiness for deployment promotion."""
    report = _readiness_report()
    return JSONResponse(
        status_code=200 if report["ready"] else 503,
        content=_public_readiness_report(report),
    )


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check — free."""
    if _anthropic_only_mode():
        anthropic_oauth_available = _connector_local_oauth_available(
            "ANTHROPIC", anthropic_mcp
        )
        return {
            "status": "healthy",
            "service": "blocksize-anthropic-mcp-beta",
            "version": APP_VERSION,
            "commit_sha": RELEASE_BUILD.get("commit_sha"),
            "mcp_url": _anthropic_mcp_url(),
            "transport": "streamable-http",
            "auth_provider": os.environ.get("ANTHROPIC_AUTH_PROVIDER", "none"),
            "oauth_available": anthropic_oauth_available,
            **(
                {"oauth_callback_url": anthropic_auth.oauth_callback_url()}
                if anthropic_oauth_available
                else {}
            ),
            **(
                {
                    "oauth_protected_resource_metadata": (
                        f"{PUBLIC_BASE_URL.rstrip()}/.well-known/"
                        "oauth-protected-resource/anthropic/mcp/"
                    ),
                    "oauth_authorization_server_metadata": (
                        f"{PUBLIC_BASE_URL.rstrip()}/.well-known/"
                        "oauth-authorization-server/anthropic/mcp"
                    ),
                }
                if anthropic_oauth_available
                else {}
            ),
            "documentation": CLAUDE_CONNECTOR_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "readiness": f"{PUBLIC_BASE_URL.rstrip('/')}/readyz",
            "beta_tokens_enabled": anthropic_auth.beta_tokens_enabled(),
            "daily_credits": int(os.environ.get("ANTHROPIC_DAILY_CREDITS", "50")),
            "starter_allowance": {
                "positioning": "Authenticated connectors receive up to 50 live data credits.",
                "eligibility": "authenticated_connector_only",
                "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            },
            "tool_surface": "read-only",
            "tool_costs": ANTHROPIC_TOOL_COSTS,
        }

    facilitator_support = getattr(app.state, "facilitator_support", None)
    health_payment_requirements = _facilitator_supported_requirements(
        settings.payment_requirements(Decimal("0.000001")),
        facilitator_support,
    )
    operational_networks = {
        str(requirement["network"]) for requirement in health_payment_requirements
    }
    rail_status = settings.x402.payment_rail_status()
    anthropic_oauth_available = _connector_local_oauth_available(
        "ANTHROPIC", anthropic_mcp
    )
    cursor_oauth_available = _connector_local_oauth_available("CURSOR", cursor_mcp)
    openai_oauth_available = _connector_local_oauth_available("OPENAI", openai_mcp)
    return {
        "status": "healthy",
        "service": "blocksize-mcp-x402",
        "version": APP_VERSION,
        "commit_sha": RELEASE_BUILD.get("commit_sha"),
        "engine": "Shielded x402 Gateway (Iron Dome Active)",
        "readiness": f"{PUBLIC_BASE_URL.rstrip('/')}/readyz",
        "networks": {
            "primary": {
                "name": "Solana",
                "configured": bool(rail_status["solana"]["configured"]),
                "operational": settings.x402.solana_network in operational_networks,
            },
            "fallback": {
                "name": "Base",
                "configured": bool(rail_status["base"]["configured"]),
                "operational": settings.x402.base_network in operational_networks,
            },
        },
        "payments": {
            "operational": bool(health_payment_requirements),
            "advertised_networks": sorted(operational_networks),
            "facilitator_capabilities_checked": _facilitator_support_readiness(
                facilitator_support
            )["ready"],
            "readiness": f"{PUBLIC_BASE_URL.rstrip('/')}/readyz",
        },
        "pricing": settings.pricing_summary,
        **(
            {"legacy_local_qa_bulk_pricing": BULK_TIERS}
            if settings.server.unverified_http_credits_enabled
            and not security_configuration_status()["production"]
            else {}
        ),
        "starter_allowance": {
            "positioning": "Authenticated connectors receive up to 50 live data credits.",
            "eligibility": "authenticated_connector_only",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "applies_to": "raw data, batches, market briefs, pre-trade checks, audit receipts, macro snapshots, and provenance lookups",
            "direct_public_http": "Signed x402 payment is required per live-data request.",
            "upgrade_path": "Contact sales for sustained access through an authenticated account plan.",
        },
        "equities": {
            "positioning": "Supported equity tickers are first-class Blocksize symbols.",
            "discovery": "/v1/search?q=AAPL&asset_class=equity",
            "live_endpoint_template": "/v1/bidask/{ticker}",
            "example_endpoint": "/v1/bidask/AAPL",
            "credit_cost": 1,
            "price_usdc": str(settings.pricing.equities),
        },
        "links": {
            "coverage": f"{PUBLIC_BASE_URL.rstrip('/')}/v1/coverage",
            "remote_mcp": REMOTE_MCP_URL,
            "manifest": MCP_MANIFEST_URL,
            "robots": ROBOTS_URL,
            "sitemap": SITEMAP_URL,
            "llms_txt": LLMS_TXT_URL,
            "quickstart": QUICKSTART_URL,
            "first_price_quickstart": FIRST_PRICE_QUICKSTART_URL,
            "agent_framework_integrations": AGENT_FRAMEWORK_INTEGRATIONS_URL,
            "category_hubs_json": CATEGORY_HUBS_JSON_URL,
            "rwa_market_data": f"{PUBLIC_BASE_URL.rstrip('/')}/rwa-market-data",
            "market_data_licensing": f"{PUBLIC_BASE_URL.rstrip('/')}/market-data-licensing",
            "signed_oracle_feeds": f"{PUBLIC_BASE_URL.rstrip('/')}/signed-oracle-feeds",
            "rwa_coverage_index": RWA_COVERAGE_INDEX_URL,
            "rwa_coverage_index_pdf": RWA_COVERAGE_INDEX_PDF_URL,
            "oracle_lineage_index": ORACLE_LINEAGE_INDEX_URL,
            "oracle_lineage_index_pdf": ORACLE_LINEAGE_INDEX_PDF_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
            "anthropic_mcp": f"{PUBLIC_BASE_URL.rstrip('/')}/anthropic/mcp/",
            **(
                {"anthropic_oauth_callback": anthropic_auth.oauth_callback_url()}
                if anthropic_oauth_available
                else {}
            ),
            "claude_connector": CLAUDE_CONNECTOR_URL,
            "cursor_mcp": f"{PUBLIC_BASE_URL.rstrip('/')}/cursor/mcp/",
            **(
                {"cursor_oauth_callback": cursor_auth.oauth_callback_url()}
                if cursor_oauth_available
                else {}
            ),
            "openai_mcp": f"{PUBLIC_BASE_URL.rstrip('/')}/openai/mcp/",
            **(
                {"openai_oauth_callback": openai_auth.oauth_callback_url()}
                if openai_oauth_available
                else {}
            ),
        },
        "anthropic_connector": {
            "mcp_url": _anthropic_mcp_url(),
            "auth_provider": os.environ.get("ANTHROPIC_AUTH_PROVIDER", "none"),
            "oauth_available": anthropic_oauth_available,
            **(
                {"oauth_callback_url": anthropic_auth.oauth_callback_url()}
                if anthropic_oauth_available
                else {}
            ),
            "beta_tokens_enabled": anthropic_auth.beta_tokens_enabled(),
            "tool_surface": "read-only",
            "tool_costs": ANTHROPIC_TOOL_COSTS,
            "equities": "Search with asset_class=equity, then call get_bid_ask for supported stock tickers such as AAPL.",
            "submission_docs": CLAUDE_CONNECTOR_URL,
        },
        "cursor_connector": {
            "mcp_url": _cursor_mcp_url(),
            "auth_provider": os.environ.get("CURSOR_AUTH_PROVIDER", "none"),
            "oauth_available": cursor_oauth_available,
            **(
                {"oauth_callback_url": cursor_auth.oauth_callback_url()}
                if cursor_oauth_available
                else {}
            ),
            "beta_tokens_enabled": cursor_auth.beta_tokens_enabled(),
            "tool_surface": "read-only",
            "tool_costs": CURSOR_TOOL_COSTS,
            "equities": "Search with asset_class=equity, then call get_bid_ask for supported stock tickers such as AAPL.",
        },
        "openai_connector": {
            "mcp_url": _openai_mcp_url(),
            "auth_provider": os.environ.get("OPENAI_AUTH_PROVIDER", "none"),
            "oauth_available": openai_oauth_available,
            **(
                {"oauth_callback_url": openai_auth.oauth_callback_url()}
                if openai_oauth_available
                else {}
            ),
            "oauth_scopes": openai_auth.oauth_scopes(),
            "beta_tokens_enabled": openai_auth.beta_tokens_enabled(),
            "tool_surface": "read-only-live-data",
            "tool_costs": OPENAI_TOOL_COSTS,
            "equities": "Search with asset_class=equity, then call get_bid_ask for supported stock tickers such as AAPL.",
        },
    }


# Keep this registration after every route/decorator middleware so it remains
# the outermost application boundary and normalizes the raw peer before any
# rate-limit, starter-credit, or observability middleware reads request.client.
app.add_middleware(
    TrustedProxyHeadersMiddleware,
    trusted_proxy_ips=settings.server.forwarded_allow_ips,
    use_x_real_ip=_hosted_environment(),
)


def run_resource_server() -> None:
    """Start the resource server with uvicorn."""
    import uvicorn
    port = int(os.environ.get("PORT", settings.server.resource_server_port))
    config = uvicorn.Config(
        "src.resource_server:app",
        host="0.0.0.0",
        port=port,
        log_level=settings.server.log_level.lower(),
        # The application middleware must see the raw TCP peer before deciding
        # whether Railway's forwarding headers are trusted.
        proxy_headers=False,
        reload=False,
    )
    install_sensitive_query_log_filter()
    uvicorn.Server(config).run()


if __name__ == "__main__":
    run_resource_server()
