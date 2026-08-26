from __future__ import annotations

from scripts.build_unsupported_symbol_opportunity_report import classify_opportunity
from scripts.ingest_marketplace_metrics import normalize_metrics_payload


def test_marketplace_metrics_normalization_removes_secret_fields() -> None:
    normalized = normalize_metrics_payload(
        {
            "metrics": {
                "views": 42,
                "conversion_rate": 0.12,
                "status": "healthy",
                "api_token": "must-not-survive",
                "nested": {"installs": 3, "password": "must-not-survive"},
            }
        }
    )

    assert normalized["views"] == 42
    assert normalized["conversion_rate"] == 0.12
    assert "api_token" not in normalized
    assert normalized["nested"] == {"installs": 3}


def test_unsupported_symbol_triage_separates_regression_demand_and_noise() -> None:
    regression = classify_opportunity(
        {"symbol": "BTC-USD", "request_count": 4, "surfaces": ["http_api"]}
    )
    demand = classify_opportunity(
        {"symbol": "NEWCO", "request_count": 6, "surfaces": ["http_api", "public_mcp"]}
    )
    noise = classify_opportunity(
        {"symbol": "ZZZZZ", "request_count": 99, "surfaces": ["http_api"]}
    )

    assert regression["classification"] == "known_supported_symbol"
    assert regression["priority"] == "P0"
    assert demand["classification"] == "candidate_demand"
    assert demand["priority"] == "P1"
    assert noise["classification"] == "synthetic_or_test"
    assert noise["priority"] == "exclude"
