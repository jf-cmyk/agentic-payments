"""One price list: every product costs the same in credits and in x402 USDC."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from src import anthropic_mcp_server as claude
from src import authenticated_mcp_server as connector
from src import free_tier, pricing_catalog, public_metadata
from src import resource_server
from src.config import FreeTierSettings, PricingSettings, settings
from src.connector_auth import ConnectorIdentity
from src.credit_manager import CREDIT_COSTS
from src.entitlement_manager import EntitlementManager
from src.free_tier_ledger import FreeTierLedger, get_free_tier_ledger

ROOT = Path(__file__).resolve().parents[1]


def _request(path: str, method: str = "GET", query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [],
        }
    )


def test_one_credit_is_one_tenth_of_a_cent() -> None:
    assert pricing_catalog.CREDIT_PRICE_USDC == Decimal("0.001")
    assert pricing_catalog.CREDITS_PER_USDC == 1_000
    assert pricing_catalog.credits_to_usdc(2) == Decimal("0.002")
    assert str(pricing_catalog.credits_to_usdc(250)) == "0.25"
    assert str(pricing_catalog.credits_to_usdc(2_500)) == "2.50"
    assert pricing_catalog.usdc_to_credits(Decimal("0.005")) == 5


def test_every_product_costs_its_credits_times_the_credit_price() -> None:
    for product in pricing_catalog.PRODUCTS.values():
        assert product.usdc == product.credits * pricing_catalog.CREDIT_PRICE_USDC
    for tier in pricing_catalog.RAW_TIERS:
        credits = pricing_catalog.tier_credits(tier)
        assert pricing_catalog.tier_usdc(tier) == credits * pricing_catalog.CREDIT_PRICE_USDC


def test_x402_route_prices_come_from_the_catalog() -> None:
    for product in pricing_catalog.PRODUCTS.values():
        assert resource_server.ROUTE_PRICING[product.route] == product.usdc


@pytest.mark.parametrize(
    ("path", "method", "query", "credits"),
    [
        ("/v1/vwap/BTCUSD", "GET", "", 2),
        ("/v1/state/MSOLUSD", "GET", "", 4),
        ("/v1/vwap24h/RAYUSD", "GET", "", 4),
        ("/v1/fx/EURUSD", "GET", "", 5),
        ("/v1/metal/XAUUSD", "GET", "", 5),
        ("/v1/briefs/market", "POST", "", 250),
        ("/v1/checks/pre-trade", "POST", "", 100),
        ("/v1/signals/trader-alpha-pack", "POST", "", 2_500),
        ("/v1/batch", "GET", "reqs=vwap:BTCUSD,fx:EURUSD", 7),
    ],
)
def test_http_credit_cost_equals_the_x402_price(path, method, query, credits) -> None:
    request = _request(path, method, query)
    price = resource_server._get_price_for_request(request)

    assert resource_server._credit_cost_for_request(request) == credits
    assert price == credits * pricing_catalog.CREDIT_PRICE_USDC


def test_connector_tool_costs_match_the_catalog() -> None:
    costs = connector.TOOL_COSTS
    assert costs["get_vwap"] == costs["get_state_price"] == pricing_catalog.tier_credits("core_crypto")
    assert costs["get_fx_rate"] == costs["get_metal_price"] == pricing_catalog.tier_credits("tradfi")
    for tool, product in pricing_catalog.PRODUCT_BY_CONNECTOR_TOOL.items():
        assert costs[tool] == product.credits
        assert free_tier.TOOL_SERVICES[tool] == "analytics"


@pytest.mark.parametrize(
    ("tool", "service", "subject", "credits"),
    [
        ("get_vwap", "crypto_vwap", "BTCUSD", 2),
        ("get_vwap", "crypto_vwap", "RAYUSD", 4),
        ("get_bid_ask", "equity_bidask", "AAPLXUSD", 8),
        ("get_bid_ask", "fx", "EURUSD", 4),  # /v1/bidask prices non-equities by crypto tier
        ("get_vwap_30m", "crypto_vwap_30m", "SOLUSD", 2),
        ("get_fx_rate", "fx", "EURUSD", 5),
        ("get_market_brief", "analytics", "BTCUSD,ETHUSD", 250),
        ("get_trader_alpha_pack", "analytics", "default", 2_500),
    ],
)
def test_connector_charge_is_the_x402_price_in_credits(tool, service, subject, credits) -> None:
    assert connector.tool_credit_cost(tool, service, subject) == credits


def test_every_table_product_is_payable_with_credits_in_the_connectors() -> None:
    connector_products = {
        product.label for product in pricing_catalog.PRODUCT_BY_CONNECTOR_TOOL.values()
    }
    table_products = {row["product"] for row in pricing_catalog.price_table()[4:]}

    assert table_products == connector_products
    assert {"get_state_price", "get_vwap_30m", "get_vwap_24h"} <= set(connector.TOOL_COSTS)


def test_published_catalogs_carry_the_unified_prices() -> None:
    assert CREDIT_COSTS["market_brief"] == 250.0
    assert CREDIT_COSTS["raw_core_crypto"] == 2.0
    packages = {package["id"]: package for package in public_metadata.DATA_PACKAGES}
    assert packages["trader-alpha-pack"]["credit_cost"] == "2500"
    assert packages["trader-alpha-pack"]["price_usdc_min"] == "2.50"
    assert packages["crypto-vwap"]["credit_cost_min"] == "2"
    assert packages["crypto-vwap"]["price_usdc_max"] == "0.004"


def test_homepage_table_matches_the_catalog() -> None:
    portal = (ROOT / "docs/developer_portal.html").read_text(encoding="utf-8")
    table = portal.split('id="unit-pricing-table"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(
        r"<tr data-price-row>\s*<td>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>\$([^<]+)</td>",
        table,
    )
    expected = [
        (row["product"], f"{row['credits']:,}", row["usdc"])
        for row in pricing_catalog.price_table()
    ]

    assert rows == expected
    assert "1 credit = $0.001 USDC" in portal
    assert "5-25 credits" not in portal and "10-25 credits" not in portal


def test_tier_prices_round_up_to_whole_credits(monkeypatch) -> None:
    monkeypatch.setenv("PRICE_CORE_CRYPTO", "0.0025")
    pricing = PricingSettings(_env_file=None)

    assert pricing.core_crypto == Decimal("0.003")


def test_free_tier_defaults_keep_the_raw_call_volume() -> None:
    defaults = FreeTierSettings(_env_file=None)

    # 30,000 credits at 2 credits per core call keep the 15,000 core calls a
    # month that the 15,000-credit pool bought at 1 credit per call.
    assert defaults.monthly_credits == 30_000
    assert defaults.per_minute_credits == 60
    assert defaults.daily_soft_cap_credits == 4_000
    assert defaults.global_daily_cap_credits == 400_000
    assert "analytics" in defaults.allowed_service_set


def test_one_oversized_product_fits_an_idle_minute(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings.free_tier, "enabled", True)
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 30_000)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 60)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 4_000)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 1.01)
    ledger = FreeTierLedger(tmp_path / "ledger.db")

    brief = ledger.reserve("g1", 250, usage_date="2026-10-01", now=1_000.0)
    blocked = ledger.reserve("g1", 2, usage_date="2026-10-01", now=1_010.0)
    after_minute = ledger.reserve("g1", 2, usage_date="2026-10-01", now=1_061.0)
    too_big_after_spend = ledger.reserve("g1", 250, usage_date="2026-10-01", now=1_062.0)

    assert brief.allowed
    assert not blocked.allowed and blocked.reason == "rate_limited_minute"
    assert after_minute.allowed
    assert not too_big_after_spend.allowed
    assert too_big_after_spend.reason == "rate_limited_minute"


# ---------------------------------------------------------------------------
# Connector products draw from the same monthly pool
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_products(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "enabled", True)
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 1_000)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 1.01)
    monkeypatch.setattr(settings.free_tier, "email_hash_salt", "pricing-test-salt-" * 2)
    identity = ConnectorIdentity(
        user_id="buyer",
        email="buyer@example.org",
        source="test",
        principal_id="test:buyer",
    )
    monkeypatch.setattr(claude.anthropic_auth, "resolve_anthropic_identity", lambda: identity)
    claude._client = AsyncMock()
    claude._entitlements = EntitlementManager(tmp_path / "claude.db", default_daily_credits=1_000)
    runner = AsyncMock(return_value={"status": "ok", "product": "agent_market_brief"})
    previous = connector._product_runner
    connector.register_product_runner(runner)
    yield runner
    connector.register_product_runner(previous)
    claude._client = None
    claude._entitlements = None


def _pool_spent() -> int:
    grant_key = free_tier.grant_key_for_email("buyer@example.org")
    return get_free_tier_ledger().status(grant_key).credits_spent


@pytest.mark.asyncio
async def test_market_brief_spends_its_credit_price_from_the_pool(claude_products) -> None:
    text = await claude._bundle.products["get_market_brief"](["btc-usd", "ETHUSD"])

    claude_products.assert_awaited_once_with(
        "get_market_brief", {"symbols": ["BTCUSD", "ETHUSD"]}
    )
    assert "Credits remaining this month: 750/1000" in text
    assert _pool_spent() == 250


@pytest.mark.asyncio
async def test_rejected_product_request_is_refunded(claude_products) -> None:
    claude_products.side_effect = connector.ProductRequestError(400, "symbol is required")

    result = await claude._bundle.products["run_pre_trade_check"]("BTCUSD")
    payload = json.loads(result)

    assert payload["error_code"] == "INVALID_REQUEST"
    assert "No credit was used" in payload["message"]
    assert _pool_spent() == 0
    assert claude._entitlements.status("buyer").credits_spent == 0


@pytest.mark.asyncio
async def test_product_tools_do_not_charge_without_a_runner(claude_products) -> None:
    connector.register_product_runner(None)

    payload = json.loads(await claude._bundle.products["get_trader_alpha_pack"](["BTCUSD"]))

    assert payload["error_code"] == "PRODUCT_UNAVAILABLE"
    assert _pool_spent() == 0


@pytest.mark.asyncio
async def test_product_price_exceeding_the_balance_is_refused(claude_products) -> None:
    payload = json.loads(await claude._bundle.products["get_trader_alpha_pack"](["BTCUSD"]))

    assert payload["error_code"] == "DAILY_CREDIT_LIMIT_REACHED"
    assert _pool_spent() == 0
    claude_products.assert_not_awaited()


@pytest.mark.asyncio
async def test_connector_runner_serves_the_x402_handler(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone

    from src.credit_manager import CreditManager
    from src.models import VWAPData

    client = AsyncMock()
    client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.50,
            timestamp=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    monkeypatch.setattr(resource_server.app.state, "blocksize", client, raising=False)
    monkeypatch.setattr(
        resource_server.app.state,
        "credits",
        CreditManager(str(tmp_path / "credits.db")),
        raising=False,
    )

    brief = await resource_server._run_connector_product(
        "get_market_brief", {"symbols": ["BTCUSD"]}
    )

    assert brief["product"] == "agent_market_brief"
    assert brief["credit_cost"] == 250.0
    assert brief["meta"]["credits"] is None  # charged by the connector, not the HTTP path
    assert brief["provenance"]["receipt_id"].startswith("rcpt_")

    with pytest.raises(connector.ProductRequestError) as rejected:
        await resource_server._run_connector_product("run_pre_trade_check", {"symbol": ""})
    assert rejected.value.status_code == 400
