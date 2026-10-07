"""Usage insights: avenues, tickers, users, paid calls and the assessment."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.observability import UsageEventStore, fingerprint
from src.usage_insights import (
    INTERNAL_WALLETS_ENV,
    UsageInsights,
    canonical_ticker,
    channel_for,
)


def _build(store: UsageEventStore, days: int = 7) -> dict:
    return UsageInsights(store).build(days=days, now=datetime.now(UTC) + timedelta(seconds=1))


def _call(store: UsageEventStore, path_label: str, subject: str, *, ua: str, ip: str,
          status: int = 402, surface: str = "http_api", **extra) -> None:
    store.record(
        "http_request",
        surface=surface,
        endpoint=path_label,
        status_code=status,
        latency_ms=12.0,
        ip_hash=fingerprint(ip),
        user_agent=ua,
        subject=subject,
        **extra,
    )


def _paid(store: UsageEventStore, n: int, *, wallet: str = "0xPayer", subject: str = "BTC-USD",
          ua: str = "node", network: str = "eip155:8453") -> None:
    common = {
        "surface": "http_api",
        "endpoint": "/v1/vwap/{pair}",
        "subject": subject,
        "price_usdc": 0.002,
        "wallet_hash": fingerprint(wallet),
        "user_agent": ua,
        "network": network,
    }
    for index in range(n):
        attempt, payment = f"{wallet}-a{index}", f"{wallet}-p{index}"
        meta = {"attempt_id": attempt, "payment_id": payment,
                "identity_hash": fingerprint(wallet), "identity_trust": "verified_x402"}
        store.record("payment_proof_submitted", **common, metadata={"attempt_id": attempt})
        store.record("payment_authorization_verified", **common, metadata=meta)
        store.record("payment_settled", **common, metadata={**meta, "payment_state": "finalized"})
        store.record("data_delivered", **common,
                     metadata={**meta, "payment_mode": "x402", "payment_state": "finalized"})


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"surface": "anthropic_mcp", "user_agent": "python-httpx"}, "claude"),
        ({"surface": "public_mcp", "user_agent": "Claude-User/1.0"}, "claude"),
        ({"surface": "openai_mcp", "user_agent": ""}, "openai"),
        ({"surface": "http_api", "user_agent": "Mozilla/5.0 ChatGPT-User/1.0"}, "openai"),
        ({"surface": "cursor_mcp", "user_agent": "node"}, "cursor"),
        ({"surface": "public_mcp", "user_agent": "node", "utm_source": "smithery"}, "smithery"),
        ({"surface": "public_mcp", "user_agent": "Glama-Inspector/2"}, "glama"),
        ({"surface": "http_api", "user_agent": "node", "referrer": "https://pay.sh/x"}, "pay_sh"),
        ({"surface": "public_mcp", "user_agent": "node"}, "direct_mcp"),
        ({"surface": "http_api", "user_agent": "python-requests/2.34"}, "x402_http"),
        ({"surface": "http_api", "user_agent": "enclave402/verifier (+https://x)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "TridentStatus/1.0"}, "monitor"),
        ({"surface": "public_mcp", "user_agent": "ProofBench/0.1 probe"}, "monitor"),
        ({"surface": "http_api", "user_agent": "Mozilla/5.0 (compatible; AgenticMarketplacePoller/1.0; +https://agents.circle.com)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "402explorer/0.1 (+https://discover.paygent.net/about)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "x402lens-indexer/1.0 (+https://x402lens.com/methodology)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "x402watch/1 (+https://x402watch.vercel.app)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "CoinbaseBazaarDiscovery/1.0 (+https://docs.cdp.coinbase.com/x402)"}, "monitor"),
        ({"surface": "http_api", "user_agent": "hermes-contact-discovery/1.0 (research; contact@hermes.ai)"}, "monitor"),
        ({"surface": "developer_portal", "user_agent": "curl/8"}, "other"),
    ],
)
def test_channel_attribution(kwargs, expected):
    assert channel_for(**kwargs) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC-USD", "BTC-USD"),
        ("btcusd", "BTC-USD"),
        ("BTC/USD", "BTC-USD"),
        ("vwap:BTCUSD", "BTC-USD"),
        ("BTCUSDC", "BTC-USDC"),
        ("EURUSD", "EUR-USD"),
        ("XAUUSD", "XAU-USD"),
        ("AAPL", "AAPL"),
        ("drop table", None),
        ("", None),
    ],
)
def test_canonical_ticker(raw, expected):
    assert canonical_ticker(raw) == expected


def test_calls_are_split_by_avenue_and_ticker_with_monitors_apart(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    for _ in range(3):
        _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="python-requests/2", ip="1.1.1.1")
    _call(store, "/v1/fx/{pair}", "EURUSD", ua="node", ip="2.2.2.2")
    for _ in range(4):
        _call(store, "/v1/vwap/{pair}", "BTC-USD", ua="TridentStatus/1.0", ip="9.9.9.9")
    # MCP transport request plus the tool call it carried.
    _call(store, "/mcp/server", None, ua="Claude-User", ip="3.3.3.3", status=200,
          surface="public_mcp")
    store.record("mcp_tool_call", surface="public_mcp", tool_name="get_market_data_endpoint",
                 subject="SOLUSD", user_agent="Claude-User")
    store.record("mcp_tool_call", surface="public_mcp", tool_name="search_pairs",
                 subject="Bitcoin", user_agent="node")
    # Test traffic never shows up.
    _call(store, "/v1/vwap/{pair}", "BTC-USD", ua="testclient", ip="7.7.7.7")

    result = _build(store)
    channels = {row["id"]: row for row in result["channels"]}
    assert channels["x402_http"]["calls"] == 4
    assert channels["monitor"]["calls"] == 4
    assert channels["claude"]["calls"] == 1
    assert channels["claude"]["users"] == 1
    assert channels["direct_mcp"]["calls"] == 1
    assert result["totals"]["calls"] == 10

    tickers = {row["ticker"]: row for row in result["tickers"]["rows"]}
    assert tickers["BTC-USD"]["calls"] == 3
    assert tickers["BTC-USD"]["monitor_calls"] == 4
    assert tickers["EUR-USD"]["calls"] == 1
    assert tickers["SOL-USD"]["mcp_lookups"] == 1
    assert result["search_terms"] == [{"term": "bitcoin", "count": 1}]

    # Monitors and test traffic are not users.
    assert result["users"]["unique_clients"] == 3
    kpis = {row["id"]: row for row in result["kpis"]}
    assert kpis["calls"]["value"] == 6
    assert kpis["monitor_share"]["value"] == pytest.approx(0.4)
    assert result["health"]["status"]["payment_required"] == 4
    assert result["activity"][-1]["calls"] == 10


def test_paid_calls_flag_bursts_and_internal_wallets(tmp_path, monkeypatch):
    store = UsageEventStore(tmp_path / "usage.db")
    _paid(store, 3, wallet="0xOwnTester")
    _paid(store, 1, wallet="SoLCustomer", subject="EURUSD", network="solana:5eykt4Us")
    monkeypatch.setenv(INTERNAL_WALLETS_ENV, "0xOwnTester")

    result = _build(store)
    paid = result["paid"]
    assert paid["x402_paid_calls"] == 4
    assert paid["settled_payments"] == 4
    assert paid["external_settled_payments"] == 1
    assert paid["internal_wallets_configured"] is True
    assert paid["paying_wallets"] == 2
    assert paid["revenue_usdc"] == pytest.approx(0.008)
    assert paid["by_network"] == {"Base": 3, "Solana": 1}
    # All four settled within minutes of each other: one burst.
    assert paid["burst_settlements"] == 4
    assert {row["ticker"] for row in paid["rows"]} == {"BTC-USD", "EUR-USD"}
    assert sum(row["internal"] for row in paid["rows"]) == 3
    tickers = {row["ticker"]: row for row in result["tickers"]["rows"]}
    assert tickers["BTC-USD"]["paid_calls"] == 3
    assert result["activity"][-1]["paid_calls"] == 4
    assert {row["id"]: row for row in result["kpis"]}["verified_identities"]["value"] == 2


def test_previous_window_feeds_deltas_and_new_clients(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE usage_events SET timestamp = ?", (old,))
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="2.2.2.2")

    result = _build(store, days=7)
    kpis = {row["id"]: row for row in result["kpis"]}
    assert kpis["calls"]["value"] == 2
    assert kpis["calls"]["previous"] == 1
    assert kpis["calls"]["delta"] == pytest.approx(1.0)
    assert result["users"]["unique_clients"] == 2
    assert result["users"]["new_clients"] == 1


def test_assessment_flags_low_conversion_and_failing_payments(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    for index in range(150):
        _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip=f"10.0.0.{index % 20}")
    for index in range(12):
        store.record("payment_proof_submitted", surface="http_api",
                     metadata={"attempt_id": f"bad-{index}"})
        store.record("payment_failed", surface="http_api",
                     reason="Payment payload is not a valid bound x402 v2 signature",
                     metadata={"attempt_id": f"bad-{index}"})
    _paid(store, 1)

    result = _build(store)
    titles = {issue["title"]: issue for issue in result["assessment"]}
    assert "92.3% of payment attempts fail" in titles
    assert "valid bound x402 v2 signature (12)" in titles["92.3% of payment attempts fail"]["evidence"]
    assert "Live-data requests almost never turn into paid calls" in titles
    assert "The signed-in connectors are barely used" in titles
    assert result["assessment"][0]["severity"] == "P0"
    assert {item["horizon"] for item in result["improvement_plan"]} == {"Now", "Next", "Later"}


def test_quiet_window_raises_no_findings(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    assert _build(store)["assessment"] == []


def test_malformed_metadata_does_not_break_the_query(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE usage_events SET metadata_json = '{not json'")
    assert _build(store)["totals"]["calls"] == 1


def _backdate_all(store: UsageEventStore, days: int) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE usage_events SET timestamp = ?", (old,))


def _claude_call(store: UsageEventStore, ip: str) -> None:
    _call(store, "/anthropic/mcp", None, ua="Claude-User", ip=ip, status=200,
          surface="anthropic_mcp")
    store.record("mcp_tool_call", surface="claude_mcp", tool_name="get_vwap",
                 subject="BTCUSD", user_agent="Claude-User")


def test_long_windows_compare_without_claude_connector_history(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    # Prior window: one API call and one Claude connector call. In production
    # the Claude rows would already be purged; either way they must not count.
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    _claude_call(store, ip="5.5.5.5")
    _backdate_all(store, 40)
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1")
    _claude_call(store, ip="6.6.6.6")

    result = _build(store, days=30)
    retention = result["retention"]
    assert retention["claude_days"] == 29
    assert retention["comparison_excludes_claude"] is True
    assert retention["window_exceeds_claude_retention"] is True

    kpis = {row["id"]: row for row in result["kpis"]}
    # Current totals still include the Claude call...
    assert kpis["calls"]["value"] == 2
    # ...but the change compares API traffic with API traffic.
    assert kpis["calls"]["comparable_value"] == 1
    assert kpis["calls"]["previous"] == 1
    assert kpis["calls"]["delta"] == pytest.approx(0.0)
    assert kpis["unique_users"]["value"] == 2
    assert kpis["unique_users"]["comparable_value"] == 1
    # A Claude client whose history was deleted is not reported as new.
    assert result["users"]["new_clients"] == 0

    channels = {row["id"]: row for row in result["channels"]}
    assert channels["claude"]["calls"] == 1
    assert "kept 29 days" in channels["claude"]["retention_note"]
    assert channels["x402_http"]["retention_note"] is None
    assert "claude_retention" in result["definitions"]


def test_short_windows_keep_claude_in_the_comparison(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _claude_call(store, ip="5.5.5.5")
    _backdate_all(store, 10)
    _claude_call(store, ip="5.5.5.5")

    result = _build(store, days=7)
    assert result["retention"]["comparison_excludes_claude"] is False
    assert result["retention"]["window_exceeds_claude_retention"] is False
    kpis = {row["id"]: row for row in result["kpis"]}
    assert kpis["calls"]["value"] == kpis["calls"]["comparable_value"] == 1
    assert kpis["calls"]["previous"] == 1
    assert result["users"]["new_clients"] == 0
    channels = {row["id"]: row for row in result["channels"]}
    assert channels["claude"]["retention_note"] is None


def _identity(store: UsageEventStore, identity: str, surface: str) -> None:
    # Not a call event, so it only feeds the identity counts.
    store.record("free_tier_grant_created", surface=surface,
                 metadata={"identity_hash": identity, "identity_trust": "verified_oauth"})


def test_long_windows_compare_rates_and_identities_without_claude(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    # Prior window: a healthy API call, an API monitor, and Claude connector rows.
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1", status=200)
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="TridentStatus/1.0", ip="9.9.9.9", status=200)
    _claude_call(store, ip="5.5.5.5")
    _identity(store, "api-old", "http_api")
    _identity(store, "claude-old", "claude_mcp")
    _backdate_all(store, 40)
    # Current window: the same API call and monitor, plus two Claude connector
    # tool calls over a failing transport request; none may move the
    # like-for-like rates.
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="node", ip="1.1.1.1", status=200)
    _call(store, "/v1/vwap/{pair}", "BTCUSD", ua="TridentStatus/1.0", ip="9.9.9.9", status=200)
    _call(store, "/anthropic/mcp", None, ua="Claude-User", ip="5.5.5.5", status=500,
          surface="anthropic_mcp")
    for _ in range(2):
        store.record("mcp_tool_call", surface="claude_mcp", tool_name="get_vwap",
                     subject="BTCUSD", user_agent="Claude-User")
    _identity(store, "api-new", "http_api")
    _identity(store, "claude-new", "claude_mcp")

    kpis = {row["id"]: row for row in _build(store, days=30)["kpis"]}

    assert kpis["monitor_share"]["value"] == pytest.approx(0.25)
    assert kpis["monitor_share"]["comparable_value"] == pytest.approx(0.5)
    assert kpis["monitor_share"]["previous"] == pytest.approx(0.5)
    assert kpis["server_error_rate"]["value"] == pytest.approx(0.5)
    assert kpis["server_error_rate"]["comparable_value"] == pytest.approx(0.0)
    assert kpis["server_error_rate"]["previous"] == pytest.approx(0.0)
    assert kpis["verified_identities"]["value"] == 2
    assert kpis["verified_identities"]["comparable_value"] == 1
    assert kpis["verified_identities"]["previous"] == 1
    assert kpis["verified_identities"]["delta"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"utm_source": "smithery", "utm_campaign": "launch"}, {"utm_source": "smithery", "utm_campaign": "launch"}),
        ({"utm_source": "mcp-registry"}, {"utm_source": "mcp-registry"}),
        ({"utm_source": "<img src=x>"}, {}),
        ({"selection_source": "live_showcase"}, {"selection_source": "live_showcase"}),
        ({"selection_source": "made_up"}, {}),
        ({"q": "BTC"}, {}),
        (None, {}),
    ],
)
def test_attribution_from_params_keeps_only_bounded_labels(params, expected):
    from src.observability import attribution_from_params

    assert attribution_from_params(params) == expected
