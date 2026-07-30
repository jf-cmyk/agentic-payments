"""Acceptance tests for dashboard authentication and privacy configuration."""

from __future__ import annotations

import logging

import pytest

from src.config import settings
from src.security_config import (
    PRIVACY_SALT_SETTINGS,
    SensitiveQueryFilter,
    hash_salt,
    is_production_environment,
    redact_sensitive_query_values,
    security_configuration_status,
    trusted_identity_configuration_status,
)


STRONG_DASHBOARD_TOKEN = "obs-7f428cdb969e14017176b82d6b67b4d1"


def _clear_security_environment(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("OBSERVABILITY_DASHBOARD_TOKEN", raising=False)
    for environment_name in PRIVACY_SALT_SETTINGS:
        monkeypatch.delenv(environment_name, raising=False)


def test_local_missing_salts_use_stable_distinct_ephemeral_values(monkeypatch):
    _clear_security_environment(monkeypatch)
    monkeypatch.setattr(
        settings.server,
        "observability_dashboard_token",
        STRONG_DASHBOARD_TOKEN,
    )
    for setting_name in PRIVACY_SALT_SETTINGS.values():
        monkeypatch.setattr(settings.server, setting_name, "")

    first = hash_salt("OBSERVABILITY_HASH_SALT")
    second = hash_salt("OBSERVABILITY_HASH_SALT")
    trial = hash_salt("TRIAL_IP_HASH_SALT")
    status = security_configuration_status()

    assert first == second
    assert first != trial
    assert len(first) >= 32
    assert status["ready"] is True
    assert status["production"] is False
    assert all(item["ephemeral"] for item in status["privacy_salts"].values())
    assert STRONG_DASHBOARD_TOKEN not in repr(status)
    assert first not in repr(status)


def test_production_rejects_missing_or_placeholder_security_values(monkeypatch):
    _clear_security_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(settings.server, "observability_dashboard_token", "secret")
    for setting_name in PRIVACY_SALT_SETTINGS.values():
        monkeypatch.setattr(settings.server, setting_name, "change-me-to-a-long-random-secret")

    status = security_configuration_status()

    assert status["ready"] is False
    assert status["production"] is True
    assert status["dashboard_auth"] == {"configured": True, "strong": False}
    assert status["production_requirements_met"] is False


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("APP_ENV", "prod"),
        ("APP_ENV", " Production "),
        ("RAILWAY_ENVIRONMENT_NAME", "PROD"),
        ("RAILWAY_ENVIRONMENT_NAME", " production "),
    ],
)
def test_all_production_aliases_enforce_the_same_policy(
    monkeypatch,
    environment_name,
    value,
):
    _clear_security_environment(monkeypatch)
    monkeypatch.setenv(environment_name, value)

    assert is_production_environment() is True
    assert security_configuration_status()["production"] is True
    assert security_configuration_status()["ready"] is False


def test_production_accepts_distinct_strong_security_values(monkeypatch):
    _clear_security_environment(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("OBSERVABILITY_DASHBOARD_TOKEN", STRONG_DASHBOARD_TOKEN)
    for index, environment_name in enumerate(PRIVACY_SALT_SETTINGS, start=1):
        monkeypatch.setenv(
            environment_name,
            f"salt-{index}-7f428cdb969e14017176b82d6b67b4d1",
        )

    status = security_configuration_status()

    assert status["ready"] is True
    assert status["production_requirements_met"] is True
    assert status["privacy_salts_unique"] is True
    assert status["privacy_salts_independent"] is True


def test_production_rejects_reused_privacy_salts(monkeypatch):
    _clear_security_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OBSERVABILITY_DASHBOARD_TOKEN", STRONG_DASHBOARD_TOKEN)
    reused = "salt-reused-7f428cdb969e14017176b82d6b67b4d1"
    for environment_name in PRIVACY_SALT_SETTINGS:
        monkeypatch.setenv(environment_name, reused)

    status = security_configuration_status()

    assert status["ready"] is False
    assert status["privacy_salts_unique"] is False
    assert status["privacy_salts_independent"] is False


def test_sensitive_query_redaction_handles_case_and_encoded_names():
    raw = (
        "GET /internal?days=7&%74oken=one&access%5Ftoken=two&refresh_token=three"
        "&code=four&password=five&api_key=six&signature=seven&safe=eight HTTP/1.1"
    )

    redacted = redact_sensitive_query_values(raw)

    for secret in ("one", "two", "three", "four", "five", "six", "seven"):
        assert secret not in redacted
    assert "safe=eight" in redacted
    assert redacted.count("[REDACTED]") == 7


def test_sensitive_query_filter_redacts_uvicorn_tuple_arguments():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/?token=top-secret&days=1", "1.1", 200),
        exc_info=None,
    )

    assert SensitiveQueryFilter().filter(record) is True
    rendered = record.getMessage()
    assert "top-secret" not in rendered
    assert "token=[REDACTED]" in rendered
    assert "days=1" in rendered


def test_production_rejects_unverified_http_credits_and_static_beta_tokens(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OPENAI_ENABLE_BETA_TOKENS", "true")
    monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 60)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 1000)

    status = trusted_identity_configuration_status()

    assert status["ready"] is False
    assert status["unverified_http_credits_enabled"] is True
    assert status["static_beta_token_connectors"] == ["openai"]


def test_production_trusted_identity_boundary_accepts_secure_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    for prefix in ("ANTHROPIC", "CURSOR", "OPENAI"):
        monkeypatch.delenv(f"{prefix}_ENABLE_BETA_TOKENS", raising=False)
    monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", False)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 60)
    monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 1000)

    status = trusted_identity_configuration_status()

    assert status["ready"] is True
    assert status["production_requirements_met"] is True
