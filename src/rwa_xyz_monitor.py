"""RWA.xyz New Asset Monitor ingestion and token sourcing plan."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import urllib.request
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORTS_DIR = Path("reports")
RWA_XYZ_VENUE_ID = "rwa_xyz_new_asset_monitor"
DEFAULT_RWA_XYZ_MONITOR_URL = "https://app.rwa.xyz/new-asset-monitor"
DEFAULT_RWA_XYZ_DOCS_URL = "https://docs.rwa.xyz"
DEFAULT_RWA_XYZ_REPORT_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_xyz_new_asset_monitor.json"
DEFAULT_RWA_XYZ_ASSET_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_xyz_new_asset_monitor_assets.csv"
DEFAULT_RWA_XYZ_TOKEN_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_xyz_new_asset_monitor_tokens.csv"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_NON_SYMBOL = re.compile(r"[^A-Za-z0-9._-]+")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_HEX_CONTRACT_ADDRESS = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_METAL_NAME_MARKERS = (
    "copper",
    "gold",
    "metal",
    "nickel",
    "palladium",
    "platinum",
    "rare earth",
    "silver",
)
_METAL_TICKER_MARKERS = {
    "AUXG",
    "AUXPD",
    "AUXPT",
    "AUXS",
    "CPER",
    "GLD",
    "GOLD",
    "IAU",
    "PALL",
    "PAXG",
    "PPLT",
    "SIVR",
    "SLV",
    "XAG",
    "XAU",
    "XAUT",
}
_SOURCE_ASSET_CLASS_MAP = {
    "us treasury debt": "treasury_fund",
    "us treasury": "treasury_fund",
    "u s treasury debt": "treasury_fund",
    "u s treasury": "treasury_fund",
    "non us government debt": "sovereign_debt",
    "non u s government debt": "sovereign_debt",
}
UTC = timezone.utc

KNOWN_CANONICAL_BASES = {
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "BUIDL",
    "COIN",
    "CRCL",
    "GOOG",
    "GOOGL",
    "HOOD",
    "IEMG",
    "IWM",
    "META",
    "MSFT",
    "MSTR",
    "MU",
    "NFLX",
    "NVDA",
    "PAXG",
    "PLTR",
    "QQQ",
    "SGOV",
    "SPY",
    "TBILL",
    "TLT",
    "TSLA",
    "USTB",
    "USCC",
    "USDY",
    "VOO",
}

KNOWN_ETF_BASES = frozenset({"IEMG", "IWM", "QQQ", "SPY"})
VERIFIED_IDENTITY_MAPPING_STATUSES = frozenset({"high"})

_YIELD_TO_MATURITY_UNIT = "decimal_fraction"
_YIELD_TO_MATURITY_BASIS = "annualized_yield_to_maturity"
_APY_30_DAY_UNIT = "percentage_points"
_APY_30_DAY_BASIS = "annual_percentage_yield_trailing_30_day"

RWA_XYZ_VENUE_DESCRIPTOR: dict[str, Any] = {
    "id": RWA_XYZ_VENUE_ID,
    "name": "RWA.xyz New Asset Monitor",
    "priority": 36,
    "status": "platform_catalog_reference",
    "instrument_type": "rwa_product_registry",
    "source_tier": "platform_catalog_reference",
    "data": [
        "asset_catalog",
        "token_contracts",
        "issuer_metadata",
        "primary_market_terms",
        "total_asset_value",
    ],
    "vwap_method": "not VWAP-suitable directly; use token contracts to discover executable DEX, exchange, issuer quote, or order-book venues",
    "bidask_method": "not bid/ask-suitable directly; use as token identity and primary-market context before venue quote discovery",
    "coverage_mode": "next_static_props_asset_monitor_catalog",
    "legal_note": "Public product monitor data is catalog/reference evidence; production redistribution and use of any API/data feed still require RWA.xyz terms review.",
}

RWA_XYZ_PROVIDER_ROW: dict[str, Any] = {
    "provider_id": RWA_XYZ_VENUE_ID,
    "name": "RWA.xyz New Asset Monitor",
    "category": "market_data_vendor",
    "region": "Global / On-chain",
    "asset_classes": [
        "tokenized_equity",
        "tokenized_etf",
        "tokenized_treasury_fund",
        "tokenized_fund",
        "tokenized_commodity",
        "real_estate",
        "private_credit",
    ],
    "coverage_scope": "RWA product registry with issuer, platform, network, token contract, primary-market, and total-asset-value metadata",
    "example_symbols": ["AAPLx", "AAPLon", "BUIDL", "TBILL", "USTB", "USCC", "PAXG"],
    "target_source_types": ["platform_catalog_reference", "token_contract_reference"],
    "endpoint_families": ["new_asset_monitor_next_data", "product_catalog", "token_contracts"],
    "access_model": "public_next_static_page_or_partner_api",
    "requires_auth": False,
    "requires_license": False,
    "ingestion_status": "ready_to_probe",
    "priority": "P0",
    "adapter_lane": "rwa_xyz_monitor_catalog_adapter",
    "promotion_gate": "token identity, pool/venue discovery, liquidity, freshness, manipulation, issuer/NAV alignment, and RWA.xyz terms review",
}

RWA_XYZ_SOURCE_RIGHTS_OVERRIDE: dict[str, Any] = {
    "category": "market_data_vendor",
    "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
    "required_dependency_ids": ["rwa_xyz_monitor_catalog", "redistribution_policy"],
    "rights_status_if_configured": "internal_reference_allowed_pending_redistribution",
    "production_requirement": "RWA.xyz data/API terms, attribution, retention, and redistribution policy signoff before production use",
}

RWA_XYZ_READINESS_DEPENDENCY: dict[str, Any] = {
    "dependency_id": "rwa_xyz_monitor_catalog",
    "category": "market_data_vendor",
    "priority": "P0",
    "name": "RWA.xyz New Asset Monitor catalog",
    "required_env": [],
    "optional_env": ["RWA_XYZ_API_KEY", "RWA_XYZ_BASE_URL"],
    "artifact_paths": [str(DEFAULT_RWA_XYZ_REPORT_JSON_PATH)],
    "missing_status": "missing_identifier_mapping",
    "required_for": [
        "RWA.xyz token identity coverage",
        "token contract discovery",
        "issuer/platform registry enrichment",
    ],
    "unblocks": [
        "RWA.xyz product coverage rows",
        "per-token DEX and venue discovery jobs",
        "issuer/NAV alignment backlog",
    ],
    "next_action": "Run scripts/run_rwa_xyz_monitor_discovery.py to refresh asset and token rows from the monitor page.",
    "quality_gate": "Catalog rows are reference only; production pricing requires token/pool discovery, live liquidity, freshness, and issuer alignment.",
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _nested_label(row: dict[str, Any] | None, key: str, field: str = "name") -> str:
    value = (row or {}).get(key)
    if isinstance(value, dict):
        return _text(value.get(field) or value.get("name") or value.get("slug") or value.get("id"))
    return _text(value)


def _safe_symbol(value: str, *, fallback: str) -> str:
    cleaned = _NON_SYMBOL.sub("", value.strip())
    cleaned = cleaned.replace("-", "")
    return cleaned or fallback


def _total_asset_value(asset: dict[str, Any]) -> float | None:
    raw = asset.get("total_asset_value_dollar")
    if isinstance(raw, dict):
        raw = raw.get("val")
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _asset_class_name(asset: dict[str, Any]) -> str:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    return _text(
        asset.get("asset_class_name")
        or asset.get("rwa_xyz_asset_class")
        or metadata.get("rwa_xyz_asset_class")
        or asset.get("asset_class")
        or "Unknown"
    )


def _asset_name(asset: dict[str, Any]) -> str:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    return _text(
        asset.get("name")
        or asset.get("asset_name")
        or metadata.get("asset_name")
    )


def _asset_ticker(asset: dict[str, Any]) -> str:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    symbol = _text(asset.get("symbol"))
    return _text(
        asset.get("ticker")
        or asset.get("rwa_xyz_ticker")
        or metadata.get("rwa_xyz_ticker")
        or symbol.split("/", 1)[0]
    )


def _canonical_source_asset_class(value: str) -> str:
    """Return a punctuation-insensitive key for an upstream asset class."""
    return _NON_ALPHANUMERIC.sub(" ", value.casefold()).strip()


def _commodity_asset_class(asset: dict[str, Any]) -> str:
    name = _asset_name(asset).casefold()
    ticker = _asset_ticker(asset).upper()
    ticker_base = ticker.split(".", 1)[0]
    for suffix in ("ON", "SW", "X", "R"):
        if ticker_base.endswith(suffix) and len(ticker_base) > len(suffix) + 1:
            ticker_base = ticker_base[: -len(suffix)]
            break
    if any(marker in name for marker in _METAL_NAME_MARKERS):
        return "metal"
    if ticker_base in _METAL_TICKER_MARKERS:
        return "metal"
    return "commodity"


def normalize_rwa_xyz_asset_class(asset: dict[str, Any]) -> str:
    """Map RWA.xyz product classes into the aggregator's supported classes."""
    raw_class = _asset_class_name(asset)
    source_class = _canonical_source_asset_class(raw_class)
    name = _asset_name(asset).lower()
    ticker = _asset_ticker(asset).lower()
    combined = f"{source_class} {name} {ticker}"
    explicit_class = _SOURCE_ASSET_CLASS_MAP.get(source_class)
    if explicit_class is not None:
        return explicit_class
    if source_class == "stocks":
        canonical_base, _ = _strip_vendor_wrappers(_asset_ticker(asset))
        if canonical_base in KNOWN_ETF_BASES:
            return "etf"
        if " etf" in combined or "spdr" in combined or "ishares" in combined or "vanguard" in combined:
            return "etf"
        return "equity"
    if "government debt" in source_class:
        return "sovereign_debt"
    if "treasury" in source_class:
        return "treasury_fund"
    if source_class in {"commodity", "commodities"}:
        return _commodity_asset_class(asset)
    if "real estate" in source_class:
        return "tokenized_fund"
    if any(
        marker in source_class
        for marker in (
            "credit",
            "active strategies",
            "private equity",
            "venture capital",
            "specialty finance",
        )
    ):
        return "tokenized_fund"
    return "tokenized_fund"


def _rwa_xyz_source_id(asset: dict[str, Any]) -> str:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    source_id = _text(
        asset.get("id")
        or asset.get("rwa_xyz_asset_id")
        or metadata.get("rwa_xyz_asset_id")
    )
    if source_id:
        return source_id
    fingerprint = json.dumps(
        {
            "issuer": _text(asset.get("issuer_name") or metadata.get("issuer_name")),
            "name": _asset_name(asset),
            "ticker": _asset_ticker(asset),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "UNIDENTIFIED_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16].upper()


def _source_scoped_asset_id(asset: dict[str, Any]) -> str:
    return f"RWA_XYZ_{_safe_symbol(_rwa_xyz_source_id(asset), fallback='UNIDENTIFIED')}"


def _identity_verification_status(mapping_status: str) -> str:
    if mapping_status in VERIFIED_IDENTITY_MAPPING_STATUSES:
        return "verified"
    return "unverified"


def _trend_value(raw: Any) -> float | None:
    value = raw.get("val") if isinstance(raw, dict) else raw
    try:
        parsed = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _yield_metric_fields(asset: dict[str, Any]) -> dict[str, Any]:
    ytm_raw = deepcopy(asset.get("yield_to_maturity_percent"))
    apy_raw = deepcopy(asset.get("apy_30_day"))
    return {
        "yield_to_maturity_value": _trend_value(ytm_raw),
        "yield_to_maturity_unit": _YIELD_TO_MATURITY_UNIT,
        "yield_to_maturity_basis": _YIELD_TO_MATURITY_BASIS,
        "yield_to_maturity_raw_trend": ytm_raw,
        "apy_30_day_value": _trend_value(apy_raw),
        "apy_30_day_unit": _APY_30_DAY_UNIT,
        "apy_30_day_basis": _APY_30_DAY_BASIS,
        "apy_30_day_raw_trend": apy_raw,
    }


def _mixed_class_asset_ids(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    classes_by_id: dict[str, set[str]] = {}
    for row in rows:
        asset_id = _text(row.get("asset_id"))
        if not asset_id:
            continue
        classes_by_id.setdefault(asset_id, set()).add(
            _text(row.get("asset_class")) or "unknown"
        )
    return {
        asset_id: sorted(classes)
        for asset_id, classes in sorted(classes_by_id.items())
        if len(classes) > 1
    }


def rwa_xyz_identity_quality_summary(asset_rows: list[dict[str, Any]]) -> dict[str, Any]:
    denominator = len(asset_rows)
    verified_rows = [
        row
        for row in asset_rows
        if _identity_verification_status(_text(row.get("identity_mapping_status")))
        == "verified"
    ]
    unverified_count = denominator - len(verified_rows)
    mixed_ids = _mixed_class_asset_ids(asset_rows)
    verified_mixed_ids = _mixed_class_asset_ids(verified_rows)
    return {
        "denominator_asset_count": denominator,
        "denominator_unit": "RWA.xyz asset rows",
        "verified_asset_count": len(verified_rows),
        "verified_asset_rate": round(len(verified_rows) / denominator, 6)
        if denominator
        else 0.0,
        "unverified_asset_count": unverified_count,
        "unverified_asset_rate": round(unverified_count / denominator, 6)
        if denominator
        else 0.0,
        "verified_unique_asset_id_count": len(
            {_text(row.get("asset_id")) for row in verified_rows if row.get("asset_id")}
        ),
        "mixed_class_asset_id_count": len(mixed_ids),
        "mixed_class_asset_ids": mixed_ids,
        "decision_grade_mixed_class_asset_id_count": len(verified_mixed_ids),
        "decision_grade_mixed_class_asset_ids": verified_mixed_ids,
    }


def _normalized_contract_network(row: dict[str, Any]) -> str:
    raw_network = _text(row.get("network_slug") or row.get("network"))
    return _NON_ALPHANUMERIC.sub("-", raw_network.casefold()).strip("-")


def _normalized_contract_address(row: dict[str, Any]) -> str:
    address = _text(row.get("address"))
    if _HEX_CONTRACT_ADDRESS.fullmatch(address):
        return address.casefold()
    return address


def rwa_xyz_contract_identity_key(row: dict[str, Any]) -> str:
    """Return one network-scoped contract key using chain-safe address casing."""
    network = _normalized_contract_network(row)
    address = _normalized_contract_address(row)
    return f"{network}:{address}" if network and address else ""


def _contract_identity_group(
    network: str,
    address: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    source_asset_ids = sorted(
        {_text(row.get("rwa_xyz_asset_id")) for row in rows if row.get("rwa_xyz_asset_id")}
    )
    return {
        "normalized_network": network,
        "normalized_address": address,
        "contract_identity": f"{network}:{address}",
        "row_count": len(rows),
        "excess_row_count": max(0, len(rows) - 1),
        "source_asset_count": len(source_asset_ids),
        "missing_source_asset_id_row_count": sum(
            not _text(row.get("rwa_xyz_asset_id")) for row in rows
        ),
        "rwa_xyz_asset_ids": source_asset_ids,
        "token_row_ids": sorted(_text(row.get("token_row_id")) for row in rows),
        "rwa_xyz_token_ids": sorted(
            _text(row.get("rwa_xyz_token_id")) for row in rows
        ),
    }


def rwa_xyz_contract_identity_quality_summary(
    token_rows: list[Any],
) -> dict[str, Any]:
    """Describe duplicate contract listings without dropping any source rows.

    A contract identity is network-scoped. Hexadecimal addresses are normalized
    case-insensitively; non-hexadecimal identifiers retain case because Solana,
    Stellar, and other address formats can be case-sensitive. Repeated listings
    within one RWA.xyz source asset are retained and reported separately from a
    contract assigned to multiple source assets. The latter fails acceptance.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    missing_network_rows: list[dict[str, Any]] = []
    missing_address_rows: list[dict[str, Any]] = []
    missing_source_asset_rows: list[dict[str, Any]] = []
    incomplete_row_references: list[str] = []
    invalid_rows = 0

    for index, row in enumerate(token_rows):
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        network = _normalized_contract_network(row)
        address = _normalized_contract_address(row)
        source_asset_id = _text(row.get("rwa_xyz_asset_id"))
        if not network:
            missing_network_rows.append(row)
        if not address:
            missing_address_rows.append(row)
        if not source_asset_id:
            missing_source_asset_rows.append(row)
        if not network or not address or not source_asset_id:
            incomplete_row_references.append(
                _text(row.get("token_row_id")) or f"row_index:{index}"
            )
        if network and address:
            groups.setdefault((network, address), []).append(row)

    duplicate_groups = [
        _contract_identity_group(network, address, rows)
        for (network, address), rows in sorted(groups.items())
        if len(rows) > 1
    ]
    benign_groups = [
        group
        for group in duplicate_groups
        if group["source_asset_count"] == 1
        and group["missing_source_asset_id_row_count"] == 0
    ]
    cross_asset_groups = [
        group for group in duplicate_groups if group["source_asset_count"] > 1
    ]
    unclassifiable_groups = [
        group
        for group in duplicate_groups
        if group["source_asset_count"] <= 1
        and group["missing_source_asset_id_row_count"] > 0
    ]

    incomplete_row_ids = sorted(incomplete_row_references)
    blockers: list[dict[str, Any]] = [
        {
            "blocker_type": "cross_asset_contract_collision",
            **group,
        }
        for group in cross_asset_groups
    ]
    if invalid_rows or incomplete_row_ids:
        blockers.append(
            {
                "blocker_type": "incomplete_contract_identity",
                "invalid_token_row_count": invalid_rows,
                "incomplete_token_row_count": len(incomplete_row_ids),
                "token_row_ids": incomplete_row_ids,
            }
        )
    if unclassifiable_groups:
        blockers.extend(
            {
                "blocker_type": "unclassifiable_duplicate_contract_identity",
                **group,
            }
            for group in unclassifiable_groups
        )

    duplicate_row_count = sum(group["row_count"] for group in duplicate_groups)
    return {
        "input_token_row_count": len(token_rows),
        "preserved_token_row_count": len(token_rows),
        "normalized_contract_identity_row_count": sum(len(rows) for rows in groups.values()),
        "unique_contract_identity_count": len(groups),
        "duplicate_contract_identity_group_count": len(duplicate_groups),
        "duplicate_contract_identity_row_count": duplicate_row_count,
        "duplicate_contract_identity_excess_row_count": sum(
            group["excess_row_count"] for group in duplicate_groups
        ),
        "benign_duplicate_source_listing_group_count": len(benign_groups),
        "benign_duplicate_source_listing_row_count": sum(
            group["row_count"] for group in benign_groups
        ),
        "benign_duplicate_source_listing_groups": benign_groups,
        "cross_asset_contract_collision_group_count": len(cross_asset_groups),
        "cross_asset_contract_collision_row_count": sum(
            group["row_count"] for group in cross_asset_groups
        ),
        "cross_asset_contract_collisions": cross_asset_groups,
        "missing_network_row_count": len(missing_network_rows),
        "missing_address_row_count": len(missing_address_rows),
        "missing_source_asset_id_row_count": len(missing_source_asset_rows),
        "invalid_token_row_count": invalid_rows,
        "decision_grade_acceptance": {
            "status": "pass" if not blockers else "blocked",
            "accepted": not blockers,
            "blocker_count": len(blockers),
            "blockers": blockers,
            "rule": (
                "Pass only when every token row has network, address, and source-asset "
                "identity and no normalized network/address contract is assigned to "
                "multiple RWA.xyz source assets. Duplicate listings within one source "
                "asset remain preserved and are non-blocking."
            ),
        },
    }


def _apply_identity_normalization(row: dict[str, Any]) -> None:
    asset_id, mapping_status, underlying = infer_rwa_xyz_asset_id(row)
    row["asset_id"] = asset_id
    row["identity_mapping_status"] = mapping_status
    row["identity_verification_status"] = _identity_verification_status(mapping_status)
    row["canonical_underlying_candidate"] = underlying


def renormalize_rwa_xyz_monitor_report(report: dict[str, Any]) -> dict[str, Any]:
    """Reapply taxonomy, identity, and units to a captured monitor report.

    The checked-in report intentionally retains the upstream RWA.xyz class,
    asset name, and ticker on every row. That makes taxonomy corrections
    reproducible without re-fetching or silently changing the source snapshot.
    The transformation does not alter source or generated timestamps.
    """
    updated = deepcopy(report)
    asset_rows = updated.get("asset_rows")
    if not isinstance(asset_rows, list):
        asset_rows = []
    asset_rows = [row for row in asset_rows if isinstance(row, dict)]
    for row in asset_rows:
        row["asset_class"] = normalize_rwa_xyz_asset_class(row)
        _apply_identity_normalization(row)
        row.update(_yield_metric_fields(row))

    token_rows = updated.get("token_rows")
    if isinstance(token_rows, list):
        for row in token_rows:
            if not isinstance(row, dict):
                continue
            row["asset_class"] = normalize_rwa_xyz_asset_class(row)
            _apply_identity_normalization(row)

    coverage_rows = updated.get("coverage_rows")
    if isinstance(coverage_rows, list):
        for row in coverage_rows:
            if not isinstance(row, dict):
                continue
            row["asset_class"] = normalize_rwa_xyz_asset_class(row)
            _apply_identity_normalization(row)
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                metadata["identity_mapping_status"] = row["identity_mapping_status"]
                metadata["identity_verification_status"] = row[
                    "identity_verification_status"
                ]
                metadata["canonical_underlying_candidate"] = row[
                    "canonical_underlying_candidate"
                ]

    summary = updated.get("summary")
    if isinstance(summary, dict):
        summary["by_asset_class"] = _counter_dict(
            Counter(
                str(row.get("asset_class") or "unknown")
                for row in asset_rows
                if isinstance(row, dict)
            )
        )
        summary["by_identity_mapping_status"] = _counter_dict(
            Counter(_text(row.get("identity_mapping_status")) for row in asset_rows)
        )
        summary["by_identity_verification_status"] = _counter_dict(
            Counter(_text(row.get("identity_verification_status")) for row in asset_rows)
        )
        summary["identity_quality"] = rwa_xyz_identity_quality_summary(asset_rows)
        summary["contract_identity_quality"] = (
            rwa_xyz_contract_identity_quality_summary(
                token_rows if isinstance(token_rows, list) else []
            )
        )
        summary["yield_metric_quality"] = {
            "yield_to_maturity": {
                "populated_asset_count": sum(
                    row.get("yield_to_maturity_value") is not None for row in asset_rows
                ),
                "unit": _YIELD_TO_MATURITY_UNIT,
                "basis": _YIELD_TO_MATURITY_BASIS,
                "raw_trend_field": "yield_to_maturity_raw_trend",
            },
            "apy_30_day": {
                "populated_asset_count": sum(
                    row.get("apy_30_day_value") is not None for row in asset_rows
                ),
                "unit": _APY_30_DAY_UNIT,
                "basis": _APY_30_DAY_BASIS,
                "raw_trend_field": "apy_30_day_raw_trend",
            },
        }
    source_assessment = updated.get("source_assessment")
    if isinstance(source_assessment, dict):
        source_assessment["identity_boundary"] = (
            "Only allowlisted high-confidence mappings use a global canonical asset_id. "
            "Every unverified RWA.xyz row keeps a source-scoped RWA_XYZ_<source-id> "
            "identity and no canonical_underlying_candidate."
        )
        source_assessment["yield_unit_contract"] = {
            "yield_to_maturity_value": {
                "unit": _YIELD_TO_MATURITY_UNIT,
                "basis": _YIELD_TO_MATURITY_BASIS,
                "raw_trend_field": "yield_to_maturity_raw_trend",
            },
            "apy_30_day_value": {
                "unit": _APY_30_DAY_UNIT,
                "basis": _APY_30_DAY_BASIS,
                "raw_trend_field": "apy_30_day_raw_trend",
            },
        }
        source_assessment["contract_identity_boundary"] = (
            "Token rows are preserved at source-listing grain. Repeated normalized "
            "network/address identities within one RWA.xyz source asset are reported "
            "but do not block acceptance; any identity assigned to multiple source "
            "assets is a decision-grade blocker."
        )
    return updated


def reclassify_rwa_xyz_monitor_report(report: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible alias for full captured-report normalization."""
    return renormalize_rwa_xyz_monitor_report(report)


def _strip_vendor_wrappers(ticker: str) -> tuple[str | None, str]:
    raw = ticker.strip()
    upper = raw.upper()
    if not raw:
        return None, "missing_ticker"
    if "." in raw:
        base, suffix = raw.split(".", 1)
        if suffix.lower() == "d" and base.upper() in KNOWN_CANONICAL_BASES:
            return base.upper(), "dinari_dot_d_suffix"
        if suffix.lower() == "robinhood":
            return None, "robinhood_descriptive_symbol_requires_underlying_map"
    for suffix in ("ON", "SW", "X", "R"):
        if upper.endswith(suffix) and len(upper) > len(suffix) + 1:
            base = upper[: -len(suffix)]
            if base in KNOWN_CANONICAL_BASES or (2 <= len(base) <= 8 and base.isalnum()):
                return base, f"issuer_suffix_{suffix.lower()}"
    for prefix in ("WT", "S", "B", "N", "M"):
        if upper.startswith(prefix) and len(upper) > len(prefix) + 1:
            base = upper[len(prefix) :]
            if base in KNOWN_CANONICAL_BASES:
                return base, f"issuer_prefix_{prefix.lower()}"
    if upper in KNOWN_CANONICAL_BASES:
        return upper, "exact_known_symbol"
    if 2 <= len(upper) <= 8 and upper.replace("-", "").isalnum():
        return upper.replace("-", ""), "issuer_symbol"
    return None, "requires_manual_underlying_map"


def infer_rwa_xyz_asset_id(asset: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return aggregator asset id, mapping confidence, and candidate underlying."""
    ticker = _asset_ticker(asset)
    candidate, reason = _strip_vendor_wrappers(ticker)
    if candidate in KNOWN_CANONICAL_BASES:
        return candidate, "high", candidate
    if candidate:
        reason = "issuer_symbol_only"
    return _source_scoped_asset_id(asset), reason, None


def _token_networks(asset: dict[str, Any]) -> list[str]:
    return sorted(
        {
            _nested_label(token, "network")
            for token in asset.get("tokens") or []
            if _nested_label(token, "network")
        }
    )


def _token_platforms(asset: dict[str, Any]) -> list[str]:
    return sorted(
        {
            _nested_label(token, "platform")
            for token in asset.get("tokens") or []
            if _nested_label(token, "platform")
        }
    )


def _standards(token: dict[str, Any]) -> list[str]:
    values = token.get("standards")
    if not isinstance(values, list):
        return []
    return sorted(str(item) for item in values if item)


def normalize_rwa_xyz_asset_row(asset: dict[str, Any]) -> dict[str, Any]:
    rwa_asset_id = _text(asset.get("id"))
    ticker = _text(asset.get("ticker")) or f"RWA{rwa_asset_id}"
    display_symbol = f"{_safe_symbol(ticker, fallback=f'RWA{rwa_asset_id}')}/USD"
    asset_id, mapping_confidence, underlying = infer_rwa_xyz_asset_id(asset)
    token_rows = asset.get("tokens") or []
    primary_markets = asset.get("primary_markets")
    if not isinstance(primary_markets, list):
        primary_markets = []
    row = {
        "rwa_xyz_asset_id": rwa_asset_id,
        "asset_id": asset_id,
        "symbol": display_symbol,
        "rwa_xyz_ticker": ticker,
        "name": _text(asset.get("name")),
        "asset_class": normalize_rwa_xyz_asset_class(asset),
        "rwa_xyz_asset_class": _asset_class_name(asset),
        "issuer_name": _text(asset.get("issuer_name") or _nested_label(asset, "issuer")),
        "manager": _text(asset.get("manager")),
        "inception_date": _text(asset.get("inception_date")),
        "created_at": _text(asset.get("_created_at")),
        "total_asset_value_dollar": _total_asset_value(asset),
        "yield_to_maturity_percent": asset.get("yield_to_maturity_percent"),
        "apy_30_day": asset.get("apy_30_day"),
        "token_count": len(token_rows),
        "token_networks": _token_networks(asset),
        "token_platforms": _token_platforms(asset),
        "token_addresses": sorted(_text(token.get("address")) for token in token_rows if token.get("address")),
        "primary_market_count": len(primary_markets),
        "identity_mapping_status": mapping_confidence,
        "identity_verification_status": _identity_verification_status(
            mapping_confidence
        ),
        "canonical_underlying_candidate": underlying,
        "monitor_url": DEFAULT_RWA_XYZ_MONITOR_URL,
    }
    row.update(_yield_metric_fields(asset))
    return row


def normalize_rwa_xyz_token_rows(asset: dict[str, Any]) -> list[dict[str, Any]]:
    asset_row = normalize_rwa_xyz_asset_row(asset)
    rows: list[dict[str, Any]] = []
    for token in asset.get("tokens") or []:
        network = token.get("network") if isinstance(token.get("network"), dict) else {}
        platform = token.get("platform") if isinstance(token.get("platform"), dict) else {}
        rows.append(
            {
                "token_row_id": f"rwa_xyz:{asset_row['rwa_xyz_asset_id']}:{token.get('id')}",
                "rwa_xyz_asset_id": asset_row["rwa_xyz_asset_id"],
                "rwa_xyz_token_id": _text(token.get("id")),
                "asset_id": asset_row["asset_id"],
                "symbol": asset_row["symbol"],
                "rwa_xyz_ticker": asset_row["rwa_xyz_ticker"],
                "asset_name": asset_row["name"],
                "asset_class": asset_row["asset_class"],
                "rwa_xyz_asset_class": asset_row["rwa_xyz_asset_class"],
                "issuer_name": asset_row["issuer_name"],
                "platform": _nested_label(token, "platform"),
                "platform_slug": _text(platform.get("slug")) if isinstance(platform, dict) else "",
                "platform_website": _text(platform.get("website")) if isinstance(platform, dict) else "",
                "tokenization_type": _text(platform.get("tokenization_type")) if isinstance(platform, dict) else "",
                "issuance_type": _text(platform.get("issuance_type")) if isinstance(platform, dict) else "",
                "network": _nested_label(token, "network"),
                "network_slug": _text(network.get("slug")) if isinstance(network, dict) else "",
                "address": _text(token.get("address")),
                "token_name": _text(token.get("name")),
                "standards": _standards(token),
                "asset_manager": _nested_label(token, "asset_manager"),
                "total_asset_value_dollar": asset_row["total_asset_value_dollar"],
                "identity_mapping_status": asset_row["identity_mapping_status"],
                "identity_verification_status": asset_row[
                    "identity_verification_status"
                ],
                "canonical_underlying_candidate": asset_row["canonical_underlying_candidate"],
                "monitor_url": DEFAULT_RWA_XYZ_MONITOR_URL,
            }
        )
    return rows


def coverage_rows_from_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for asset in assets:
        normalized = normalize_rwa_xyz_asset_row(asset)
        rows.append(
            {
                "symbol": normalized["symbol"],
                "asset_id": normalized["asset_id"],
                "asset_class": normalized["asset_class"],
                "venue": RWA_XYZ_VENUE_ID,
                "source_type": "platform_catalog_reference",
                "coverage_status": "rwa_xyz_catalog_candidate_requires_token_pool_liquidity_freshness_and_issuer_validation",
                "vwap_support": "not_vwap_suitable_catalog_only",
                "bidask_support": "not_bidask_suitable_catalog_only",
                "block_sizes_usd": [],
                "metadata": {
                    "rwa_xyz_asset_id": normalized["rwa_xyz_asset_id"],
                    "rwa_xyz_ticker": normalized["rwa_xyz_ticker"],
                    "asset_name": normalized["name"],
                    "rwa_xyz_asset_class": normalized["rwa_xyz_asset_class"],
                    "issuer_name": normalized["issuer_name"],
                    "manager": normalized["manager"],
                    "inception_date": normalized["inception_date"],
                    "created_at": normalized["created_at"],
                    "total_asset_value_dollar": normalized["total_asset_value_dollar"],
                    "token_count": normalized["token_count"],
                    "token_networks": normalized["token_networks"],
                    "token_platforms": normalized["token_platforms"],
                    "token_addresses": normalized["token_addresses"],
                    "identity_mapping_status": normalized["identity_mapping_status"],
                    "identity_verification_status": normalized[
                        "identity_verification_status"
                    ],
                    "canonical_underlying_candidate": normalized["canonical_underlying_candidate"],
                    "promotion_gate": "token_contract_pool_or_venue_discovery_liquidity_freshness_manipulation_issuer_nav_and_benchmark_alignment_required",
                    "source_url": DEFAULT_RWA_XYZ_MONITOR_URL,
                },
            }
        )
    return sorted(rows, key=lambda row: (str(row["asset_id"]), str(row["symbol"])))


def parse_next_data_from_html(html_text: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html_text)
    if not match:
        raise ValueError("RWA.xyz monitor HTML did not contain __NEXT_DATA__")
    return json.loads(html.unescape(match.group(1)))


def assets_from_monitor_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    page_props = payload.get("pageProps") if isinstance(payload.get("pageProps"), dict) else None
    if page_props is None:
        props = payload.get("props") if isinstance(payload.get("props"), dict) else {}
        page_props = props.get("pageProps") if isinstance(props.get("pageProps"), dict) else {}
    assets = page_props.get("assets") if isinstance(page_props, dict) else []
    if not isinstance(assets, list):
        raise ValueError("RWA.xyz monitor payload did not contain pageProps.assets")
    return [row for row in assets if isinstance(row, dict)]


def next_data_url_for_build(build_id: str) -> str:
    return f"https://app.rwa.xyz/_next/data/{build_id}/new-asset-monitor.json"


def fetch_rwa_xyz_monitor_payload(
    *,
    monitor_url: str = DEFAULT_RWA_XYZ_MONITOR_URL,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the monitor via the public page and Next.js data endpoint."""
    request = urllib.request.Request(
        monitor_url,
        headers={"User-Agent": "Blocksize-RWA-coverage-discovery/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        html_text = response.read().decode("utf-8")
    next_data = parse_next_data_from_html(html_text)
    build_id = _text(next_data.get("buildId"))
    data_url = next_data_url_for_build(build_id) if build_id else ""
    if data_url:
        data_request = urllib.request.Request(
            data_url,
            headers={"User-Agent": "Blocksize-RWA-coverage-discovery/1.0"},
        )
        with urllib.request.urlopen(data_request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        source = "next_data_endpoint"
    else:
        payload = next_data
        source = "embedded_next_data"
    return payload, {
        "monitor_url": monitor_url,
        "next_build_id": build_id,
        "next_data_url": data_url or None,
        "source": source,
        "fetched_at": _utc_now_iso(),
        "html_bytes": len(html_text),
    }


def load_payload_from_file(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = Path(path).read_text(encoding="utf-8")
    if raw.lstrip().startswith("<"):
        payload = parse_next_data_from_html(raw)
        source = "local_html_next_data"
    else:
        payload = json.loads(raw)
        source = "local_json"
    return payload, {
        "monitor_url": DEFAULT_RWA_XYZ_MONITOR_URL,
        "next_build_id": payload.get("buildId"),
        "next_data_url": None,
        "source": source,
        "fetched_at": _utc_now_iso(),
        "input_path": str(path),
    }


def _counter_dict(counter: Counter[str], *, limit: int | None = None) -> dict[str, int]:
    items = counter.most_common(limit)
    return {key: int(value) for key, value in items}


def build_rwa_xyz_monitor_report(
    assets: list[dict[str, Any]],
    *,
    fetch_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = _utc_now_iso()
    asset_rows = [normalize_rwa_xyz_asset_row(asset) for asset in assets]
    token_rows = [
        token_row
        for asset in assets
        for token_row in normalize_rwa_xyz_token_rows(asset)
    ]
    coverage_rows = coverage_rows_from_assets(assets)
    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(days=30)

    def _created_dt(row: dict[str, Any]) -> datetime | None:
        raw = _text(row.get("created_at"))
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    recent_assets = [row for row in asset_rows if (_created_dt(row) or datetime.min.replace(tzinfo=UTC)) >= recent_cutoff]
    by_asset_class = Counter(row["rwa_xyz_asset_class"] for row in asset_rows)
    by_normalized_asset_class = Counter(row["asset_class"] for row in asset_rows)
    by_network = Counter(row["network"] or "unknown" for row in token_rows)
    by_platform = Counter(row["platform"] or "unknown" for row in token_rows)
    by_issuer = Counter(row["issuer_name"] or "unknown" for row in asset_rows)
    by_standard = Counter("|".join(row["standards"]) or "unknown" for row in token_rows)
    by_identity_status = Counter(row["identity_mapping_status"] for row in asset_rows)
    by_identity_verification_status = Counter(
        row["identity_verification_status"] for row in asset_rows
    )

    return {
        "generated_at": generated_at,
        "source": {
            "platform": "RWA.xyz",
            "monitor_url": DEFAULT_RWA_XYZ_MONITOR_URL,
            "docs_url": DEFAULT_RWA_XYZ_DOCS_URL,
            **(fetch_metadata or {}),
        },
        "summary": {
            "asset_count": len(asset_rows),
            "token_count": len(token_rows),
            "coverage_row_count": len(coverage_rows),
            "tokens_with_contract_address": sum(1 for row in token_rows if row["address"]),
            "recent_30d_asset_count": len(recent_assets),
            "latest_created_at": max((row["created_at"] for row in asset_rows if row["created_at"]), default=None),
            "earliest_created_at": min((row["created_at"] for row in asset_rows if row["created_at"]), default=None),
            "by_rwa_xyz_asset_class": _counter_dict(by_asset_class),
            "by_asset_class": _counter_dict(by_normalized_asset_class),
            "by_network": _counter_dict(by_network),
            "by_platform": _counter_dict(by_platform),
            "by_token_standard": _counter_dict(by_standard),
            "by_identity_mapping_status": _counter_dict(by_identity_status),
            "by_identity_verification_status": _counter_dict(
                by_identity_verification_status
            ),
            "identity_quality": rwa_xyz_identity_quality_summary(asset_rows),
            "contract_identity_quality": rwa_xyz_contract_identity_quality_summary(
                token_rows
            ),
            "yield_metric_quality": {
                "yield_to_maturity": {
                    "populated_asset_count": sum(
                        row["yield_to_maturity_value"] is not None
                        for row in asset_rows
                    ),
                    "unit": _YIELD_TO_MATURITY_UNIT,
                    "basis": _YIELD_TO_MATURITY_BASIS,
                    "raw_trend_field": "yield_to_maturity_raw_trend",
                },
                "apy_30_day": {
                    "populated_asset_count": sum(
                        row["apy_30_day_value"] is not None for row in asset_rows
                    ),
                    "unit": _APY_30_DAY_UNIT,
                    "basis": _APY_30_DAY_BASIS,
                    "raw_trend_field": "apy_30_day_raw_trend",
                },
            },
            "top_issuers": _counter_dict(by_issuer, limit=25),
        },
        "source_assessment": {
            "catalog_extraction": "Fetch the public monitor page, parse __NEXT_DATA__.buildId, then read the matching /_next/data/{buildId}/new-asset-monitor.json payload.",
            "direct_realtime_price_feed_available_from_monitor": False,
            "direct_realtime_price_feed_note": "The monitor provides product, issuer, platform, network, token-contract, primary-market, yield, APY, and total-asset-value metadata; it does not expose tick-by-tick executable bid/ask or block-size depth in the observed public payload.",
            "real_time_sourcing_path": [
                "Use token address plus network from token_rows as the hard identity.",
                "Discover executable pools/routes/order books by network and platform: Jupiter/Raydium/Orca/Meteora on Solana, Uniswap/Curve/Balancer/Aerodrome on EVM networks, and native venue APIs where issuers expose quote streams.",
                "Attach pool id, route plan, fee tier, slot/block number, raw payload hash, liquidity, spread, and price-impact evidence.",
                "Align issuer NAV, reserve, primary-market terms, and RWA.xyz total-asset-value context before production promotion.",
                "Run 30-minute freshness, latency, tick-frequency, manipulation, and Blocksize benchmark windows before the source can affect consensus.",
            ],
            "production_boundary": "RWA.xyz rows are candidate/supplemental catalog coverage until token/pool discovery, liquidity checks, state/freshness, manipulation checks, issuer NAV alignment, and data-rights review pass.",
            "identity_boundary": "Only allowlisted high-confidence mappings use a global canonical asset_id. Every unverified RWA.xyz row keeps a source-scoped RWA_XYZ_<source-id> identity and no canonical_underlying_candidate.",
            "contract_identity_boundary": "Token rows are preserved at source-listing grain. Repeated normalized network/address identities within one RWA.xyz source asset are reported but do not block acceptance; any identity assigned to multiple source assets is a decision-grade blocker.",
            "yield_unit_contract": {
                "yield_to_maturity_value": {
                    "unit": _YIELD_TO_MATURITY_UNIT,
                    "basis": _YIELD_TO_MATURITY_BASIS,
                    "raw_trend_field": "yield_to_maturity_raw_trend",
                },
                "apy_30_day_value": {
                    "unit": _APY_30_DAY_UNIT,
                    "basis": _APY_30_DAY_BASIS,
                    "raw_trend_field": "apy_30_day_raw_trend",
                },
            },
        },
        "asset_rows": sorted(asset_rows, key=lambda row: (str(row["created_at"]), str(row["rwa_xyz_asset_id"])), reverse=True),
        "token_rows": sorted(token_rows, key=lambda row: (str(row["asset_id"]), str(row["network"]), str(row["platform"]), str(row["address"]))),
        "coverage_rows": coverage_rows,
    }


def load_rwa_xyz_monitor_report(
    path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
) -> dict[str, Any]:
    report_path = Path(path)
    if not report_path.exists():
        return {
            "generated_at": None,
            "source": {
                "platform": "RWA.xyz",
                "monitor_url": DEFAULT_RWA_XYZ_MONITOR_URL,
                "docs_url": DEFAULT_RWA_XYZ_DOCS_URL,
                "status": "report_missing",
            },
            "summary": {
                "asset_count": 0,
                "token_count": 0,
                "coverage_row_count": 0,
                "tokens_with_contract_address": 0,
                "by_asset_class": {},
                "by_network": {},
                "by_platform": {},
            },
            "source_assessment": {
                "direct_realtime_price_feed_available_from_monitor": False,
                "production_boundary": "Run the RWA.xyz monitor discovery script before using this source.",
            },
            "asset_rows": [],
            "token_rows": [],
            "coverage_rows": [],
        }
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "generated_at": None,
            "source": {"platform": "RWA.xyz", "status": "report_unreadable", "path": str(report_path)},
            "summary": {"asset_count": 0, "token_count": 0, "coverage_row_count": 0},
            "source_assessment": {"direct_realtime_price_feed_available_from_monitor": False},
            "asset_rows": [],
            "token_rows": [],
            "coverage_rows": [],
        }
    return payload if isinstance(payload, dict) else {}


def load_rwa_xyz_coverage_rows(
    path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
) -> list[dict[str, Any]]:
    rows = load_rwa_xyz_monitor_report(path).get("coverage_rows")
    if not isinstance(rows, list):
        return []
    return [deepcopy(row) for row in rows if isinstance(row, dict)]


def load_rwa_xyz_token_rows(
    path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
) -> list[dict[str, Any]]:
    rows = load_rwa_xyz_monitor_report(path).get("token_rows")
    if not isinstance(rows, list):
        return []
    return [deepcopy(row) for row in rows if isinstance(row, dict)]


def build_rwa_xyz_monitor_view(
    *,
    include_asset_rows: bool = False,
    include_token_rows: bool = False,
    include_coverage_rows: bool = False,
    row_limit: int = 100,
    path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
) -> dict[str, Any]:
    report = load_rwa_xyz_monitor_report(path)
    result = {
        key: deepcopy(value)
        for key, value in report.items()
        if key not in {"asset_rows", "token_rows", "coverage_rows"}
    }
    result["report_path"] = str(path)
    result["available_row_sets"] = {
        "asset_rows": len(report.get("asset_rows") or []),
        "token_rows": len(report.get("token_rows") or []),
        "coverage_rows": len(report.get("coverage_rows") or []),
    }
    limit = max(0, int(row_limit))
    if include_asset_rows:
        result["asset_rows"] = deepcopy((report.get("asset_rows") or [])[:limit])
    if include_token_rows:
        result["token_rows"] = deepcopy((report.get("token_rows") or [])[:limit])
    if include_coverage_rows:
        result["coverage_rows"] = deepcopy((report.get("coverage_rows") or [])[:limit])
    return result


def write_rwa_xyz_monitor_reports(
    *,
    json_path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    asset_csv_path: str | Path = DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    token_csv_path: str | Path = DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
    payload: dict[str, Any] | None = None,
    fetch_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload, fetch_metadata = fetch_rwa_xyz_monitor_payload()
    assets = assets_from_monitor_payload(payload)
    report = build_rwa_xyz_monitor_report(assets, fetch_metadata=fetch_metadata)

    json_out = Path(json_path)
    asset_csv_out = Path(asset_csv_path)
    token_csv_out = Path(token_csv_path)
    for path in (json_out, asset_csv_out, token_csv_out):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _write_csv(asset_csv_out, report["asset_rows"])
    _write_csv(token_csv_out, report["token_rows"])
    return report


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value
                    for key, value in row.items()
                }
            )
