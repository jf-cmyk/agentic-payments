"""Fail-closed Coinbase x402 v2 gateway with durable replay protection.

This module is intentionally independent from FastAPI.  The HTTP integration
owns challenge construction and response rendering; this boundary owns the
security-sensitive parts of accepting a signed payment:

* construct the canonical, authenticated Coinbase facilitator client using the
  pinned CDP and x402 SDKs;
* strictly bind a v2 payment payload to the method, canonical URL, raw body,
  and the complete server-issued payment requirement;
* reserve each payment proof in SQLite before verification and move it to
  ``settlement_unknown`` *before* the settlement network call;
* return a bounded cached response for an exact replay without settling twice.

The deliberately narrow initial enforcement surface is an allowlist of
read-only GET route IDs.  ``shadow`` mode never parses a signed submission and
never calls ``verify`` or ``settle``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import inspect
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


EXPECTED_X402_VERSION = "2.8.0"
EXPECTED_CDP_SDK_VERSION = "1.47.1"
COINBASE_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
BASE_MAINNET_NETWORK = "eip155:8453"
SOLANA_MAINNET_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

MAX_PAYMENT_HEADER_CHARS = 16_384
MAX_PAYMENT_JSON_BYTES = 12_288
MAX_REQUIREMENT_JSON_BYTES = 16_384
MAX_CACHED_HEADERS_BYTES = 16_384
MAX_CACHED_HEADER_COUNT = 64
MAX_TIMEOUT_SECONDS = 3_600
MAX_REPLAY_TTL_SECONDS = 3_600
MAX_REPLAY_ENTRIES = 500
MAX_CACHED_RESPONSE_BYTES = 512 * 1024

SUPPORTED_MAINNET_NETWORKS = frozenset(
    {
        BASE_MAINNET_NETWORK,
        SOLANA_MAINNET_NETWORK,
    }
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_METHOD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ATOMIC_AMOUNT_RE = re.compile(r"^[1-9][0-9]*$")
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9A-Fa-f]{40}$")
_EVM_NONCE_RE = re.compile(r"^0x[0-9A-Fa-f]{64}$")
_EVM_TRANSACTION_RE = re.compile(r"^0x[0-9A-Fa-f]{64}$")
_SOLANA_PUBLIC_ID_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SOLANA_TRANSACTION_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}

_PAYMENT_ENVELOPE_KEYS = frozenset(
    {"x402Version", "payload", "accepted", "resource", "extensions"}
)
_PAYMENT_REQUIREMENT_KEYS = frozenset(
    {"scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds", "extra"}
)
_RESOURCE_KEYS = frozenset({"url", "description", "mimeType"})
_EIP3009_PAYLOAD_KEYS = frozenset({"signature", "authorization"})
_EIP3009_AUTHORIZATION_KEYS = frozenset(
    {"from", "to", "value", "validAfter", "validBefore", "nonce"}
)
_SOLANA_PAYLOAD_KEYS = frozenset({"transaction"})
_FORBIDDEN_CACHE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "payment-signature",
        "x-payment",
    }
)
_LEDGER_STATES = (
    "pending",
    "settled",
    "finalized",
    "settlement_unknown",
    "released",
)
_COUNTER_NAMES = (
    "supported_refresh_total",
    "supported_refresh_failed_total",
    "supported_refresh_succeeded_total",
    "signed_submissions_total",
    "shadow_blocked_total",
    "route_rejected_total",
    "payment_rejected_total",
    "replay_hits_total",
    "payment_busy_total",
    "payment_binding_conflicts_total",
    "settlement_unknown_replay_total",
    "settled_unfinalized_replay_total",
    "ledger_capacity_rejected_total",
    "verify_calls_total",
    "verify_unavailable_total",
    "verify_invalid_total",
    "verify_succeeded_total",
    "settle_calls_total",
    "settlement_unknown_total",
    "settlement_checkpoint_failed_total",
    "settled_unfinalized_total",
    "settlement_succeeded_total",
    "finalized_total",
    "released_total",
)


class CoinbaseX402Error(ValueError):
    """A redacted configuration, parsing, or ledger-boundary failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class PaymentMode(str, Enum):
    SHADOW = "shadow"
    ENFORCE = "enforce"

    @classmethod
    def parse(cls, value: str | None) -> PaymentMode:
        """Parse the only two supported modes; missing config is safe shadow."""

        candidate = "shadow" if value is None else value.strip().lower()
        try:
            return cls(candidate)
        except ValueError as exc:
            raise CoinbaseX402Error("payment_mode_invalid") from exc


class DecisionKind(str, Enum):
    SHADOW_BLOCKED = "shadow_blocked"
    PROCEED = "proceed"
    FINALIZED = "finalized"
    REPLAY = "replay"
    BUSY = "busy"
    REJECTED = "rejected"
    SETTLEMENT_UNKNOWN = "settlement_unknown"
    SETTLED_UNFINALIZED = "settled_unfinalized"


@dataclass(frozen=True, slots=True)
class CoinbaseX402Config:
    """Non-secret and secret gateway inputs, validated at construction."""

    mode: PaymentMode
    db_path: Path
    cdp_api_key_id: str = field(repr=False)
    cdp_api_key_secret: str = field(repr=False)
    allowed_get_routes: frozenset[str] = frozenset({"v1_vwap"})
    verification_lease_seconds: int = 120
    replay_ttl_seconds: int = 3_600
    replay_max_entries: int = 500
    max_cached_response_bytes: int = 512 * 1024
    readiness_timeout_seconds: float = 5.0
    readiness_max_age_seconds: int = 180

    def __post_init__(self) -> None:
        if not isinstance(self.mode, PaymentMode):
            raise CoinbaseX402Error("payment_mode_invalid")
        if not isinstance(self.db_path, Path) or not str(self.db_path):
            raise CoinbaseX402Error("payment_db_path_invalid")
        _validate_credential(self.cdp_api_key_id, "cdp_api_key_id_invalid", maximum=2_048)
        _validate_credential(
            self.cdp_api_key_secret,
            "cdp_api_key_secret_invalid",
            maximum=32_768,
            allow_newlines=True,
        )
        if self.allowed_get_routes != frozenset({"v1_vwap"}) or any(
            not isinstance(route, str)
            or not route
            or len(route) > 128
            or _CONTROL_CHAR_RE.search(route)
            for route in self.allowed_get_routes
        ):
            raise CoinbaseX402Error("enforce_get_routes_invalid")
        _bounded_int(
            self.verification_lease_seconds,
            "verification_lease_invalid",
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        )
        _bounded_int(
            self.replay_ttl_seconds,
            "replay_ttl_invalid",
            minimum=1,
            maximum=MAX_REPLAY_TTL_SECONDS,
        )
        _bounded_int(
            self.replay_max_entries,
            "replay_max_entries_invalid",
            minimum=1,
            maximum=MAX_REPLAY_ENTRIES,
        )
        _bounded_int(
            self.max_cached_response_bytes,
            "cached_response_limit_invalid",
            minimum=1,
            maximum=MAX_CACHED_RESPONSE_BYTES,
        )
        if (
            isinstance(self.readiness_timeout_seconds, bool)
            or not isinstance(self.readiness_timeout_seconds, (int, float))
            or not 0 < float(self.readiness_timeout_seconds) <= 30
        ):
            raise CoinbaseX402Error("readiness_timeout_invalid")
        _bounded_int(
            self.readiness_max_age_seconds,
            "readiness_max_age_invalid",
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> CoinbaseX402Config:
        values = os.environ if env is None else env
        routes_value = values.get("X402_ENFORCE_GET_ROUTES", "v1_vwap")
        routes = frozenset(item.strip() for item in routes_value.split(",") if item.strip())
        return cls(
            mode=PaymentMode.parse(values.get("X402_PAYMENT_MODE")),
            db_path=Path(values.get("X402_PAYMENT_DB_PATH", "x402_payments.sqlite3")),
            cdp_api_key_id=values.get("CDP_API_KEY_ID", ""),
            cdp_api_key_secret=values.get("CDP_API_KEY_SECRET", ""),
            allowed_get_routes=routes,
            verification_lease_seconds=_env_int(
                values,
                "X402_PAYMENT_VERIFICATION_LEASE_SECONDS",
                120,
            ),
            replay_ttl_seconds=_env_int(
                values,
                "X402_PAYMENT_REPLAY_TTL_SECONDS",
                3_600,
            ),
            replay_max_entries=_env_int(
                values,
                "X402_PAYMENT_REPLAY_MAX_ENTRIES",
                500,
            ),
            max_cached_response_bytes=_env_int(
                values,
                "X402_PAYMENT_MAX_CACHED_RESPONSE_BYTES",
                512 * 1024,
            ),
            readiness_timeout_seconds=_env_float(
                values,
                "X402_FACILITATOR_READINESS_TIMEOUT_SECONDS",
                5.0,
            ),
            readiness_max_age_seconds=_env_int(
                values,
                "X402_FACILITATOR_READINESS_MAX_AGE_SECONDS",
                180,
            ),
        )


@dataclass(frozen=True, slots=True)
class SDKBindings:
    """The exact official SDK types used at the facilitator boundary."""

    payment_payload_type: Any = field(repr=False)
    payment_requirements_type: Any = field(repr=False)
    x402_version: str
    cdp_sdk_version: str


@dataclass(frozen=True, slots=True)
class CachedResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SettlementReceipt:
    network: str
    transaction: str
    payer: str | None = None
    amount: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedPayment:
    payment_id: str
    request_binding: str
    requirement_hash: str
    network: str
    amount: str
    max_timeout_seconds: int
    proof_expires_at: float | None
    sdk_payload: Any = field(repr=False)
    sdk_requirement: Any = field(repr=False)


@dataclass(frozen=True, slots=True)
class PaymentTicket:
    payment_id: str
    request_binding: str
    lease_token: str = field(repr=False)
    parsed: _ParsedPayment = field(repr=False)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(payment_id={self.payment_id!r}, bound=True)"


@dataclass(frozen=True, slots=True)
class PaymentDecision:
    kind: DecisionKind
    code: str
    ticket: PaymentTicket | None = field(default=None, repr=False)
    response: CachedResponse | None = field(default=None, repr=False)
    receipt: SettlementReceipt | None = None


@dataclass(frozen=True, slots=True)
class SupportSnapshot:
    checked_at: float | None
    available: bool
    reason: str
    kinds: tuple[dict[str, Any], ...]
    signers: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _Reservation:
    outcome: str
    lease_token: str | None = field(default=None, repr=False)
    response: CachedResponse | None = field(default=None, repr=False)
    receipt: SettlementReceipt | None = None


def _env_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise CoinbaseX402Error(f"{name.lower()}_invalid") from exc


def _env_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise CoinbaseX402Error(f"{name.lower()}_invalid") from exc


def _bounded_int(
    value: Any,
    code: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CoinbaseX402Error(code)
    return value


def _validate_credential(
    value: Any,
    code: str,
    *,
    maximum: int,
    allow_newlines: bool = False,
) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CoinbaseX402Error(code)
    forbidden = (
        re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")
        if allow_newlines
        else _CONTROL_CHAR_RE
    )
    if forbidden.search(value):
        raise CoinbaseX402Error(code)


def _reject_json_constant(_: str) -> None:
    raise ValueError("invalid_constant")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _canonical_json(value: Any, *, maximum: int | None = None) -> bytes:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CoinbaseX402Error("canonical_json_invalid") from exc
    if maximum is not None and len(result) > maximum:
        raise CoinbaseX402Error("canonical_json_too_large")
    return result


def canonicalize_resource_url(value: str) -> str:
    """Return a strict canonical HTTP(S) URL without leaking it in errors."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or _CONTROL_CHAR_RE.search(value)
        or any(character.isspace() for character in value)
        or "\\" in value
    ):
        raise CoinbaseX402Error("resource_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CoinbaseX402Error("resource_url_invalid") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise CoinbaseX402Error("resource_url_invalid")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise CoinbaseX402Error("resource_url_invalid")
    if parsed.fragment:
        raise CoinbaseX402Error("resource_url_invalid")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise CoinbaseX402Error("resource_url_invalid") from exc
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise CoinbaseX402Error("resource_url_invalid")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _exact_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    code: str,
) -> None:
    keys = set(value)
    if keys - allowed or required - keys:
        raise CoinbaseX402Error(code)


def _nonempty_string(value: Any, code: str, *, maximum: int = 4_096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL_CHAR_RE.search(value)
    ):
        raise CoinbaseX402Error(code)
    return value


def _base58_bytes(value: str) -> bytes | None:
    number = 0
    try:
        for character in value:
            number = (number * 58) + _BASE58_INDEX[character]
    except KeyError:
        return None
    encoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return (b"\0" * (len(value) - len(value.lstrip("1")))) + encoded


def _valid_solana_public_id(value: Any) -> bool:
    if not isinstance(value, str) or not _SOLANA_PUBLIC_ID_RE.fullmatch(value):
        return False
    decoded = _base58_bytes(value)
    return decoded is not None and len(decoded) == 32 and any(decoded)


def _decode_payment_signature(header_value: str) -> dict[str, Any]:
    if (
        not isinstance(header_value, str)
        or not header_value
        or len(header_value) > MAX_PAYMENT_HEADER_CHARS
        or _CONTROL_CHAR_RE.search(header_value)
    ):
        raise CoinbaseX402Error("invalid_payment_signature")
    try:
        raw = base64.b64decode(header_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CoinbaseX402Error("invalid_payment_signature") from exc
    if not raw or len(raw) > MAX_PAYMENT_JSON_BYTES:
        raise CoinbaseX402Error("invalid_payment_signature")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CoinbaseX402Error("invalid_payment_signature") from exc
    if not isinstance(decoded, dict):
        raise CoinbaseX402Error("invalid_payment_signature")
    return decoded


def _normalize_requirement(
    value: Mapping[str, Any],
    *,
    replay_ttl_seconds: int,
    require_prepared: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoinbaseX402Error("payment_requirement_invalid")
    _exact_keys(
        value,
        allowed=_PAYMENT_REQUIREMENT_KEYS,
        required=_PAYMENT_REQUIREMENT_KEYS,
        code="payment_requirement_invalid",
    )
    result = copy.deepcopy(dict(value))
    if result.get("scheme") != "exact":
        raise CoinbaseX402Error("payment_requirement_invalid")
    network = _nonempty_string(result.get("network"), "payment_requirement_invalid", maximum=128)
    if network not in SUPPORTED_MAINNET_NETWORKS:
        raise CoinbaseX402Error("payment_network_unsupported")
    asset = _nonempty_string(result.get("asset"), "payment_requirement_invalid", maximum=256)
    pay_to = _nonempty_string(result.get("payTo"), "payment_requirement_invalid", maximum=256)
    amount = result.get("amount")
    if not isinstance(amount, str) or not _ATOMIC_AMOUNT_RE.fullmatch(amount) or len(amount) > 78:
        raise CoinbaseX402Error("payment_requirement_invalid")
    timeout = result.get("maxTimeoutSeconds")
    _bounded_int(
        timeout,
        "payment_requirement_invalid",
        minimum=1,
        maximum=min(MAX_TIMEOUT_SECONDS, replay_ttl_seconds),
    )
    extra = result.get("extra")
    if not isinstance(extra, Mapping):
        raise CoinbaseX402Error("payment_requirement_invalid")
    result["extra"] = copy.deepcopy(dict(extra))
    _canonical_json(result, maximum=MAX_REQUIREMENT_JSON_BYTES)

    if network.startswith("eip155:"):
        if (
            not _EVM_ADDRESS_RE.fullmatch(asset)
            or int(asset, 16) == 0
            or not _EVM_ADDRESS_RE.fullmatch(pay_to)
            or int(pay_to, 16) == 0
        ):
            raise CoinbaseX402Error("payment_requirement_invalid")
        if result["extra"].get("name") != "USD Coin" or result["extra"].get("version") != "2":
            raise CoinbaseX402Error("base_payment_metadata_invalid")
    else:
        if not (_valid_solana_public_id(asset) and _valid_solana_public_id(pay_to)):
            raise CoinbaseX402Error("payment_requirement_invalid")
        if require_prepared and not _valid_solana_public_id(result["extra"].get("feePayer")):
            raise CoinbaseX402Error("solana_fee_payer_incomplete")
    return result


def _validate_evm_payload(
    value: Any,
    requirement: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoinbaseX402Error("payment_payload_invalid")
    _exact_keys(
        value,
        allowed=_EIP3009_PAYLOAD_KEYS,
        required=_EIP3009_PAYLOAD_KEYS,
        code="payment_payload_invalid",
    )
    payload = copy.deepcopy(dict(value))
    signature = payload.get("signature")
    if (
        not isinstance(signature, str)
        or not signature.startswith("0x")
        or len(signature) < 132
        or len(signature) > 8_194
        or len(signature) % 2 != 0
        or re.fullmatch(r"0x[0-9A-Fa-f]+", signature) is None
    ):
        raise CoinbaseX402Error("payment_payload_invalid")
    try:
        bytes.fromhex(signature[2:])
    except ValueError as exc:
        raise CoinbaseX402Error("payment_payload_invalid") from exc
    authorization = payload.get("authorization")
    if not isinstance(authorization, Mapping):
        raise CoinbaseX402Error("payment_payload_invalid")
    _exact_keys(
        authorization,
        allowed=_EIP3009_AUTHORIZATION_KEYS,
        required=_EIP3009_AUTHORIZATION_KEYS,
        code="payment_payload_invalid",
    )
    from_address = authorization.get("from")
    to_address = authorization.get("to")
    nonce = authorization.get("nonce")
    if (
        not isinstance(from_address, str)
        or not _EVM_ADDRESS_RE.fullmatch(from_address)
        or int(from_address, 16) == 0
        or not isinstance(to_address, str)
        or not _EVM_ADDRESS_RE.fullmatch(to_address)
        or int(to_address, 16) == 0
        or not isinstance(nonce, str)
        or not _EVM_NONCE_RE.fullmatch(nonce)
    ):
        raise CoinbaseX402Error("payment_payload_invalid")
    for name in ("value", "validAfter", "validBefore"):
        field_value = authorization.get(name)
        if (
            not isinstance(field_value, str)
            or not field_value.isdigit()
            or len(field_value) > 78
        ):
            raise CoinbaseX402Error("payment_payload_invalid")
    if (
        str(to_address).lower() != str(requirement["payTo"]).lower()
        or authorization.get("value") != requirement["amount"]
    ):
        raise CoinbaseX402Error("payment_authorization_mismatch")
    return payload


def _validate_solana_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoinbaseX402Error("payment_payload_invalid")
    _exact_keys(
        value,
        allowed=_SOLANA_PAYLOAD_KEYS,
        required=_SOLANA_PAYLOAD_KEYS,
        code="payment_payload_invalid",
    )
    transaction = value.get("transaction")
    if not isinstance(transaction, str) or not transaction or len(transaction) > 16_384:
        raise CoinbaseX402Error("payment_payload_invalid")
    try:
        decoded = base64.b64decode(transaction, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CoinbaseX402Error("payment_payload_invalid") from exc
    if not decoded or base64.b64encode(decoded).decode("ascii") != transaction:
        raise CoinbaseX402Error("payment_payload_invalid")
    return {"transaction": transaction}


def _validate_payment_shape(
    decoded: Mapping[str, Any],
    *,
    expected_requirements: Sequence[Mapping[str, Any]],
    canonical_url: str,
    replay_ttl_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(
        decoded,
        allowed=_PAYMENT_ENVELOPE_KEYS,
        required=frozenset({"x402Version", "payload", "accepted", "resource"}),
        code="payment_payload_invalid",
    )
    version = decoded.get("x402Version")
    if isinstance(version, bool) or version != 2:
        raise CoinbaseX402Error("payment_version_invalid")
    extensions = decoded.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
        raise CoinbaseX402Error("payment_payload_invalid")
    resource = decoded.get("resource")
    if not isinstance(resource, Mapping):
        raise CoinbaseX402Error("payment_payload_invalid")
    _exact_keys(
        resource,
        allowed=_RESOURCE_KEYS,
        required=frozenset({"url"}),
        code="payment_payload_invalid",
    )
    for optional in ("description", "mimeType"):
        optional_value = resource.get(optional)
        if optional_value is not None:
            _nonempty_string(optional_value, "payment_payload_invalid", maximum=1_024)
    if resource.get("url") != canonical_url:
        raise CoinbaseX402Error("payment_resource_mismatch")

    accepted = _normalize_requirement(
        decoded.get("accepted"),
        replay_ttl_seconds=replay_ttl_seconds,
        require_prepared=True,
    )
    if (
        not isinstance(expected_requirements, Sequence)
        or isinstance(expected_requirements, (str, bytes, bytearray))
        or not expected_requirements
    ):
        raise CoinbaseX402Error("payment_requirements_invalid")
    accepted_bytes = _canonical_json(accepted)
    exact_matches = 0
    normalized_expected: list[dict[str, Any]] = []
    for candidate in expected_requirements:
        normalized = _normalize_requirement(
            candidate,
            replay_ttl_seconds=replay_ttl_seconds,
            require_prepared=True,
        )
        normalized_expected.append(normalized)
        if _canonical_json(normalized) == accepted_bytes:
            exact_matches += 1
    expected_network_counts = Counter(item["network"] for item in normalized_expected)
    if len(normalized_expected) != 2 or expected_network_counts != Counter(
        {BASE_MAINNET_NETWORK: 1, SOLANA_MAINNET_NETWORK: 1}
    ):
        raise CoinbaseX402Error("payment_requirements_incomplete")
    if exact_matches != 1:
        raise CoinbaseX402Error("payment_requirement_mismatch")
    if accepted["network"].startswith("eip155:"):
        payload = _validate_evm_payload(decoded.get("payload"), accepted)
    else:
        payload = _validate_solana_payload(decoded.get("payload"))
    return payload, accepted


def _request_binding(
    *,
    method: str,
    canonical_url: str,
    body: bytes,
    requirement: Mapping[str, Any],
) -> tuple[str, str]:
    if not isinstance(method, str) or not _HTTP_METHOD_RE.fullmatch(method):
        raise CoinbaseX402Error("request_method_invalid")
    requirement_bytes = _canonical_json(requirement, maximum=MAX_REQUIREMENT_JSON_BYTES)
    requirement_hash = hashlib.sha256(requirement_bytes).hexdigest()
    material = {
        "method": method.upper(),
        "url": canonical_url,
        "bodySha256": hashlib.sha256(body).hexdigest(),
        "requirementSha256": requirement_hash,
        "amount": requirement["amount"],
        "payTo": requirement["payTo"],
        "asset": requirement["asset"],
        "network": requirement["network"],
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest(), requirement_hash


def _payment_id(payload: Mapping[str, Any], requirement: Mapping[str, Any]) -> str:
    """Hash immutable proof material, excluding mutable resource presentation."""

    network = str(requirement["network"])
    if network.startswith("eip155:"):
        authorization = payload["authorization"]
        # EIP-3009's authorization nonce, not its textual signature encoding,
        # is the exactly-once identity.  Omitting the signature also prevents
        # equivalent/malleated signatures from creating separate local rows.
        proof: dict[str, Any] = {
            "from": str(authorization["from"]).lower(),
            "to": str(authorization["to"]).lower(),
            "value": str(int(authorization["value"])),
            "validAfter": str(int(authorization["validAfter"])),
            "validBefore": str(int(authorization["validBefore"])),
            "nonce": str(authorization["nonce"]).lower(),
        }
        asset = str(requirement["asset"]).lower()
    else:
        transaction = base64.b64decode(str(payload["transaction"]), validate=True)
        proof = {"transactionSha256": hashlib.sha256(transaction).hexdigest()}
        asset = str(requirement["asset"])
    identity = {
        "x402Version": 2,
        "scheme": requirement["scheme"],
        "network": network,
        "asset": asset,
        "proof": proof,
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _parse_payment(
    header_value: str,
    *,
    method: str,
    resource_url: str,
    body: bytes,
    expected_requirements: Sequence[Mapping[str, Any]],
    replay_ttl_seconds: int,
    now: float,
    sdk: SDKBindings,
) -> _ParsedPayment:
    decoded = _decode_payment_signature(header_value)
    canonical_url = canonicalize_resource_url(resource_url)
    payload, accepted = _validate_payment_shape(
        decoded,
        expected_requirements=expected_requirements,
        canonical_url=canonical_url,
        replay_ttl_seconds=replay_ttl_seconds,
    )
    proof_expires_at: float | None = None
    if accepted["network"].startswith("eip155:"):
        authorization = payload["authorization"]
        valid_after = int(authorization["validAfter"])
        valid_before = int(authorization["validBefore"])
        maximum_valid_before = now + max(
            replay_ttl_seconds,
            accepted["maxTimeoutSeconds"],
        )
        if (
            valid_after >= valid_before
            or valid_before <= now
            or valid_before > maximum_valid_before
        ):
            raise CoinbaseX402Error("payment_authorization_window_invalid")
        proof_expires_at = float(valid_before + 1)
    try:
        sdk_payload = sdk.payment_payload_type.model_validate(decoded)
        sdk_requirement = sdk.payment_requirements_type.model_validate(accepted)
    except Exception as exc:
        raise CoinbaseX402Error("payment_sdk_validation_failed") from exc
    if getattr(sdk_payload, "x402_version", None) != 2:
        raise CoinbaseX402Error("payment_sdk_validation_failed")
    binding, requirement_hash = _request_binding(
        method=method,
        canonical_url=canonical_url,
        body=body,
        requirement=accepted,
    )
    return _ParsedPayment(
        payment_id=_payment_id(payload, accepted),
        request_binding=binding,
        requirement_hash=requirement_hash,
        network=accepted["network"],
        amount=accepted["amount"],
        max_timeout_seconds=accepted["maxTimeoutSeconds"],
        proof_expires_at=proof_expires_at,
        sdk_payload=sdk_payload,
        sdk_requirement=sdk_requirement,
    )


def _load_official_sdk(
    api_key_id: str,
    api_key_secret: str,
    timeout_seconds: float = 5.0,
) -> tuple[Any, SDKBindings]:
    """Construct the canonical authenticated Coinbase facilitator client."""

    try:
        x402_version = metadata.version("x402")
        cdp_version = metadata.version("cdp-sdk")
    except metadata.PackageNotFoundError as exc:
        raise CoinbaseX402Error("payment_sdk_missing") from exc
    if x402_version != EXPECTED_X402_VERSION or cdp_version != EXPECTED_CDP_SDK_VERSION:
        raise CoinbaseX402Error("payment_sdk_version_mismatch")
    try:
        from cdp.x402 import create_facilitator_config
        from x402.http import (
            CreateHeadersAuthProvider,
            FacilitatorConfig,
            HTTPFacilitatorClient,
        )
        from x402.schemas import PaymentPayload, PaymentRequirements
    except Exception as exc:
        raise CoinbaseX402Error("payment_sdk_import_failed") from exc
    try:
        facilitator_config = create_facilitator_config(api_key_id, api_key_secret)
    except Exception as exc:
        raise CoinbaseX402Error("cdp_auth_configuration_failed") from exc
    if not isinstance(facilitator_config, Mapping):
        raise CoinbaseX402Error("cdp_auth_configuration_failed")
    if facilitator_config.get("url") != COINBASE_FACILITATOR_URL:
        raise CoinbaseX402Error("coinbase_facilitator_not_canonical")
    if not callable(facilitator_config.get("create_headers")):
        raise CoinbaseX402Error("cdp_auth_configuration_failed")
    try:
        client = HTTPFacilitatorClient(
            FacilitatorConfig(
                url=COINBASE_FACILITATOR_URL,
                timeout=float(timeout_seconds),
                auth_provider=CreateHeadersAuthProvider(
                    facilitator_config["create_headers"],
                ),
                identifier="coinbase_cdp",
            )
        )
    except Exception as exc:
        raise CoinbaseX402Error("facilitator_client_initialization_failed") from exc
    return client, SDKBindings(
        payment_payload_type=PaymentPayload,
        payment_requirements_type=PaymentRequirements,
        x402_version=x402_version,
        cdp_sdk_version=cdp_version,
    )


def _field(value: Any, snake_name: str, camel_name: str | None = None) -> Any:
    if isinstance(value, Mapping):
        if snake_name in value:
            return value[snake_name]
        return value.get(camel_name) if camel_name is not None else None
    return getattr(value, snake_name, None)


def _safe_supported(
    value: Any,
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[str, ...]]]:
    raw_kinds = _field(value, "kinds")
    if not isinstance(raw_kinds, (list, tuple)):
        raise CoinbaseX402Error("supported_response_invalid")
    kinds: list[dict[str, Any]] = []
    for raw_kind in raw_kinds:
        version = _field(raw_kind, "x402_version", "x402Version")
        scheme = _field(raw_kind, "scheme")
        network = _field(raw_kind, "network")
        if version != 2 or scheme != "exact" or network not in SUPPORTED_MAINNET_NETWORKS:
            continue
        safe_extra: dict[str, str] = {}
        extra = _field(raw_kind, "extra")
        if isinstance(extra, Mapping):
            fee_payer = extra.get("feePayer")
            if fee_payer is not None and not _valid_solana_public_id(fee_payer):
                raise CoinbaseX402Error("supported_response_invalid")
            if fee_payer is not None:
                safe_extra["feePayer"] = fee_payer
        kinds.append(
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": network,
                "extra": safe_extra,
            }
        )
    deduplicated: dict[bytes, dict[str, Any]] = {}
    for kind in kinds:
        deduplicated[_canonical_json(kind)] = kind
    kinds = list(deduplicated.values())
    if {kind["network"] for kind in kinds} != SUPPORTED_MAINNET_NETWORKS:
        raise CoinbaseX402Error("supported_v2_exact_missing")

    signers: dict[str, tuple[str, ...]] = {}
    raw_signers = _field(value, "signers")
    if raw_signers is not None:
        if not isinstance(raw_signers, Mapping):
            raise CoinbaseX402Error("supported_response_invalid")
        solana_signers = raw_signers.get("solana:*")
        if solana_signers is not None:
            if not isinstance(solana_signers, (list, tuple)) or any(
                not _valid_solana_public_id(signer) for signer in solana_signers
            ):
                raise CoinbaseX402Error("supported_response_invalid")
            signers["solana:*"] = tuple(dict.fromkeys(solana_signers))
    _resolve_solana_fee_payer(tuple(kinds), signers)
    return tuple(kinds), signers


def _resolve_solana_fee_payer(
    kinds: Sequence[Mapping[str, Any]],
    signers: Mapping[str, Sequence[str]],
) -> str:
    matches = [
        kind
        for kind in kinds
        if kind.get("x402Version") == 2
        and kind.get("scheme") == "exact"
        and kind.get("network") == SOLANA_MAINNET_NETWORK
    ]
    if len(matches) != 1:
        raise CoinbaseX402Error("solana_fee_payer_incomplete")
    extra = matches[0].get("extra")
    if not isinstance(extra, Mapping):
        raise CoinbaseX402Error("solana_fee_payer_incomplete")
    if "feePayer" in extra:
        fee_payer = extra["feePayer"]
        if not _valid_solana_public_id(fee_payer):
            raise CoinbaseX402Error("solana_fee_payer_incomplete")
        return fee_payer
    fallback = tuple(dict.fromkeys(signers.get("solana:*", ())))
    if len(fallback) != 1 or not _valid_solana_public_id(fallback[0]):
        raise CoinbaseX402Error("solana_fee_payer_incomplete")
    return fallback[0]


def _solana_fee_payer_ready(
    kinds: Sequence[Mapping[str, Any]],
    signers: Mapping[str, Sequence[str]],
) -> bool:
    try:
        _resolve_solana_fee_payer(kinds, signers)
    except CoinbaseX402Error:
        return False
    return True


def _safe_verify(value: Any) -> tuple[bool, str | None]:
    is_valid = _field(value, "is_valid", "isValid")
    if not isinstance(is_valid, bool):
        raise CoinbaseX402Error("verify_response_invalid")
    payer = _field(value, "payer")
    return is_valid, payer if isinstance(payer, str) and len(payer) <= 256 else None


def _safe_receipt(value: Any, expected_network: str, expected_amount: str) -> SettlementReceipt:
    if _field(value, "success") is not True:
        raise CoinbaseX402Error("settlement_failed")
    network = _field(value, "network")
    transaction = _field(value, "transaction")
    payer = _field(value, "payer")
    amount = _field(value, "amount")
    if network != expected_network:
        raise CoinbaseX402Error("settlement_response_invalid")
    if amount is not None and amount != expected_amount:
        raise CoinbaseX402Error("settlement_response_invalid")
    if network.startswith("eip155:"):
        if (
            not isinstance(transaction, str)
            or not _EVM_TRANSACTION_RE.fullmatch(transaction)
            or int(transaction, 16) == 0
        ):
            raise CoinbaseX402Error("settlement_response_invalid")
        if payer is not None and (
            not isinstance(payer, str)
            or not _EVM_ADDRESS_RE.fullmatch(payer)
            or int(payer, 16) == 0
        ):
            payer = None
    else:
        if (
            not isinstance(transaction, str)
            or not _SOLANA_TRANSACTION_RE.fullmatch(transaction)
            or _base58_bytes(transaction) is None
            or len(_base58_bytes(transaction) or b"") != 64
        ):
            raise CoinbaseX402Error("settlement_response_invalid")
        if payer is not None and not _valid_solana_public_id(payer):
            payer = None
    return SettlementReceipt(
        network=network,
        transaction=transaction,
        payer=payer,
        amount=amount,
    )


def _normalize_cached_response(response: CachedResponse, maximum: int) -> CachedResponse:
    if not isinstance(response, CachedResponse):
        raise CoinbaseX402Error("cached_response_invalid")
    if (
        isinstance(response.status_code, bool)
        or not isinstance(response.status_code, int)
        or not 200 <= response.status_code <= 299
    ):
        raise CoinbaseX402Error("cached_response_not_success")
    if not isinstance(response.body, bytes) or len(response.body) > maximum:
        raise CoinbaseX402Error("cached_response_too_large")
    if not isinstance(response.headers, Mapping) or len(response.headers) > MAX_CACHED_HEADER_COUNT:
        raise CoinbaseX402Error("cached_response_invalid")
    headers: dict[str, str] = {}
    for raw_name, raw_value in response.headers.items():
        if (
            not isinstance(raw_name, str)
            or not _HEADER_NAME_RE.fullmatch(raw_name)
            or not isinstance(raw_value, str)
            or len(raw_value) > 8_192
            or _CONTROL_CHAR_RE.search(raw_value)
        ):
            raise CoinbaseX402Error("cached_response_invalid")
        name = raw_name.lower()
        if name in _FORBIDDEN_CACHE_HEADERS:
            raise CoinbaseX402Error("cached_response_sensitive_header")
        headers[name] = raw_value
    if len(_canonical_json(headers)) > MAX_CACHED_HEADERS_BYTES:
        raise CoinbaseX402Error("cached_response_invalid")
    return CachedResponse(response.status_code, headers, bytes(response.body))


class _ReplayLedger:
    """Small synchronous SQLite ledger; every mutation uses BEGIN IMMEDIATE."""

    def __init__(self, config: CoinbaseX402Config, clock: Callable[[], float]) -> None:
        self._path = config.db_path
        self._lease_seconds = config.verification_lease_seconds
        self._ttl_seconds = config.replay_ttl_seconds
        self._max_entries = config.replay_max_entries
        self._clock = clock
        self._lock = threading.RLock()
        parent = self._path.parent
        if str(parent) not in {"", "."}:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self._path),
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def _initialize(self) -> None:
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = FULL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS x402_payments (
                            payment_id TEXT PRIMARY KEY,
                            request_binding TEXT NOT NULL,
                            requirement_hash TEXT NOT NULL,
                            state TEXT NOT NULL CHECK (
                                state IN (
                                    'pending', 'settled', 'finalized',
                                    'settlement_unknown', 'released'
                                )
                            ),
                            lease_token TEXT,
                            lease_expires_at REAL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            expires_at REAL NOT NULL,
                            settled_at REAL,
                            finalized_at REAL,
                            settlement_unknown_at REAL,
                            released_at REAL,
                            response_status INTEGER,
                            response_headers TEXT,
                            response_body BLOB,
                            receipt_network TEXT,
                            receipt_transaction TEXT,
                            receipt_payer TEXT,
                            receipt_amount TEXT
                        );
                        CREATE INDEX IF NOT EXISTS x402_payments_state_idx
                        ON x402_payments(state, updated_at);
                        CREATE INDEX IF NOT EXISTS x402_payments_expiry_idx
                        ON x402_payments(expires_at);
                        """
                    )
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _cleanup_expired(connection: sqlite3.Connection, now: float) -> int:
        # Unknown or externally settled outcomes are permanent manual-review
        # checkpoints.  They are never discarded by automatic retention.
        # Released proofs have never reached settlement and carry no economic
        # ambiguity, so retaining them would let invalid submissions exhaust
        # the bounded ledger without spending funds.
        cursor = connection.execute(
            """
            DELETE FROM x402_payments
            WHERE state = 'released'
               OR (
                    expires_at <= ?
                    AND state IN ('pending', 'finalized')
               )
            """,
            (now,),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _decode_response(row: sqlite3.Row) -> CachedResponse | None:
        status = row["response_status"]
        headers_json = row["response_headers"]
        body = row["response_body"]
        if status is None or headers_json is None or body is None:
            return None
        try:
            headers = json.loads(headers_json)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(headers, dict) or not isinstance(body, bytes):
            return None
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
            return None
        return CachedResponse(int(status), headers, body)

    @staticmethod
    def _decode_receipt(row: sqlite3.Row) -> SettlementReceipt | None:
        network = row["receipt_network"]
        transaction = row["receipt_transaction"]
        if not isinstance(network, str) or not isinstance(transaction, str):
            return None
        payer = row["receipt_payer"]
        amount = row["receipt_amount"]
        return SettlementReceipt(
            network=network,
            transaction=transaction,
            payer=payer if isinstance(payer, str) else None,
            amount=amount if isinstance(amount, str) else None,
        )

    def reserve(self, parsed: _ParsedPayment) -> _Reservation:
        now = self._clock()
        lease_token = secrets.token_urlsafe(32)
        local_retention_deadline = now + max(
            self._ttl_seconds,
            parsed.max_timeout_seconds,
        )
        expires_at = (
            min(local_retention_deadline, parsed.proof_expires_at)
            if parsed.proof_expires_at is not None
            else local_retention_deadline
        )
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    self._cleanup_expired(connection, now)
                    row = connection.execute(
                        "SELECT * FROM x402_payments WHERE payment_id = ?",
                        (parsed.payment_id,),
                    ).fetchone()
                    if row is None:
                        count = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM x402_payments WHERE state != 'released'"
                            ).fetchone()[0]
                        )
                        if count >= self._max_entries:
                            connection.rollback()
                            return _Reservation("capacity")
                        connection.execute(
                            """
                            INSERT INTO x402_payments (
                                payment_id, request_binding, requirement_hash,
                                state, lease_token, lease_expires_at,
                                created_at, updated_at, expires_at
                            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                            """,
                            (
                                parsed.payment_id,
                                parsed.request_binding,
                                parsed.requirement_hash,
                                lease_token,
                                now + self._lease_seconds,
                                now,
                                now,
                                expires_at,
                            ),
                        )
                        connection.commit()
                        return _Reservation("acquired", lease_token=lease_token)

                    if (
                        row["request_binding"] != parsed.request_binding
                        or row["requirement_hash"] != parsed.requirement_hash
                    ):
                        connection.rollback()
                        return _Reservation("conflict")
                    state = str(row["state"])
                    if state == "finalized":
                        response = self._decode_response(row)
                        receipt = self._decode_receipt(row)
                        connection.rollback()
                        if response is None or receipt is None:
                            return _Reservation("settled")
                        return _Reservation("replay", response=response, receipt=receipt)
                    if state == "settlement_unknown":
                        connection.rollback()
                        return _Reservation("settlement_unknown")
                    if state == "settled":
                        connection.rollback()
                        return _Reservation("settled")
                    if state == "pending" and float(row["lease_expires_at"] or 0) > now:
                        connection.rollback()
                        return _Reservation("busy")
                    # A stale pre-settlement lease or an explicitly released
                    # proof can safely be verified again.  Settlement attempts
                    # are represented by a different, terminal state first.
                    connection.execute(
                        """
                        UPDATE x402_payments
                        SET state = 'pending', lease_token = ?, lease_expires_at = ?,
                            updated_at = ?, expires_at = ?, released_at = NULL
                        WHERE payment_id = ?
                        """,
                        (
                            lease_token,
                            now + self._lease_seconds,
                            now,
                            expires_at,
                            parsed.payment_id,
                        ),
                    )
                    connection.commit()
                    return _Reservation("acquired", lease_token=lease_token)
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def mark_settlement_unknown(self, ticket: PaymentTicket) -> bool:
        now = self._clock()
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    cursor = connection.execute(
                        """
                        UPDATE x402_payments
                        SET state = 'settlement_unknown', lease_token = NULL,
                            lease_expires_at = NULL, updated_at = ?,
                            settlement_unknown_at = ?
                        WHERE payment_id = ? AND request_binding = ?
                          AND state = 'pending' AND lease_token = ?
                        """,
                        (
                            now,
                            now,
                            ticket.payment_id,
                            ticket.request_binding,
                            ticket.lease_token,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def mark_settled(self, ticket: PaymentTicket, receipt: SettlementReceipt) -> bool:
        now = self._clock()
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    cursor = connection.execute(
                        """
                        UPDATE x402_payments
                        SET state = 'settled', updated_at = ?, settled_at = ?,
                            receipt_network = ?, receipt_transaction = ?,
                            receipt_payer = ?, receipt_amount = ?
                        WHERE payment_id = ? AND request_binding = ?
                          AND state = 'settlement_unknown'
                        """,
                        (
                            now,
                            now,
                            receipt.network,
                            receipt.transaction,
                            receipt.payer,
                            receipt.amount,
                            ticket.payment_id,
                            ticket.request_binding,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def finalize(self, ticket: PaymentTicket, response: CachedResponse) -> bool:
        now = self._clock()
        headers_json = _canonical_json(dict(response.headers)).decode("utf-8")
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    cursor = connection.execute(
                        """
                        UPDATE x402_payments
                        SET state = 'finalized', updated_at = ?, finalized_at = ?,
                            expires_at = MAX(expires_at, ?), response_status = ?,
                            response_headers = ?, response_body = ?
                        WHERE payment_id = ? AND request_binding = ?
                          AND state = 'settled'
                        """,
                        (
                            now,
                            now,
                            now + self._ttl_seconds,
                            response.status_code,
                            headers_json,
                            response.body,
                            ticket.payment_id,
                            ticket.request_binding,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def release(self, ticket: PaymentTicket) -> bool:
        now = self._clock()
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    cursor = connection.execute(
                        """
                        UPDATE x402_payments
                        SET state = 'released', lease_token = NULL,
                            lease_expires_at = NULL, updated_at = ?, released_at = ?
                        WHERE payment_id = ? AND request_binding = ?
                          AND state = 'pending' AND lease_token = ?
                        """,
                        (
                            now,
                            now,
                            ticket.payment_id,
                            ticket.request_binding,
                            ticket.lease_token,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc

    def state_counts(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    self._begin(connection)
                    self._cleanup_expired(connection, now)
                    rows = connection.execute(
                        "SELECT state, COUNT(*) AS count FROM x402_payments GROUP BY state"
                    ).fetchall()
                    stale = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM x402_payments
                            WHERE state = 'pending' AND lease_expires_at <= ?
                            """,
                            (now,),
                        ).fetchone()[0]
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                raise CoinbaseX402Error("payment_ledger_unavailable") from exc
        counts = {state: 0 for state in _LEDGER_STATES}
        for row in rows:
            counts[str(row["state"])] = int(row["count"])
        counts["stale_pending"] = stale
        counts["total"] = sum(counts[state] for state in _LEDGER_STATES)
        return counts


class CoinbaseX402Gateway:
    """Coinbase-only v2 verifier/settler with an exactly-once local boundary."""

    def __init__(
        self,
        config: CoinbaseX402Config,
        *,
        facilitator_client: Any | None = None,
        sdk_types: SDKBindings | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, CoinbaseX402Config):
            raise CoinbaseX402Error("payment_config_invalid")
        if (facilitator_client is None) != (sdk_types is None):
            raise CoinbaseX402Error("payment_sdk_injection_incomplete")
        if facilitator_client is None:
            facilitator_client, sdk_types = _load_official_sdk(
                config.cdp_api_key_id,
                config.cdp_api_key_secret,
                config.readiness_timeout_seconds,
            )
        assert sdk_types is not None
        self._config = config
        self._facilitator = facilitator_client
        self._sdk = sdk_types
        self._clock = time.time if clock is None else clock
        self._ledger = _ReplayLedger(config, self._clock)
        self._support_lock = asyncio.Lock()
        self._support_guard = threading.RLock()
        self._support = SupportSnapshot(None, False, "not_checked", (), {})
        self._counter_guard = threading.RLock()
        self._counters: Counter[str] = Counter({name: 0 for name in _COUNTER_NAMES})

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self._config.mode.value!r}, configured=True)"

    @property
    def mode(self) -> PaymentMode:
        return self._config.mode

    def _increment(self, name: str) -> None:
        with self._counter_guard:
            self._counters[name] += 1

    def _counter_snapshot(self) -> dict[str, int]:
        with self._counter_guard:
            return dict(sorted(self._counters.items()))

    async def refresh_supported(self) -> SupportSnapshot:
        """Refresh authenticated ``/supported`` state with a hard timeout."""

        async with self._support_lock:
            self._increment("supported_refresh_total")
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._facilitator.get_supported),
                    timeout=float(self._config.readiness_timeout_seconds),
                )
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(
                        result,
                        timeout=float(self._config.readiness_timeout_seconds),
                    )
                kinds, signers = _safe_supported(result)
            except CoinbaseX402Error as exc:
                snapshot = SupportSnapshot(
                    checked_at=self._clock(),
                    available=False,
                    reason=exc.code,
                    kinds=(),
                    signers={},
                )
                self._increment("supported_refresh_failed_total")
            except Exception:
                snapshot = SupportSnapshot(
                    checked_at=self._clock(),
                    available=False,
                    reason="facilitator_unavailable",
                    kinds=(),
                    signers={},
                )
                self._increment("supported_refresh_failed_total")
            else:
                snapshot = SupportSnapshot(
                    checked_at=self._clock(),
                    available=True,
                    reason="ok",
                    kinds=kinds,
                    signers=signers,
                )
                self._increment("supported_refresh_succeeded_total")
            with self._support_guard:
                self._support = snapshot
            return snapshot

    def _support_snapshot(self) -> SupportSnapshot:
        with self._support_guard:
            return self._support

    def _require_supported(self, network: str) -> None:
        snapshot = self._support_snapshot()
        now = self._clock()
        if (
            not snapshot.available
            or snapshot.checked_at is None
            or now - snapshot.checked_at > self._config.readiness_max_age_seconds
            or not any(
                kind.get("x402Version") == 2
                and kind.get("scheme") == "exact"
                and kind.get("network") == network
                for kind in snapshot.kinds
            )
        ):
            raise CoinbaseX402Error("facilitator_not_ready")

    def prepare_requirements(
        self,
        requirements: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Materialize an exact challenge from fresh authenticated support.

        Solana's fee payer comes from the exact v2 kind.  The top-level
        ``signers['solana:*']`` list is used only when the kind omits it and
        contains exactly one valid signer.
        """

        if (
            not isinstance(requirements, Sequence)
            or isinstance(requirements, (str, bytes, bytearray))
            or not requirements
        ):
            raise CoinbaseX402Error("payment_requirements_invalid")
        snapshot = self._support_snapshot()
        if (
            not snapshot.available
            or snapshot.checked_at is None
            or self._clock() - snapshot.checked_at > self._config.readiness_max_age_seconds
        ):
            raise CoinbaseX402Error("facilitator_not_ready")
        fee_payer = _resolve_solana_fee_payer(snapshot.kinds, snapshot.signers)
        result: list[dict[str, Any]] = []
        seen: set[bytes] = set()
        for requirement in requirements:
            normalized = _normalize_requirement(
                requirement,
                replay_ttl_seconds=self._config.replay_ttl_seconds,
            )
            self._require_supported(normalized["network"])
            if normalized["network"].startswith("solana:"):
                extra = dict(normalized["extra"])
                extra["feePayer"] = fee_payer
                normalized["extra"] = extra
            encoded = _canonical_json(normalized)
            if encoded in seen:
                raise CoinbaseX402Error("payment_requirements_ambiguous")
            seen.add(encoded)
            result.append(normalized)
        network_counts = Counter(item["network"] for item in result)
        if len(result) != 2 or network_counts != Counter(
            {BASE_MAINNET_NETWORK: 1, SOLANA_MAINNET_NETWORK: 1}
        ):
            raise CoinbaseX402Error("payment_requirements_incomplete")
        return tuple(result)

    def _route_allowed(self, method: str, route_id: str) -> bool:
        return (
            isinstance(method, str)
            and method.upper() == "GET"
            and isinstance(route_id, str)
            and route_id in self._config.allowed_get_routes
        )

    async def begin(
        self,
        signature: str,
        *,
        method: str,
        resource_url: str,
        body: bytes,
        requirements: Sequence[Mapping[str, Any]],
        route_id: str,
    ) -> PaymentDecision:
        """Bind, reserve, and verify one signed request without settling it."""

        self._increment("signed_submissions_total")
        if self._config.mode is PaymentMode.SHADOW:
            self._increment("shadow_blocked_total")
            return PaymentDecision(DecisionKind.SHADOW_BLOCKED, "x402_shadow_locked")
        if not self._route_allowed(method, route_id):
            self._increment("route_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, "payment_route_not_allowed")
        if not isinstance(body, bytes):
            self._increment("payment_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, "request_body_invalid")
        try:
            parsed = _parse_payment(
                signature,
                method=method,
                resource_url=resource_url,
                body=body,
                expected_requirements=requirements,
                replay_ttl_seconds=self._config.replay_ttl_seconds,
                now=self._clock(),
                sdk=self._sdk,
            )
            self._require_supported(parsed.network)
            reservation = self._ledger.reserve(parsed)
        except CoinbaseX402Error as exc:
            self._increment("payment_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, exc.code)
        except RecursionError:
            # Pathological nesting can exceed Python's JSON/model recursion
            # limit. Treat it like any other malformed untrusted signature.
            self._increment("payment_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, "invalid_payment_signature")

        if reservation.outcome == "replay":
            self._increment("replay_hits_total")
            return PaymentDecision(
                DecisionKind.REPLAY,
                "payment_replayed",
                response=reservation.response,
                receipt=reservation.receipt,
            )
        if reservation.outcome == "busy":
            self._increment("payment_busy_total")
            return PaymentDecision(DecisionKind.BUSY, "payment_verification_in_progress")
        if reservation.outcome == "conflict":
            self._increment("payment_binding_conflicts_total")
            return PaymentDecision(DecisionKind.REJECTED, "payment_binding_conflict")
        if reservation.outcome == "settlement_unknown":
            self._increment("settlement_unknown_replay_total")
            return PaymentDecision(
                DecisionKind.SETTLEMENT_UNKNOWN,
                "payment_settlement_unknown",
            )
        if reservation.outcome == "settled":
            self._increment("settled_unfinalized_replay_total")
            return PaymentDecision(
                DecisionKind.SETTLED_UNFINALIZED,
                "payment_settled_response_unavailable",
            )
        if reservation.outcome == "capacity":
            self._increment("ledger_capacity_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, "payment_ledger_capacity")
        if reservation.outcome != "acquired" or reservation.lease_token is None:
            self._increment("payment_rejected_total")
            return PaymentDecision(DecisionKind.REJECTED, "payment_ledger_unavailable")

        ticket = PaymentTicket(
            payment_id=parsed.payment_id,
            request_binding=parsed.request_binding,
            lease_token=reservation.lease_token,
            parsed=parsed,
        )
        self._increment("verify_calls_total")
        try:
            verify_result = await self._facilitator.verify(
                parsed.sdk_payload,
                parsed.sdk_requirement,
            )
            is_valid, _ = _safe_verify(verify_result)
        except Exception:
            self.release(ticket, reason="verify_unavailable")
            self._increment("verify_unavailable_total")
            return PaymentDecision(DecisionKind.REJECTED, "facilitator_verify_unavailable")
        if not is_valid:
            self.release(ticket, reason="verify_invalid")
            self._increment("verify_invalid_total")
            return PaymentDecision(DecisionKind.REJECTED, "payment_invalid")
        self._increment("verify_succeeded_total")
        return PaymentDecision(
            DecisionKind.PROCEED,
            "payment_verified",
            ticket=ticket,
        )

    async def settle_and_finalize(
        self,
        ticket: PaymentTicket,
        response: CachedResponse,
    ) -> PaymentDecision:
        """Settle once and atomically checkpoint a bounded replay response.

        The ledger enters ``settlement_unknown`` before the remote call.  Any
        exception, malformed response, or process death therefore requires
        manual reconciliation and cannot cause an automatic second settlement.
        """

        if not isinstance(ticket, PaymentTicket):
            return PaymentDecision(DecisionKind.REJECTED, "payment_ticket_invalid")
        try:
            cached = _normalize_cached_response(
                response,
                self._config.max_cached_response_bytes,
            )
        except CoinbaseX402Error as exc:
            self.release(ticket)
            return PaymentDecision(DecisionKind.REJECTED, exc.code)
        try:
            transitioned = self._ledger.mark_settlement_unknown(ticket)
        except CoinbaseX402Error as exc:
            return PaymentDecision(DecisionKind.REJECTED, exc.code)
        if not transitioned:
            return PaymentDecision(DecisionKind.REJECTED, "payment_ticket_not_active")

        self._increment("settle_calls_total")
        try:
            raw_receipt = await self._facilitator.settle(
                ticket.parsed.sdk_payload,
                ticket.parsed.sdk_requirement,
            )
            receipt = _safe_receipt(
                raw_receipt,
                ticket.parsed.network,
                ticket.parsed.amount,
            )
        except Exception:
            self._increment("settlement_unknown_total")
            return PaymentDecision(
                DecisionKind.SETTLEMENT_UNKNOWN,
                "payment_settlement_unknown",
            )
        try:
            if not self._ledger.mark_settled(ticket, receipt):
                self._increment("settlement_checkpoint_failed_total")
                return PaymentDecision(
                    DecisionKind.SETTLEMENT_UNKNOWN,
                    "payment_settlement_checkpoint_failed",
                )
            if not self._ledger.finalize(ticket, cached):
                self._increment("settled_unfinalized_total")
                return PaymentDecision(
                    DecisionKind.SETTLED_UNFINALIZED,
                    "payment_settled_response_unavailable",
                    receipt=receipt,
                )
        except CoinbaseX402Error:
            self._increment("settled_unfinalized_total")
            return PaymentDecision(
                DecisionKind.SETTLED_UNFINALIZED,
                "payment_settled_response_unavailable",
                receipt=receipt,
            )
        self._increment("settlement_succeeded_total")
        self._increment("finalized_total")
        return PaymentDecision(
            DecisionKind.FINALIZED,
            "payment_finalized",
            response=cached,
            receipt=receipt,
        )

    def release(self, ticket: PaymentTicket, *, reason: str = "handler_not_successful") -> bool:
        """Release a verified proof before settlement; the reason is not stored."""

        _ = reason
        if not isinstance(ticket, PaymentTicket):
            return False
        try:
            released = self._ledger.release(ticket)
        except CoinbaseX402Error:
            return False
        if released:
            self._increment("released_total")
        return released

    def readiness_snapshot(self) -> dict[str, Any]:
        """Return non-secret payment readiness, ledger state, and counters."""

        support = self._support_snapshot()
        now = self._clock()
        age = None if support.checked_at is None else max(0.0, now - support.checked_at)
        support_fresh = (
            support.available
            and age is not None
            and age <= self._config.readiness_max_age_seconds
        )
        try:
            states = self._ledger.state_counts()
        except CoinbaseX402Error:
            states = {state: 0 for state in _LEDGER_STATES}
            states.update({"stale_pending": 0, "total": 0})
            ledger_ready = False
        else:
            ledger_ready = True
        reconciliation_required = states["settlement_unknown"] + states["settled"]
        unresolved = states["pending"] + reconciliation_required
        try:
            durable_path = self._config.db_path.expanduser().resolve(strict=False) == Path(
                "/data/x402_payments.sqlite3"
            )
        except OSError:
            durable_path = False
        blockers: list[str] = []
        if not support_fresh:
            blockers.append("facilitator_support_not_ready")
        if not ledger_ready:
            blockers.append("payment_ledger_unavailable")
        if reconciliation_required:
            blockers.append("payment_reconciliation_required")
        if states["pending"]:
            blockers.append("payment_verification_in_progress")
        if states["total"] >= self._config.replay_max_entries:
            blockers.append("payment_ledger_at_capacity")
        return {
            "ready": not blockers,
            "mode": self._config.mode.value,
            "payment_locked": self._config.mode is PaymentMode.SHADOW,
            "allowed_get_routes": sorted(self._config.allowed_get_routes),
            "sdk": {
                "x402": self._sdk.x402_version,
                "cdp_sdk": self._sdk.cdp_sdk_version,
            },
            "facilitator": {
                "provider": "coinbase_cdp",
                "authentication_configured": True,
                "authenticated": support.available,
                "checked": support.checked_at is not None,
                "available": support.available,
                "fresh": support_fresh,
                "age_seconds": None if age is None else round(age, 3),
                "reason": support.reason,
                "kinds": [copy.deepcopy(kind) for kind in support.kinds],
                "solana_fee_payer_ready": (
                    support.available
                    and _solana_fee_payer_ready(support.kinds, support.signers)
                ),
            },
            "ledger": {
                "ready": ledger_ready,
                "durable_path": durable_path,
                "states": states,
                "unresolved": unresolved,
                "reconciliation_required": reconciliation_required,
            },
            "counters": self._counter_snapshot(),
            "blockers": blockers,
        }

    async def aclose(self) -> None:
        """Close the official async facilitator client when supported."""

        close = getattr(self._facilitator, "aclose", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                return


__all__ = [
    "COINBASE_FACILITATOR_URL",
    "EXPECTED_CDP_SDK_VERSION",
    "EXPECTED_X402_VERSION",
    "CachedResponse",
    "CoinbaseX402Config",
    "CoinbaseX402Error",
    "CoinbaseX402Gateway",
    "DecisionKind",
    "PaymentDecision",
    "PaymentMode",
    "PaymentTicket",
    "SDKBindings",
    "SettlementReceipt",
    "SupportSnapshot",
    "canonicalize_resource_url",
]
