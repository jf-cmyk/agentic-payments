"""Fail-closed RWA underlying-identity and instrument semantics.

The coverage catalog combines spot assets, tokenized wrappers, synthetic
markets, perpetuals, options, and source-specific discovery rows.  A venue's
``asset_class`` is therefore preserved as source evidence, but it is not used
as the contract type and is not assumed to identify a canonical underlying.

The explicit map below is the reviewed resolution of every mixed-class bare
ticker in the 2026-07-30 captured catalog.  It is intentionally finite: an
unlisted or explicitly ambiguous ticker is never guessed into a security
master identity.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


# Reviewed against the checked-in static catalog and captured venue metadata.
# This includes all 55 raw mixed-class ids in the 2026-07-30 snapshot.
CURATED_UNDERLYING_ASSET_CLASSES: dict[str, str] = {
    "AAVE": "crypto",
    "ADA": "crypto",
    "ARM": "equity",
    "ASML": "equity",
    "BB": "equity",
    "BE": "equity",
    "BMNR": "equity",
    "BOT": "equity",
    "BTC": "crypto",
    "BZ": "commodity",
    "CAT": "equity",
    "CBRS": "equity",
    "CC": "crypto",
    "CL": "commodity",
    "COST": "equity",
    "CRWV": "equity",
    "DRAM": "etf",
    "ETH": "crypto",
    "EWY": "etf",
    "GME": "equity",
    "HYPE": "crypto",
    "INTC": "equity",
    "IREN": "equity",
    "IWM": "etf",
    "KORU": "etf",
    "LITE": "equity",
    "MRVL": "equity",
    "NATGAS": "commodity",
    "NBIS": "equity",
    "QQQ": "etf",
    "RIVN": "equity",
    "RKLB": "equity",
    "SAMSUNG": "equity",
    "SGOV": "etf",
    "SKHYNIX": "equity",
    "SNDK": "equity",
    "SNX": "crypto",
    "SOL": "crypto",
    "SOXL": "etf",
    "SPCX": "equity",
    "SPY": "etf",
    "SYRUP": "crypto",
    "TLT": "etf",
    "URA": "etf",
    "URNM": "etf",
    "US100": "index",
    "US500": "index",
    "USDH": "fx",
    "XAG": "metal",
    "XAU": "metal",
    "XAUT": "metal",
    "XAUT0": "metal",
    "XLE": "etf",
    "XPD": "metal",
    "XPT": "metal",
    "XRP": "crypto",
    "ZEC": "crypto",
}

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _normalized_contract_type(row: dict[str, Any]) -> str:
    """Separate the traded contract from the underlying asset class."""
    metadata = _metadata(row)
    raw_values = [
        metadata.get("instrument_type"),
        metadata.get("contract_type"),
        metadata.get("market_type"),
    ]
    instrument_types = metadata.get("instrument_types")
    if isinstance(instrument_types, list):
        raw_values.extend(instrument_types)
    combined = " ".join(_text(value).lower() for value in raw_values)
    market_type = _text(metadata.get("market_type")).lower()
    source_type = _text(row.get("source_type")).lower()

    if "option" in combined:
        return "option"
    if "future" in combined and "perpet" not in combined:
        return "dated_future"
    if "perp" in combined or "perpetual" in combined:
        return "perpetual"
    if "stock_contract" in combined or market_type == "stock":
        return "synthetic_security_contract"
    if "prediction" in combined:
        return "prediction_contract"
    if "yield" in combined or source_type == "onchain_yield_market":
        return "yield_derivative"
    if market_type == "spot" or source_type in {
        "native_l2",
        "quote_sweep",
        "onchain_clmm_pool",
        "onchain_stableswap_pool",
    }:
        return "spot_or_executable_reference"
    if source_type in {
        "nav_reference",
        "issuer_reference",
        "benchmark_reference",
        "blocksize_state_reference",
        "platform_catalog_reference",
        "price_stream_no_book",
    }:
        return "reference"
    if source_type == "synthetic_depth":
        return "synthetic_perpetual"
    return "unspecified"


def _source_scope_key(row: dict[str, Any], raw_asset_id: str) -> str:
    """Return one stable, source-scoped id for an ambiguous bare ticker."""
    metadata = _metadata(row)
    venue = _text(row.get("venue")).upper()
    # The two Hyperliquid catalog representations are the same source family.
    if venue in {"HYPERLIQUID_RWA_SPOT", "HYPERLIQUID_SPOT"}:
        venue = "HYPERLIQUID_SPOT"
    identity = (
        metadata.get("pair_index")
        or metadata.get("base_token_id")
        or metadata.get("token_id")
        or metadata.get("venue_market_id")
        or row.get("symbol")
        or raw_asset_id
    )
    normalized_identity = _NON_ALNUM.sub("", _text(identity).upper())[:40]
    return f"{venue}_{raw_asset_id}_{normalized_identity}"


def _is_explicit_bare_ticker_ambiguity(
    row: dict[str, Any], raw_asset_id: str
) -> bool:
    """Identify captured rows that must not share a ticker-only identity."""
    venue = _text(row.get("venue")).lower()
    coverage_status = _text(row.get("coverage_status")).lower()
    if raw_asset_id == "CAT" and venue in {
        "hyperliquid_rwa_spot",
        "hyperliquid_spot",
    }:
        # Contract-backed CAT is not evidence for Caterpillar equity.
        return True
    if raw_asset_id == "SPCX" and venue in {
        "hyperliquid_rwa_spot",
        "hyperliquid_spot",
    } and "unverified_identity" in coverage_status:
        # The captured token claims SPCX but explicitly lacks issuer identity.
        return True
    return False


def normalize_instrument_semantics(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one row without dropping its raw/source evidence.

    The function is idempotent so a captured report can be re-normalized
    offline without changing source timestamps or compounding identifiers.
    """
    normalized = deepcopy(row)
    metadata = deepcopy(_metadata(normalized))
    source_asset_id = _text(normalized.get("asset_id")).upper()
    raw_asset_id = _text(
        normalized.get("raw_source_asset_id")
        or metadata.get("raw_source_asset_id")
        or source_asset_id
    ).upper()
    raw_asset_class = _text(
        normalized.get("raw_source_asset_class")
        or metadata.get("raw_source_asset_class")
        or normalized.get("asset_class")
        or "unknown"
    ).lower()
    raw_asset_family = _text(
        normalized.get("raw_source_asset_family")
        or metadata.get("raw_source_asset_family")
        or normalized.get("asset_family")
        or ("crypto" if raw_asset_class == "crypto" else "rwa_or_traditional")
    ).lower()
    normalized["raw_source_asset_id"] = raw_asset_id
    normalized["raw_source_asset_class"] = raw_asset_class
    normalized["raw_source_asset_family"] = raw_asset_family
    normalized["contract_type"] = _normalized_contract_type(normalized)

    metadata.setdefault("raw_source_asset_id", raw_asset_id)
    metadata.setdefault("raw_source_asset_class", raw_asset_class)
    metadata.setdefault("raw_source_asset_family", raw_asset_family)
    raw_instrument_type = metadata.get("raw_instrument_type")
    if raw_instrument_type is None:
        raw_instrument_type = metadata.get("instrument_type")
    if raw_instrument_type is not None:
        metadata["raw_instrument_type"] = raw_instrument_type

    existing_identity_status = _text(
        normalized.get("identity_status") or metadata.get("identity_status")
    )
    if existing_identity_status == "source_scoped_ambiguous":
        canonical_id = source_asset_id
        underlying_class = "unknown"
        identity_status = "source_scoped_ambiguous"
        decision_grade = False
        evidence = (
            "bare ticker conflicts with a different documented underlying; "
            "contract identity is retained pending manual issuer verification"
        )
    elif _is_explicit_bare_ticker_ambiguity(normalized, source_asset_id):
        canonical_id = _source_scope_key(normalized, source_asset_id)
        underlying_class = "unknown"
        identity_status = "source_scoped_ambiguous"
        decision_grade = False
        evidence = (
            "bare ticker conflicts with a different documented underlying; "
            "contract identity is retained pending manual issuer verification"
        )
    else:
        # Preserve the caller's stable catalog identity. In the assembled
        # matrix this may intentionally include a full outcome/market suffix
        # (for example two opposing prediction outcomes) even when the source
        # artifact reports a shared baseAssetSymbol.
        canonical_id = source_asset_id
        curated_class = CURATED_UNDERLYING_ASSET_CLASSES.get(source_asset_id)
        source_identity_status = _text(
            metadata.get("identity_verification_status")
        ).lower()
        coverage_status = _text(normalized.get("coverage_status")).lower()
        if curated_class:
            underlying_class = curated_class
            identity_status = "verified_curated_underlying"
            decision_grade = True
            evidence = (
                "reviewed static security master and captured venue metadata"
            )
        else:
            underlying_class = raw_asset_class
            if source_identity_status == "verified":
                identity_status = "verified_source_identity"
                decision_grade = True
                evidence = "captured source identity is explicitly verified"
            elif (
                source_identity_status == "unverified"
                or "unverified_identity" in coverage_status
            ):
                identity_status = "source_scoped_unverified"
                decision_grade = False
                evidence = "captured source explicitly requires identity review"
            elif _text(normalized.get("source_component")) == (
                "static_coverage_catalog"
            ):
                identity_status = "documented_static_catalog"
                decision_grade = True
                evidence = "checked-in documented static catalog"
            else:
                identity_status = "source_classification_unverified"
                decision_grade = False
                evidence = (
                    "source classification retained; canonical identity has "
                    "not been independently verified"
                )

    normalized["asset_id"] = canonical_id
    normalized["canonical_underlying_id"] = canonical_id
    normalized["asset_class"] = underlying_class
    normalized["asset_family"] = (
        "crypto"
        if underlying_class == "crypto"
        else "unknown"
        if underlying_class == "unknown"
        else "rwa_or_traditional"
    )
    normalized["underlying_asset_class"] = underlying_class
    normalized["identity_status"] = identity_status
    normalized["decision_grade"] = decision_grade
    normalized["manual_verification_required"] = not decision_grade
    normalized["identity_evidence"] = evidence
    metadata["canonical_underlying_id"] = canonical_id
    metadata["underlying_asset_class"] = underlying_class
    metadata["contract_type"] = normalized["contract_type"]
    metadata["identity_status"] = identity_status
    metadata["decision_grade"] = decision_grade
    normalized["metadata"] = metadata
    return normalized


def normalize_instrument_semantics_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize a row collection while preserving order and row count."""
    return [normalize_instrument_semantics(row) for row in rows]
