from __future__ import annotations

from datetime import UTC, datetime

from scripts.ingest_marketplace_metrics import normalize_metrics_payload, smithery_metrics


def test_marketplace_metrics_normalization_removes_secret_fields() -> None:
    normalized = normalize_metrics_payload(
        {
            "metrics": {
                "views": 42,
                "conversion_rate": 0.12,
                "api_token": "must-not-survive",
                "nested": {"installs": 3, "password": "must-not-survive"},
            }
        }
    )

    assert normalized["views"] == 42
    assert normalized["conversion_rate"] == 0.12
    assert "api_token" not in normalized
    assert normalized["nested"] == {"installs": 3}


def test_smithery_logs_are_reduced_to_aggregate_counts() -> None:
    start = datetime(2026, 7, 27, tzinfo=UTC)
    end = datetime(2026, 7, 28, tzinfo=UTC)
    metrics = smithery_metrics(
        {
            "total": 3,
            "invocations": [
                {"toolName": "get_vwap", "status": "success", "arguments": {"pair": "BTC"}},
                {"toolName": "get_vwap", "status": "failed", "response": "private"},
                {"toolName": "search_pairs", "status": "success"},
            ],
        },
        start=start,
        end=end,
    )

    assert metrics["runtime_invocations_total"] == 3
    assert metrics["successful_invocations_in_page"] == 2
    assert metrics["failed_invocations_in_page"] == 1
    assert metrics["tool_calls_in_page"] == {"get_vwap": 2, "search_pairs": 1}
    assert "invocations" not in metrics
