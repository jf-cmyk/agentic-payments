"""
Configuration tests for Blocksize endpoints.
"""

from __future__ import annotations

from decimal import Decimal

from src.config import BlocksizeSettings, Settings, X402Settings


SOLANA_RECIPIENT = "11111111111111111111111111111111"
SOLANA_FEE_PAYER = "SysvarRent111111111111111111111111111111111"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_RECIPIENT = "0x1111111111111111111111111111111111111111"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _settings_with_x402(x402: X402Settings) -> Settings:
    configured = object.__new__(Settings)
    configured.x402 = x402
    return configured


def test_blocksize_ws_url_converts_https_to_wss():
    cfg = BlocksizeSettings(
        BLOCKSIZE_API_KEY="test-key",
        BLOCKSIZE_BASE_URL="https://data.blocksize.capital/marketdata/v1",
    )

    assert cfg.rest_url == "https://data.blocksize.capital/marketdata/v1/api"
    assert cfg.ws_url == "wss://data.blocksize.capital/marketdata/v1/ws"


def test_payment_requirements_emit_official_client_scheme_metadata():
    configured = _settings_with_x402(
        X402Settings(
            _env_file=None,
            X402_SOLANA_WALLET_ADDRESS=SOLANA_RECIPIENT,
            X402_SOLANA_FEE_PAYER=SOLANA_FEE_PAYER,
            X402_SOLANA_USDC_ADDRESS=SOLANA_USDC,
            X402_EVM_WALLET_ADDRESS=BASE_RECIPIENT,
            X402_BASE_USDC_ADDRESS=BASE_USDC,
            X402_BASE_USDC_NAME="USD Coin",
            X402_BASE_USDC_VERSION="2",
        )
    )

    requirements = configured.payment_requirements(Decimal("0.002"))

    assert [item["network"] for item in requirements] == [
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "eip155:8453",
    ]
    assert requirements[0]["extra"] == {"feePayer": SOLANA_FEE_PAYER}
    assert requirements[1]["extra"] == {"name": "USD Coin", "version": "2"}
    assert all(item["maxAmountRequired"] == "2000" for item in requirements)


def test_payment_requirements_do_not_advertise_solana_without_fee_payer():
    configured = _settings_with_x402(
        X402Settings(
            _env_file=None,
            X402_SOLANA_WALLET_ADDRESS=SOLANA_RECIPIENT,
            X402_SOLANA_FEE_PAYER="",
            X402_SOLANA_USDC_ADDRESS=SOLANA_USDC,
            X402_EVM_WALLET_ADDRESS=BASE_RECIPIENT,
            X402_BASE_USDC_ADDRESS=BASE_USDC,
        )
    )

    requirements = configured.payment_requirements(Decimal("0.002"))
    rail_status = configured.x402.payment_rail_status()

    assert [item["network"] for item in requirements] == ["eip155:8453"]
    assert rail_status["solana"]["ready"] is False
    assert "fee_payer_missing" in rail_status["solana"]["blockers"]
    assert configured.x402.primary_wallet == BASE_RECIPIENT


def test_payment_requirements_honor_independent_rail_controls():
    configured = _settings_with_x402(
        X402Settings(
            _env_file=None,
            X402_SOLANA_WALLET_ADDRESS=SOLANA_RECIPIENT,
            X402_SOLANA_FEE_PAYER=SOLANA_FEE_PAYER,
            X402_SOLANA_USDC_ADDRESS=SOLANA_USDC,
            X402_EVM_WALLET_ADDRESS=BASE_RECIPIENT,
            X402_BASE_USDC_ADDRESS=BASE_USDC,
            X402_SOLANA_PAYMENTS_ENABLED=False,
            X402_BASE_PAYMENTS_ENABLED=True,
        )
    )

    requirements = configured.payment_requirements(Decimal("0.002"))
    rail_status = configured.x402.payment_rail_status()

    assert [item["network"] for item in requirements] == ["eip155:8453"]
    assert rail_status["solana"]["ready"] is True
    assert rail_status["solana"]["enabled"] is False
    assert rail_status["solana"]["operational"] is False
    assert rail_status["base"]["operational"] is True

    configured.x402.base_payments_enabled = False
    assert configured.payment_requirements(Decimal("0.002")) == []
    assert configured.x402.primary_wallet == ""
    assert configured.x402.primary_network == ""


def test_malformed_payment_addresses_disable_their_rails():
    configured = _settings_with_x402(
        X402Settings(
            _env_file=None,
            X402_SOLANA_WALLET_ADDRESS="not-a-solana-address",
            X402_SOLANA_FEE_PAYER=SOLANA_FEE_PAYER,
            X402_EVM_WALLET_ADDRESS="0xnot-an-address",
        )
    )

    assert configured.payment_requirements(Decimal("0.002")) == []
    assert configured.x402.primary_wallet == ""
