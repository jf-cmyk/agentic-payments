"""Unit tests for the free-tier guards: identity, scope, and the shared ledger.

Every abuse-control layer from docs/gtm/free_tier_dev_checkpoint_2026-09-23.md
section 4 has a test here.
"""

from __future__ import annotations

import sqlite3

import pytest

from src import free_tier
from src.config import settings
from src.connector_auth import ConnectorIdentity, identity_from_access_token
from src.free_tier_ledger import FreeTierLedger, get_free_tier_ledger

T0 = 1_800_000_000.0  # fixed synthetic clock (seconds)


@pytest.fixture
def guard_settings(monkeypatch):
    """Small, deterministic limits so each guard can be hit quickly."""
    monkeypatch.setattr(settings.free_tier, "enabled", True)
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 100)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 5)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 20)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 50)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 0.8)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_window_hours", 24)
    monkeypatch.setattr(settings.free_tier, "abuse_sweep_distinct_symbols", 3)
    monkeypatch.setattr(settings.free_tier, "abuse_sweep_window_minutes", 10)
    monkeypatch.setattr(settings.free_tier, "email_hash_salt", "test-salt-" * 4)
    return settings.free_tier


@pytest.fixture
def ledger(tmp_path):
    return FreeTierLedger(tmp_path / "free_tier.db")


# --- 4.1 identity ------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("John.Doe+agent@GMail.com", "johndoe@gmail.com"),
        ("j.o.h.n@googlemail.com", "john@gmail.com"),
        ("Jane+tag@Example.ORG", "jane@example.org"),
        ("jane.doe@example.org", "jane.doe@example.org"),
        ("  padded@example.org ", "padded@example.org"),
        ("not-an-email", None),
        ("", None),
        (None, None),
        ("+onlytag@gmail.com", None),
    ],
)
def test_normalize_email_folds_tags_case_and_gmail_dots(raw, expected):
    assert free_tier.normalize_email(raw) == expected


def test_grant_key_is_salted_hash_and_identical_across_aliases(guard_settings, monkeypatch):
    key_a = free_tier.grant_key_for_email("John.Doe+claude@gmail.com")
    key_b = free_tier.grant_key_for_email("johndoe+cursor@GMAIL.COM")
    other = free_tier.grant_key_for_email("someone.else@gmail.com")

    assert key_a == key_b
    assert key_a != other
    assert key_a is not None and key_a.startswith("ft_") and "john" not in key_a
    assert free_tier.grant_key_for_email(None) is None

    monkeypatch.setattr(settings.free_tier, "email_hash_salt", "another-salt-" * 4)
    assert free_tier.grant_key_for_email("John.Doe+claude@gmail.com") != key_a


def test_disposable_domains_are_blocked_unless_allowlisted(monkeypatch, tmp_path):
    blocklist = tmp_path / "disposable.txt"
    blocklist.write_text("# comment\nmailinator.com\nTempMail.com  # trailing\n", encoding="utf-8")
    monkeypatch.setattr(settings.free_tier, "disposable_email_blocklist_path", str(blocklist))
    monkeypatch.setattr(settings.free_tier, "email_domain_allowlist", "")

    assert free_tier.is_disposable_email("agent@mailinator.com") is True
    assert free_tier.is_disposable_email("agent@sub.tempmail.com") is True
    assert free_tier.is_disposable_email("agent@example.org") is False

    monkeypatch.setattr(settings.free_tier, "email_domain_allowlist", "@Mailinator.com")
    assert free_tier.is_disposable_email("agent@mailinator.com") is False


def test_shipped_blocklist_loads_and_contains_common_providers():
    domains = free_tier.disposable_email_domains()
    assert {"mailinator.com", "guerrillamail.com", "yopmail.com"} <= domains
    assert all(domain == domain.lower() and " " not in domain for domain in domains)


def test_missing_blocklist_file_fails_open_but_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings.free_tier, "disposable_email_blocklist_path", str(tmp_path / "absent.txt")
    )
    assert free_tier.disposable_email_domains() == frozenset()
    assert free_tier.is_disposable_email("a@mailinator.com") is False


def test_eligibility_requires_email_and_rejects_unverified_or_disposable(guard_settings, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "require_verified_email", True)

    assert free_tier.eligibility_for(ConnectorIdentity(user_id="u")).reason == "missing_verified_email"
    unverified = free_tier.eligibility_for(
        ConnectorIdentity(user_id="u", email="a@example.org", email_verified=False)
    )
    assert (unverified.eligible, unverified.reason) == (False, "email_not_verified")
    disposable = free_tier.eligibility_for(
        ConnectorIdentity(user_id="u", email="a@mailinator.com", email_verified=True)
    )
    assert (disposable.eligible, disposable.reason) == (False, "disposable_email_domain")
    ok = free_tier.eligibility_for(ConnectorIdentity(user_id="u", email="A@Example.org"))
    assert ok.eligible is True
    assert ok.normalized_email == "a@example.org"
    assert ok.grant_key == free_tier.grant_key_for_email("a@example.org")

    monkeypatch.setattr(settings.free_tier, "require_verified_email", False)
    assert free_tier.eligibility_for(
        ConnectorIdentity(user_id="u", email="a@example.org", email_verified=False)
    ).eligible is True


def test_identity_from_access_token_carries_email_verified():
    class Token:
        client_id = "client"

        def __init__(self, claims):
            self.claims = claims

    verified = identity_from_access_token(
        Token({"sub": "u1", "email": "a@example.org", "email_verified": "true", "iss": "i"})
    )
    unverified = identity_from_access_token(
        Token({"sub": "u1", "email": "a@example.org", "email_verified": False, "iss": "i"})
    )
    absent = identity_from_access_token(Token({"sub": "u1", "email": "a@example.org"}))

    assert verified is not None and verified.email_verified is True
    assert unverified is not None and unverified.email_verified is False
    assert absent is not None and absent.email_verified is None


# --- section 5 scope ---------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "symbol", "service"),
    [
        ("get_vwap", "BTCUSD", "crypto_vwap"),
        ("get_bid_ask", "BTCUSD", "crypto_bidask"),
        ("get_bid_ask", "ETH-USDT", "crypto_bidask"),
        ("get_bid_ask", "AAPLXUSD", "equity_bidask"),
        ("get_bid_ask", "AAPL", "equity_bidask"),
        ("get_bid_ask", "BTC", "crypto_bidask"),
        ("get_bid_ask", "PEPE", "crypto_bidask"),
        ("get_fx_rate", "EURUSD", "fx"),
        ("get_metal_price", "XAUUSD", "metals"),
    ],
)
def test_service_for_tool_classifies_equities_conservatively(tool, symbol, service):
    assert free_tier.service_for_tool(tool, symbol) == service


def test_unknown_tool_has_no_free_scope():
    with pytest.raises(KeyError):
        free_tier.service_for_tool("get_history")


def test_default_free_scope_is_crypto_only_until_cleared(monkeypatch):
    monkeypatch.setattr(settings.free_tier, "allowed_services", "crypto_vwap,crypto_bidask")
    assert free_tier.service_in_free_scope("crypto_vwap") is True
    assert free_tier.service_in_free_scope("equity_bidask") is False
    assert free_tier.service_in_free_scope("fx") is False
    monkeypatch.setattr(settings.free_tier, "allowed_services", "crypto_vwap,fx")
    assert free_tier.service_in_free_scope("fx") is True
    payload = free_tier.scope_excluded_payload("metals")
    assert payload["free_scope"] == ["crypto_vwap", "fx"]
    assert payload["attribution"]["text"] == "Data by Blocksize"


def test_batch_cap_is_below_paid_maximum(monkeypatch):
    monkeypatch.setattr(settings.free_tier, "max_batch_items", 5)
    assert free_tier.batch_items_allowed(5) is True
    assert free_tier.batch_items_allowed(6) is False
    assert free_tier.batch_items_allowed(0) is False
    assert settings.free_tier.max_batch_items < settings.server.max_batch_size


# --- 4.2 / 4.4 / 4.5 shared ledger guards ------------------------------------

def test_reserve_records_monthly_spend_and_creates_grant_once(guard_settings, ledger):
    first = ledger.reserve("g1", 1, symbol="BTCUSD", usage_date="2026-09-23", now=T0)
    second = ledger.reserve("g1", 2, symbol="ETHUSD", usage_date="2026-09-24", now=T0 + 90_000)

    assert first.allowed and first.grant_created is True
    assert second.allowed and second.grant_created is False
    snap = second.snapshot
    assert (snap.monthly_limit, snap.credits_spent, snap.credits_remaining) == (100, 3, 97)
    assert snap.credits_spent_today == 2
    assert snap.resets_at == "2026-10-01"
    assert ledger.status("g1", usage_date="2026-10-01").credits_spent == 0


def test_per_minute_limit_returns_retry_after(guard_settings, ledger):
    results = [
        ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + index)
        for index in range(6)
    ]

    assert [item.allowed for item in results] == [True] * 5 + [False]
    denied = results[-1]
    assert denied.reason == "rate_limited_minute"
    assert 1 <= denied.retry_after_seconds <= 60
    assert denied.snapshot.credits_spent == 5
    later = ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + 61)
    assert later.allowed is True


def test_per_minute_limit_is_credit_weighted(guard_settings, ledger):
    assert ledger.reserve("g1", 4, usage_date="2026-09-23", now=T0).allowed is True
    heavy = ledger.reserve("g1", 2, usage_date="2026-09-23", now=T0 + 1)
    assert (heavy.allowed, heavy.reason) == (False, "rate_limited_minute")


def test_daily_soft_cap_blocks_until_next_utc_day(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    for index in range(20):
        assert ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + index).allowed

    capped = ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + 30)
    assert (capped.allowed, capped.reason) == (False, "daily_soft_cap")
    assert 1 <= capped.retry_after_seconds <= 86_400
    assert capped.snapshot.credits_spent_today == 20
    next_day = ledger.reserve("g1", 1, usage_date="2026-09-24", now=T0 + 90_000)
    assert next_day.allowed is True
    assert next_day.snapshot.credits_spent == 21


def test_monthly_pool_exhausts_and_resets_next_month(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 1.01)

    results = [
        ledger.reserve("g1", 40, usage_date=f"2026-09-{day:02d}", now=T0 + day * 86_400)
        for day in (1, 2, 3)
    ]
    assert [item.allowed for item in results] == [True, True, False]
    assert results[1].thresholds_crossed == (50, 80)
    assert results[2].reason == "monthly_pool_exhausted"
    assert results[2].snapshot.credits_remaining == 20
    exact = ledger.reserve("g1", 20, usage_date="2026-09-04", now=T0 + 4 * 86_400)
    assert exact.allowed and exact.thresholds_crossed == (95, 100)
    assert ledger.reserve("g1", 1, usage_date="2026-09-05", now=T0 + 5 * 86_400).allowed is False
    october = ledger.reserve("g1", 1, usage_date="2026-10-01", now=T0 + 31 * 86_400)
    assert october.allowed and october.snapshot.credits_remaining == 99


def test_global_daily_cap_is_a_circuit_breaker_across_identities(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    for grant in ("g1", "g2", "g3", "g4", "g5"):
        assert ledger.reserve(grant, 10, usage_date="2026-09-23", now=T0).allowed

    blocked = ledger.reserve("g6", 1, usage_date="2026-09-23", now=T0 + 1)
    assert (blocked.allowed, blocked.reason) == (False, "global_daily_cap")
    assert blocked.retry_after_seconds >= 1
    assert blocked.snapshot.credits_spent == 0
    assert ledger.reserve("g6", 1, usage_date="2026-09-24", now=T0 + 90_000).allowed is True
    summary = ledger.summary(usage_date="2026-09-23")
    assert summary["global_credits_today"] == 50
    assert summary["global_cap_remaining_today"] == 0
    assert summary["grants"] == 6


def test_kill_switch_denies_without_recording_spend(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "enabled", False)
    decision = ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0)
    assert (decision.allowed, decision.reason) == (False, "free_tier_disabled")
    assert ledger.status("g1", usage_date="2026-09-23").credits_spent == 0


def test_release_refunds_grant_and_global_usage(guard_settings, ledger):
    ledger.reserve("g1", 3, usage_date="2026-09-23", now=T0)
    after = ledger.release("g1", 2, usage_date="2026-09-23")
    assert after.credits_spent == 1
    assert ledger.summary(usage_date="2026-09-23")["global_credits_today"] == 1
    assert ledger.release("g1", 50, usage_date="2026-09-23").credits_spent == 0


def test_fast_drain_flags_and_suspends_the_grant(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)

    first = ledger.reserve("g1", 40, usage_date="2026-09-23", now=T0)
    second = ledger.reserve("g1", 40, usage_date="2026-09-23", now=T0 + 3_600)
    blocked = ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + 3_601)

    assert first.abuse_flags == ()
    assert second.allowed and second.abuse_flags == ("fast_drain",)
    assert second.snapshot.status == "suspended"
    assert (blocked.allowed, blocked.reason) == (False, "suspended")

    # A slow user crossing 80% a week after the first call is not flagged.
    slow_first = ledger.reserve("g2", 40, usage_date="2026-09-01", now=T0)
    slow_second = ledger.reserve("g2", 40, usage_date="2026-09-08", now=T0 + 7 * 86_400)
    assert slow_first.abuse_flags == () and slow_second.abuse_flags == ()

    reinstated = ledger.set_status("g1", "active", reason="manual_review")
    assert reinstated.status == "active"
    assert ledger.reserve("g1", 1, usage_date="2026-09-23", now=T0 + 3_700).allowed is True
    with pytest.raises(ValueError):
        ledger.set_status("g1", "banned")


def test_symbol_sweep_flags_scraping_pattern(guard_settings, ledger, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    results = [
        ledger.reserve("g1", 1, symbol=f"SYM{index}USD", usage_date="2026-09-23", now=T0 + index)
        for index in range(5)
    ]

    assert [item.abuse_flags for item in results[:3]] == [(), (), ()]
    assert results[3].abuse_flags == ("symbol_sweep",)
    assert results[4].reason == "suspended"
    # Repeated lookups of the same few symbols never trip the sweep detector.
    for index in range(10):
        assert ledger.reserve("g2", 1, symbol="BTCUSD", usage_date="2026-09-23", now=T0 + index).abuse_flags == ()


def test_ledger_summary_reports_exposure_without_identifiers(guard_settings, ledger):
    ledger.reserve("g1", 5, usage_date="2026-09-23", now=T0)
    ledger.bind_subject("g1", "anthropic:scope:user-1")
    summary = ledger.summary(usage_date="2026-09-23")
    assert summary["credits_consumed_this_month"] == 5
    assert summary["worst_case_exposure_credits"] == 100
    assert "user" not in str(summary)


def test_get_free_tier_ledger_follows_configured_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "ledger_db_path", str(tmp_path / "nested" / "ft.db"))
    from src import free_tier_ledger as module

    module.reset_free_tier_ledger()
    first = get_free_tier_ledger()
    assert first.db_path.endswith("ft.db") and (tmp_path / "nested" / "ft.db").exists()
    assert get_free_tier_ledger() is first
    with sqlite3.connect(first.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"grants", "grant_usage", "global_usage", "grant_minute_events", "grant_symbol_events"} <= tables


# --- catalog-backed classification -------------------------------------------

class _CatalogClient:
    """Minimal stand-in for BlocksizeClient with a fixed instrument catalog."""

    def __init__(self, entries, vwap):
        from src.blocksize_client import BlocksizeClient

        self._entries = entries
        self._vwap = vwap
        self._classification_cache = None
        self._classification_cached_at = 0.0
        self.CLASSIFICATION_CACHE_TTL_SECONDS = 3600.0
        self._is_fx_entry = BlocksizeClient._is_fx_entry
        self._is_metal_entry = BlocksizeClient._is_metal_entry
        self._is_equity_like_entry = BlocksizeClient._is_equity_like_entry
        self.calls = 0

    async def _list_bidask_entries(self):
        self.calls += 1
        return self._entries

    async def list_vwap_instruments(self):
        return self._vwap

    _classification_map = __import__("src.blocksize_client", fromlist=["BlocksizeClient"]).BlocksizeClient._classification_map
    classify_symbol = __import__("src.blocksize_client", fromlist=["BlocksizeClient"]).BlocksizeClient.classify_symbol


def _entry(ticker, base, quote, asset_class=""):
    return {"ticker": ticker, "base_currency": base, "quote_currency": quote, "asset_class": asset_class}


@pytest.fixture
def catalog_client():
    return _CatalogClient(
        entries=[
            _entry("BTCUSD", "BTC", "USD"),
            _entry("ARKMUSD", "ARKM", "USD"),  # long-tail crypto, bare ticker not top-250
            _entry("AAPLXUSD", "AAPLX", "USD", "equity"),
            _entry("NVDAXUSDC", "NVDAX", "USDC"),  # tokenized equity by shape
            _entry("EURUSD", "EUR", "USD"),
            _entry("XAUUSD", "XAU", "USD"),
        ],
        vwap=["BTCUSD", "ETHUSD", "ARKMUSD"],
    )


@pytest.mark.asyncio
async def test_catalog_classifies_symbols_by_metadata_not_naming(catalog_client):
    assert await catalog_client.classify_symbol("btc-usd") == "crypto"
    assert await catalog_client.classify_symbol("ARKM") == "crypto"  # heuristic alone would say equity
    assert await catalog_client.classify_symbol("ARKMUSD") == "crypto"
    assert await catalog_client.classify_symbol("AAPLXUSD") == "equity"
    assert await catalog_client.classify_symbol("NVDAX") == "equity"
    assert await catalog_client.classify_symbol("EURUSD") == "fx"
    assert await catalog_client.classify_symbol("XAUUSD") == "metal"
    assert await catalog_client.classify_symbol("NOPE") == "unknown"
    assert catalog_client.calls == 1  # catalog is cached across lookups


@pytest.mark.asyncio
async def test_service_resolution_prefers_catalog_and_falls_back_on_outage(catalog_client):
    assert await free_tier.service_for_tool_async("get_bid_ask", "ARKM", catalog_client) == "crypto_bidask"
    assert await free_tier.service_for_tool_async("get_bid_ask", "AAPLXUSD", catalog_client) == "equity_bidask"
    assert await free_tier.service_for_tool_async("get_bid_ask", "EURUSD", catalog_client) == "fx"
    assert await free_tier.service_for_tool_async("get_vwap", "ARKM", catalog_client) == "crypto_vwap"
    assert await free_tier.service_for_tool_async("get_fx_rate", "EURUSD", catalog_client) == "fx"
    # Not in the catalog: the naming heuristic decides (the upstream call would fail anyway).
    assert await free_tier.service_for_tool_async("get_bid_ask", "ZZZZ", catalog_client) == "equity_bidask"
    assert await free_tier.service_for_tool_async("get_bid_ask", "ZZZZUSD", catalog_client) == "crypto_bidask"

    class Outage:
        async def classify_symbol(self, symbol):
            raise RuntimeError("catalog down")

    assert await free_tier.service_for_tool_async("get_bid_ask", "ARKM", Outage()) == "equity_bidask"
    assert await free_tier.service_for_tool_async("get_bid_ask", "BTCUSD", Outage()) == "crypto_bidask"
    assert await free_tier.service_for_tool_async("get_bid_ask", "BTCUSD", None) == "crypto_bidask"
