"""Fail-closed x402 v2 payment parsing and facilitator boundary.

This module deliberately has no dependency on the FastAPI application.  It
provides four integration surfaces:

``parse_payment_signature``
    Decode an official ``PAYMENT-SIGNATURE`` value, validate it with the x402
    Python SDK, bind it to the exact server-issued requirement and request, and
    return stable identifiers.  Legacy/v1 payloads are never accepted here.

``compute_payment_id``
    Return the SHA-256 digest of the immutable, cryptographic payment material.

``compute_request_binding``
    Bind the HTTP method, canonical resource URL, and raw request-body digest.

``FacilitatorAdapter``
    Send already-validated v2 payloads to injectable ``/verify`` and
    ``/settle`` endpoints.  Responses are reduced to non-secret plain dicts.

The x402 package is a mandatory runtime dependency for this boundary. Missing
or broken SDK imports still fail closed so packaging drift cannot bypass it.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.payment_limits import MAX_PAYMENT_REPLAY_ENTRIES, MAX_PAYMENT_REPLAY_TTL_SECONDS
from src.security_config import is_production_environment

try:  # The production image must install the optional x402 dependency.
    from x402.schemas import SupportedResponse as _SDK_SUPPORTED_RESPONSE
    from x402.schemas import parse_payment_payload as _SDK_PARSE_PAYMENT_PAYLOAD
except Exception:  # pragma: no cover - the normal core-only installation path
    _SDK_PARSE_PAYMENT_PAYLOAD = None
    _SDK_SUPPORTED_RESPONSE = None

try:  # Official CDP SDK support is mandatory for authenticated CDP requests.
    from cdp.auth import JwtOptions as _CDP_JWT_OPTIONS
    from cdp.auth import generate_jwt as _CDP_GENERATE_JWT
except Exception:  # pragma: no cover - packaging drift must fail closed
    _CDP_GENERATE_JWT = None
    _CDP_JWT_OPTIONS = None

try:
    _SDK_VERSION = metadata.version("x402")
except metadata.PackageNotFoundError:  # pragma: no cover - environment dependent
    _SDK_VERSION = None

try:
    _CDP_SDK_VERSION = metadata.version("cdp-sdk")
except metadata.PackageNotFoundError:  # pragma: no cover - environment dependent
    _CDP_SDK_VERSION = None


MAX_PAYMENT_HEADER_CHARS = 16_384
MAX_PAYMENT_JSON_BYTES = 12_288
MAX_FACILITATOR_RESPONSE_BYTES = 65_536
MAINNET_NETWORKS = frozenset(
    {
        "eip155:8453",
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    }
)
PUBLIC_DEVELOPMENT_FACILITATOR_HOSTS = frozenset({"x402.org", "www.x402.org"})
CDP_FACILITATOR_HOST = "api.cdp.coinbase.com"
CDP_FACILITATOR_PATH = "/platform/v2/x402"
_ATOMIC_AMOUNT_RE = re.compile(r"^[1-9][0-9]*$")
_HTTP_METHOD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_EVM_PUBLIC_ID_RE = re.compile(r"^0x[0-9A-Fa-f]{40}$")
_EVM_TRANSACTION_RE = re.compile(r"^0x[0-9A-Fa-f]{64}$")
_SOLANA_PUBLIC_ID_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SOLANA_TRANSACTION_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}
_REQUIREMENT_FIELDS = (
    "scheme",
    "network",
    "asset",
    "amount",
    "payTo",
    "maxTimeoutSeconds",
)
_PAYMENT_ENVELOPE_KEYS = frozenset(
    {"x402Version", "payload", "accepted", "resource", "extensions"}
)
_PAYMENT_REQUIREMENT_KEYS = frozenset({*_REQUIREMENT_FIELDS, "extra"})
_RESOURCE_KEYS = frozenset({"url", "description", "mimeType"})
_EIP3009_PAYLOAD_KEYS = frozenset({"signature", "authorization"})
_EIP3009_AUTHORIZATION_KEYS = frozenset(
    {"from", "to", "value", "validAfter", "validBefore", "nonce"}
)
_SOLANA_PAYLOAD_KEYS = frozenset({"transaction"})
_EVM_NONCE_RE = re.compile(r"^0x[0-9A-Fa-f]{64}$")

PostCallable = Callable[..., Awaitable[Any]]
GetCallable = Callable[..., Awaitable[Any]]


class PaymentSecurityError(ValueError):
    """A payment failed local structural or request-binding validation."""


@dataclass(frozen=True, slots=True)
class ParsedPayment:
    """A validated x402 v2 payment and its stable request identifiers.

    ``payload`` contains payment authorization material and must never be
    logged.  ``payment_id`` and ``request_binding`` are safe correlation keys.
    """

    payment_id: str
    request_binding: str
    body_sha256: str
    resource_url: str
    payload: dict[str, Any] = field(repr=False)
    accepted: dict[str, Any] = field(repr=False)
    x402_version: int = 2


def sdk_available() -> bool:
    """Return whether the official x402 payload parser imported successfully."""

    return _SDK_PARSE_PAYMENT_PAYLOAD is not None


def cdp_auth_available() -> bool:
    """Return whether the official CDP request-bound JWT helper imported."""

    return _CDP_GENERATE_JWT is not None and _CDP_JWT_OPTIONS is not None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise PaymentSecurityError("Payment data is not canonical JSON") from exc
    return encoded.encode("utf-8")


def compute_payment_id(decoded_payload: Mapping[str, Any]) -> str:
    """Hash only immutable payment proof material, namespaced to its rail.

    Resource presentation fields and protocol extensions are deliberately
    excluded: they are not signed payment authorization material and allowing
    them to affect the replay key would let one proof acquire multiple IDs.
    """

    return hashlib.sha256(
        _canonical_json(_payment_identity_material(decoded_payload))
    ).hexdigest()


def canonicalize_resource_url(url: str) -> str:
    """Return a strict canonical HTTP(S) resource URL.

    Scheme and host are lower-cased, an explicit default port is removed, and
    an empty path becomes ``/``.  Path and query bytes are otherwise preserved
    because they are part of the paid resource identity.  Userinfo and URL
    fragments are forbidden.
    """

    if not isinstance(url, str) or not url or len(url) > 4096:
        raise PaymentSecurityError("Resource URL is missing or too long")
    if _CONTROL_CHAR_RE.search(url) or "\\" in url or any(char.isspace() for char in url):
        raise PaymentSecurityError("Resource URL contains control characters")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise PaymentSecurityError("Resource URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise PaymentSecurityError("Resource URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise PaymentSecurityError("Resource URL must not contain userinfo")
    if parsed.fragment:
        raise PaymentSecurityError("Resource URL must not contain a fragment")
    if not parsed.hostname:
        raise PaymentSecurityError("Resource URL must contain a host")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PaymentSecurityError("Resource URL host is malformed") from exc
    if ":" in host:
        host = f"[{host}]"
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise PaymentSecurityError("Resource URL path is malformed")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _body_bytes(body: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise PaymentSecurityError("Request body must be bytes")
    return bytes(body)


def compute_request_binding(
    method: str,
    resource_url: str,
    body: bytes | bytearray | memoryview = b"",
) -> str:
    """Hash the uppercase method, canonical URL, and raw body SHA-256 digest."""

    if not isinstance(method, str) or not _HTTP_METHOD_RE.fullmatch(method):
        raise PaymentSecurityError("HTTP method is malformed")
    canonical_url = canonicalize_resource_url(resource_url)
    body_sha256 = hashlib.sha256(_body_bytes(body)).hexdigest()
    binding = {
        "bodySha256": body_sha256,
        "method": method.upper(),
        "url": canonical_url,
    }
    return hashlib.sha256(_canonical_json(binding)).hexdigest()


def _decode_payment_header(header_value: str) -> dict[str, Any]:
    if not isinstance(header_value, str) or not header_value:
        raise PaymentSecurityError("PAYMENT-SIGNATURE is missing")
    if len(header_value) > MAX_PAYMENT_HEADER_CHARS:
        raise PaymentSecurityError("PAYMENT-SIGNATURE is too large")
    try:
        decoded = base64.b64decode(header_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PaymentSecurityError("PAYMENT-SIGNATURE is not valid base64") from exc
    if not decoded or len(decoded) > MAX_PAYMENT_JSON_BYTES:
        raise PaymentSecurityError("Decoded payment payload is empty or too large")
    try:
        value = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PaymentSecurityError("PAYMENT-SIGNATURE is not strict JSON") from exc
    if not isinstance(value, dict):
        raise PaymentSecurityError("Payment payload must be a JSON object")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL_CHAR_RE.search(value):
        raise PaymentSecurityError(f"Payment requirement {field} is malformed")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    keys = set(value)
    unknown = keys - allowed
    missing = required - keys
    if unknown or missing:
        raise PaymentSecurityError(f"{label} fields do not match the supported schema")


def _validate_payment_envelope_shape(decoded: Mapping[str, Any]) -> None:
    _require_exact_keys(
        decoded,
        allowed=_PAYMENT_ENVELOPE_KEYS,
        required=frozenset({"x402Version", "payload", "accepted"}),
        label="Payment envelope",
    )
    resource = decoded.get("resource")
    if not isinstance(resource, Mapping):
        raise PaymentSecurityError("x402 v2 payload is missing resource metadata")
    _require_exact_keys(
        resource,
        allowed=_RESOURCE_KEYS,
        required=frozenset({"url"}),
        label="Payment resource",
    )
    for field_name in ("description", "mimeType"):
        value = resource.get(field_name)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value) > 1024
            or _CONTROL_CHAR_RE.search(value)
        ):
            raise PaymentSecurityError(f"Payment resource {field_name} is malformed")
    extensions = decoded.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
        raise PaymentSecurityError("Payment extensions must be an object")


def _validate_scheme_payload(
    payload: Any,
    accepted: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise PaymentSecurityError("Payment scheme payload must be an object")
    payload_copy = copy.deepcopy(dict(payload))
    network = str(accepted.get("network") or "")
    if accepted.get("scheme") != "exact":
        raise PaymentSecurityError("Only the exact payment scheme is supported")

    if network.startswith("eip155:"):
        _require_exact_keys(
            payload_copy,
            allowed=_EIP3009_PAYLOAD_KEYS,
            required=_EIP3009_PAYLOAD_KEYS,
            label="EIP-3009 payload",
        )
        signature = payload_copy.get("signature")
        authorization = payload_copy.get("authorization")
        if not isinstance(signature, str) or not signature or len(signature) > 8192:
            raise PaymentSecurityError("EIP-3009 signature is malformed")
        if not isinstance(authorization, Mapping):
            raise PaymentSecurityError("EIP-3009 authorization is malformed")
        _require_exact_keys(
            authorization,
            allowed=_EIP3009_AUTHORIZATION_KEYS,
            required=_EIP3009_AUTHORIZATION_KEYS,
            label="EIP-3009 authorization",
        )
        from_address = authorization.get("from")
        to_address = authorization.get("to")
        nonce = authorization.get("nonce")
        if (
            not isinstance(from_address, str)
            or not _EVM_PUBLIC_ID_RE.fullmatch(from_address)
            or int(from_address, 16) == 0
            or not isinstance(to_address, str)
            or not _EVM_PUBLIC_ID_RE.fullmatch(to_address)
            or int(to_address, 16) == 0
            or not isinstance(nonce, str)
            or not _EVM_NONCE_RE.fullmatch(nonce)
        ):
            raise PaymentSecurityError("EIP-3009 authorization is malformed")
        for field_name in ("value", "validAfter", "validBefore"):
            value = authorization.get(field_name)
            if not isinstance(value, str) or not value.isdigit() or len(value) > 78:
                raise PaymentSecurityError("EIP-3009 authorization is malformed")
        return payload_copy

    if network.startswith("solana:"):
        _require_exact_keys(
            payload_copy,
            allowed=_SOLANA_PAYLOAD_KEYS,
            required=_SOLANA_PAYLOAD_KEYS,
            label="Solana payload",
        )
        transaction = payload_copy.get("transaction")
        if not isinstance(transaction, str) or not transaction or len(transaction) > 8192:
            raise PaymentSecurityError("Solana transaction is malformed")
        try:
            decoded_transaction = base64.b64decode(transaction, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaymentSecurityError("Solana transaction is not canonical base64") from exc
        if (
            not decoded_transaction
            or base64.b64encode(decoded_transaction).decode("ascii") != transaction
        ):
            raise PaymentSecurityError("Solana transaction is not canonical base64")
        return payload_copy

    raise PaymentSecurityError("Payment network is unsupported")


def _payment_identity_material(decoded: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(decoded, Mapping):
        raise PaymentSecurityError("Payment payload must be an object")
    _validate_payment_envelope_shape(decoded)
    accepted = _validate_requirement(decoded.get("accepted"), "Accepted")
    payload = _validate_scheme_payload(decoded.get("payload"), accepted)
    network = str(accepted["network"])
    if network.startswith("eip155:"):
        authorization = payload["authorization"]
        proof_material: dict[str, Any] = {
            "from": str(authorization["from"]).lower(),
            "to": str(authorization["to"]).lower(),
            "value": str(int(authorization["value"])),
            "validAfter": str(int(authorization["validAfter"])),
            "validBefore": str(int(authorization["validBefore"])),
            "nonce": str(authorization["nonce"]).lower(),
        }
        asset = str(accepted["asset"]).lower()
    else:
        transaction_bytes = base64.b64decode(str(payload["transaction"]), validate=True)
        proof_material = {
            "transactionSha256": hashlib.sha256(transaction_bytes).hexdigest()
        }
        asset = str(accepted["asset"])
    return {
        "scheme": accepted["scheme"],
        "network": network,
        "asset": asset,
        "proof": proof_material,
    }


def _validate_requirement(requirement: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(requirement, Mapping):
        raise PaymentSecurityError(f"{label} payment requirement is malformed")
    _require_exact_keys(
        requirement,
        allowed=_PAYMENT_REQUIREMENT_KEYS,
        required=_PAYMENT_REQUIREMENT_KEYS,
        label=f"{label} payment requirement",
    )
    result = copy.deepcopy(dict(requirement))
    for field_name in ("scheme", "network", "asset", "payTo"):
        _require_nonempty_string(result.get(field_name), field_name)

    amount = result.get("amount")
    if not isinstance(amount, str) or not _ATOMIC_AMOUNT_RE.fullmatch(amount):
        raise PaymentSecurityError(
            "Payment requirement amount must be a positive atomic-unit string"
        )

    timeout = result.get("maxTimeoutSeconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise PaymentSecurityError("Payment requirement maxTimeoutSeconds must be positive")

    extra = result.get("extra")
    if not isinstance(extra, Mapping):
        raise PaymentSecurityError("Payment requirement extra must be an object")
    _require_nonempty_string(extra.get("resource"), "extra.resource")
    return result


def _invoke_sdk_parser(payload: dict[str, Any]) -> None:
    parser = _SDK_PARSE_PAYMENT_PAYLOAD
    if parser is None:
        raise PaymentSecurityError("The official x402 SDK is required for payment validation")
    try:
        parsed = parser(payload)
    except Exception as exc:
        # SDK validation errors may embed authorization material; never surface them.
        raise PaymentSecurityError("The x402 SDK rejected the payment payload") from exc
    parsed_version = getattr(parsed, "x402_version", None)
    if isinstance(parsed, Mapping):
        parsed_version = parsed.get("x402Version", parsed_version)
    if parsed_version != 2:
        raise PaymentSecurityError("The x402 SDK did not return a v2 payment payload")


def _assert_requirement_match(
    accepted: Mapping[str, Any],
    expected: Mapping[str, Any],
    resource_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    accepted_copy = _validate_requirement(accepted, "Accepted")
    expected_copy = _validate_requirement(expected, "Expected")
    for field_name in _REQUIREMENT_FIELDS:
        if accepted_copy[field_name] != expected_copy[field_name]:
            raise PaymentSecurityError(f"Accepted payment requirement {field_name} does not match")

    expected_resource = canonicalize_resource_url(resource_url)
    accepted_resource = accepted_copy["extra"]["resource"]
    configured_resource = expected_copy["extra"]["resource"]
    if accepted_resource != expected_resource or configured_resource != expected_resource:
        raise PaymentSecurityError("Accepted payment resource does not match the request")
    if accepted_copy["extra"] != expected_copy["extra"]:
        raise PaymentSecurityError("Accepted payment requirement extra does not match")
    return accepted_copy, expected_copy


def parse_payment_signature(
    header_value: str,
    *,
    accepted_requirement: Mapping[str, Any],
    method: str,
    resource_url: str,
    body: bytes | bytearray | memoryview = b"",
) -> ParsedPayment:
    """Parse and bind an official x402 v2 ``PAYMENT-SIGNATURE`` value.

    The payload's embedded ``accepted`` fields must exactly match the selected
    server requirement.  Both ``payload.resource.url`` and
    ``payload.accepted.extra.resource`` must equal the canonical paid request
    URL.  Amounts are positive base-10 integer strings in atomic units; decimal
    and scientific forms are rejected even when numerically equivalent.

    Raises ``PaymentSecurityError`` on every failure, including a missing SDK.
    """

    decoded = _decode_payment_header(header_value)
    _validate_payment_envelope_shape(decoded)
    version = decoded.get("x402Version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PaymentSecurityError("x402Version must be the integer 2")
    if version != 2:
        raise PaymentSecurityError("Legacy x402 payment payloads are disabled")

    _invoke_sdk_parser(decoded)
    accepted = decoded.get("accepted")
    if not isinstance(accepted, Mapping):
        raise PaymentSecurityError("x402 v2 payload is missing accepted requirements")

    canonical_resource = canonicalize_resource_url(resource_url)
    accepted_copy, _ = _assert_requirement_match(
        accepted,
        accepted_requirement,
        canonical_resource,
    )
    _validate_scheme_payload(decoded.get("payload"), accepted_copy)
    payload_resource = decoded.get("resource")
    if not isinstance(payload_resource, Mapping):
        raise PaymentSecurityError("x402 v2 payload is missing resource metadata")
    if payload_resource.get("url") != canonical_resource:
        raise PaymentSecurityError("Payment payload resource URL does not match the request")

    raw_body = _body_bytes(body)
    return ParsedPayment(
        payment_id=compute_payment_id(decoded),
        request_binding=compute_request_binding(method, canonical_resource, raw_body),
        body_sha256=hashlib.sha256(raw_body).hexdigest(),
        resource_url=canonical_resource,
        payload=copy.deepcopy(decoded),
        accepted=accepted_copy,
    )


def _safe_public_string(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if _CONTROL_CHAR_RE.search(value):
        return None
    return value


def _base58_bytes(value: str) -> bytes | None:
    number = 0
    try:
        for character in value:
            number = (number * 58) + _BASE58_INDEX[character]
    except KeyError:
        return None
    encoded = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    return (b"\0" * (len(value) - len(value.lstrip("1")))) + encoded


def _safe_payer(value: Any, network: str) -> str | None:
    payer = _safe_public_string(value, maximum=256)
    if payer is None:
        return None
    if (
        network.startswith("eip155:")
        and _EVM_PUBLIC_ID_RE.fullmatch(payer)
        and int(payer, 16) != 0
    ):
        return payer
    if network.startswith("solana:") and _SOLANA_PUBLIC_ID_RE.fullmatch(payer):
        decoded = _base58_bytes(payer)
        if decoded is not None and len(decoded) == 32 and any(decoded):
            return payer
    return None


def _safe_transaction(value: Any, network: str) -> str | None:
    transaction = _safe_public_string(value, maximum=256)
    if transaction is None:
        return None
    if (
        network.startswith("eip155:")
        and _EVM_TRANSACTION_RE.fullmatch(transaction)
        and int(transaction, 16) != 0
    ):
        return transaction
    if network.startswith("solana:") and _SOLANA_TRANSACTION_RE.fullmatch(transaction):
        decoded = _base58_bytes(transaction)
        if decoded is not None and len(decoded) == 64 and any(decoded):
            return transaction
    return None


def _sanitize_verify_response(value: Any, *, expected_network: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("isValid"), bool):
        return {"isValid": False, "invalidReason": "malformed_facilitator_response"}
    if value["isValid"] is not True:
        return {"isValid": False, "invalidReason": "payment_invalid"}
    result: dict[str, Any] = {"isValid": True}
    payer = _safe_payer(value.get("payer"), expected_network)
    if payer is not None:
        result["payer"] = payer
    return result


def _sanitize_settle_response(value: Any, *, expected_network: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("success"), bool):
        return {"success": False, "errorReason": "malformed_facilitator_response"}
    if value["success"] is not True:
        return {"success": False, "errorReason": "settlement_failed"}

    network = _safe_public_string(value.get("network"), maximum=128)
    transaction = _safe_transaction(value.get("transaction"), expected_network)
    if transaction is None or network is None:
        return {"success": False, "errorReason": "malformed_facilitator_response"}
    result: dict[str, Any] = {
        "success": True,
        "transaction": transaction,
        "network": network,
    }
    payer = _safe_payer(value.get("payer"), expected_network)
    if payer is not None:
        result["payer"] = payer
    amount = value.get("amount")
    if amount is not None:
        if not isinstance(amount, str) or not _ATOMIC_AMOUNT_RE.fullmatch(amount):
            return {"success": False, "errorReason": "malformed_facilitator_response"}
        result["amount"] = amount
    return result


def _sanitize_supported_response(value: Any) -> dict[str, Any]:
    """Validate `/supported` with the official schema and retain safe fields only."""
    schema = _SDK_SUPPORTED_RESPONSE
    if schema is None:
        return {
            "checked": True,
            "available": False,
            "reason": "x402_sdk_unavailable",
            "kinds": [],
        }
    try:
        parsed = schema.model_validate(value)
    except Exception:
        return {
            "checked": True,
            "available": False,
            "reason": "malformed_facilitator_response",
            "kinds": [],
        }

    kinds: list[dict[str, Any]] = []
    for kind in parsed.kinds:
        network = _safe_public_string(str(kind.network), maximum=128)
        scheme_name = _safe_public_string(kind.scheme, maximum=64)
        version = kind.x402_version
        if network is None or scheme_name is None or version != 2:
            continue
        safe_extra: dict[str, str] = {}
        raw_extra = kind.extra or {}
        fee_payer = _safe_payer(raw_extra.get("feePayer"), str(kind.network))
        if fee_payer is not None:
            safe_extra["feePayer"] = fee_payer
        kinds.append(
            {
                "x402Version": 2,
                "scheme": scheme_name,
                "network": network,
                "extra": safe_extra,
            }
        )

    extensions = [
        extension
        for extension in parsed.extensions
        if _safe_public_string(extension, maximum=128) is not None
    ]
    return {
        "checked": True,
        "available": True,
        "reason": None,
        "kinds": kinds,
        "extensions": extensions,
    }


def _facilitator_url_problem(url: str | None, *, production: bool) -> str | None:
    if not isinstance(url, str) or not url:
        return "facilitator_missing"
    if (
        len(url) > 4096
        or _CONTROL_CHAR_RE.search(url)
        or "\\" in url
        or any(char.isspace() for char in url)
    ):
        return "facilitator_url_malformed"
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return "facilitator_url_malformed"
    if parsed.scheme.lower() != "https":
        return "facilitator_https_required"
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return "facilitator_url_malformed"
    if parsed.query or parsed.fragment:
        return "facilitator_url_malformed"

    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return "facilitator_private_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return "facilitator_private_host"
    if production and host in PUBLIC_DEVELOPMENT_FACILITATOR_HOSTS:
        return "facilitator_public_development_only"
    return None


class FacilitatorAdapter:
    """Minimal async x402 facilitator client with injectable endpoint calls.

    ``post`` receives the same keyword arguments as ``httpx.AsyncClient.post``.
    Supplying it makes unit and integration tests network-free. Static bearer
    credentials remain available for non-CDP facilitators. Coinbase's hosted
    facilitator instead receives a fresh official CDP JWT bound to the exact
    HTTP method, host, and operation path on every request. Credentials are
    excluded from representations, errors, and returned dictionaries.
    """

    __slots__ = (
        "_base_url",
        "_bearer_token",
        "_cdp_api_key_id",
        "_cdp_api_key_secret",
        "_get",
        "_post",
        "_timeout_seconds",
    )

    def __init__(
        self,
        facilitator_url: str,
        *,
        bearer_token: str | None = None,
        cdp_api_key_id: str | None = None,
        cdp_api_key_secret: str | None = None,
        timeout_seconds: float = 10.0,
        post: PostCallable | None = None,
        get: GetCallable | None = None,
        production: bool = False,
    ) -> None:
        problem = _facilitator_url_problem(facilitator_url, production=production)
        if problem is not None:
            raise PaymentSecurityError("Facilitator URL is not safe for this environment")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise PaymentSecurityError("Facilitator timeout must be positive")
        if bearer_token is not None and (
            not isinstance(bearer_token, str)
            or not bearer_token
            or len(bearer_token) > 8192
            or _CONTROL_CHAR_RE.search(bearer_token)
        ):
            raise PaymentSecurityError("Facilitator bearer credential is malformed")
        has_cdp_id = isinstance(cdp_api_key_id, str) and bool(cdp_api_key_id)
        has_cdp_secret = isinstance(cdp_api_key_secret, str) and bool(cdp_api_key_secret)
        if has_cdp_id != has_cdp_secret:
            raise PaymentSecurityError("CDP facilitator credentials are incomplete")
        if cdp_api_key_id is not None and not has_cdp_id:
            raise PaymentSecurityError("CDP API key ID is malformed")
        if cdp_api_key_secret is not None and not has_cdp_secret:
            raise PaymentSecurityError("CDP API key secret is malformed")
        if has_cdp_id and (
            len(cdp_api_key_id) > 2048 or _CONTROL_CHAR_RE.search(cdp_api_key_id)
        ):
            raise PaymentSecurityError("CDP API key ID is malformed")
        if has_cdp_secret and (
            len(cdp_api_key_secret) > 32_768
            or re.search(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]", cdp_api_key_secret)
        ):
            raise PaymentSecurityError("CDP API key secret is malformed")

        parsed_facilitator = urlsplit(facilitator_url)
        host = parsed_facilitator.hostname or ""
        using_cdp = host.lower().rstrip(".") == CDP_FACILITATOR_HOST
        canonical_cdp_url = (
            parsed_facilitator.scheme == "https"
            and parsed_facilitator.netloc == CDP_FACILITATOR_HOST
            and parsed_facilitator.path.rstrip("/") == CDP_FACILITATOR_PATH
        )
        if using_cdp and not canonical_cdp_url:
            raise PaymentSecurityError("CDP facilitator URL is not canonical")
        if using_cdp and bearer_token is not None:
            raise PaymentSecurityError("Static bearer tokens are not supported by CDP")
        if using_cdp and not (has_cdp_id and has_cdp_secret):
            raise PaymentSecurityError("CDP facilitator credentials are required")
        if not using_cdp and (has_cdp_id or has_cdp_secret):
            raise PaymentSecurityError("CDP credentials cannot be sent to a non-CDP host")
        if using_cdp and not cdp_auth_available():
            raise PaymentSecurityError("CDP authentication SDK is unavailable")
        self._base_url = facilitator_url.rstrip("/")
        self._bearer_token = bearer_token
        self._cdp_api_key_id = cdp_api_key_id if has_cdp_id else None
        self._cdp_api_key_secret = cdp_api_key_secret if has_cdp_secret else None
        self._timeout_seconds = float(timeout_seconds)
        self._post = post
        self._get = get

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured=True)"

    def _headers(self, operation: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        elif self._cdp_api_key_id is not None and self._cdp_api_key_secret is not None:
            method = "GET" if operation == "supported" else "POST"
            parsed = urlsplit(self._base_url)
            path = f"{parsed.path.rstrip('/')}/{operation}"
            try:
                options = _CDP_JWT_OPTIONS(
                    api_key_id=self._cdp_api_key_id,
                    api_key_secret=self._cdp_api_key_secret,
                    request_method=method,
                    request_host=parsed.netloc,
                    request_path=path,
                )
                jwt_token = _CDP_GENERATE_JWT(options)
            except Exception as exc:
                raise PaymentSecurityError("CDP facilitator authentication failed") from exc
            if (
                not isinstance(jwt_token, str)
                or not jwt_token
                or len(jwt_token) > 8192
                or _CONTROL_CHAR_RE.search(jwt_token)
            ):
                raise PaymentSecurityError("CDP facilitator authentication failed")
            headers["Authorization"] = f"Bearer {jwt_token}"
        return headers

    async def _call(self, operation: str, request_json: dict[str, Any]) -> Any:
        endpoint = f"{self._base_url}/{operation}"
        if self._post is not None:
            response = await self._post(
                endpoint,
                json=request_json,
                headers=self._headers(operation),
                timeout=self._timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    endpoint,
                    json=request_json,
                    headers=self._headers(operation),
                )

        if isinstance(response, Mapping):
            return dict(response)
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and len(content) > MAX_FACILITATOR_RESPONSE_BYTES:
            raise PaymentSecurityError("Facilitator response is too large")
        response.raise_for_status()
        return response.json()

    async def _call_supported(self) -> Any:
        endpoint = f"{self._base_url}/supported"
        if self._get is not None:
            response = await self._get(
                endpoint,
                headers=self._headers("supported"),
                timeout=self._timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(endpoint, headers=self._headers("supported"))

        if isinstance(response, Mapping):
            return dict(response)
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and len(content) > MAX_FACILITATOR_RESPONSE_BYTES:
            raise PaymentSecurityError("Facilitator response is too large")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _request(payment: ParsedPayment, requirement: Mapping[str, Any]) -> dict[str, Any]:
        if payment.x402_version != 2 or payment.payload.get("x402Version") != 2:
            raise PaymentSecurityError("Parsed payment version changed unexpectedly")
        if compute_payment_id(payment.payload) != payment.payment_id:
            raise PaymentSecurityError("Parsed payment payload changed unexpectedly")
        _invoke_sdk_parser(payment.payload)
        payload_accepted = payment.payload.get("accepted")
        payload_resource = payment.payload.get("resource")
        if not isinstance(payload_accepted, Mapping) or not isinstance(payload_resource, Mapping):
            raise PaymentSecurityError("Parsed payment payload changed unexpectedly")
        if payload_resource.get("url") != payment.resource_url:
            raise PaymentSecurityError("Parsed payment resource changed unexpectedly")
        accepted, expected = _assert_requirement_match(
            payload_accepted,
            requirement,
            payment.resource_url,
        )
        if accepted != payment.accepted:
            raise PaymentSecurityError("Parsed payment requirement changed unexpectedly")
        return {
            "x402Version": 2,
            "paymentPayload": copy.deepcopy(payment.payload),
            "paymentRequirements": expected,
        }

    async def verify(
        self,
        payment: ParsedPayment,
        requirement: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Call ``/verify`` and return a sanitized fail-closed plain dict."""

        if not sdk_available():
            return {"isValid": False, "invalidReason": "x402_sdk_unavailable"}
        try:
            request_json = self._request(payment, requirement)
        except PaymentSecurityError:
            return {"isValid": False, "invalidReason": "payment_requirement_mismatch"}
        try:
            response = await self._call("verify", request_json)
        except Exception:
            return {"isValid": False, "invalidReason": "facilitator_unavailable"}
        return _sanitize_verify_response(
            response,
            expected_network=str(payment.accepted["network"]),
        )

    async def supported(self) -> dict[str, Any]:
        """Return a sanitized, schema-validated facilitator capability snapshot."""
        if not sdk_available() or _SDK_SUPPORTED_RESPONSE is None:
            return {
                "checked": True,
                "available": False,
                "reason": "x402_sdk_unavailable",
                "kinds": [],
            }
        try:
            response = await self._call_supported()
        except Exception:
            return {
                "checked": True,
                "available": False,
                "reason": "facilitator_unavailable",
                "kinds": [],
            }
        return _sanitize_supported_response(response)

    async def settle(
        self,
        payment: ParsedPayment,
        requirement: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Call ``/settle`` and return a sanitized fail-closed plain dict."""

        if not sdk_available():
            return {"success": False, "errorReason": "x402_sdk_unavailable"}
        try:
            request_json = self._request(payment, requirement)
        except PaymentSecurityError:
            return {"success": False, "errorReason": "payment_requirement_mismatch"}
        try:
            response = await self._call("settle", request_json)
        except Exception:
            return {
                "success": False,
                "errorReason": "facilitator_unavailable",
                "outcomeUnknown": True,
            }
        result = _sanitize_settle_response(
            response,
            expected_network=str(payment.accepted["network"]),
        )
        if result.get("success") is not True:
            if result.get("errorReason") == "malformed_facilitator_response":
                result["outcomeUnknown"] = True
            return result
        if result.get("network") != payment.accepted["network"]:
            return {
                "success": False,
                "errorReason": "settlement_network_mismatch",
                "outcomeUnknown": True,
            }
        amount = result.get("amount")
        if amount is not None and amount != payment.accepted["amount"]:
            return {
                "success": False,
                "errorReason": "settlement_amount_mismatch",
                "outcomeUnknown": True,
            }
        return result


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_int(name: str) -> int:
    try:
        value = int(os.environ.get(name, "0"))
    except ValueError:
        return 0
    return value


def _sequence(value: Sequence[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _production_environment() -> bool:
    return is_production_environment()


def _railway_environment() -> bool:
    return any(
        os.environ.get(name)
        for name in (
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
        )
    )


def _trusted_proxy_problems(value: Sequence[str] | str | None) -> list[str]:
    entries = _sequence(value)
    if not entries:
        return ["trusted_proxies_missing"]
    problems: list[str] = []
    for entry in entries:
        if entry == "*":
            problems.append("trusted_proxies_wildcard")
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            problems.append("trusted_proxies_malformed")
            continue
        if network.prefixlen == 0:
            problems.append("trusted_proxies_wildcard")
    return problems


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _credit_db_problem(path_value: str | os.PathLike[str] | None, railway: bool) -> str | None:
    if path_value is None or not str(path_value).strip():
        return "credit_db_missing"
    raw = Path(str(path_value))
    if not raw.is_absolute():
        return "credit_db_not_durable"
    resolved = raw.resolve(strict=False)
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(strict=False),
        Path("/tmp").resolve(strict=False),
        Path("/private/tmp").resolve(strict=False),
        Path("/var/tmp").resolve(strict=False),
    }
    if any(_path_is_within(resolved, root) for root in temp_roots):
        return "credit_db_not_durable"
    if railway and not _path_is_within(resolved, Path("/data")):
        return "credit_db_not_on_railway_volume"
    return None


def payment_security_status(
    *,
    production: bool | None = None,
    railway_hosted: bool | None = None,
    sdk_present: bool | None = None,
    cdp_auth_sdk_present: bool | None = None,
    facilitator_url: str | None = None,
    facilitator_bearer_configured: bool | None = None,
    cdp_api_key_id_configured: bool | None = None,
    cdp_api_key_secret_configured: bool | None = None,
    mock_enabled: bool | None = None,
    legacy_enabled: bool | None = None,
    networks: Sequence[str] | str | None = None,
    trusted_proxies: Sequence[str] | str | None = None,
    freshness_seconds: int | None = None,
    finality_confirmations: int | None = None,
    verification_lease_seconds: int | None = None,
    replay_ttl_seconds: int | None = None,
    replay_max_entries: int | None = None,
    credit_db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a non-secret production-readiness assessment for x402 payments.

    Every input is injectable.  ``None`` values read their documented
    environment fallback.  Local development remains runnable, but
    ``production_ready`` is false whenever a safeguard is missing; in a
    production environment ``ready`` is also false.
    """

    is_production = _production_environment() if production is None else bool(production)
    is_railway = _railway_environment() if railway_hosted is None else bool(railway_hosted)
    has_sdk = sdk_available() if sdk_present is None else bool(sdk_present)
    has_cdp_auth_sdk = (
        cdp_auth_available() if cdp_auth_sdk_present is None else bool(cdp_auth_sdk_present)
    )
    facilitator = facilitator_url
    if facilitator is None:
        facilitator = os.environ.get("X402_FACILITATOR_URL")
    has_bearer = (
        bool(os.environ.get("X402_FACILITATOR_BEARER_TOKEN"))
        if facilitator_bearer_configured is None
        else bool(facilitator_bearer_configured)
    )
    has_cdp_id = (
        bool(os.environ.get("CDP_API_KEY_ID"))
        if cdp_api_key_id_configured is None
        else bool(cdp_api_key_id_configured)
    )
    has_cdp_secret = (
        bool(os.environ.get("CDP_API_KEY_SECRET"))
        if cdp_api_key_secret_configured is None
        else bool(cdp_api_key_secret_configured)
    )
    mocks = _env_bool("X402_ALLOW_MOCK_PAYMENTS") if mock_enabled is None else bool(mock_enabled)
    legacy = (
        _env_bool("X402_ALLOW_LEGACY_PAYMENTS") if legacy_enabled is None else bool(legacy_enabled)
    )
    configured_networks = _sequence(networks)
    if networks is None:
        configured_networks = [
            item
            for item in (
                os.environ.get("X402_SOLANA_NETWORK", ""),
                os.environ.get("X402_BASE_NETWORK", ""),
            )
            if item
        ]
    proxy_value = (
        os.environ.get("FORWARDED_ALLOW_IPS", "") if trusted_proxies is None else trusted_proxies
    )
    freshness = (
        _env_positive_int("X402_PAYMENT_MAX_AGE_SECONDS")
        if freshness_seconds is None
        else freshness_seconds
    )
    finality = (
        _env_positive_int("X402_PAYMENT_MIN_CONFIRMATIONS")
        if finality_confirmations is None
        else finality_confirmations
    )
    lease = (
        _env_positive_int("X402_PAYMENT_VERIFICATION_LEASE_SECONDS")
        if verification_lease_seconds is None
        else verification_lease_seconds
    )
    replay_ttl = (
        _env_positive_int("X402_PAYMENT_REPLAY_TTL_SECONDS")
        if replay_ttl_seconds is None
        else replay_ttl_seconds
    )
    replay_entries = (
        _env_positive_int("X402_PAYMENT_REPLAY_MAX_ENTRIES")
        if replay_max_entries is None
        else replay_max_entries
    )
    db_path = os.environ.get("CREDIT_DB_PATH") if credit_db_path is None else credit_db_path

    blockers: list[str] = []
    if not has_sdk:
        blockers.append("x402_sdk_missing")
    facilitator_problem = _facilitator_url_problem(facilitator, production=is_production)
    if facilitator_problem is not None:
        blockers.append(facilitator_problem)
    facilitator_host = None
    if facilitator_problem is None and facilitator:
        facilitator_host = (urlsplit(facilitator).hostname or "").lower().rstrip(".")
    uses_cdp = facilitator_host == CDP_FACILITATOR_HOST
    canonical_cdp_url = False
    if uses_cdp and facilitator:
        parsed_facilitator = urlsplit(facilitator)
        canonical_cdp_url = (
            parsed_facilitator.scheme == "https"
            and parsed_facilitator.netloc == CDP_FACILITATOR_HOST
            and parsed_facilitator.path.rstrip("/") == CDP_FACILITATOR_PATH
        )
        if not canonical_cdp_url:
            blockers.append("cdp_facilitator_url_noncanonical")
    if has_cdp_id != has_cdp_secret:
        blockers.append("cdp_credentials_incomplete")
    if uses_cdp:
        if has_bearer:
            blockers.append("cdp_static_bearer_unsupported")
        if not (has_cdp_id and has_cdp_secret):
            blockers.append("cdp_credentials_missing")
        if not has_cdp_auth_sdk:
            blockers.append("cdp_auth_sdk_missing")
    elif has_cdp_id or has_cdp_secret:
        blockers.append("cdp_credentials_host_mismatch")
    if mocks:
        blockers.append("mock_payments_enabled")
    if legacy:
        blockers.append("legacy_payments_enabled")
    if not configured_networks:
        blockers.append("payment_networks_missing")
    elif any(network not in MAINNET_NETWORKS for network in configured_networks):
        blockers.append("unsupported_mainnet_network")
    blockers.extend(_trusted_proxy_problems(proxy_value))
    if isinstance(freshness, bool) or not isinstance(freshness, int) or freshness <= 0:
        blockers.append("payment_freshness_nonpositive")
    if isinstance(finality, bool) or not isinstance(finality, int) or finality <= 0:
        blockers.append("payment_finality_nonpositive")
    if isinstance(lease, bool) or not isinstance(lease, int) or lease <= 0:
        blockers.append("payment_verification_lease_nonpositive")
    if isinstance(replay_ttl, bool) or not isinstance(replay_ttl, int) or replay_ttl <= 0:
        blockers.append("payment_replay_ttl_nonpositive")
    elif replay_ttl > MAX_PAYMENT_REPLAY_TTL_SECONDS:
        blockers.append("payment_replay_ttl_too_large")
    if (
        isinstance(replay_entries, bool)
        or not isinstance(replay_entries, int)
        or replay_entries <= 0
    ):
        blockers.append("payment_replay_entries_nonpositive")
    elif replay_entries > MAX_PAYMENT_REPLAY_ENTRIES:
        blockers.append("payment_replay_entries_too_large")
    db_problem = _credit_db_problem(db_path, is_railway)
    if db_problem is not None:
        blockers.append(db_problem)

    unique_blockers = sorted(set(blockers))
    production_ready = not unique_blockers
    return {
        "ready": production_ready if is_production else True,
        "production_ready": production_ready,
        "production": is_production,
        "railway_hosted": is_railway,
        "sdk": {"available": has_sdk, "version": _SDK_VERSION if has_sdk else None},
        "facilitator": {
            "configured": bool(facilitator),
            "safe": facilitator_problem is None,
            "host": facilitator_host,
            "authentication": (
                "cdp_jwt"
                if uses_cdp and has_cdp_id and has_cdp_secret and not has_bearer
                else "bearer"
                if has_bearer
                else "none"
            ),
            "cdp_auth_sdk": {
                "available": has_cdp_auth_sdk,
                "version": _CDP_SDK_VERSION if has_cdp_auth_sdk else None,
            },
        },
        "modes": {"mock_enabled": mocks, "legacy_enabled": legacy},
        "networks": sorted(set(configured_networks)),
        "controls": {
            "freshness_seconds": freshness,
            "finality_confirmations": finality,
            "verification_lease_seconds": lease,
            "replay_ttl_seconds": replay_ttl,
            "replay_max_entries": replay_entries,
            "replay_hard_limits": {
                "ttl_seconds": MAX_PAYMENT_REPLAY_TTL_SECONDS,
                "entries": MAX_PAYMENT_REPLAY_ENTRIES,
            },
            "credit_db_durable": db_problem is None,
        },
        "blockers": unique_blockers,
    }


__all__ = [
    "FacilitatorAdapter",
    "MAINNET_NETWORKS",
    "ParsedPayment",
    "PaymentSecurityError",
    "canonicalize_resource_url",
    "cdp_auth_available",
    "compute_payment_id",
    "compute_request_binding",
    "parse_payment_signature",
    "payment_security_status",
    "sdk_available",
]
