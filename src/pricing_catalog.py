"""One price list for every paid Blocksize product, in credits and in USDC.

One credit is worth exactly ``CREDIT_PRICE_USDC`` ($0.001), so 1,000 credits
equal $1. Every paid call has one price in credits, and its x402 price is that
number of credits times $0.001. Free monthly connector credits and USDC
therefore buy the same products at the same rate.

x402 route pricing, the connector tools, the HTTP starter allowance, the
product catalogs, and the public pricing table all read from this module, so
the two currencies cannot drift apart again. Raw-data tiers default to
``DEFAULT_TIER_CREDITS`` and stay overridable through the ``PRICE_*`` USDC
settings (``src/config.py``); workflow products and trader indicators are
priced here in credits.

This module imports ``src.config`` lazily: release tooling imports the public
metadata (and so this price list) without server credentials, where the
settings object cannot be built.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

# One credit costs exactly this much USDC, so 1,000 credits equal $1. This is a
# published promise, so it is a constant rather than an environment variable.
CREDIT_PRICE_USDC = Decimal("0.001")
CREDITS_PER_USDC = int(Decimal("1") / CREDIT_PRICE_USDC)
QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "BTC", "ETH")

# Raw-data tiers, keyed by the PricingSettings field that holds the USDC price,
# with their default price in credits.
DEFAULT_TIER_CREDITS = {
    "core_crypto": 2,
    "extended_crypto": 4,
    "tradfi": 5,
    "equities": 8,
}
RAW_TIERS = tuple(DEFAULT_TIER_CREDITS)

# Services from the free-tier scope (``FreeTierSettings.KNOWN_SERVICES``) that
# are priced per call by the crypto tier of the symbol's base asset.
CRYPTO_TIER_SERVICES = frozenset(
    {"crypto_vwap", "crypto_bidask", "crypto_state", "crypto_vwap_30m", "crypto_vwap_24h"}
)


_CENT = Decimal("0.01")


def credits_to_usdc(credits: int | float | Decimal) -> Decimal:
    """Return the USDC price of a whole number of credits.

    Whole-cent amounts keep two decimals ("0.25", "2.50") so published x402
    prices read the same as before; sub-cent amounts keep three ("0.002").
    """
    usdc = (Decimal(str(credits)) * CREDIT_PRICE_USDC).quantize(CREDIT_PRICE_USDC)
    if usdc >= _CENT and usdc == usdc.quantize(_CENT):
        return usdc.quantize(_CENT)
    return usdc


def usdc_to_credits(usdc: Decimal | str | float) -> int:
    """Return the credits that cost the same as ``usdc``.

    Every USDC price is a whole number of credits (``PricingSettings`` rounds
    configured tier prices up to the next credit), so this is exact. Rounding
    up guards callers that pass a sum of arbitrary amounts.
    """
    credits = Decimal(str(usdc)) / CREDIT_PRICE_USDC
    return int(credits.to_integral_value(rounding=ROUND_CEILING))


def base_asset(symbol: str) -> str:
    """Extract a likely base asset from a compact pair symbol."""
    clean = symbol.strip().upper()
    for quote in QUOTE_SUFFIXES:
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean[: -len(quote)]
    return clean[: len(clean) // 2]


def crypto_tier(base: str) -> str:
    """Return ``core_crypto`` for top-250 assets and ``extended_crypto`` otherwise."""
    from src.config import TOP_250_CRYPTO

    return "core_crypto" if base.upper() in TOP_250_CRYPTO else "extended_crypto"


def tier_usdc(tier: str) -> Decimal:
    """Return the configured USDC price of a raw-data tier."""
    if tier not in RAW_TIERS:
        raise KeyError(f"Unknown raw-data tier {tier!r}")
    from src.config import settings

    return Decimal(getattr(settings.pricing, tier))


def tier_credits(tier: str) -> int:
    return usdc_to_credits(tier_usdc(tier))


def raw_tier_for_service(service: str, symbol: str = "") -> str:
    """Map a free-scope service and symbol to the raw-data tier it is billed at."""
    if service in {"fx", "metals"}:
        return "tradfi"
    if service == "equity_bidask":
        return "equities"
    if service in CRYPTO_TIER_SERVICES:
        return crypto_tier(base_asset(symbol)) if symbol else "core_crypto"
    raise KeyError(f"No raw-data tier is defined for service {service!r}")


def raw_credits(service: str, symbol: str = "") -> int:
    """Return the credit cost of one raw-data call."""
    return tier_credits(raw_tier_for_service(service, symbol))


@dataclass(frozen=True)
class Product:
    """A packaged product with one price in credits."""

    key: str
    credits: int
    label: str
    group: str
    route: str
    connector_tool: str | None = None

    @property
    def usdc(self) -> Decimal:
        return credits_to_usdc(self.credits)


AGENT_WORKFLOW = "agent_workflow"
TRADER_INDICATOR = "trader_indicator"
RWA = "rwa"

PRODUCTS: dict[str, Product] = {
    product.key: product
    for product in (
        Product(
            "pre_trade_check", 100, "Pre-trade sanity check", AGENT_WORKFLOW,
            "/v1/checks/pre-trade", "run_pre_trade_check",
        ),
        Product(
            "market_brief", 250, "Market brief", AGENT_WORKFLOW,
            "/v1/briefs/market", "get_market_brief",
        ),
        Product(
            "audit_receipt", 250, "Price receipt", AGENT_WORKFLOW,
            "/v1/receipts/price", "create_price_receipt",
        ),
        Product(
            "monitor_evaluate", 250, "Market monitor evaluation", AGENT_WORKFLOW,
            "/v1/monitors/evaluate",
        ),
        Product(
            "macro_snapshot", 1_000, "Macro snapshot", AGENT_WORKFLOW,
            "/v1/snapshots/macro", "get_macro_snapshot",
        ),
        Product(
            "token_quality_indicator", 500, "Token quality", TRADER_INDICATOR,
            "/v1/indicators/token-quality", "get_token_quality",
        ),
        Product(
            "state_divergence_indicator", 500, "State divergence", TRADER_INDICATOR,
            "/v1/indicators/state-divergence", "get_state_divergence",
        ),
        Product(
            "solana_token_brief", 1_000, "Solana token brief", TRADER_INDICATOR,
            "/v1/signals/solana-token-brief", "get_solana_token_brief",
        ),
        Product(
            "trader_alpha_pack", 2_500, "Trader alpha pack", TRADER_INDICATOR,
            "/v1/signals/trader-alpha-pack", "get_trader_alpha_pack",
        ),
        Product(
            "rwa_blocksize_benchmark", 250, "RWA Blocksize benchmark", RWA,
            "/v1/rwa/benchmark/blocksize",
        ),
    )
}

PRODUCT_BY_CONNECTOR_TOOL = {
    product.connector_tool: product for product in PRODUCTS.values() if product.connector_tool
}


def product_credits(key: str) -> int:
    return PRODUCTS[key].credits


def product_usdc(key: str) -> Decimal:
    return PRODUCTS[key].usdc


def credit_costs() -> dict[str, float]:
    """Return the per-product credit costs published in catalogs.

    Raw-data entries are tier prices because a raw call costs what its tier
    costs, whichever endpoint serves it.
    """
    costs: dict[str, float] = {
        "raw_core_crypto": float(tier_credits("core_crypto")),
        "raw_extended_crypto": float(tier_credits("extended_crypto")),
        "fx": float(tier_credits("tradfi")),
        "metals": float(tier_credits("tradfi")),
        "raw_equities": float(tier_credits("equities")),
    }
    costs.update({key: float(product.credits) for key, product in PRODUCTS.items()})
    costs["provenance_lookup"] = 0.0
    return costs


def _row(name: str, credits: int, covers: str, examples: str) -> dict[str, Any]:
    return {
        "product": name,
        "credits": credits,
        "usdc": str(credits_to_usdc(credits)),
        "covers": covers,
        "examples": examples,
    }


RAW_ROUTES = "VWAP, bid/ask, state price, 30-minute and 24-hour VWAP"


def price_table() -> list[dict[str, Any]]:
    """Return the public price table: one row per price, in credits and USDC."""
    rows = [
        _row("Core crypto", tier_credits("core_crypto"), RAW_ROUTES, "BTCUSD, ETHUSD, SOLUSD"),
        _row("Extended crypto", tier_credits("extended_crypto"), RAW_ROUTES, "RAYUSD, ORCAUSD, MSOLUSD"),
        _row("FX and metals", tier_credits("tradfi"), "Spot FX and metal prices", "EURUSD, GBPUSD, XAUUSD"),
        _row("Tokenized equities", tier_credits("equities"), "Bid/ask for tokenized stocks", "AAPLXUSD, TSLAXUSD"),
    ]
    for group in (AGENT_WORKFLOW, TRADER_INDICATOR):
        for product in sorted(
            (item for item in PRODUCTS.values() if item.group == group and item.connector_tool),
            key=lambda item: (item.credits, item.label),
        ):
            rows.append(
                _row(
                    product.label,
                    product.credits,
                    "Agent workflow product" if group == AGENT_WORKFLOW else "Trader indicator",
                    f"POST {product.route}",
                )
            )
    return rows


def rate_sentence() -> str:
    return (
        f"1 credit = ${CREDIT_PRICE_USDC} USDC. "
        "Every product has one price: spend free monthly credits in the Claude, "
        "ChatGPT, and Cursor connectors, or pay the same amount in USDC per call over x402."
    )


def pricing_payload() -> dict[str, Any]:
    """Return the machine-readable unified price list."""
    return {
        "credit_price_usdc": str(CREDIT_PRICE_USDC),
        "credits_per_usdc": CREDITS_PER_USDC,
        "summary": rate_sentence(),
        "raw_tiers": {
            tier: {"credits": tier_credits(tier), "usdc": str(credits_to_usdc(tier_credits(tier)))}
            for tier in RAW_TIERS
        },
        "products": {
            key: {
                "label": product.label,
                "group": product.group,
                "route": product.route,
                "credits": product.credits,
                "usdc": str(product.usdc),
                "connector_tool": product.connector_tool,
            }
            for key, product in PRODUCTS.items()
        },
        "table": price_table(),
    }
