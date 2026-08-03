"""Security boundaries for operator-only RWA collection workflows."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request


RWA_STORE_BODY_MAX_BYTES = 128 * 1024
RWA_PROBE_BODY_MAX_BYTES = 16 * 1024
RWA_DEFAULT_BODY_MAX_BYTES = 256 * 1024
RWA_COMPONENT_MAX_BYTES = {
    "raw_payload": 64 * 1024,
    "normalized_observation": 32 * 1024,
    "realtime_quality": 16 * 1024,
    "blocksize_benchmark": 16 * 1024,
    "promotion": 8 * 1024,
    "metadata": 8 * 1024,
}
RWA_MAX_JSON_DEPTH = 12
RWA_MAX_LIST_ITEMS = 200
RWA_MAX_STRING_LENGTH = 8_192
RWA_MAX_KEY_LENGTH = 128

_SENSITIVE_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "bearer_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "decryption_key",
    "encryption_key",
    "id_token",
    "jwt",
    "payment_signature",
    "password",
    "passphrase",
    "private-key",
    "private_key",
    "private_seed",
    "proxy_authorization",
    "refresh-token",
    "refresh_token",
    "secret",
    "seed_phrase",
    "session_token",
    "set_cookie",
    "signature",
    "mnemonic",
    "recovery_phrase",
    "secret_key",
    "signing_key",
    "x-api-key",
    "x_api_key",
}
_PLACEHOLDER_TOKENS = {
    "changeme",
    "change-me",
    "example",
    "placeholder",
    "replace-me",
    "secret",
    "test",
}


def _server_setting(name: str, default: Any) -> Any:
    """Read Pydantic's `.env`-backed settings without creating an import cycle."""
    try:
        from src.config import settings

        return getattr(settings.server, name, default)
    except (AttributeError, ImportError):
        return default


def _enabled(name: str, default: str = "false") -> bool:
    if name in os.environ:
        value: Any = os.environ[name]
    elif name == "RWA_MUTATIONS_ENABLED":
        value = _server_setting("rwa_mutations_enabled", default)
    else:
        value = default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def rwa_mutations_enabled() -> bool:
    """Return whether operator mutation endpoints are intentionally enabled."""
    return _enabled("RWA_MUTATIONS_ENABLED")


def rwa_operator_token() -> str:
    """Return the configured operator credential without logging it."""
    if "RWA_OPERATOR_TOKEN" in os.environ:
        return os.environ["RWA_OPERATOR_TOKEN"].strip()
    return str(_server_setting("rwa_operator_token", "")).strip()


def operator_token_is_strong(token: str | None = None) -> bool:
    """Reject empty, short, or obvious placeholder operator credentials."""
    value = (token if token is not None else rwa_operator_token()).strip()
    lowered = value.lower()
    placeholder_fragment = any(
        fragment in lowered
        for fragment in ("changeme", "change-me", "placeholder", "replace-me")
    )
    return (
        len(value) >= 32
        and len(set(value)) >= 8
        and lowered not in _PLACEHOLDER_TOKENS
        and not placeholder_fragment
    )


def require_rwa_operator(request: Request, *, require_mutations: bool) -> None:
    """Authenticate an RWA operator without accepting URLs or cookies as credentials."""
    configured = rwa_operator_token()
    if not operator_token_is_strong(configured):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "RWA_OPERATOR_AUTH_NOT_CONFIGURED",
                "message": "RWA operator authentication is not configured.",
            },
        )
    if require_mutations and not rwa_mutations_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "RWA_MUTATIONS_DISABLED",
                "message": "RWA mutation workflows are disabled.",
            },
        )

    authorization = request.headers.get("authorization", "").strip()
    header_token = request.headers.get("x-rwa-operator-token", "").strip()
    bearer_token = ""
    if authorization:
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            bearer_token = credential.strip()
    supplied = header_token or bearer_token
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "RWA_OPERATOR_AUTH_REQUIRED",
                "message": "Valid RWA operator authentication is required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def default_rwa_observation_db_path() -> str:
    """Use a dedicated volume path when hosted and a dedicated local file otherwise."""
    hosted = any(
        os.environ.get(name)
        for name in ("RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID")
    )
    return "/data/rwa_observations.v2.db" if hosted else "rwa_observations.db"


def configured_rwa_observation_db_path() -> str:
    """Resolve the RWA evidence database without falling back to usage telemetry."""
    if "RWA_OBSERVATION_DB_PATH" in os.environ:
        configured = os.environ["RWA_OBSERVATION_DB_PATH"].strip()
    else:
        configured = str(_server_setting("rwa_observation_db_path", "")).strip()
    return configured or default_rwa_observation_db_path()


def rwa_store_lock_timeout_seconds() -> float:
    """Return a safe SQLite writer-lock ceiling without trusting malformed environment input."""
    try:
        raw_value = (
            os.environ["RWA_STORE_LOCK_TIMEOUT_SECONDS"]
            if "RWA_STORE_LOCK_TIMEOUT_SECONDS" in os.environ
            else _server_setting("rwa_store_lock_timeout_seconds", 1.0)
        )
        value = float(raw_value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 1.0
    return max(0.05, min(value, 5.0))


def database_paths_collide(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    """Compare database paths after normalizing relative paths and symlinks where possible."""
    first_path = Path(first).expanduser()
    second_path = Path(second).expanduser()
    try:
        if first_path.exists() and second_path.exists():
            return os.path.samefile(first_path, second_path)
    except OSError:
        pass
    return first_path.resolve(strict=False) == second_path.resolve(strict=False)


def protected_state_database_paths(observability_db_path: str) -> dict[str, str]:
    """Return every SQLite state store that RWA evidence must remain separate from."""
    return {
        "observability": observability_db_path,
        "credits": os.environ.get("CREDIT_DB_PATH", "credits.db"),
        "anthropic_entitlements": os.environ.get(
            "ANTHROPIC_ENTITLEMENT_DB_PATH",
            "anthropic_entitlements.db",
        ),
        "cursor_entitlements": os.environ.get(
            "CURSOR_ENTITLEMENT_DB_PATH",
            "cursor_entitlements.db",
        ),
        "openai_entitlements": os.environ.get(
            "OPENAI_ENTITLEMENT_DB_PATH",
            "openai_entitlements.db",
        ),
    }


def rwa_database_collisions(
    db_path: str,
    observability_db_path: str,
    additional_paths: dict[str, str] | None = None,
) -> list[str]:
    """Return state-store labels that resolve to the RWA evidence database."""
    protected_paths = protected_state_database_paths(observability_db_path)
    protected_paths.update(additional_paths or {})
    return [
        label
        for label, state_path in protected_paths.items()
        if database_paths_collide(db_path, state_path)
    ]


def rwa_security_status(
    observability_db_path: str,
    additional_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a non-secret readiness view of the RWA operator boundary."""
    db_path = configured_rwa_observation_db_path()
    token_ready = operator_token_is_strong()
    mutations_enabled = rwa_mutations_enabled()
    collisions = rwa_database_collisions(db_path, observability_db_path, additional_paths)
    isolated = not collisions
    return {
        "ready": isolated and (not mutations_enabled or token_ready),
        "mutations_enabled": mutations_enabled,
        "operator_auth_configured": token_ready,
        "database_isolated": isolated,
        "database_collisions": collisions,
        "database_path": db_path,
    }


def stable_json_bytes(value: Any) -> bytes:
    """Serialize evidence deterministically for size checks and hashing."""
    return json.dumps(
        value,
        allow_nan=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalized_key(value: str) -> str:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def _sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    normalized_keys = {_normalized_key(item) for item in _SENSITIVE_KEYS}
    return normalized in normalized_keys or normalized.endswith(
        (
            "_access_token",
            "_api_key",
            "_auth_token",
            "_client_secret",
            "_decryption_key",
            "_encryption_key",
            "_password",
            "_passphrase",
            "_payment_signature",
            "_private_key",
            "_refresh_token",
            "_secret",
            "_secret_key",
            "_seed_phrase",
            "_session_token",
            "_signature",
            "_signing_key",
        )
    )


def _sensitive_query_key(value: str) -> bool:
    return _sensitive_key(value) or _normalized_key(value) in {
        "key",
        "sig",
        "token",
    }


def _sensitive_string(value: str) -> bool:
    if re.search(
        r"(?i)\b(?:access[_-]?token|api[_-]?key|authorization|aws[_-]?access[_-]?key[_-]?id|"
        r"aws[_-]?secret[_-]?access[_-]?key|client[_-]?secret|encryption[_-]?key|mnemonic|"
        r"passphrase|password|payment[_-]?signature|private[_-]?key|refresh[_-]?token|"
        r"secret[_-]?key|seed[_-]?phrase|signing[_-]?key)\s*[:=]",
        value,
    ):
        return True
    if re.search(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}", value):
        return True
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", value, re.IGNORECASE):
        return True
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value):
        return True
    if re.match(
        r"(?i)^(?:sk-(?:proj-|live-)?[a-z0-9_-]{12,}|sk_live_[a-z0-9]{12,}|"
        r"ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{12,}|"
        r"AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{20,})$",
        value.strip(),
    ):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https", "ws", "wss"}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return True
    try:
        return any(
            _sensitive_query_key(key)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    except ValueError:
        return True


def validate_json_shape(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    """Reject evidence that is deeply nested, unbounded, non-finite, or secret-bearing."""
    if depth > RWA_MAX_JSON_DEPTH:
        raise ValueError(f"{path} exceeds the maximum nesting depth")
    if isinstance(value, dict):
        if len(value) > RWA_MAX_LIST_ITEMS:
            raise ValueError(f"{path} contains too many fields")
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"{path} contains a non-string key")
            if len(raw_key) > RWA_MAX_KEY_LENGTH:
                raise ValueError(f"{path} contains an oversized key")
            if _sensitive_key(raw_key):
                raise ValueError(f"{path} contains a sensitive field")
            validate_json_shape(item, path=f"{path}.{raw_key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > RWA_MAX_LIST_ITEMS:
            raise ValueError(f"{path} contains too many list items")
        for index, item in enumerate(value):
            validate_json_shape(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > RWA_MAX_STRING_LENGTH:
            raise ValueError(f"{path} contains an oversized string")
        if _sensitive_string(value):
            raise ValueError(f"{path} contains sensitive credential material")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f"{path} contains an unsupported value")


def validate_component_size(name: str, value: Any) -> int:
    """Enforce the stored byte ceiling for one evidence component."""
    byte_count = len(stable_json_bytes(value))
    maximum = RWA_COMPONENT_MAX_BYTES[name]
    if byte_count > maximum:
        raise ValueError(f"{name} exceeds the {maximum}-byte limit")
    return byte_count


def rwa_body_limit_for_path(path: str) -> int:
    if path == "/v1/rwa/observations/store":
        return RWA_STORE_BODY_MAX_BYTES
    if path == "/v1/rwa/sourcing/probe":
        return RWA_PROBE_BODY_MAX_BYTES
    return RWA_DEFAULT_BODY_MAX_BYTES


class _RWARequestTooLarge(Exception):
    pass


class RWARequestBodyLimitMiddleware:
    """Enforce RWA JSON limits for Content-Length and chunked request bodies."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() != "POST"
            or not str(scope.get("path", "")).startswith("/v1/rwa/")
        ):
            await self.app(scope, receive, send)
            return

        maximum = rwa_body_limit_for_path(str(scope.get("path", "")))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_content_length = headers.get(b"content-length")
        if raw_content_length:
            try:
                if int(raw_content_length) > maximum:
                    await self._send_too_large(send, maximum)
                    return
            except ValueError:
                await self._send_too_large(send, maximum)
                return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    raise _RWARequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RWARequestTooLarge:
            await self._send_too_large(send, maximum)

    @staticmethod
    async def _send_too_large(send: Any, maximum: int) -> None:
        body = json.dumps(
            {
                "detail": {
                    "error_code": "RWA_REQUEST_TOO_LARGE",
                    "message": f"RWA request body exceeds the {maximum}-byte limit.",
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def public_probe_error_message(exc: Exception) -> str:
    """Avoid reflecting adapter URLs, credentials, or raw upstream response bodies."""
    if isinstance(exc, TimeoutError):
        return "Upstream adapter timed out."
    return "Upstream adapter request failed."
