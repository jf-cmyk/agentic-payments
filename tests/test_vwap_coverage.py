"""The audit-derived VWAP coverage gate must remove only engine-unknown tickers."""

from __future__ import annotations

import json

import pytest

from src import instrument_discovery, vwap_coverage
from src.config import settings
from src.models import PairInfo


@pytest.fixture
def gate_file(tmp_path, monkeypatch):
    path = tmp_path / "vwap_live_coverage.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-24T15:00:00+00:00",
                "stale_seconds": 300,
                "catalog_size": 5,
                "live": ["BTCUSD", "ETHUSD"],
                "low_activity": {"1INCHETH": 2655.0, "QUIETONLYUSD": 9000.0},
                "unavailable": ["1INCHDYDX", "3CRV-MIM"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings.server, "vwap_coverage_gate_enabled", True)
    monkeypatch.setattr(settings.server, "vwap_coverage_path", str(path))
    vwap_coverage._load.cache_clear()
    yield path
    vwap_coverage._load.cache_clear()


def test_filter_removes_only_unavailable_and_normalises_symbols(gate_file):
    kept = vwap_coverage.filter_vwap_instruments(
        ["BTC-USD", "1INCHDYDX", "3CRVMIM", "1INCHETH", "NEVERAUDITEDUSD"]
    )
    assert kept == ["BTC-USD", "1INCHETH", "NEVERAUDITEDUSD"]
    assert vwap_coverage.vwap_status("btc-usd") == "live"
    assert vwap_coverage.vwap_status("1INCHETH") == "low_activity"
    assert vwap_coverage.vwap_status("3CRVMIM") == "unavailable"
    assert vwap_coverage.vwap_status("NEVERAUDITEDUSD") == "unaudited"
    assert vwap_coverage.last_trade_age_seconds("1INCHETH") == 2655.0


def test_missing_or_disabled_gate_is_a_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.server, "vwap_coverage_path", str(tmp_path / "absent.json"))
    monkeypatch.setattr(settings.server, "vwap_coverage_gate_enabled", True)
    vwap_coverage._load.cache_clear()
    assert vwap_coverage.filter_vwap_instruments(["1INCHDYDX"]) == ["1INCHDYDX"]
    assert vwap_coverage.gate_metadata()["enabled"] is False

    monkeypatch.setattr(settings.server, "vwap_coverage_gate_enabled", False)
    assert vwap_coverage.coverage() is None
    assert vwap_coverage.gate_metadata()["reason"] == "disabled by configuration"


def test_low_activity_vwap_recommends_bidask_when_available(gate_file):
    quiet_with_bidask = PairInfo(
        pair="1INCHETH", base_currency="1INCH", quote_currency="ETH",
        asset_class="crypto", services=["vwap", "bidask"], tier="extended",
    )
    live = PairInfo(
        pair="BTCUSD", base_currency="BTC", quote_currency="USD",
        asset_class="crypto", services=["vwap", "bidask"], tier="core",
    )
    quiet_vwap_only = PairInfo(
        pair="QUIETONLYUSD", base_currency="QUIETONLY", quote_currency="USD",
        asset_class="crypto", services=["vwap"], tier="extended",
    )
    assert instrument_discovery.recommended_service(quiet_with_bidask) == "bidask"
    assert instrument_discovery.recommended_service(live) == "vwap"
    assert instrument_discovery.recommended_service(quiet_vwap_only) == "vwap"

    ranked = instrument_discovery.commercialize_pair(quiet_with_bidask, settings.pricing)
    assert ranked.recommended_service == "bidask"
    assert ranked.endpoint_path == "/v1/bidask/1INCHETH"
    assert ranked.readiness == "catalog_confirmed"
    assert ranked.vwap_activity == "low_activity"
    assert ranked.vwap_last_trade_age_seconds == 2655.0

    only = instrument_discovery.commercialize_pair(quiet_vwap_only, settings.pricing)
    assert only.readiness == "low_activity"
    assert only.recommended_service == "vwap"

    fresh = instrument_discovery.commercialize_pair(live, settings.pricing)
    assert fresh.readiness == "catalog_confirmed"
    assert fresh.vwap_activity == "live"


def test_gate_metadata_describes_the_audit(gate_file):
    meta = vwap_coverage.gate_metadata(excluded_count=2)
    assert meta["enabled"] is True
    assert meta["audited_at"] == "2026-09-24T15:00:00+00:00"
    assert meta["live_symbols"] == 2
    assert meta["low_activity_symbols"] == 2
    assert meta["unavailable_symbols_removed"] == 2
    assert meta["excluded_from_this_listing"] == 2
    assert "not found" in meta["definitions"]["unavailable"]


def test_packaged_gate_file_is_present_and_consistent():
    """The shipped file must exist so production filters engine-unknown tickers."""
    gate = vwap_coverage._load(
        str(vwap_coverage.PACKAGED_COVERAGE_PATH),
        vwap_coverage.PACKAGED_COVERAGE_PATH.stat().st_mtime,
    )
    assert gate is not None
    assert gate.catalog_size >= len(gate.live) + len(gate.low_activity) + len(gate.unavailable)
    assert "BTCUSD" in gate.live
    assert not (gate.live & gate.unavailable)
    assert not (set(gate.low_activity) & gate.unavailable)
