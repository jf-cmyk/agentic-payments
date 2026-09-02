from __future__ import annotations

from datetime import UTC, datetime

from scripts.ingest_marketplace_metrics import normalize_metrics_payload, smithery_metrics
from src.marketplace_performance import (
    configured_public_feeds,
    smithery_metrics as scheduled_smithery_metrics,
)


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


def test_scheduled_smithery_parser_matches_current_nested_response_shape() -> None:
    start = datetime(2026, 8, 31, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    metrics = scheduled_smithery_metrics(
        {
            "total": 2,
            "invocations": [
                {
                    "request": {"method": "POST", "url": "https://gateway.smithery.ai/example"},
                    "response": {"status": 200, "outcome": "ok"},
                    "logs": [{"message": "must not be retained"}],
                },
                {
                    "request": {"method": "POST", "url": "https://gateway.smithery.ai/example"},
                    "response": {"status": 500, "outcome": "error"},
                    "exceptions": [{"message": "must not be retained"}],
                },
            ],
        },
        start=start,
        end=end,
    )

    assert metrics["successful_invocations_in_page"] == 1
    assert metrics["failed_invocations_in_page"] == 1
    assert metrics["request_methods_in_page"] == {"POST": 2}
    assert "logs" not in metrics
    assert "exceptions" not in metrics


def test_automatic_public_feed_requires_https() -> None:
    try:
        configured_public_feeds(
            {"MARKETPLACE_METRICS_FEEDS_JSON": '{"pay_sh":"http://example.test/metrics"}'}
        )
    except ValueError as exc:
        assert "must use HTTPS" in str(exc)
    else:
        raise AssertionError("insecure automatic marketplace feed was accepted")
