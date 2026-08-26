"""Fail-closed security configuration and privacy-safe hashing helpers."""

from __future__ import annotations

import logging
import os
import re
import secrets
from typing import Any
from urllib.parse import unquote_plus

from src.config import settings


PRIVACY_SALT_SETTINGS = {
    "OBSERVABILITY_HASH_SALT": "observability_hash_salt",
    "TRIAL_IP_HASH_SALT": "trial_ip_hash_salt",
    "RECEIPT_HASH_SALT": "receipt_hash_salt",
    "RECEIPT_ID_SALT": "receipt_id_salt",
}
DASHBOARD_TOKEN_ENV = "OBSERVABILITY_DASHBOARD_TOKEN"
DASHBOARD_TOKEN_SETTING = "observability_dashboard_token"

_EPHEMERAL_SALTS: dict[str, str] = {}
_SENSITIVE_QUERY_PARAMETERS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "code",
        "password",
        "api_key",
        "signature",
    }
)
_QUERY_PAIR_RE = re.compile(r"([?&])([^=&\s#]+)=([^&\s#]*)")
_PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "changeme",
    "placeholder",
    "replace-with",
    "your_",
    "your-",
    "example",
    "blocksize-agentic-payments-observability",
    "blocksize-agentic-payments",
)
PRODUCTION_ENVIRONMENT_NAMES = frozenset({"prod", "production"})


def is_production_environment() -> bool:
    """Return whether runtime security must satisfy hosted production policy."""
    app_environment = os.environ.get("APP_ENV", "").strip().lower()
    railway_environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
    return (
        app_environment in PRODUCTION_ENVIRONMENT_NAMES
        or railway_environment in PRODUCTION_ENVIRONMENT_NAMES
    )


def _configured_value(environment_name: str, setting_name: str) -> str:
    if environment_name in os.environ:
        return os.environ[environment_name].strip()
    return str(getattr(settings.server, setting_name, "") or "").strip()


def is_strong_secret(value: str | None) -> bool:
    """Apply a conservative minimum policy without exposing secret material."""
    candidate = (value or "").strip()
    lowered = candidate.lower()
    if len(candidate) < 32 or len(set(candidate)) < 8:
        return False
    if lowered in {"secret", "password", "token"}:
        return False
    return not any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS)


def dashboard_token() -> str:
    """Return the configured dashboard token only when it meets policy."""
    candidate = _configured_value(DASHBOARD_TOKEN_ENV, DASHBOARD_TOKEN_SETTING)
    return candidate if is_strong_secret(candidate) else ""


def hash_salt(environment_name: str) -> str:
    """Return a strong configured salt or a process-random local fallback.

    Production readiness fails when a salt is missing or weak, but callers still
    receive an unpredictable value so telemetry never falls back to a public,
    static salt while the readiness response is being served.
    """
    setting_name = PRIVACY_SALT_SETTINGS.get(environment_name)
    if setting_name is None:
        raise ValueError(f"Unsupported privacy salt: {environment_name}")
    candidate = _configured_value(environment_name, setting_name)
    if is_strong_secret(candidate):
        return candidate
    return _EPHEMERAL_SALTS.setdefault(environment_name, secrets.token_urlsafe(48))


def security_configuration_status() -> dict[str, Any]:
    """Return a non-secret readiness view of dashboard and hashing controls."""
    production = is_production_environment()
    salt_status: dict[str, dict[str, bool]] = {}
    strong_salt_values: list[str] = []
    for environment_name, setting_name in PRIVACY_SALT_SETTINGS.items():
        candidate = _configured_value(environment_name, setting_name)
        strong = is_strong_secret(candidate)
        if strong:
            strong_salt_values.append(candidate)
        salt_status[environment_name] = {
            "configured": bool(candidate),
            "strong": strong,
            "ephemeral": not strong,
        }

    configured_dashboard_token = _configured_value(
        DASHBOARD_TOKEN_ENV,
        DASHBOARD_TOKEN_SETTING,
    )
    dashboard_strong = is_strong_secret(configured_dashboard_token)
    all_salts_strong = all(item["strong"] for item in salt_status.values())
    salts_unique = all_salts_strong and len(strong_salt_values) == len(
        set(strong_salt_values)
    )
    salts_independent = salts_unique and configured_dashboard_token not in strong_salt_values
    salts_ready = salts_independent
    production_requirements_met = dashboard_strong and salts_ready
    return {
        "ready": production_requirements_met if production else True,
        "production": production,
        "dashboard_auth": {
            "configured": bool(configured_dashboard_token),
            "strong": dashboard_strong,
        },
        "privacy_salts": salt_status,
        "privacy_salts_unique": salts_unique,
        "privacy_salts_independent": salts_independent,
        "production_requirements_met": production_requirements_met,
    }


def trusted_identity_configuration_status() -> dict[str, Any]:
    """Assess caller-controlled identity, static-token, and limiter controls."""
    production = is_production_environment()
    unverified_credits = bool(settings.server.unverified_http_credits_enabled)
    beta_token_prefixes = [
        prefix.lower()
        for prefix in ("ANTHROPIC", "CURSOR", "OPENAI")
        if os.environ.get(f"{prefix}_ENABLE_BETA_TOKENS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    ]
    limiter_ready = (
        settings.server.discovery_rate_limit_enabled
        and settings.server.discovery_rate_limit_per_minute > 0
        and settings.server.discovery_rate_limit_per_day > 0
    )
    production_requirements_met = (
        not unverified_credits
        and not beta_token_prefixes
        and limiter_ready
    )
    return {
        "ready": production_requirements_met if production else True,
        "production": production,
        "unverified_http_credits_enabled": unverified_credits,
        "static_beta_token_connectors": beta_token_prefixes,
        "persistent_discovery_limiter": {
            "enabled": settings.server.discovery_rate_limit_enabled,
            "positive_limits": limiter_ready,
        },
        "production_requirements_met": production_requirements_met,
    }


def redact_sensitive_query_values(value: str) -> str:
    """Redact credential-like query values while preserving useful access logs."""
    def _replace(match: re.Match[str]) -> str:
        try:
            parameter_name = unquote_plus(match.group(2)).lower()
        except (UnicodeDecodeError, ValueError):
            parameter_name = match.group(2).lower()
        if parameter_name not in _SENSITIVE_QUERY_PARAMETERS:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}=[REDACTED]"

    return _QUERY_PAIR_RE.sub(_replace, value)


class SensitiveQueryFilter(logging.Filter):
    """Remove common credentials from Uvicorn access-log messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_query_values(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_sensitive_query_values(value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_sensitive_query_values(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def install_sensitive_query_log_filter() -> None:
    """Install one redaction filter on every Uvicorn access handler/logger."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SensitiveQueryFilter) for item in access_logger.filters):
        access_logger.addFilter(SensitiveQueryFilter())
    for handler in access_logger.handlers:
        if not any(isinstance(item, SensitiveQueryFilter) for item in handler.filters):
            handler.addFilter(SensitiveQueryFilter())


def sensitive_query_parameter_names() -> frozenset[str]:
    """Expose the redaction allowlist for focused policy tests."""
    return _SENSITIVE_QUERY_PARAMETERS
