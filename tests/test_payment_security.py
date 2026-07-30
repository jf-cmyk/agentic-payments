"""Adversarial tests for the isolated x402 v2 security boundary."""

from __future__ import annotations

import base64
import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src import payment_security
from src.payment_security import (
    FacilitatorAdapter,
    PaymentSecurityError,
    canonicalize_resource_url,
    compute_request_binding,
    parse_payment_signature,
    payment_security_status,
)


RESOURCE_URL = "https://mcp.blocksize.info/v1/vwap/BTC-USD?window=latest"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x1111111111111111111111111111111111111111"


def _requirement() -> dict[str, Any]:
    return {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": BASE_USDC,
        "amount": "2000",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {
            "resource": RESOURCE_URL,
            "name": "USD Coin",
            "version": "2",
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "x402Version": 2,
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0x2222222222222222222222222222222222222222",
                "to": PAY_TO,
                "value": "2000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "12" * 32,
            },
        },
        "accepted": _requirement(),
        "resource": {"url": RESOURCE_URL},
    }


def _header(payload: dict[str, Any], *, pretty: bool = False) -> str:
    raw = json.dumps(payload, indent=2 if pretty else None).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture(autouse=True)
def fake_x402_sdk(monkeypatch):
    calls: list[dict[str, Any]] = []

    def parse(value: dict[str, Any]):
        calls.append(value)
        return SimpleNamespace(x402_version=value.get("x402Version"))

    monkeypatch.setattr(payment_security, "_SDK_PARSE_PAYMENT_PAYLOAD", parse)
    return calls


def _parse(
    payload: dict[str, Any] | None = None,
    requirement: dict[str, Any] | None = None,
    *,
    body: bytes = b"",
):
    return parse_payment_signature(
        _header(payload or _payload()),
        accepted_requirement=requirement or _requirement(),
        method="GET",
        resource_url=RESOURCE_URL,
        body=body,
    )


def test_valid_v2_payload_is_sdk_validated_and_request_bound(fake_x402_sdk):
    parsed = _parse(body=b'{"query":"btc"}')

    assert fake_x402_sdk == [_payload()]
    assert parsed.x402_version == 2
    assert len(parsed.payment_id) == 64
    assert len(parsed.request_binding) == 64
    assert parsed.resource_url == RESOURCE_URL
    assert parsed.accepted == _requirement()
    assert parsed.body_sha256 != ""


def test_parsed_payment_repr_redacts_authorization_and_requirements():
    parsed = _parse()
    rendered = repr(parsed)

    assert "payload=" not in rendered
    assert "accepted=" not in rendered
    assert "authorization" not in rendered
    assert BASE_USDC not in rendered


def test_real_x402_parser_contract_when_optional_sdk_is_installed(monkeypatch):
    schemas = pytest.importorskip("x402.schemas")
    monkeypatch.setattr(
        payment_security,
        "_SDK_PARSE_PAYMENT_PAYLOAD",
        schemas.parse_payment_payload,
    )

    parsed = _parse()

    assert parsed.x402_version == 2
    assert parsed.accepted == _requirement()


def test_payment_id_is_canonical_across_json_whitespace_and_key_order():
    payload = _payload()
    reversed_payload = dict(reversed(list(payload.items())))

    first = parse_payment_signature(
        _header(payload, pretty=True),
        accepted_requirement=_requirement(),
        method="GET",
        resource_url=RESOURCE_URL,
    )
    second = parse_payment_signature(
        _header(reversed_payload),
        accepted_requirement=_requirement(),
        method="get",
        resource_url=RESOURCE_URL,
    )

    assert first.payment_id == second.payment_id
    assert first.request_binding == second.request_binding


def test_request_binding_changes_with_method_url_or_raw_body():
    baseline = compute_request_binding("POST", RESOURCE_URL, b'{"a":1}')

    assert compute_request_binding("GET", RESOURCE_URL, b'{"a":1}') != baseline
    assert compute_request_binding("POST", RESOURCE_URL + "&extra=1", b'{"a":1}') != baseline
    assert compute_request_binding("POST", RESOURCE_URL, b'{"a":1 }') != baseline


def test_resource_url_canonicalizes_host_and_default_port_only():
    assert (
        canonicalize_resource_url("https://MCP.BLOCKSIZE.INFO:443/v1/vwap/BTC-USD?window=latest")
        == RESOURCE_URL
    )
    parsed = parse_payment_signature(
        _header(_payload()),
        accepted_requirement=_requirement(),
        method="GET",
        resource_url="https://MCP.BLOCKSIZE.INFO:443/v1/vwap/BTC-USD?window=latest",
    )
    assert parsed.resource_url == RESOURCE_URL


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("scheme", "upto"),
        ("network", "eip155:1"),
        ("asset", "0x2222222222222222222222222222222222222222"),
        ("amount", "2001"),
        ("payTo", "0x3333333333333333333333333333333333333333"),
        ("maxTimeoutSeconds", 61),
    ],
)
def test_every_accepted_requirement_field_must_match_exactly(field, replacement):
    payload = _payload()
    payload["accepted"][field] = replacement

    with pytest.raises(PaymentSecurityError, match=field):
        _parse(payload)


@pytest.mark.parametrize("amount", ["0", "02000", "2000.0", "2e3", "-2000", 2000])
def test_atomic_amount_rejects_zero_decimal_scientific_signed_and_non_string(amount):
    payload = _payload()
    requirement = _requirement()
    payload["accepted"]["amount"] = amount
    requirement["amount"] = amount

    with pytest.raises(PaymentSecurityError, match="atomic-unit string"):
        _parse(payload, requirement)


def test_payload_resource_url_is_mandatory_and_exact():
    missing = _payload()
    del missing["resource"]
    with pytest.raises(PaymentSecurityError, match="missing resource"):
        _parse(missing)

    wrong = _payload()
    wrong["resource"]["url"] = "https://mcp.blocksize.info/v1/vwap/ETH-USD"
    with pytest.raises(PaymentSecurityError, match="resource URL"):
        _parse(wrong)


def test_payment_id_ignores_unsigned_resource_presentation_and_extensions():
    original = _payload()
    original["resource"].update(
        {"description": "Original description", "mimeType": "application/json"}
    )
    original["extensions"] = {"bazaar": {"info": {"discoverable": True}}}
    changed = copy.deepcopy(original)
    changed["resource"]["description"] = "Caller-selected description"
    changed["extensions"] = {"bazaar": {"info": {"discoverable": False}}}

    assert _parse(original).payment_id == _parse(changed).payment_id


def test_eip3009_payment_id_canonicalizes_equivalent_authorization_encodings(
    monkeypatch,
):
    from x402.schemas import parse_payment_payload

    monkeypatch.setattr(payment_security, "_SDK_PARSE_PAYMENT_PAYLOAD", parse_payment_payload)
    original = _payload()
    equivalent = copy.deepcopy(original)
    equivalent["payload"]["authorization"].update(
        {
            "value": "02000",
            "validAfter": "00",
            "nonce": str(original["payload"]["authorization"]["nonce"]).upper().replace(
                "0X", "0x"
            ),
        }
    )

    assert _parse(original).payment_id == _parse(equivalent).payment_id


def test_solana_transaction_encoding_must_be_canonical_base64():
    requirement = _requirement()
    requirement.update(
        {
            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "payTo": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "extra": {
                "resource": RESOURCE_URL,
                "feePayer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        }
    )
    payload = _payload()
    payload["accepted"] = copy.deepcopy(requirement)
    payload["payload"] = {"transaction": "A\nQ=="}

    with pytest.raises(PaymentSecurityError, match="canonical base64"):
        _parse(payload, requirement)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": "ignored-by-sdk"}),
        lambda value: value["resource"].update({"unexpected": "ignored-by-sdk"}),
        lambda value: value["accepted"].update({"unexpected": "ignored-by-sdk"}),
        lambda value: value["payload"].update({"unexpected": "ignored-by-sdk"}),
        lambda value: value["payload"]["authorization"].update(
            {"unexpected": "ignored-by-sdk"}
        ),
    ],
)
def test_unknown_payment_fields_fail_closed_before_sdk_can_ignore_them(mutate):
    payload = _payload()
    mutate(payload)

    with pytest.raises(PaymentSecurityError, match="supported schema"):
        _parse(payload)


def test_accepted_and_expected_extra_resource_are_mandatory_and_exact():
    payload = _payload()
    del payload["accepted"]["extra"]["resource"]
    with pytest.raises(PaymentSecurityError, match="extra.resource"):
        _parse(payload)

    requirement = _requirement()
    requirement["extra"]["resource"] = "https://mcp.blocksize.info/v1/vwap/ETH-USD"
    with pytest.raises(PaymentSecurityError, match="resource does not match"):
        _parse(requirement=requirement)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("name", "Fake USD"),
        ("version", "1"),
        ("feePayer", "11111111111111111111111111111111"),
    ],
)
def test_scheme_specific_extra_fields_must_match_exactly(key, replacement):
    payload = _payload()
    payload["accepted"]["extra"][key] = replacement

    with pytest.raises(PaymentSecurityError, match="extra"):
        _parse(payload)


def test_legacy_and_non_integer_versions_are_rejected_before_sdk(fake_x402_sdk):
    for version in (1, "2", True, None):
        payload = _payload()
        payload["x402Version"] = version
        with pytest.raises(PaymentSecurityError):
            _parse(payload)

    assert fake_x402_sdk == []


def test_missing_sdk_fails_closed(monkeypatch):
    monkeypatch.setattr(payment_security, "_SDK_PARSE_PAYMENT_PAYLOAD", None)

    with pytest.raises(PaymentSecurityError, match="official x402 SDK"):
        _parse()


def test_sdk_error_is_sanitized(monkeypatch):
    secret = "0xsecret-payment-authorization"

    def reject(_value):
        raise RuntimeError(secret)

    monkeypatch.setattr(payment_security, "_SDK_PARSE_PAYMENT_PAYLOAD", reject)
    with pytest.raises(PaymentSecurityError) as exc_info:
        _parse()

    assert secret not in str(exc_info.value)


def test_invalid_base64_duplicate_keys_and_nonstandard_json_fail_closed():
    with pytest.raises(PaymentSecurityError, match="base64"):
        parse_payment_signature(
            "not-base64!",
            accepted_requirement=_requirement(),
            method="GET",
            resource_url=RESOURCE_URL,
        )

    duplicate = base64.b64encode(b'{"x402Version":2,"x402Version":2}').decode()
    with pytest.raises(PaymentSecurityError, match="strict JSON"):
        parse_payment_signature(
            duplicate,
            accepted_requirement=_requirement(),
            method="GET",
            resource_url=RESOURCE_URL,
        )

    nonstandard = base64.b64encode(b'{"x402Version":2,"value":NaN}').decode()
    with pytest.raises(PaymentSecurityError, match="strict JSON"):
        parse_payment_signature(
            nonstandard,
            accepted_requirement=_requirement(),
            method="GET",
            resource_url=RESOURCE_URL,
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mcp.blocksize.info/data",
        "https://user:password@mcp.blocksize.info/data",
        "https://mcp.blocksize.info/data#fragment",
        "https://mcp.blocksize.info:invalid/data",
    ],
)
def test_malformed_or_ambiguous_resource_urls_are_rejected(url):
    with pytest.raises(PaymentSecurityError):
        canonicalize_resource_url(url)


@pytest.mark.asyncio
async def test_facilitator_adapter_calls_injectable_v2_endpoints_without_leaking_bearer():
    parsed = _parse()
    calls: list[tuple[str, dict[str, Any]]] = []
    bearer = "facilitator-secret-token"

    async def post(url: str, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/verify"):
            return {
                "isValid": True,
                "payer": "0x2222222222222222222222222222222222222222",
                "authorization": bearer,
                "extra": {"credential": bearer},
            }
        return {
            "success": True,
            "transaction": "0x" + "34" * 32,
            "network": "eip155:8453",
            "amount": "2000",
            "extra": {"credential": bearer},
        }

    adapter = FacilitatorAdapter(
        "https://facilitator.example/x402/",
        bearer_token=bearer,
        post=post,
    )
    verified = await adapter.verify(parsed, _requirement())
    settled = await adapter.settle(parsed, _requirement())

    assert verified == {
        "isValid": True,
        "payer": "0x2222222222222222222222222222222222222222",
    }
    assert settled == {
        "success": True,
        "transaction": "0x" + "34" * 32,
        "network": "eip155:8453",
        "amount": "2000",
    }
    assert bearer not in repr(adapter)
    assert bearer not in json.dumps(verified)
    assert bearer not in json.dumps(settled)
    assert [call[0] for call in calls] == [
        "https://facilitator.example/x402/verify",
        "https://facilitator.example/x402/settle",
    ]
    assert calls[0][1]["headers"]["Authorization"] == f"Bearer {bearer}"
    assert calls[0][1]["json"] == {
        "x402Version": 2,
        "paymentPayload": parsed.payload,
        "paymentRequirements": _requirement(),
    }


@pytest.mark.asyncio
async def test_facilitator_supported_is_schema_validated_and_sanitized():
    calls: list[tuple[str, dict[str, Any]]] = []
    bearer = "supported-secret-token"

    async def get(url: str, **kwargs):
        calls.append((url, kwargs))
        return {
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "extra": {"private": bearer},
                },
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                    "extra": {
                        "feePayer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                    },
                },
                {
                    "x402Version": 1,
                    "scheme": "exact",
                    "network": "eip155:84532",
                },
            ],
            "extensions": ["bazaar"],
            "signers": {"eip155:*": [bearer]},
        }

    adapter = FacilitatorAdapter(
        "https://facilitator.example",
        bearer_token=bearer,
        get=get,
    )
    supported = await adapter.supported()

    assert supported == {
        "checked": True,
        "available": True,
        "reason": None,
        "kinds": [
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:8453",
                "extra": {},
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                "extra": {
                    "feePayer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                },
            },
        ],
        "extensions": ["bazaar"],
    }
    assert calls[0][0] == "https://facilitator.example/supported"
    assert calls[0][1]["headers"]["Authorization"] == f"Bearer {bearer}"
    assert bearer not in json.dumps(supported)


@pytest.mark.asyncio
async def test_cdp_facilitator_uses_fresh_official_request_bound_jwts():
    pytest.importorskip("cdp.auth")
    private_key = ed25519.Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    secret = base64.b64encode(seed + public_key).decode("ascii")
    key_id = "organizations/test/apiKeys/key-id"
    calls: list[tuple[str, str]] = []

    async def get(url: str, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"]))
        return {"kinds": [], "extensions": []}

    async def post(url: str, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"]))
        if url.endswith("/verify"):
            return {"isValid": True, "payer": "0x" + "22" * 20}
        return {
            "success": True,
            "transaction": "0x" + "34" * 32,
            "network": "eip155:8453",
            "amount": "2000",
        }

    adapter = FacilitatorAdapter(
        "https://api.cdp.coinbase.com/platform/v2/x402",
        cdp_api_key_id=key_id,
        cdp_api_key_secret=secret,
        get=get,
        post=post,
        production=True,
    )
    assert (await adapter.supported())["available"] is True
    assert (await adapter.supported())["available"] is True
    assert (await adapter.verify(_parse(), _requirement()))["isValid"] is True
    assert (await adapter.settle(_parse(), _requirement()))["success"] is True

    expected_uris = [
        "GET api.cdp.coinbase.com/platform/v2/x402/supported",
        "GET api.cdp.coinbase.com/platform/v2/x402/supported",
        "POST api.cdp.coinbase.com/platform/v2/x402/verify",
        "POST api.cdp.coinbase.com/platform/v2/x402/settle",
    ]
    tokens = []
    for (url, authorization), expected_uri in zip(calls, expected_uris, strict=True):
        assert url.endswith(expected_uri.rsplit("/", 1)[-1])
        assert authorization.startswith("Bearer ")
        token = authorization.removeprefix("Bearer ")
        tokens.append(token)
        claims = jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
        )
        assert claims["sub"] == key_id
        assert claims["uris"] == [expected_uri]
        assert jwt.get_unverified_header(token)["kid"] == key_id
    assert len(set(tokens)) == 4
    assert secret not in repr(adapter)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"bearer_token": "short-lived-static-token"},
        {"cdp_api_key_id": "key-id"},
    ],
)
def test_cdp_facilitator_rejects_missing_or_static_credentials(kwargs):
    with pytest.raises(PaymentSecurityError):
        FacilitatorAdapter(
            "https://api.cdp.coinbase.com/platform/v2/x402",
            production=True,
            **kwargs,
        )


def test_cdp_credentials_cannot_be_redirected_to_another_facilitator():
    with pytest.raises(PaymentSecurityError, match="non-CDP host"):
        FacilitatorAdapter(
            "https://facilitator.example",
            cdp_api_key_id="key-id",
            cdp_api_key_secret="secret",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://API.CDP.COINBASE.COM/platform/v2/x402",
        "https://api.cdp.coinbase.com./platform/v2/x402",
        "https://api.cdp.coinbase.com:443/platform/v2/x402",
        "https://api.cdp.coinbase.com/wrong/x402",
    ],
)
def test_cdp_facilitator_requires_canonical_url(url):
    with pytest.raises(PaymentSecurityError, match="not canonical"):
        FacilitatorAdapter(
            url,
            cdp_api_key_id="key-id",
            cdp_api_key_secret="secret",
        )


@pytest.mark.asyncio
async def test_facilitator_supported_fails_closed_on_malformed_response():
    async def get(_url: str, **_kwargs):
        return {"kinds": "not-a-list"}

    adapter = FacilitatorAdapter("https://facilitator.example", get=get)

    assert await adapter.supported() == {
        "checked": True,
        "available": False,
        "reason": "malformed_facilitator_response",
        "kinds": [],
    }


@pytest.mark.asyncio
async def test_facilitator_adapter_fails_closed_without_sdk_or_network_call(monkeypatch):
    parsed = _parse()
    called = False

    async def post(_url: str, **_kwargs):
        nonlocal called
        called = True
        return {"isValid": True}

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    monkeypatch.setattr(payment_security, "_SDK_PARSE_PAYMENT_PAYLOAD", None)

    assert await adapter.verify(parsed, _requirement()) == {
        "isValid": False,
        "invalidReason": "x402_sdk_unavailable",
    }
    assert await adapter.settle(parsed, _requirement()) == {
        "success": False,
        "errorReason": "x402_sdk_unavailable",
    }
    assert called is False


@pytest.mark.asyncio
async def test_facilitator_errors_and_malformed_responses_are_sanitized():
    parsed = _parse()
    secret = "bearer-secret-that-must-not-escape"

    async def failing_post(_url: str, **_kwargs):
        raise RuntimeError(secret)

    unavailable = FacilitatorAdapter(
        "https://facilitator.example",
        bearer_token=secret,
        post=failing_post,
    )
    verify_result = await unavailable.verify(parsed, _requirement())
    settle_result = await unavailable.settle(parsed, _requirement())
    assert verify_result == {
        "isValid": False,
        "invalidReason": "facilitator_unavailable",
    }
    assert settle_result == {
        "success": False,
        "errorReason": "facilitator_unavailable",
        "outcomeUnknown": True,
    }
    assert secret not in json.dumps(verify_result)
    assert secret not in json.dumps(settle_result)

    async def malformed_post(_url: str, **_kwargs):
        return {"isValid": "true", "success": "true", "secret": secret}

    malformed = FacilitatorAdapter("https://facilitator.example", post=malformed_post)
    assert (await malformed.verify(parsed, _requirement()))["isValid"] is False
    assert (await malformed.settle(parsed, _requirement()))["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_patch", "reason"),
    [
        ({"network": "eip155:1"}, "settlement_network_mismatch"),
        ({"amount": "2001"}, "settlement_amount_mismatch"),
    ],
)
async def test_settlement_response_must_match_network_and_amount(response_patch, reason):
    parsed = _parse()
    response = {
        "success": True,
        "transaction": "0x" + "56" * 32,
        "network": "eip155:8453",
        "amount": "2000",
        **response_patch,
    }

    async def post(_url: str, **_kwargs):
        return response

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    assert await adapter.settle(parsed, _requirement()) == {
        "success": False,
        "errorReason": reason,
        "outcomeUnknown": True,
    }


@pytest.mark.asyncio
async def test_base_settlement_rejects_solana_shaped_transaction():
    async def post(_url: str, **_kwargs):
        return {
            "success": True,
            "transaction": "1" * 88,
            "network": "eip155:8453",
            "amount": "2000",
        }

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    assert await adapter.settle(_parse(), _requirement()) == {
        "success": False,
        "errorReason": "malformed_facilitator_response",
        "outcomeUnknown": True,
    }


@pytest.mark.asyncio
async def test_solana_settlement_rejects_evm_shaped_transaction():
    requirement = _requirement()
    requirement.update(
        {
            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "payTo": "11111111111111111111111111111111",
            "extra": {
                "resource": RESOURCE_URL,
                "feePayer": "11111111111111111111111111111111",
            },
        }
    )
    payload = _payload()
    payload["accepted"] = copy.deepcopy(requirement)
    payload["payload"] = {"transaction": "AQ=="}
    parsed = _parse(payload, requirement)

    async def post(_url: str, **_kwargs):
        return {
            "success": True,
            "transaction": "0x" + "56" * 32,
            "network": requirement["network"],
            "amount": "2000",
        }

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    assert await adapter.settle(parsed, requirement) == {
        "success": False,
        "errorReason": "malformed_facilitator_response",
        "outcomeUnknown": True,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://facilitator.example",
        "https://user:password@facilitator.example",
        "https://127.0.0.1/x402",
        "https://facilitator.local/x402",
    ],
)
def test_facilitator_adapter_rejects_unsafe_urls(url):
    with pytest.raises(PaymentSecurityError, match="not safe"):
        FacilitatorAdapter(url)


def _ready_status_kwargs() -> dict[str, Any]:
    return {
        "production": True,
        "railway_hosted": True,
        "sdk_present": True,
        "cdp_auth_sdk_present": True,
        "facilitator_url": "https://api.cdp.coinbase.com/platform/v2/x402",
        "facilitator_bearer_configured": False,
        "cdp_api_key_id_configured": True,
        "cdp_api_key_secret_configured": True,
        "mock_enabled": False,
        "legacy_enabled": False,
        "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"],
        "trusted_proxies": "10.0.0.0/8,127.0.0.1",
        "freshness_seconds": 900,
        "finality_confirmations": 3,
        "verification_lease_seconds": 30,
        "replay_ttl_seconds": 3_600,
        "replay_max_entries": 500,
        "credit_db_path": "/data/credits.db",
    }


def test_payment_security_status_accepts_complete_production_configuration():
    status = payment_security_status(**_ready_status_kwargs())

    assert status["ready"] is True
    assert status["production_ready"] is True
    assert status["blockers"] == []
    assert status["facilitator"]["configured"] is True
    assert status["facilitator"]["safe"] is True
    assert status["facilitator"]["host"] == "api.cdp.coinbase.com"
    assert status["facilitator"]["authentication"] == "cdp_jwt"
    assert status["facilitator"]["cdp_auth_sdk"]["available"] is True


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"sdk_present": False}, "x402_sdk_missing"),
        ({"cdp_auth_sdk_present": False}, "cdp_auth_sdk_missing"),
        ({"cdp_api_key_id_configured": False}, "cdp_credentials_missing"),
        ({"cdp_api_key_secret_configured": False}, "cdp_credentials_missing"),
        ({"facilitator_bearer_configured": True}, "cdp_static_bearer_unsupported"),
        (
            {"facilitator_url": "https://API.CDP.COINBASE.COM/platform/v2/x402"},
            "cdp_facilitator_url_noncanonical",
        ),
        ({"facilitator_url": ""}, "facilitator_missing"),
        ({"facilitator_url": "http://facilitator.example"}, "facilitator_https_required"),
        (
            {"facilitator_url": "https://x402.org/facilitator"},
            "facilitator_public_development_only",
        ),
        ({"facilitator_url": "https://127.0.0.1/x402"}, "facilitator_private_host"),
        ({"mock_enabled": True}, "mock_payments_enabled"),
        ({"legacy_enabled": True}, "legacy_payments_enabled"),
        ({"networks": ["eip155:1"]}, "unsupported_mainnet_network"),
        ({"trusted_proxies": "*"}, "trusted_proxies_wildcard"),
        ({"trusted_proxies": "proxy.internal"}, "trusted_proxies_malformed"),
        ({"freshness_seconds": 0}, "payment_freshness_nonpositive"),
        ({"finality_confirmations": 0}, "payment_finality_nonpositive"),
        ({"verification_lease_seconds": 0}, "payment_verification_lease_nonpositive"),
        ({"replay_ttl_seconds": 0}, "payment_replay_ttl_nonpositive"),
        ({"replay_max_entries": 0}, "payment_replay_entries_nonpositive"),
        ({"replay_ttl_seconds": 3_601}, "payment_replay_ttl_too_large"),
        ({"replay_max_entries": 501}, "payment_replay_entries_too_large"),
        ({"credit_db_path": "credits.db"}, "credit_db_not_durable"),
        ({"credit_db_path": "/tmp/credits.db"}, "credit_db_not_durable"),
        (
            {"credit_db_path": "/var/lib/blocksize/credits.db"},
            "credit_db_not_on_railway_volume",
        ),
    ],
)
def test_each_production_safeguard_independently_blocks_readiness(overrides, blocker):
    kwargs = _ready_status_kwargs()
    kwargs.update(overrides)

    status = payment_security_status(**kwargs)

    assert status["ready"] is False
    assert status["production_ready"] is False
    assert blocker in status["blockers"]


def test_local_status_stays_runnable_but_exposes_production_blockers():
    kwargs = _ready_status_kwargs()
    kwargs.update(
        production=False,
        railway_hosted=False,
        sdk_present=False,
        credit_db_path="credits.db",
    )

    status = payment_security_status(**kwargs)

    assert status["ready"] is True
    assert status["production_ready"] is False
    assert "x402_sdk_missing" in status["blockers"]
    assert "credit_db_not_durable" in status["blockers"]


def test_non_railway_accepts_absolute_non_temporary_credit_database_path():
    kwargs = _ready_status_kwargs()
    kwargs.update(
        railway_hosted=False,
        credit_db_path="/var/lib/blocksize/credits.db",
    )

    status = payment_security_status(**kwargs)

    assert status["production_ready"] is True
    assert status["controls"]["credit_db_durable"] is True


@pytest.mark.asyncio
async def test_facilitator_requirement_argument_cannot_be_swapped_after_parsing():
    parsed = _parse()
    swapped = copy.deepcopy(_requirement())
    swapped["amount"] = "9000"

    async def post(_url: str, **_kwargs):  # pragma: no cover - must not be reached
        return {"isValid": True}

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    assert await adapter.verify(parsed, swapped) == {
        "isValid": False,
        "invalidReason": "payment_requirement_mismatch",
    }
    assert await adapter.settle(parsed, swapped) == {
        "success": False,
        "errorReason": "payment_requirement_mismatch",
    }


@pytest.mark.asyncio
async def test_mutating_or_forging_parsed_payload_cannot_bypass_adapter_validation():
    parsed = _parse()
    parsed.payload["accepted"]["amount"] = "1"
    called = False

    async def post(_url: str, **_kwargs):  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        return {"isValid": True}

    adapter = FacilitatorAdapter("https://facilitator.example", post=post)
    assert await adapter.verify(parsed, _requirement()) == {
        "isValid": False,
        "invalidReason": "payment_requirement_mismatch",
    }
    assert called is False


@pytest.mark.asyncio
async def test_facilitator_invalid_reasons_and_public_ids_cannot_carry_secrets():
    parsed = _parse()
    secret = "facilitator-secret-token"

    async def post(url: str, **_kwargs):
        if url.endswith("/verify"):
            return {"isValid": False, "invalidReason": secret, "payer": secret}
        return {"success": False, "errorReason": secret, "transaction": secret}

    adapter = FacilitatorAdapter(
        "https://facilitator.example",
        bearer_token=secret,
        post=post,
    )
    verified = await adapter.verify(parsed, _requirement())
    settled = await adapter.settle(parsed, _requirement())

    assert verified == {"isValid": False, "invalidReason": "payment_invalid"}
    assert settled == {"success": False, "errorReason": "settlement_failed"}
    assert secret not in json.dumps(verified)
    assert secret not in json.dumps(settled)
