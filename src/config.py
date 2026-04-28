"""
Centralized configuration for the Blocksize MCP + x402 server.

Loads settings from environment variables / .env file.
Supports tiered pricing by asset class and data type.
Dual-network payment: Solana (priority) + Base (fallback).
"""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Top 250 crypto by market cap — classified as "core" tier
# Agents pay $0.002/call for these; long-tail pays $0.004/call
# Update this list periodically as market caps shift.
# ---------------------------------------------------------------------------

TOP_250_CRYPTO: set[str] = {
    # Blue-chip (top 20)
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "MATIC", "UNI", "SHIB", "LTC", "ATOM", "NEAR", "ARB", "OP", "FIL", "APT",
    # Large-cap (top 50)
    "TRX", "TON", "BCH", "LEO", "OKB", "MKR", "AAVE", "CRV", "LDO", "STX",
    "FTM", "SAND", "MANA", "GRT", "IMX", "RNDR", "INJ", "SUI", "SEI", "TIA",
    "JUP", "WIF", "PEPE", "BONK", "FLOKI", "PENDLE", "RUNE", "SNX", "COMP",
    # Mid-cap (top 250)
    "YFI", "BAL", "CAKE", "FRAX", "REN", "SUSHI", "STG", "WOO", "RDNT", "ZRO",
    "UMA", "ALGO", "EOS", "XLM", "VET", "HBAR", "EGLD", "ICP", "FET", "THETA",
    "FLOW", "ROSE", "AXS", "ENJ", "CHZ", "ZEC", "DASH", "IOTA", "KAVA", "CELO",
    "ONE", "ZIL", "ENS", "LRC", "DYDX", "GMX", "SSV", "RPL", "BLUR", "CFX",
    "MASK", "MAGIC", "API3", "OCEAN", "STORJ", "BAT", "ANKR", "SKL", "AUDIO",
    "CTSI", "NKN", "BAND", "OGN", "RLC", "REQ", "CELR", "PERP", "BICO", "SPELL",
    "LQTY", "KNC", "GNO", "MET", "MPL", "CVX", "FXS", "LOOKS", "WSTETH",
    "QNT", "GALA", "CKB", "KAS", "TAO", "WLD", "PYTH", "JTO", "ONDO", "STRK",
    "ETHFI", "ENA", "W", "AERO", "BRETT", "MEW", "POPCAT", "EIGEN", "SAFE",
    "MOVE", "GRASS",  "VIRTUAL", "AI16Z", "FARTCOIN", "GRIFFAIN", "ZEREBRO",
    "ARC", "DEEP", "LAYER", "BERA", "IP", "KAITO", "B3", "NIL",
    "TRUMP", "MELANIA", "VINE", "HEX", "FLR", "RENDER",
    "POL", "ORDI", "STG", "MINA", "RBN", "XTZ", "NEO", "WAVES", "QTUM",
    "ICX", "OMG", "COTI", "AGLD", "ASTR", "GLMR", "MOVR", "BOBA",
    "ACH", "RAD", "MLN", "POND", "TRB", "DIA", "POWR", "FORTH",
    "FIS", "SUPER", "HIGH", "RARE", "ASM", "JASMY", "LEVER", "LOOM",
    "ILV", "YGG", "PYR", "WAXP", "GODS", "PLA", "ALICE",
    "MBOX", "DEGO", "REVV", "SLP", "TLM", "STARL",
}


def _find_dotenv() -> str | None:
    """Walk up from CWD to find a .env file."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            return str(candidate)
    return None


class BlocksizeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """Blocksize Capital API settings."""

    api_key: str = Field(..., alias="BLOCKSIZE_API_KEY")
    base_url: str = Field(
        "https://data.blocksize.capital/marketdata/v1",
        alias="BLOCKSIZE_BASE_URL",
    )

    @property
    def rest_url(self) -> str:
        return f"{self.base_url}/api"

    @property
    def ws_url(self) -> str:
        return f"{self.base_url}/ws"


class X402Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """x402 payment protocol settings — Solana (primary) + Base (fallback)."""

    # Receiving wallet addresses
    solana_wallet_address: str = Field(
        "", alias="X402_SOLANA_WALLET_ADDRESS",
    )
    evm_wallet_address: str = Field(
        "", alias="X402_EVM_WALLET_ADDRESS",
    )

    # Facilitator
    facilitator_url: str = Field(
        "https://x402.org/facilitator",
        alias="X402_FACILITATOR_URL",
    )

    # Solana config (primary)
    solana_network: str = Field(
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        alias="X402_SOLANA_NETWORK",
    )
    solana_usdc_address: str = Field(
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        alias="X402_SOLANA_USDC_ADDRESS",
    )

    # Base config (fallback)
    base_network: str = Field(
        "eip155:8453",
        alias="X402_BASE_NETWORK",
    )
    base_usdc_address: str = Field(
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        alias="X402_BASE_USDC_ADDRESS",
    )

    @property
    def primary_wallet(self) -> str:
        """Return the primary wallet (Solana if set, else Base)."""
        return self.solana_wallet_address or self.evm_wallet_address

    @property
    def primary_network(self) -> str:
        """Return the primary network."""
        if self.solana_wallet_address:
            return self.solana_network
        return self.base_network


class PricingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """Tiered per-call pricing in USDC.

    Tiers:
      - Discovery:       FREE
      - Core Crypto:     $0.002 (top 250 by market cap)
      - Extended & State: $0.004 (niche/long-tail + state snapshots)
      - TradFi:          $0.005 (FX, metals, commodities)
      - Equities:        $0.008 (US + Chinese stocks)
      - Analytics:       $0.001 (30-min VWAP, 24-hr VWAP)
    """

    core_crypto: Decimal = Field(Decimal("0.002"), alias="PRICE_CORE_CRYPTO")
    extended_crypto: Decimal = Field(Decimal("0.004"), alias="PRICE_EXTENDED_CRYPTO")
    tradfi: Decimal = Field(Decimal("0.005"), alias="PRICE_TRADFI")
    equities: Decimal = Field(Decimal("0.008"), alias="PRICE_EQUITIES")
    analytics: Decimal = Field(Decimal("0.001"), alias="PRICE_ANALYTICS")

    def get_crypto_price(self, base_currency: str) -> Decimal:
        """Get the price for a crypto data call based on asset tier."""
        if base_currency.upper() in TOP_250_CRYPTO:
            return self.core_crypto
        return self.extended_crypto


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """Server runtime settings."""

    resource_server_port: int = Field(
        8402,
        validation_alias=AliasChoices("PORT", "RESOURCE_SERVER_PORT"),
    )
    mcp_transport: str = Field("stdio", alias="MCP_TRANSPORT")
    mcp_server_port: int = Field(8403, alias="MCP_SERVER_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    forwarded_allow_ips: str = Field("127.0.0.1", alias="FORWARDED_ALLOW_IPS")
    cors_allow_origins: str = Field(
        "https://mcp.blocksize.info,http://localhost:8402,http://127.0.0.1:8402",
        alias="CORS_ALLOW_ORIGINS",
    )
    x402_payment_max_age_seconds: int = Field(900, alias="X402_PAYMENT_MAX_AGE_SECONDS")
    max_batch_size: int = Field(20, alias="MAX_BATCH_SIZE")
    discovery_rate_limit_enabled: bool = Field(True, alias="DISCOVERY_RATE_LIMIT_ENABLED")
    discovery_rate_limit_per_minute: int = Field(60, alias="DISCOVERY_RATE_LIMIT_PER_MINUTE")
    discovery_rate_limit_per_day: int = Field(1000, alias="DISCOVERY_RATE_LIMIT_PER_DAY")

    @property
    def cors_origins(self) -> list[str]:
        """Return configured browser origins for CORS."""
        origins = [origin.strip() for origin in self.cors_allow_origins.split(",")]
        return [origin for origin in origins if origin]


class Settings:
    """Aggregate settings container — instantiated once at import time."""

    def __init__(self) -> None:
        dotenv_path = _find_dotenv()
        env_kwargs = {"_env_file": dotenv_path} if dotenv_path else {}

        self.blocksize = BlocksizeSettings(**env_kwargs)  # type: ignore[arg-type]
        self.x402 = X402Settings(**env_kwargs)  # type: ignore[arg-type]
        self.pricing = PricingSettings(**env_kwargs)  # type: ignore[arg-type]
        self.server = ServerSettings(**env_kwargs)  # type: ignore[arg-type]

    def payment_requirements(self, price: Decimal) -> list[dict]:
        """
        Build x402 PaymentRequired objects for all supported networks.

        Returns a list — the agent/client chooses which network to pay on.
        Solana is listed first (preferred).
        """
        # Convert USDC amount to atomic units (6 decimals for both chains)
        amount_atomic = str(int(price * Decimal("1000000")))

        requirements = []

        # Solana (primary) — if wallet is configured
        if self.x402.solana_wallet_address:
            requirements.append({
                "scheme": "exact",
                "network": self.x402.solana_network,
                "maxAmountRequired": amount_atomic,
                "resource": self.x402.solana_wallet_address,
                "description": "Blocksize Capital institutional market data",
                "mimeType": "application/json",
                "payTo": self.x402.solana_wallet_address,
                "maxTimeoutSeconds": 30,
                "asset": f"{self.x402.solana_network}/{self.x402.solana_usdc_address}",
                "extra": {},
            })

        # Base (fallback) — if wallet is configured
        if self.x402.evm_wallet_address:
            requirements.append({
                "scheme": "exact",
                "network": self.x402.base_network,
                "maxAmountRequired": amount_atomic,
                "resource": self.x402.evm_wallet_address,
                "description": "Blocksize Capital institutional market data",
                "mimeType": "application/json",
                "payTo": self.x402.evm_wallet_address,
                "maxTimeoutSeconds": 60,
                "asset": f"{self.x402.base_network}/{self.x402.base_usdc_address}",
                "extra": {},
            })

        return requirements

    @property
    def pricing_summary(self) -> dict:
        """Return a human-readable pricing summary."""
        return {
            "discovery": {"price": "FREE", "includes": "search_pairs, list_instruments, get_pricing_info"},
            "core_crypto": {"price": f"${self.pricing.core_crypto}", "includes": "RT VWAP for top crypto pairs"},
            "extended_crypto": {"price": f"${self.pricing.extended_crypto}", "includes": "Bid/ask and long-tail crypto pairs"},
            "tradfi": {"price": f"${self.pricing.tradfi}", "includes": "FX pairs and supported metal snapshots"},
        }


settings = Settings()
