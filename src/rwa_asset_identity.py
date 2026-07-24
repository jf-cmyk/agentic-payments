"""Ticker identity audit for RWA coverage symbols.

The feed registry uses venue-normalized symbols. This module separates the
registry routing class from the instrument identity and underlying exposure so
tokenized funds are not mislabeled as direct Treasury instruments.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.rwa_coverage import build_rwa_asset_matrix


IdentityOverride = dict[str, str]


IDENTITY_OVERRIDES: dict[str, IdentityOverride] = {
    "ABCL": {"name": "AbCellera Biologics Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "AAPL": {"name": "Apple Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "AMD": {"name": "Advanced Micro Devices, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "AMZN": {"name": "Amazon.com, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "ARM": {"name": "Arm Holdings plc ADR", "primary_type": "listed_equity_adr", "exposure": "single_company_equity"},
    "ASML": {"name": "ASML Holding N.V. ADR", "primary_type": "listed_equity_adr", "exposure": "single_company_equity"},
    "AUD": {"name": "Australian dollar", "primary_type": "fiat_currency", "exposure": "fx_rate"},
    "AVGO": {"name": "Broadcom Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "BB": {"name": "BlackBerry Limited", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "BE": {"name": "Bloom Energy Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "BMNR": {"name": "BitMine Immersion Technologies, Inc.", "primary_type": "listed_equity", "exposure": "digital_asset_treasury_company_equity"},
    "BOT": {"name": "Venue-listed BOT equity symbol", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "BRENT": {"name": "Brent crude oil benchmark", "primary_type": "commodity_benchmark", "exposure": "energy"},
    "BRUN": {"name": "Venue-listed BRUN equity symbol", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "BUIDL": {"name": "BlackRock USD Institutional Digital Liquidity Fund token", "primary_type": "tokenized_liquidity_fund", "exposure": "cash_us_treasury_bills_repurchase_agreements"},
    "CAT": {"name": "Caterpillar Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "COIN": {"name": "Coinbase Global, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "COST": {"name": "Costco Wholesale Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "CRCL": {"name": "Circle Internet Group, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "CRWV": {"name": "CoreWeave, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "CVX": {"name": "Chevron Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "DIA": {"name": "SPDR Dow Jones Industrial Average ETF Trust", "primary_type": "etf", "exposure": "dow_jones_industrial_average"},
    "DRAM": {"name": "Global X Semiconductor ETF or venue DRAM ETF symbol", "primary_type": "etf", "exposure": "semiconductor_equities"},
    "EUR": {"name": "Euro", "primary_type": "fiat_currency", "exposure": "fx_rate"},
    "EURC": {"name": "Circle Euro Coin / EURC", "primary_type": "euro_stablecoin", "exposure": "eur_cash_equivalent"},
    "GBP": {"name": "British pound sterling", "primary_type": "fiat_currency", "exposure": "fx_rate"},
    "GDX": {"name": "VanEck Gold Miners ETF", "primary_type": "etf", "exposure": "gold_mining_equities"},
    "GER40": {"name": "Germany 40 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "germany_large_cap_equities"},
    "GEV": {"name": "GE Vernova Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "GLXY": {"name": "Galaxy Digital Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "GME": {"name": "GameStop Corp.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "GOOG": {"name": "Alphabet Inc. Class C", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "GOOGL": {"name": "Alphabet Inc. Class A", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "HG": {"name": "Copper futures benchmark", "primary_type": "commodity_benchmark", "exposure": "industrial_metal"},
    "HK50": {"name": "Hong Kong 50 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "hong_kong_large_cap_equities"},
    "HOOD": {"name": "Robinhood Markets, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "HYG": {"name": "iShares iBoxx $ High Yield Corporate Bond ETF", "primary_type": "etf", "exposure": "high_yield_corporate_bonds"},
    "INTC": {"name": "Intel Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "IOVA": {"name": "Iovance Biotherapeutics, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "IREN": {"name": "IREN Limited", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "IWM": {"name": "iShares Russell 2000 ETF", "primary_type": "etf", "exposure": "russell_2000_index"},
    "JP225": {"name": "Japan 225 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "japan_large_cap_equities"},
    "KR2550": {"name": "Venue KR2550 ETF/index basket", "primary_type": "etf", "exposure": "korea_equity_basket"},
    "LMT": {"name": "Lockheed Martin Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "LPTH": {"name": "LightPath Technologies, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MARA": {"name": "MARA Holdings, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MCD": {"name": "McDonald's Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "META": {"name": "Meta Platforms, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MP": {"name": "MP Materials Corp.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MRVL": {"name": "Marvell Technology, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MSFT": {"name": "Microsoft Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "MSTR": {"name": "Strategy Inc.", "primary_type": "listed_equity", "exposure": "bitcoin_treasury_company_equity"},
    "MU": {"name": "Micron Technology, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "NBIS": {"name": "Nebius Group N.V.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "NFLX": {"name": "Netflix, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "NVDA": {"name": "NVIDIA Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "NZD": {"name": "New Zealand dollar", "primary_type": "fiat_currency", "exposure": "fx_rate"},
    "ORCL": {"name": "Oracle Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "OUSG": {"name": "Ondo Short-Term US Government Treasuries product token", "primary_type": "tokenized_treasury_fund", "exposure": "short_term_us_treasuries"},
    "PAXG": {"name": "PAX Gold token", "primary_type": "tokenized_gold", "exposure": "allocated_physical_gold"},
    "PLTR": {"name": "Palantir Technologies Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "PYPL": {"name": "PayPal Holdings, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "QQQ": {"name": "Invesco QQQ Trust, Series 1", "primary_type": "etf", "exposure": "nasdaq_100_index"},
    "REMX": {"name": "VanEck Rare Earth/Strategic Metals ETF", "primary_type": "etf", "exposure": "rare_earth_and_strategic_metals_equities"},
    "RIOT": {"name": "Riot Platforms, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "RIVN": {"name": "Rivian Automotive, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SAMSUNG": {"name": "Samsung Electronics Co., Ltd.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SBET": {"name": "SharpLink Gaming, Inc.", "primary_type": "listed_equity", "exposure": "ethereum_treasury_company_equity"},
    "SGOV": {"name": "iShares 0-3 Month Treasury Bond ETF", "primary_type": "etf", "exposure": "short_term_us_treasury_bills"},
    "SHAZ": {"name": "Sharon AI, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SHEL": {"name": "Shell plc ADR", "primary_type": "listed_equity_adr", "exposure": "single_company_equity"},
    "SKHYNIX": {"name": "SK hynix Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SMCI": {"name": "Super Micro Computer, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SNAP": {"name": "Snap Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SNDK": {"name": "SanDisk Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SPCX": {"name": "SpaceX Corp.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "SPY": {"name": "SPDR S&P 500 ETF Trust", "primary_type": "etf", "exposure": "sp_500_index"},
    "TBILL": {"name": "OpenEden TBILL tokenized U.S. Treasury Bills product", "primary_type": "tokenized_treasury_fund", "exposure": "us_treasury_bills"},
    "TBLL": {"name": "Tokenized Treasury-bill ETF or venue TBLLx product", "primary_type": "tokenized_etf", "exposure": "short_term_us_treasury_bills"},
    "TLT": {"name": "iShares 20+ Year Treasury Bond ETF", "primary_type": "etf", "exposure": "long_duration_us_treasuries"},
    "TSLA": {"name": "Tesla, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "TSM": {"name": "Taiwan Semiconductor Manufacturing Company Limited ADR", "primary_type": "listed_equity_adr", "exposure": "single_company_equity"},
    "UK100": {"name": "UK 100 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "uk_large_cap_equities"},
    "UNG": {"name": "United States Natural Gas Fund LP", "primary_type": "commodity_pool_etf", "exposure": "natural_gas_futures"},
    "URA": {"name": "Global X Uranium ETF", "primary_type": "etf", "exposure": "uranium_equities"},
    "URNM": {"name": "Sprott Uranium Miners ETF", "primary_type": "etf", "exposure": "uranium_mining_equities"},
    "US100": {"name": "U.S. 100 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "nasdaq_100_index"},
    "US30": {"name": "U.S. 30 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "dow_jones_industrial_average"},
    "US500": {"name": "U.S. 500 equity index CFD/synthetic", "primary_type": "equity_index_cfd", "exposure": "sp_500_index"},
    "USCC": {"name": "Superstate Crypto Carry Fund token", "primary_type": "tokenized_crypto_carry_fund", "exposure": "crypto_basis_strategy_with_us_treasury_securities"},
    "USD": {"name": "U.S. dollar", "primary_type": "fiat_currency", "exposure": "fx_rate"},
    "USDY": {"name": "Ondo US Dollar Yield Token", "primary_type": "tokenized_yield_note", "exposure": "short_term_us_treasuries_etf_shares_or_bank_demand_deposits"},
    "USTB": {"name": "Superstate Short Duration U.S. Government Securities Fund token", "primary_type": "tokenized_treasury_fund", "exposure": "short_duration_us_treasury_bills"},
    "VOO": {"name": "Vanguard S&P 500 ETF", "primary_type": "etf", "exposure": "sp_500_index"},
    "WPM": {"name": "Wheaton Precious Metals Corp.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "WTI": {"name": "West Texas Intermediate crude oil benchmark", "primary_type": "commodity_benchmark", "exposure": "energy"},
    "WYFI": {"name": "WhiteFiber, Inc.", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "XAG": {"name": "Silver spot benchmark", "primary_type": "metal_benchmark", "exposure": "silver"},
    "XAU": {"name": "Gold spot benchmark", "primary_type": "metal_benchmark", "exposure": "gold"},
    "XCU": {"name": "Copper spot benchmark", "primary_type": "commodity_benchmark", "exposure": "industrial_metal"},
    "XLE": {"name": "Energy Select Sector SPDR Fund", "primary_type": "etf", "exposure": "us_energy_sector_equities"},
    "XOM": {"name": "Exxon Mobil Corporation", "primary_type": "listed_equity", "exposure": "single_company_equity"},
    "XPD": {"name": "Palladium spot benchmark", "primary_type": "metal_benchmark", "exposure": "palladium"},
    "XPT": {"name": "Platinum spot benchmark", "primary_type": "metal_benchmark", "exposure": "platinum"},
}


SOURCE_NOTES: dict[str, str] = {
    "BUIDL": "External evidence: BlackRock/Securitize tokenized liquidity fund; secondary reporting states assets are cash, U.S. Treasury bills, and repo.",
    "OUSG": "External evidence: Ondo docs describe OUSG as backed by short-term U.S. Treasuries and invested through ETFs or MMFs.",
    "USDY": "External evidence: Ondo docs describe USDY as a tokenized note secured by short-term U.S. Treasuries, ETF shares, or bank demand deposits depending on issuance date.",
    "USTB": "External evidence: Superstate describes USTB as a tokenized private fund providing access to short-duration Treasury Bills.",
    "USCC": "External evidence: Superstate describes USCC as a crypto basis/carry fund across crypto assets and U.S. Treasury securities.",
    "SGOV": "External evidence: iShares product family identifies SGOV as an ETF tracking 0-3 month Treasury bonds.",
    "PAXG": "External evidence: Paxos identifies PAXG as a gold-backed digital asset.",
    "SPCX": "External evidence: current July 2026 market coverage identifies SPCX as SpaceX stock, not the historical SPCX ETF symbol.",
}


def _fallback_identity(asset_id: str, asset_classes: list[str]) -> IdentityOverride:
    primary_class = asset_classes[0] if asset_classes else "unknown"
    if primary_class == "equity":
        return {
            "name": f"{asset_id} venue-listed equity symbol",
            "primary_type": "listed_equity",
            "exposure": "single_company_equity",
        }
    if primary_class == "etf":
        return {"name": f"{asset_id} ETF symbol", "primary_type": "etf", "exposure": "basket_or_fund_holdings"}
    if primary_class == "index":
        return {
            "name": f"{asset_id} equity index or CFD symbol",
            "primary_type": "equity_index_cfd",
            "exposure": "equity_index",
        }
    if primary_class == "fx":
        return {"name": f"{asset_id} fiat or stablecoin FX symbol", "primary_type": "fx_or_stablecoin", "exposure": "fx_rate"}
    if primary_class in {"commodity", "metal"}:
        return {"name": f"{asset_id} commodity benchmark", "primary_type": "commodity_benchmark", "exposure": primary_class}
    return {"name": f"{asset_id} venue-listed asset", "primary_type": primary_class, "exposure": primary_class}


def _verification_status(asset_id: str, registry_classes: list[str], primary_type: str) -> str:
    registry_set = set(registry_classes)
    if asset_id in {"BOT", "BRUN", "KR2550", "TBLL"}:
        return "needs_security_master_verification"
    if asset_id in SOURCE_NOTES:
        return "externally_verified"
    if primary_type in {"listed_equity", "listed_equity_adr", "etf"}:
        return "venue_documented_security_master_recommended"
    if primary_type in {"fiat_currency", "commodity_benchmark", "metal_benchmark", "equity_index_cfd"}:
        return "venue_documented"
    if registry_set:
        return "venue_documented"
    return "unknown"


def _classification_action(asset_classes: list[str], primary_type: str) -> str:
    registry = set(asset_classes)
    if "treasury" in registry:
        return "reclassify_legacy_treasury_bucket"
    if primary_type in {"tokenized_liquidity_fund", "tokenized_treasury_fund", "tokenized_yield_note"}:
        if "treasury_fund" not in registry:
            return "split_primary_type_from_treasury_exposure"
    if primary_type == "tokenized_crypto_carry_fund" and "tokenized_fund" not in registry:
        return "correct_from_treasury_to_tokenized_fund"
    if primary_type == "etf" and "index" in registry and "etf" not in registry:
        return "correct_index_bucket_to_etf"
    return "keep"


def build_rwa_ticker_identity_audit() -> dict[str, Any]:
    """Return an identity and classification audit for every covered ticker."""
    matrix = build_rwa_asset_matrix()
    rows: list[dict[str, Any]] = []
    for asset in matrix["assets"]:
        asset_id = str(asset["asset_id"])
        asset_classes = [str(item) for item in asset.get("asset_classes", [])]
        identity = IDENTITY_OVERRIDES.get(asset_id) or _fallback_identity(asset_id, asset_classes)
        primary_type = identity["primary_type"]
        rows.append(
            {
                "asset_id": asset_id,
                "name": identity["name"],
                "registry_asset_classes": asset_classes,
                "verified_primary_type": primary_type,
                "underlying_exposure": identity["exposure"],
                "canonical_symbols": asset["symbols"],
                "venues": sorted(asset["venues"]),
                "venue_symbols": {
                    venue_id: venue_data["symbol"]
                    for venue_id, venue_data in sorted(asset["venues"].items())
                },
                "verification_status": _verification_status(asset_id, asset_classes, primary_type),
                "classification_action": _classification_action(asset_classes, primary_type),
                "note": SOURCE_NOTES.get(asset_id, ""),
            }
        )

    by_status = Counter(row["verification_status"] for row in rows)
    by_primary_type = Counter(row["verified_primary_type"] for row in rows)
    actions = Counter(row["classification_action"] for row in rows)
    return {
        "summary": {
            "asset_count": len(rows),
            "by_verification_status": dict(sorted(by_status.items())),
            "by_primary_type": dict(sorted(by_primary_type.items())),
            "by_classification_action": dict(sorted(actions.items())),
            "buidl_answer": "BUIDL is not a direct Treasury instrument; it is a tokenized liquidity fund with Treasury-bill, cash, and repo exposure.",
        },
        "rows": sorted(rows, key=lambda item: str(item["asset_id"])),
    }


def write_rwa_ticker_identity_audit(
    *,
    json_path: str | Path,
    csv_path: str | Path,
) -> dict[str, Any]:
    """Write the identity audit as JSON and CSV for review."""
    audit = build_rwa_ticker_identity_audit()
    json_output = Path(json_path)
    csv_output = Path(csv_path)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "asset_id",
        "name",
        "registry_asset_classes",
        "verified_primary_type",
        "underlying_exposure",
        "canonical_symbols",
        "venues",
        "venue_symbols",
        "verification_status",
        "classification_action",
        "note",
    ]
    with csv_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in audit["rows"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True) if isinstance(row[key], (list, dict)) else row[key]
                    for key in fieldnames
                }
            )
    return audit
