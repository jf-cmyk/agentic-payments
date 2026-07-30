"""
Centralized configuration for the Blocksize MCP + x402 server.

Loads settings from environment variables / .env file.
Supports tiered pricing by asset class and data type.
Dual-network payment: Solana (priority) + Base (fallback).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.payment_limits import MAX_PAYMENT_REPLAY_ENTRIES, MAX_PAYMENT_REPLAY_TTL_SECONDS


_EVM_ADDRESS_RE = re.compile(r"^0x[0-9A-Fa-f]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _csv_list(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


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
    stream_cache_enabled: bool = Field(False, alias="BLOCKSIZE_STREAM_CACHE_ENABLED")
    stream_cache_ttl_seconds: int = Field(3600, alias="BLOCKSIZE_STREAM_CACHE_TTL_SECONDS")
    stream_cache_reconnect_seconds: float = Field(5.0, alias="BLOCKSIZE_STREAM_CACHE_RECONNECT_SECONDS")
    vwap24h_cache_tickers: str = Field(
        "BTCUSD,ETHUSD,SOLUSD,JUPUSD,PYTHUSD",
        alias="BLOCKSIZE_24H_CACHE_TICKERS",
    )
    state_cache_tickers: str = Field(
        "MSOLUSD,JUPSOLUSD,WSTETHETH,WSTETHUSD",
        alias="BLOCKSIZE_STATE_CACHE_TICKERS",
    )
    state_cache_mode: str = Field("configured", alias="BLOCKSIZE_STATE_CACHE_MODE")
    state_cache_max_tickers: int = Field(250, alias="BLOCKSIZE_STATE_CACHE_MAX_TICKERS")

    @property
    def rest_url(self) -> str:
        return f"{self.base_url}/api"

    @property
    def ws_url(self) -> str:
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/ws"

    @property
    def fixed_vwap_ticker_list(self) -> list[str]:
        return _csv_list(self.vwap24h_cache_tickers)

    @property
    def state_cache_ticker_list(self) -> list[str]:
        return _csv_list(self.state_cache_tickers)


class TiingoSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """Tiingo real-time equities settings."""

    api_key: str = Field("", alias="TIINGO_API_KEY")
    base_url: str = Field("https://api.tiingo.com/iex", alias="TIINGO_BASE_URL")
    equity_base_url: str = Field(
        "https://api.tiingo.com/tiingo/equity/intraday",
        alias="TIINGO_EQUITY_BASE_URL",
    )


class X402Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """x402 payment protocol settings — Solana (primary) + Base (fallback)."""

    # Receiving wallet addresses
    solana_wallet_address: str = Field(
        "", alias="X402_SOLANA_WALLET_ADDRESS",
    )
    solana_fee_payer: str = Field(
        "", alias="X402_SOLANA_FEE_PAYER",
    )
    evm_wallet_address: str = Field(
        "", alias="X402_EVM_WALLET_ADDRESS",
    )

    # Facilitator
    facilitator_url: str = Field(
        "",
        alias="X402_FACILITATOR_URL",
    )
    facilitator_bearer_token: str = Field(
        "",
        alias="X402_FACILITATOR_BEARER_TOKEN",
    )
    cdp_api_key_id: str = Field(
        "",
        alias="CDP_API_KEY_ID",
    )
    cdp_api_key_secret: str = Field(
        "",
        alias="CDP_API_KEY_SECRET",
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
    base_usdc_name: str = Field(
        "USD Coin",
        alias="X402_BASE_USDC_NAME",
    )
    base_usdc_version: str = Field(
        "2",
        alias="X402_BASE_USDC_VERSION",
    )

    def payment_rail_status(self) -> dict[str, dict[str, object]]:
        """Return non-secret configuration status for each advertised rail."""
        solana_blockers: list[str] = []
        if not self.solana_wallet_address:
            solana_blockers.append("recipient_missing")
        elif not _SOLANA_ADDRESS_RE.fullmatch(self.solana_wallet_address):
            solana_blockers.append("recipient_malformed")
        if not self.solana_fee_payer:
            solana_blockers.append("fee_payer_missing")
        elif not _SOLANA_ADDRESS_RE.fullmatch(self.solana_fee_payer):
            solana_blockers.append("fee_payer_malformed")
        if not _SOLANA_ADDRESS_RE.fullmatch(self.solana_usdc_address):
            solana_blockers.append("asset_malformed")

        base_blockers: list[str] = []
        if not self.evm_wallet_address:
            base_blockers.append("recipient_missing")
        elif not _EVM_ADDRESS_RE.fullmatch(self.evm_wallet_address):
            base_blockers.append("recipient_malformed")
        if not _EVM_ADDRESS_RE.fullmatch(self.base_usdc_address):
            base_blockers.append("asset_malformed")
        if not self.base_usdc_name.strip():
            base_blockers.append("asset_name_missing")
        if not self.base_usdc_version.strip():
            base_blockers.append("asset_version_missing")

        return {
            "solana": {
                "configured": bool(self.solana_wallet_address),
                "ready": not solana_blockers,
                "blockers": solana_blockers,
            },
            "base": {
                "configured": bool(self.evm_wallet_address),
                "ready": not base_blockers,
                "blockers": base_blockers,
            },
        }

    @property
    def primary_wallet(self) -> str:
        """Return the first recipient on an operational payment rail."""
        rail_status = self.payment_rail_status()
        if rail_status["solana"]["ready"]:
            return self.solana_wallet_address
        if rail_status["base"]["ready"]:
            return self.evm_wallet_address
        return ""

    @property
    def primary_network(self) -> str:
        """Return the first operational payment network."""
        if self.payment_rail_status()["solana"]["ready"]:
            return self.solana_network
        return self.base_network


class PricingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    """Tiered per-call pricing in USDC.

    Tiers:
      - Discovery:       FREE
      - Core Crypto:     $0.002 (top 250 by market cap)
      - Extended Crypto: $0.004 (niche/long-tail bid/ask and VWAP)
      - TradFi:          $0.005 (currently enabled FX and metals)
      - Equities:        $0.008 (supported tickers via shared bid/ask)
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
    x402_payment_future_skew_seconds: int = Field(
        30,
        alias="X402_PAYMENT_FUTURE_SKEW_SECONDS",
    )
    x402_payment_min_confirmations: int = Field(
        2,
        alias="X402_PAYMENT_MIN_CONFIRMATIONS",
    )
    x402_payment_verification_lease_seconds: int = Field(
        120,
        alias="X402_PAYMENT_VERIFICATION_LEASE_SECONDS",
    )
    x402_payment_replay_ttl_seconds: int = Field(
        MAX_PAYMENT_REPLAY_TTL_SECONDS,
        alias="X402_PAYMENT_REPLAY_TTL_SECONDS",
    )
    x402_payment_replay_max_entries: int = Field(
        MAX_PAYMENT_REPLAY_ENTRIES,
        alias="X402_PAYMENT_REPLAY_MAX_ENTRIES",
    )
    x402_allow_mock_payments: bool = Field(False, alias="X402_ALLOW_MOCK_PAYMENTS")
    x402_allow_legacy_payments: bool = Field(False, alias="X402_ALLOW_LEGACY_PAYMENTS")
    unverified_http_credits_enabled: bool = Field(
        False,
        alias="UNVERIFIED_HTTP_CREDITS_ENABLED",
    )
    max_batch_size: int = Field(20, alias="MAX_BATCH_SIZE")
    discovery_rate_limit_enabled: bool = Field(True, alias="DISCOVERY_RATE_LIMIT_ENABLED")
    discovery_rate_limit_per_minute: int = Field(60, alias="DISCOVERY_RATE_LIMIT_PER_MINUTE")
    discovery_rate_limit_per_day: int = Field(1000, alias="DISCOVERY_RATE_LIMIT_PER_DAY")
    observability_enabled: bool = Field(True, alias="OBSERVABILITY_ENABLED")
    observability_db_path: str = Field("usage_events.db", alias="OBSERVABILITY_DB_PATH")
    observability_dashboard_token: str = Field("", alias="OBSERVABILITY_DASHBOARD_TOKEN")
    observability_hash_salt: str = Field("", alias="OBSERVABILITY_HASH_SALT")
    trial_ip_hash_salt: str = Field("", alias="TRIAL_IP_HASH_SALT")
    receipt_hash_salt: str = Field("", alias="RECEIPT_HASH_SALT")
    receipt_id_salt: str = Field("", alias="RECEIPT_ID_SALT")
    rwa_observation_db_path: str = Field("", alias="RWA_OBSERVATION_DB_PATH")
    rwa_mutations_enabled: bool = Field(False, alias="RWA_MUTATIONS_ENABLED")
    rwa_operator_token: str = Field("", alias="RWA_OPERATOR_TOKEN")
    rwa_store_lock_timeout_seconds: float = Field(1.0, alias="RWA_STORE_LOCK_TIMEOUT_SECONDS")
    rwa_probe_call_timeout_seconds: float = Field(10.0, alias="RWA_PROBE_CALL_TIMEOUT_SECONDS")
    rwa_probe_total_timeout_seconds: float = Field(30.0, alias="RWA_PROBE_TOTAL_TIMEOUT_SECONDS")
    rwa_probe_max_concurrency: int = Field(2, alias="RWA_PROBE_MAX_CONCURRENCY")

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
        self.tiingo = TiingoSettings(**env_kwargs)  # type: ignore[arg-type]
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

        rail_status = self.x402.payment_rail_status()

        # Solana requires the facilitator fee payer advertised by `/supported`.
        if rail_status["solana"]["ready"]:
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
                "extra": {"feePayer": self.x402.solana_fee_payer},
            })

        # Base EIP-3009 clients need the token's EIP-712 domain metadata.
        if rail_status["base"]["ready"]:
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
                "extra": {
                    "name": self.x402.base_usdc_name,
                    "version": self.x402.base_usdc_version,
                },
            })

        return requirements

    @property
    def pricing_summary(self) -> dict:
        """Return a human-readable pricing summary."""
        return {
            "discovery": {
                "price": "FREE",
                "includes": (
                    "search_pairs, list_instruments, get_pricing_info, "
                    "get_market_data_endpoint"
                ),
            },
            "core_crypto": {"price": f"${self.pricing.core_crypto}", "includes": "RT VWAP for top crypto pairs"},
            "extended_crypto": {"price": f"${self.pricing.extended_crypto}", "includes": "Bid/ask and long-tail crypto pairs"},
            "tradfi": {"price": f"${self.pricing.tradfi}", "includes": "FX pairs and supported metal snapshots"},
            "equities": {"price": f"${self.pricing.equities}", "includes": "Supported equity tickers via shared bid/ask"},
        }


settings = Settings()
