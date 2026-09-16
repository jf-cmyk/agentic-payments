from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src import public_mcp_server
from src.commercial_plans import (
    account_plan_catalog,
    recommend_account_plan,
    tracked_plan_contact_path,
    upgrade_recommendation,
)
from src.config import settings
from src.resource_server import app


@pytest.fixture
def commercial_test_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv(
        "RWA_OPERATOR_TOKEN", "rwa-test-operator-token-0123456789abcdef"
    )
    monkeypatch.setenv(
        "RWA_OBSERVATION_DB_PATH", str(tmp_path / "rwa_observations.sqlite3")
    )
    monkeypatch.setenv("CREDIT_DB_PATH", str(tmp_path / "credits.sqlite3"))
    monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)
    monkeypatch.setattr(
        settings.x402,
        "solana_wallet_address",
        "11111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "solana_fee_payer",
        "SysvarRent111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "evm_wallet_address",
        "0x1111111111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.server,
        "observability_dashboard_token",
        "obs-test-token-0123456789abcdef",
    )
    with TestClient(
        app,
        base_url="https://testserver",
        headers={"Authorization": "Bearer obs-test-token-0123456789abcdef"},
    ) as client:
        yield client


def test_account_plan_catalog_is_sales_assisted_and_bounded() -> None:
    catalog = account_plan_catalog()

    assert catalog["sales_model"] == "sales_assisted"
    assert catalog["self_serve_purchase_available"] is False
    assert [plan["id"] for plan in catalog["plans"]] == [
        "developer",
        "production",
        "institutional",
    ]
    assert [plan["indicative_monthly_price_usd"] for plan in catalog["plans"]] == [
        49,
        249,
        999,
    ]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"expected_monthly_live_calls": 5_000}, "developer"),
        (
            {
                "expected_monthly_live_calls": 20_000,
                "team_seats": 3,
                "recurring_days_per_month": 20,
            },
            "production",
        ),
        (
            {
                "expected_monthly_live_calls": 20_000,
                "team_seats": 3,
                "needs_sla": True,
            },
            "institutional",
        ),
    ],
)
def test_account_plan_recommendation(kwargs, expected) -> None:
    result = recommend_account_plan(**kwargs)
    assert result["recommended_plan"]["id"] == expected


def test_upgrade_trigger_appears_only_for_low_or_exhausted_starter_balance() -> None:
    assert upgrade_recommendation(11) is None
    assert upgrade_recommendation(10)["status"] == "low_balance"
    assert upgrade_recommendation(0)["status"] == "exhausted"
    assert tracked_plan_contact_path("developer", source="test").startswith(
        "/go/contact?"
    )


def test_account_plan_endpoints_and_macro_preview_are_free(
    commercial_test_client,
) -> None:
    catalog = commercial_test_client.get("/v1/account-plans")
    recommendation = commercial_test_client.post(
        "/v1/account-plans/recommend",
        json={
            "expected_monthly_live_calls": 25_000,
            "team_seats": 4,
            "recurring_days_per_month": 20,
        },
    )
    preview = commercial_test_client.get("/v1/previews/macro")

    assert catalog.status_code == 200
    assert catalog.json()["plans"][1]["id"] == "production"
    assert recommendation.status_code == 200
    assert recommendation.json()["recommended_plan"]["id"] == "production"
    assert recommendation.json()["conversion"]["purchase_mode"] == "sales_assisted"
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["preview"]["data_class"] == "synthetic_example"
    assert payload["preview"]["live_market_data"] is False
    assert payload["live_request"]["price_usdc"] == "1.00"
    assert "selection_source=product_preview" in payload["live_request"]["url"]


def test_existing_x402_handoff_is_actionable(commercial_test_client) -> None:
    challenge = commercial_test_client.post(
        "/v1/snapshots/macro",
        json={"universe": ["BTCUSD", "ETHUSD"]},
    )

    assert challenge.status_code == 402
    handoff = challenge.json()["purchase_handoff"]
    assert handoff["protocol"] == "x402 v2"
    assert handoff["payment_header"] == "PAYMENT-SIGNATURE"
    assert handoff["retry_method"] == "POST"
    assert handoff["recovery"]["safe_retry"]


def test_repeat_monitor_recipe_is_bounded_and_attributed(
    commercial_test_client,
) -> None:
    response = commercial_test_client.post(
        "/v1/monitors/recipe",
        json={
            "symbols": ["BTCUSD", "ETHUSD"],
            "rules": [{"metric": "spread_bps", "operator": ">", "value": 50}],
            "cadence_seconds": 300,
            "max_runs": 20,
            "max_spend_usdc": "2.00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["execution"]["owner"] == "caller"
    assert payload["execution"]["server_side_scheduler_enabled"] is False
    assert payload["execution"]["max_runs"] == 8
    assert payload["spend_control"]["maximum_spend_usdc"] == "2.00"
    assert payload["spend_control"]["budget_reduced_runs"] is True
    assert "selection_source=repeat_workflow_recipe" in payload["live_request"]["url"]


@pytest.mark.asyncio
async def test_public_mcp_recommends_plan_and_exposes_growth_handoffs() -> None:
    recommendation = json.loads(
        await public_mcp_server.public_recommend_account_plan(
            expected_monthly_live_calls=30_000,
            team_seats=4,
            recurring_days_per_month=20,
        )
    )
    macro = json.loads(
        await public_mcp_server.public_get_workflow_endpoint(
            "multi_asset_macro_snapshot"
        )
    )
    monitor = json.loads(
        await public_mcp_server.public_get_workflow_endpoint(
            "spend_controlled_market_monitor"
        )
    )
    endpoint = json.loads(
        await public_mcp_server.public_get_market_data_endpoint("vwap", "BTCUSD")
    )

    assert recommendation["recommended_plan"]["id"] == "production"
    assert recommendation["conversion"]["self_serve_purchase_available"] is False
    assert macro["free_preview"]["data_class"] == "synthetic_example"
    assert macro["free_preview"]["live_market_data"] is False
    assert monitor["repeat_recipe"]["cost"] == "free"
    assert monitor["repeat_recipe"]["execution_owner"] == "caller"
    assert endpoint["x402_handoff"]["payment_header"] == "PAYMENT-SIGNATURE"
