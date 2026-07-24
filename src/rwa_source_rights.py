"""Rights-to-source registry for RWA market data promotion."""

from __future__ import annotations

import csv
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.rwa_derivative_venues import DERIVATIVE_SOURCE_RIGHTS_OVERRIDES
from src.rwa_feed_discovery import DEFAULT_REPORTS_DIR, build_feed_discovery_audit
from src.rwa_provider_catalog import build_provider_catalog
from src.rwa_rights_clearance import (
    env_or_clearance_ack,
    load_rights_clearance,
    rights_clearance_summary,
)
from src.rwa_coverage import build_rwa_coverage_overview
from src.rwa_source_readiness import build_source_readiness
from src.rwa_xyz_monitor import RWA_XYZ_SOURCE_RIGHTS_OVERRIDE, RWA_XYZ_VENUE_ID


DEFAULT_SOURCE_RIGHTS_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_source_rights.json"
DEFAULT_SOURCE_RIGHTS_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_source_rights.csv"

RIGHTS_BY_CATEGORY: dict[str, dict[str, Any]] = {
    "dex_liquidity": {
        "default_internal_use": "technical_probe_allowed_when_api_or_rpc_configured",
        "production_requirement": "provider/API terms, RPC terms, public-chain data policy, and redistribution policy signoff",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "medium",
    },
    "tokenized_security": {
        "default_internal_use": "candidate_probe_only",
        "production_requirement": "issuer/venue terms, transfer restriction review, jurisdiction review, and redistribution policy signoff",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "high",
    },
    "licensed_exchange": {
        "default_internal_use": "blocked_until_license",
        "production_requirement": "direct exchange or consolidated-feed license with redistribution and entitlement scope",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "critical",
    },
    "market_data_vendor": {
        "default_internal_use": "blocked_until_contract",
        "production_requirement": "commercial data plan, redisplay rights, storage/retention rights, and user entitlement controls",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "critical",
    },
    "oracle_reference": {
        "default_internal_use": "supplemental_reference_only_when_credentials_configured",
        "production_requirement": "oracle provider terms, feed-id entitlement, verification policy, and redistribution scope",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "high",
    },
    "issuer_nav_reserve": {
        "default_internal_use": "blocked_or_reference_only_until_issuer_access",
        "production_requirement": "issuer/admin permission, NAV/reserve publication terms, attribution, and redistribution policy",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "high",
    },
    "futures_fair_value": {
        "default_internal_use": "blocked_until_futures_license_and_specs",
        "production_requirement": "futures exchange license, contract specs, derived-data policy, and component-source rights",
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "source_rights_risk": "critical",
    },
}

VENUE_RIGHTS_OVERRIDES: dict[str, dict[str, Any]] = {
    RWA_XYZ_VENUE_ID: RWA_XYZ_SOURCE_RIGHTS_OVERRIDE,
    "jupiter_router": {
        "category": "dex_liquidity",
        "required_policy_env": ["JUPITER_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["jupiter_api_key", "jupiter_route_allowlist", "redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "Jupiter API terms review, quote-data retention policy, and redistribution signoff",
    },
    "raydium_clmm": {
        "category": "dex_liquidity",
        "required_policy_env": ["SOLANA_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["solana_rpc_indexer", "solana_pool_allowlist", "redistribution_policy"],
    },
    "orca_whirlpool": {
        "category": "dex_liquidity",
        "required_policy_env": ["SOLANA_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["solana_rpc_indexer", "solana_pool_allowlist", "redistribution_policy"],
    },
    "meteora_dlmm": {
        "category": "dex_liquidity",
        "required_policy_env": ["SOLANA_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["solana_rpc_indexer", "solana_pool_allowlist", "redistribution_policy"],
    },
    "uniswap_v3_v4": {
        "category": "dex_liquidity",
        "required_policy_env": ["EVM_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["evm_rpc_multichain", "evm_pool_allowlist", "redistribution_policy"],
        "production_requirement": "direct onchain pool-state reads plus RPC/indexer terms and redistribution policy signoff",
    },
    "curve_stableswap": {
        "category": "dex_liquidity",
        "required_policy_env": ["EVM_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["evm_rpc_multichain", "evm_pool_allowlist", "redistribution_policy"],
    },
    "balancer_pools": {
        "category": "dex_liquidity",
        "required_policy_env": ["EVM_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["evm_rpc_multichain", "evm_pool_allowlist", "redistribution_policy"],
    },
    "aerodrome_slipstream": {
        "category": "dex_liquidity",
        "required_policy_env": ["EVM_RPC_PROVIDER_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["evm_rpc_multichain", "evm_pool_allowlist", "redistribution_policy"],
    },
    "hyperliquid_paxg": {
        "category": "tokenized_security",
        "required_policy_env": ["HYPERLIQUID_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "Hyperliquid API/data-use terms review, identity review, and redistribution signoff",
    },
    "hyperliquid_rwa_spot": {
        "category": "tokenized_security",
        "required_policy_env": ["HYPERLIQUID_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "Hyperliquid spot identity review, issuer review, API/data-use terms, and redistribution signoff",
    },
    "hyperliquid_perps": {
        "category": "dex_liquidity",
        "required_policy_env": ["HYPERLIQUID_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "Hyperliquid API/data-use terms review, leverage/perp risk labeling, and redistribution signoff",
    },
    "hyperliquid_spot": {
        "category": "tokenized_security",
        "required_policy_env": ["HYPERLIQUID_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "Hyperliquid spot API/data-use terms plus issuer identity review for RWA-like spot rows",
    },
    "blocksize_state": {
        "category": "oracle_reference",
        "required_policy_env": ["BLOCKSIZE_STATE_REFERENCE_POLICY_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["blocksize_benchmark_access", "redistribution_policy"],
        "rights_status_if_configured": "supplemental_reference_allowed_pending_state_coverage",
        "production_requirement": "Blocksize state instrument coverage plus internal policy signoff; state rows are not executable liquidity",
    },
    "treasury_nav": {
        "category": "issuer_nav_reserve",
        "required_policy_env": ["ISSUER_NAV_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["treasury_issuer_pack", "redistribution_policy"],
        "production_requirement": "issuer/admin NAV permission, reserve/attestation terms, and redistribution signoff",
    },
    "ostium": {
        "category": "tokenized_security",
        "required_policy_env": ["OSTIUM_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
    },
    "gains": {
        "category": "tokenized_security",
        "required_policy_env": ["GAINS_API_TERMS_ACK", "RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
    },
}

VENUE_RIGHTS_OVERRIDES.update(
    {
        venue_id: deepcopy(row)
        for venue_id, row in DERIVATIVE_SOURCE_RIGHTS_OVERRIDES.items()
        if venue_id not in VENUE_RIGHTS_OVERRIDES
    }
)


def _env_ack(name: str) -> bool:
    return env_or_clearance_ack(name)


def _dependency_rows() -> dict[str, dict[str, Any]]:
    readiness = build_source_readiness()
    return {
        str(row["dependency_id"]): row
        for row in readiness.get("dependencies", [])
        if isinstance(row, dict) and row.get("dependency_id")
    }


def _provider_rows() -> dict[str, dict[str, Any]]:
    catalog = build_provider_catalog()
    return {
        str(row["provider_id"]): row
        for row in catalog.get("providers", [])
        if isinstance(row, dict) and row.get("provider_id")
    }


def _venue_feed_counts() -> dict[str, int]:
    audit = build_feed_discovery_audit(include_feed_details=False)
    counts = {
        str(venue): int(count)
        for venue, count in (audit.get("summary", {}).get("by_venue") or {}).items()
    }
    coverage = build_rwa_coverage_overview(include_symbols=False)
    for venue, count in (coverage.get("coverage_summary", {}).get("by_venue") or {}).items():
        counts.setdefault(str(venue), int(count))
    return counts


def _infer_provider(venue: str, providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if venue in providers:
        return providers[venue]
    if venue in {"hyperliquid_paxg", "hyperliquid_perps", "hyperliquid_spot"}:
        return providers.get("hyperliquid_rwa_spot", {})
    if venue == "blocksize_state":
        return {
            "provider_id": "blocksize_state",
            "name": "Blocksize state reference",
            "category": "oracle_reference",
            "requires_auth": True,
            "requires_license": False,
            "ingestion_status": "ready_to_probe",
            "access_model": "authenticated_blocksize_api",
        }
    if venue == "treasury_nav":
        return {
            "provider_id": "treasury_nav",
            "name": "Treasury/fund issuer NAV references",
            "category": "issuer_nav_reserve",
            "requires_auth": True,
            "requires_license": True,
            "ingestion_status": "blocked_by_auth_or_license",
            "access_model": "issuer_or_admin_reference",
        }
    return {
        "provider_id": venue,
        "name": venue,
        "category": VENUE_RIGHTS_OVERRIDES.get(venue, {}).get("category", "dex_liquidity"),
        "requires_auth": True,
        "requires_license": False,
        "ingestion_status": "planned_adapter",
        "access_model": "unknown",
    }


def _dependency_status(
    dependency_ids: list[str],
    dependencies: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    configured: list[str] = []
    blocked: list[str] = []
    missing: list[str] = []
    for dependency_id in dependency_ids:
        row = dependencies.get(dependency_id)
        status = str((row or {}).get("status") or "missing_required_config")
        if status == "configured":
            configured.append(dependency_id)
        elif status.startswith("blocked"):
            blocked.append(dependency_id)
        else:
            missing.append(dependency_id)
    return configured, missing, blocked


def _build_rights_row(
    *,
    venue: str,
    feed_count: int,
    provider: dict[str, Any],
    dependencies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    category = str(
        VENUE_RIGHTS_OVERRIDES.get(venue, {}).get("category")
        or provider.get("category")
        or "dex_liquidity"
    )
    defaults = RIGHTS_BY_CATEGORY.get(category, RIGHTS_BY_CATEGORY["dex_liquidity"])
    override = VENUE_RIGHTS_OVERRIDES.get(venue, {})
    required_dependency_ids = list(
        override.get("required_dependency_ids")
        or _default_dependency_ids_for_category(category)
    )
    required_policy_env = list(
        override.get("required_policy_env")
        or defaults.get("required_policy_env")
        or []
    )
    policy_dependency_ids = {"redistribution_policy", "promotion_signoff"}
    access_dependency_ids = [
        dependency_id for dependency_id in required_dependency_ids if dependency_id not in policy_dependency_ids
    ]
    policy_dependency_ids_required = [
        dependency_id for dependency_id in required_dependency_ids if dependency_id in policy_dependency_ids
    ]
    configured_deps, missing_deps, blocked_deps = _dependency_status(required_dependency_ids, dependencies)
    _configured_access, missing_access_deps, blocked_access_deps = _dependency_status(
        access_dependency_ids,
        dependencies,
    )
    _configured_policy_deps, missing_policy_deps, blocked_policy_deps = _dependency_status(
        policy_dependency_ids_required,
        dependencies,
    )
    configured_policy_env = [name for name in required_policy_env if _env_ack(name)]
    missing_policy_env = [name for name in required_policy_env if name not in configured_policy_env]

    requires_license = bool(provider.get("requires_license")) or category in {
        "licensed_exchange",
        "market_data_vendor",
        "futures_fair_value",
    }
    requires_auth = bool(provider.get("requires_auth"))
    access_ready = not missing_access_deps and not blocked_access_deps
    policy_ready = not missing_policy_env and not missing_policy_deps and not blocked_policy_deps
    production_access_ready = access_ready and policy_ready

    if policy_ready:
        rights_status = "production_rights_cleared"
    elif blocked_policy_deps:
        rights_status = "blocked_by_license_or_contract"
    elif missing_policy_deps or missing_policy_env:
        rights_status = "missing_access_or_rights_evidence"
    else:
        rights_status = str(
            override.get("rights_status_if_configured")
            or "internal_benchmark_allowed_pending_redistribution"
        )

    if requires_license and not access_ready:
        can_source_internal = False
    else:
        can_source_internal = access_ready

    if category in {"oracle_reference", "issuer_nav_reserve"}:
        consensus_role = "supplemental_reference_only"
    elif production_access_ready:
        consensus_role = "eligible_after_quality_gates"
    elif policy_ready:
        consensus_role = "rights_cleared_pending_source_access"
    else:
        consensus_role = "internal_benchmark_only"

    return {
        "venue": venue,
        "provider_id": str(provider.get("provider_id") or venue),
        "provider_name": str(provider.get("name") or venue),
        "category": category,
        "feed_count": feed_count,
        "rights_status": rights_status,
        "legal_rights_cleared": policy_ready,
        "source_access_ready": access_ready,
        "production_access_ready": production_access_ready,
        "can_source_for_internal_benchmark": can_source_internal,
        "can_redistribute_production": policy_ready,
        "can_use_for_consensus": production_access_ready
        and category not in {"oracle_reference", "issuer_nav_reserve"},
        "consensus_role": consensus_role,
        "requires_auth": requires_auth,
        "requires_license_or_contract": requires_license,
        "source_rights_risk": str(defaults.get("source_rights_risk") or "high"),
        "access_model": provider.get("access_model"),
        "required_dependency_ids": required_dependency_ids,
        "configured_dependency_ids": configured_deps,
        "missing_dependency_ids": missing_deps,
        "blocked_dependency_ids": blocked_deps,
        "required_policy_env": required_policy_env,
        "configured_policy_env": configured_policy_env,
        "missing_policy_env": missing_policy_env,
        "production_requirement": str(
            override.get("production_requirement")
            or defaults.get("production_requirement")
            or "rights review required"
        ),
        "next_action": _next_action(
            rights_status=rights_status,
            access_ready=access_ready,
            missing_deps=missing_deps,
            blocked_deps=blocked_deps,
            missing_policy_env=missing_policy_env,
            provider=provider,
        ),
        "not_legal_advice": True,
    }


def _default_dependency_ids_for_category(category: str) -> list[str]:
    if category == "dex_liquidity":
        return ["redistribution_policy"]
    if category == "oracle_reference":
        return ["redistribution_policy"]
    if category == "issuer_nav_reserve":
        return ["treasury_issuer_pack", "redistribution_policy"]
    if category == "licensed_exchange":
        return ["us_equity_realtime_license", "redistribution_policy"]
    if category == "market_data_vendor":
        return ["databento_vendor_access", "redistribution_policy"]
    if category == "futures_fair_value":
        return ["futures_exchange_licenses", "redistribution_policy"]
    return ["redistribution_policy"]


def _next_action(
    *,
    rights_status: str,
    access_ready: bool,
    missing_deps: list[str],
    blocked_deps: list[str],
    missing_policy_env: list[str],
    provider: dict[str, Any],
) -> str:
    if rights_status == "production_rights_cleared":
        if access_ready:
            return "Rights gate is clear; continue with replay, liquidity, freshness, and manipulation gates."
        return "Rights gate is clear; configure source access, identifiers, and replay evidence before live use."
    if blocked_deps:
        return f"Resolve license/contract blockers: {', '.join(blocked_deps)}."
    if missing_deps:
        return f"Configure or attach evidence for dependencies: {', '.join(missing_deps)}."
    if missing_policy_env:
        return f"Record policy/legal acknowledgements after review: {', '.join(missing_policy_env)}."
    next_action = str(provider.get("next_action") or "").strip()
    return next_action or "Complete provider terms review and record redistribution signoff."


def build_source_rights_registry(
    *,
    venue: str = "all",
    status: str = "all",
) -> dict[str, Any]:
    """Return rights-to-source status by venue/provider."""
    venue_filter = venue.strip().lower()
    status_filter = status.strip().lower()
    dependencies = _dependency_rows()
    providers = _provider_rows()
    feed_counts = _venue_feed_counts()
    clearance = load_rights_clearance()
    rows = [
        _build_rights_row(
            venue=venue_id,
            feed_count=count,
            provider=_infer_provider(venue_id, providers),
            dependencies=dependencies,
        )
        for venue_id, count in sorted(feed_counts.items())
    ]
    if venue_filter != "all":
        rows = [row for row in rows if str(row["venue"]).lower() == venue_filter]
    if status_filter != "all":
        rows = [row for row in rows if str(row["rights_status"]).lower() == status_filter]

    by_status = Counter(str(row["rights_status"]) for row in rows)
    by_category = Counter(str(row["category"]) for row in rows)
    return {
        "summary": {
            "venue_count": len(rows),
            "feed_count": sum(int(row["feed_count"]) for row in rows),
            "production_rights_cleared": sum(1 for row in rows if row["can_redistribute_production"]),
            "production_access_ready": sum(1 for row in rows if row["production_access_ready"]),
            "internal_benchmark_sourceable": sum(1 for row in rows if row["can_source_for_internal_benchmark"]),
            "blocked_or_missing_rights": sum(1 for row in rows if not row["can_redistribute_production"]),
            "missing_or_blocked_source_access": sum(1 for row in rows if not row["source_access_ready"]),
            "by_status": dict(sorted(by_status.items())),
            "by_category": dict(sorted(by_category.items())),
        },
        "filters": {"venue": venue, "status": status},
        "policy": {
            "production_rule": "Production redistribution requires configured dependencies and explicit policy/legal acknowledgements.",
            "internal_rule": "Public/API sources may be probed for internal benchmarking only when access is configured and terms review is pending.",
            "reference_rule": "Oracle, NAV, and state references are supplemental and do not become executable liquidity through rights clearance alone.",
            "not_legal_advice": True,
        },
        "clearance_evidence": rights_clearance_summary(clearance),
        "rows": rows,
    }


def write_source_rights_reports(
    *,
    json_path: str | Path = DEFAULT_SOURCE_RIGHTS_JSON_PATH,
    csv_path: str | Path = DEFAULT_SOURCE_RIGHTS_CSV_PATH,
) -> dict[str, Any]:
    """Write rights-to-source reports."""
    registry = build_source_rights_registry()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fieldnames = [
        "venue",
        "provider_id",
        "provider_name",
        "category",
        "feed_count",
        "rights_status",
        "legal_rights_cleared",
        "source_access_ready",
        "production_access_ready",
        "can_source_for_internal_benchmark",
        "can_redistribute_production",
        "can_use_for_consensus",
        "consensus_role",
        "requires_license_or_contract",
        "source_rights_risk",
        "required_dependency_ids",
        "missing_dependency_ids",
        "blocked_dependency_ids",
        "required_policy_env",
        "missing_policy_env",
        "production_requirement",
        "next_action",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in registry["rows"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fieldnames
                }
            )
    return registry
