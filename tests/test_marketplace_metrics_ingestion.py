from __future__ import annotations

import json

import pytest

from scripts.ingest_marketplace_metrics import (
    configured_feeds,
    load_input,
    normalize_metrics_payload,
)


def test_normalize_marketplace_metrics_removes_secret_like_fields():
    normalized = normalize_metrics_payload(
        {
            "views": 120,
            "installs": 8,
            "conversion_rate": 0.0667,
            "api_token": "must-not-survive",
            "nested": {"active_users": 4, "authorization": "secret"},
        }
    )

    assert normalized["views"] == 120
    assert normalized["installs"] == 8
    assert normalized["metric_scope"] == "performance"
    assert "api_token" not in normalized
    assert normalized["nested"] == {"active_users": 4}


def test_load_input_accepts_offline_platform_export(tmp_path):
    export = tmp_path / "marketplace.json"
    export.write_text(
        json.dumps({"platforms": {"pay_sh": {"views": 10, "installs": 2}}}),
        encoding="utf-8",
    )

    assert load_input(str(export)) == {
        "pay_sh": {"views": 10, "installs": 2, "metric_scope": "performance"}
    }


def test_configured_feeds_rejects_invalid_platform_ids(monkeypatch):
    monkeypatch.setenv(
        "MARKETPLACE_METRICS_FEEDS_JSON",
        json.dumps({"bad platform": "https://example.invalid/metrics"}),
    )

    with pytest.raises(ValueError, match="Invalid platform ids"):
        configured_feeds()
