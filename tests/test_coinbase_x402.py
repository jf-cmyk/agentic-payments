from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from x402.schemas import PaymentPayload, PaymentRequirements

from src import coinbase_x402 as payment
from src.coinbase_x402 import (
    COINBASE_FACILITATOR_URL,
    CachedResponse,
    CoinbaseX402Config,
    CoinbaseX402Error,
    CoinbaseX402Gateway,
    DecisionKind,
    PaymentMode,
    SDKBindings,
)


BASE_NETWORK = "eip155:8453"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
BASE_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_PAY_TO = "0x1111111111111111111111111111111111111111"
BASE_PAYER = "0x2222222222222222222222222222222222222222"
SOLANA_ASSET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAY_TO = "11111111111111111111111111111112"
SOLANA_FEE_PAYER = "11111111111111111111111111111113"
RESOURCE_URL = "https://api.blocksize.capital/v1/vwap/BTC-USD?window=60"


@dataclass
class MutableClock:
    value: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeFacilitator:
    def __init__(
        self,
        *,
        fee_payer: str | None = SOLANA_FEE_PAYER,
        signers: dict[str, list[str]] | None = None,
    ) -> None:
        solana_extra = {} if fee_payer is None else {"feePayer": fee_payer}
        self.supported_response = SimpleNamespace(
            kinds=[
                SimpleNamespace(
                    x402_version=2,
                    scheme="exact",
                    network=BASE_NETWORK,
                    extra={},
                ),
                SimpleNamespace(
                    x402_version=2,
                    scheme="exact",
                    network=SOLANA_NETWORK,
                    extra=solana_extra,
                ),
            ],
            signers={} if signers is None else signers,
        )
        self.verify_result = SimpleNamespace(is_valid=True, payer=BASE_PAYER)
        self.settle_result = SimpleNamespace(
            success=True,
            network=BASE_NETWORK,
            transaction="0x" + ("ab" * 32),
            payer=BASE_PAYER,
            amount="2000",
        )
        self.supported_calls = 0
        self.verify_calls = 0
        self.settle_calls = 0
        self.verify_error: Exception | None = None
        self.settle_error: Exception | None = None

    def get_supported(self):
        self.supported_calls += 1
        return self.supported_response

    async def verify(self, payload, requirement):
        self.verify_calls += 1
        assert payload.x402_version == 2
        assert requirement.network in {BASE_NETWORK, SOLANA_NETWORK}
        if self.verify_error is not None:
            raise self.verify_error
        return self.verify_result

    async def settle(self, payload, requirement):
        self.settle_calls += 1
        assert payload.x402_version == 2
        assert requirement.network in {BASE_NETWORK, SOLANA_NETWORK}
        if self.settle_error is not None:
            raise self.settle_error
        return self.settle_result


SDK = SDKBindings(
    payment_payload_type=PaymentPayload,
    payment_requirements_type=PaymentRequirements,
    x402_version="2.8.0",
    cdp_sdk_version="1.47.1",
)


def make_config(
    tmp_path: Path,
    *,
    mode: PaymentMode = PaymentMode.ENFORCE,
    clock: MutableClock | None = None,
    **overrides,
) -> CoinbaseX402Config:
    del clock
    values = {
        "mode": mode,
        "db_path": tmp_path / "payments.sqlite3",
        "cdp_api_key_id": "organizations/example/apiKeys/example",
        "cdp_api_key_secret": "not-a-real-secret",
        "allowed_get_routes": frozenset({"v1_vwap"}),
        "verification_lease_seconds": 10,
        "replay_ttl_seconds": 120,
        "replay_max_entries": 20,
        "max_cached_response_bytes": 1024,
        "readiness_timeout_seconds": 1.0,
        "readiness_max_age_seconds": 30,
    }
    values.update(overrides)
    return CoinbaseX402Config(**values)


def raw_requirements(*, timeout: int = 60, amount: str = "2000") -> list[dict]:
    return [
        {
            "scheme": "exact",
            "network": SOLANA_NETWORK,
            "asset": SOLANA_ASSET,
            "amount": amount,
            "payTo": SOLANA_PAY_TO,
            "maxTimeoutSeconds": timeout,
            "extra": {},
        },
        {
            "scheme": "exact",
            "network": BASE_NETWORK,
            "asset": BASE_ASSET,
            "amount": amount,
            "payTo": BASE_PAY_TO,
            "maxTimeoutSeconds": timeout,
            "extra": {"name": "USD Coin", "version": "2"},
        },
    ]


def payment_header(
    requirement: dict,
    *,
    resource_url: str = RESOURCE_URL,
    nonce_byte: str = "33",
    valid_before: str = "1800000060",
) -> str:
    if requirement["network"] == BASE_NETWORK:
        scheme_payload = {
            "signature": "0x" + ("44" * 65),
            "authorization": {
                "from": BASE_PAYER,
                "to": requirement["payTo"],
                "value": requirement["amount"],
                "validAfter": "0",
                "validBefore": valid_before,
                "nonce": "0x" + (nonce_byte * 32),
            },
        }
    else:
        scheme_payload = {
            "transaction": base64.b64encode((nonce_byte * 100).encode()).decode()
        }
    envelope = {
        "x402Version": 2,
        "payload": scheme_payload,
        "accepted": requirement,
        "resource": {
            "url": resource_url,
            "description": "VWAP",
            "mimeType": "application/json",
        },
    }
    return base64.b64encode(
        json.dumps(envelope, separators=(",", ":")).encode()
    ).decode()


async def ready_gateway(
    tmp_path: Path,
    *,
    facilitator: FakeFacilitator | None = None,
    config: CoinbaseX402Config | None = None,
    clock: MutableClock | None = None,
) -> tuple[CoinbaseX402Gateway, FakeFacilitator, tuple[dict, ...]]:
    fake = FakeFacilitator() if facilitator is None else facilitator
    effective_clock = MutableClock() if clock is None else clock
    gateway = CoinbaseX402Gateway(
        make_config(tmp_path) if config is None else config,
        facilitator_client=fake,
        sdk_types=SDK,
        clock=effective_clock,
    )
    snapshot = await gateway.refresh_supported()
    assert snapshot.available is True
    return gateway, fake, gateway.prepare_requirements(raw_requirements())


def test_config_defaults_to_safe_shadow_and_redacts_credentials(tmp_path):
    config = CoinbaseX402Config.from_env(
        {
            "CDP_API_KEY_ID": "key-id-marker",
            "CDP_API_KEY_SECRET": "secret-marker",
        }
    )

    assert config.mode is PaymentMode.SHADOW
    assert config.db_path == Path("x402_payments.sqlite3")
    assert config.allowed_get_routes == frozenset({"v1_vwap"})
    assert config.readiness_timeout_seconds == 5.0
    assert "key-id-marker" not in repr(config)
    assert "secret-marker" not in repr(config)


@pytest.mark.parametrize("mode", ["", "off", "true", "enforce?token=secret"])
def test_payment_mode_rejects_everything_except_shadow_or_enforce(mode):
    with pytest.raises(CoinbaseX402Error) as caught:
        CoinbaseX402Config.from_env(
            {
                "X402_PAYMENT_MODE": mode,
                "CDP_API_KEY_ID": "key-id",
                "CDP_API_KEY_SECRET": "secret",
            }
        )

    assert str(caught.value) == "payment_mode_invalid"
    if mode:
        assert mode not in repr(caught.value)


def test_environment_bounds_fail_closed():
    with pytest.raises(CoinbaseX402Error, match="replay_ttl_invalid"):
        CoinbaseX402Config.from_env(
            {
                "CDP_API_KEY_ID": "key-id",
                "CDP_API_KEY_SECRET": "secret",
                "X402_PAYMENT_REPLAY_TTL_SECONDS": "3601",
            }
        )
    with pytest.raises(CoinbaseX402Error, match="cdp_api_key_secret_invalid"):
        CoinbaseX402Config.from_env({"CDP_API_KEY_ID": "key-id"})
    with pytest.raises(CoinbaseX402Error, match="enforce_get_routes_invalid"):
        CoinbaseX402Config.from_env(
            {
                "CDP_API_KEY_ID": "key-id",
                "CDP_API_KEY_SECRET": "secret",
                "X402_ENFORCE_GET_ROUTES": "v1_vwap,v1_bidask",
            }
        )


@pytest.mark.asyncio
async def test_official_sdk_client_is_canonical_and_uses_cdp_auth_helper():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    secret = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    client, sdk = payment._load_official_sdk(
        "organizations/example/apiKeys/example",
        secret,
    )
    try:
        assert client.url == COINBASE_FACILITATOR_URL
        assert sdk.x402_version == "2.8.0"
        assert sdk.cdp_sdk_version == "1.47.1"
        headers = client._get_supported_headers()
        assert headers["Authorization"].startswith("Bearer ")
        assert secret not in repr(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_supported_refresh_injects_exact_solana_fee_payer(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)

    solana = next(item for item in requirements if item["network"] == SOLANA_NETWORK)
    assert solana["extra"] == {"feePayer": SOLANA_FEE_PAYER}
    assert fake.supported_calls == 1
    readiness = gateway.readiness_snapshot()
    assert readiness["ready"] is True
    assert readiness["facilitator"]["authenticated"] is True
    assert readiness["facilitator"]["solana_fee_payer_ready"] is True
    assert readiness["ledger"]["durable_path"] is False
    assert COINBASE_FACILITATOR_URL not in json.dumps(readiness)

    extra_base = dict(raw_requirements()[1])
    extra_base["amount"] = "3000"
    with pytest.raises(CoinbaseX402Error, match="payment_requirements_incomplete"):
        gateway.prepare_requirements([*raw_requirements(), extra_base])


@pytest.mark.asyncio
async def test_supported_signer_fallback_requires_exactly_one_valid_signer(tmp_path):
    fake = FakeFacilitator(
        fee_payer=None,
        signers={"solana:*": [SOLANA_FEE_PAYER]},
    )
    gateway, _, requirements = await ready_gateway(tmp_path, facilitator=fake)
    solana = next(item for item in requirements if item["network"] == SOLANA_NETWORK)
    assert solana["extra"]["feePayer"] == SOLANA_FEE_PAYER

    ambiguous = FakeFacilitator(
        fee_payer=None,
        signers={"solana:*": [SOLANA_FEE_PAYER, SOLANA_PAY_TO]},
    )
    other = CoinbaseX402Gateway(
        make_config(tmp_path, db_path=tmp_path / "other.sqlite3"),
        facilitator_client=ambiguous,
        sdk_types=SDK,
    )
    snapshot = await other.refresh_supported()
    assert snapshot.available is False
    assert snapshot.reason == "solana_fee_payer_incomplete"
    assert other.readiness_snapshot()["ready"] is False
    with pytest.raises(CoinbaseX402Error, match="facilitator_not_ready"):
        other.prepare_requirements(raw_requirements())


@pytest.mark.asyncio
async def test_shadow_blocks_signed_submissions_before_parsing_or_network_calls(tmp_path):
    fake = FakeFacilitator()
    gateway = CoinbaseX402Gateway(
        make_config(tmp_path, mode=PaymentMode.SHADOW),
        facilitator_client=fake,
        sdk_types=SDK,
    )

    decision = await gateway.begin(
        "token=https://secret.example/private",
        method="POST",
        resource_url="also-not-a-url",
        body=b"secret payload",
        requirements=[],
        route_id="not-allowed",
    )

    assert decision.kind is DecisionKind.SHADOW_BLOCKED
    assert decision.code == "x402_shadow_locked"
    assert fake.verify_calls == 0
    assert fake.settle_calls == 0
    assert "secret" not in repr(decision)
    assert gateway.readiness_snapshot()["counters"]["shadow_blocked_total"] == 1


@pytest.mark.asyncio
async def test_enforce_rejects_non_get_and_non_allowlisted_routes_before_parsing(tmp_path):
    fake = FakeFacilitator()
    gateway = CoinbaseX402Gateway(
        make_config(tmp_path),
        facilitator_client=fake,
        sdk_types=SDK,
    )

    for method, route_id in (("POST", "v1_vwap"), ("GET", "v1_bidask")):
        decision = await gateway.begin(
            "not even base64",
            method=method,
            resource_url=RESOURCE_URL,
            body=b"",
            requirements=raw_requirements(),
            route_id=route_id,
        )
        assert decision.kind is DecisionKind.REJECTED
        assert decision.code == "payment_route_not_allowed"

    assert fake.verify_calls == 0
    assert fake.settle_calls == 0


@pytest.mark.asyncio
async def test_enforce_malformed_signature_is_local_and_generic(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)

    decision = await gateway.begin(
        "https://secret.example/?token=do-not-leak",
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert decision.kind is DecisionKind.REJECTED
    assert decision.code == "invalid_payment_signature"
    assert "secret.example" not in repr(decision)
    assert fake.verify_calls == 0
    assert fake.settle_calls == 0
    counters = gateway.readiness_snapshot()["counters"]
    assert counters["verify_calls_total"] == 0
    assert counters["settle_calls_total"] == 0


@pytest.mark.asyncio
async def test_enforce_excessively_nested_signature_fails_closed(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    nested = ("[" * 2_000) + "0" + ("]" * 2_000)
    raw = base64.b64decode(payment_header(base)).replace(
        b'"extra":{"name":"USD Coin","version":"2"}',
        (
            '{"extra":{"name":"USD Coin","version":"2","nested":'
            + nested
            + "}}"
        ).encode(),
    )

    decision = await gateway.begin(
        base64.b64encode(raw).decode(),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert decision.kind is DecisionKind.REJECTED
    assert decision.code == "invalid_payment_signature"
    assert fake.verify_calls == 0
    assert fake.settle_calls == 0


@pytest.mark.asyncio
async def test_begin_selects_one_exact_requirement_without_caller_parsing(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)

    decision = await gateway.begin(
        payment_header(base),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert decision.kind is DecisionKind.PROCEED
    assert decision.ticket is not None
    assert fake.verify_calls == 1
    assert "signature" not in repr(decision.ticket)

    duplicate = await gateway.begin(
        payment_header(base, nonce_byte="34"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=[base, base],
        route_id="v1_vwap",
    )
    assert duplicate.kind is DecisionKind.REJECTED
    assert duplicate.code == "payment_requirements_incomplete"
    assert fake.verify_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["amount", "payTo", "asset", "network"])
async def test_requirement_critical_fields_are_exactly_bound(tmp_path, field):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = dict(next(item for item in requirements if item["network"] == BASE_NETWORK))
    altered = dict(base)
    altered[field] = {
        "amount": "2001",
        "payTo": "0x3333333333333333333333333333333333333333",
        "asset": "0x4444444444444444444444444444444444444444",
        "network": SOLANA_NETWORK,
    }[field]

    decision = await gateway.begin(
        payment_header(altered),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert decision.kind is DecisionKind.REJECTED
    assert fake.verify_calls == 0


@pytest.mark.asyncio
async def test_payload_resource_and_raw_body_are_bound(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    header = payment_header(base)

    wrong_url = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL + "&extra=1",
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert wrong_url.code == "payment_resource_mismatch"

    first = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"body-one",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert first.kind is DecisionKind.PROCEED
    changed_body = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"body-two",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert changed_body.code == "payment_binding_conflict"
    assert fake.verify_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["to", "value"])
async def test_evm_authorization_is_locally_bound_to_recipient_and_amount(tmp_path, field):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    envelope = json.loads(base64.b64decode(payment_header(base)))
    envelope["payload"]["authorization"][field] = (
        "0x5555555555555555555555555555555555555555" if field == "to" else "2001"
    )
    tampered = base64.b64encode(
        json.dumps(envelope, separators=(",", ":")).encode()
    ).decode()

    decision = await gateway.begin(
        tampered,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert decision.kind is DecisionKind.REJECTED
    assert decision.code == "payment_authorization_mismatch"
    assert fake.verify_calls == 0


@pytest.mark.asyncio
async def test_equivalent_evm_proof_encodings_share_one_local_payment_id(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    original = payment_header(base, nonce_byte="ab")
    first = await gateway.begin(
        original,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert first.kind is DecisionKind.PROCEED

    envelope = json.loads(base64.b64decode(original))
    envelope["payload"]["signature"] = (
        "0x" + envelope["payload"]["signature"][2:].upper()
    )
    authorization = envelope["payload"]["authorization"]
    authorization["nonce"] = "0x" + authorization["nonce"][2:].upper()
    authorization["validAfter"] = "000"
    authorization["validBefore"] = "01800000060"
    equivalent = base64.b64encode(
        json.dumps(envelope, separators=(",", ":")).encode()
    ).decode()

    second = await gateway.begin(
        equivalent,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert second.kind is DecisionKind.BUSY
    assert fake.verify_calls == 1


@pytest.mark.asyncio
async def test_far_future_evm_authorizations_cannot_consume_ledger_capacity(tmp_path):
    clock = MutableClock()
    config = make_config(
        tmp_path,
        clock=clock,
        replay_ttl_seconds=120,
        replay_max_entries=2,
    )
    gateway, fake, requirements = await ready_gateway(
        tmp_path,
        config=config,
        clock=clock,
    )
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)

    for nonce in ("a1", "a2", "a3"):
        decision = await gateway.begin(
            payment_header(
                base,
                nonce_byte=nonce,
                valid_before=str(int(clock.value) + 100 * 365 * 24 * 60 * 60),
            ),
            method="GET",
            resource_url=RESOURCE_URL,
            body=b"",
            requirements=requirements,
            route_id="v1_vwap",
        )
        assert decision.kind is DecisionKind.REJECTED
        assert decision.code == "payment_authorization_window_invalid"

    readiness = gateway.readiness_snapshot()
    assert readiness["ledger"]["states"]["total"] == 0
    assert readiness["ready"] is True
    assert fake.verify_calls == 0


@pytest.mark.asyncio
async def test_stale_verification_lease_can_be_safely_reacquired(tmp_path):
    clock = MutableClock()
    config = make_config(tmp_path, verification_lease_seconds=5)
    gateway, fake, requirements = await ready_gateway(tmp_path, config=config, clock=clock)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    header = payment_header(base)

    first = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert first.kind is DecisionKind.PROCEED
    clock.advance(6)
    second = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert second.kind is DecisionKind.PROCEED
    assert fake.verify_calls == 2
    assert first.ticket is not None and second.ticket is not None
    assert first.ticket.lease_token != second.ticket.lease_token
    stale = await gateway.settle_and_finalize(
        first.ticket,
        CachedResponse(200, {"content-type": "application/json"}, b"{}"),
    )
    assert stale.code == "payment_ticket_not_active"
    assert fake.settle_calls == 0


@pytest.mark.asyncio
async def test_finalized_response_replays_across_restart_without_second_settlement(tmp_path):
    clock = MutableClock()
    config = make_config(tmp_path)
    gateway, fake, requirements = await ready_gateway(tmp_path, config=config, clock=clock)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    header = payment_header(base)

    begun = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert begun.ticket is not None
    response = CachedResponse(
        200,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
        b'{"price":"100.00"}',
    )
    finalized = await gateway.settle_and_finalize(begun.ticket, response)
    assert finalized.kind is DecisionKind.FINALIZED
    assert finalized.receipt is not None
    assert fake.verify_calls == 1
    assert fake.settle_calls == 1

    replay = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert replay.kind is DecisionKind.REPLAY
    assert replay.response == CachedResponse(
        200,
        {"content-type": "application/json", "cache-control": "no-store"},
        b'{"price":"100.00"}',
    )
    assert fake.verify_calls == 1
    assert fake.settle_calls == 1

    after_restart = FakeFacilitator()
    restarted = CoinbaseX402Gateway(
        config,
        facilitator_client=after_restart,
        sdk_types=SDK,
        clock=clock,
    )
    await restarted.refresh_supported()
    restarted_requirements = restarted.prepare_requirements(raw_requirements())
    durable = await restarted.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=restarted_requirements,
        route_id="v1_vwap",
    )
    assert durable.kind is DecisionKind.REPLAY
    assert after_restart.verify_calls == 0
    assert after_restart.settle_calls == 0


@pytest.mark.asyncio
async def test_settlement_failure_is_unknown_and_never_automatically_retried(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    header = payment_header(base)
    begun = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert begun.ticket is not None
    fake.settle_error = RuntimeError(
        "https://api.cdp.coinbase.com/private?token=do-not-leak payload=secret"
    )

    outcome = await gateway.settle_and_finalize(
        begun.ticket,
        CachedResponse(200, {"content-type": "application/json"}, b"{}"),
    )
    assert outcome.kind is DecisionKind.SETTLEMENT_UNKNOWN
    assert outcome.code == "payment_settlement_unknown"
    assert "coinbase.com" not in repr(outcome)
    assert "do-not-leak" not in repr(outcome)

    retry = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert retry.kind is DecisionKind.SETTLEMENT_UNKNOWN
    assert fake.settle_calls == 1
    readiness = gateway.readiness_snapshot()
    assert readiness["ready"] is False
    assert "payment_reconciliation_required" in readiness["blockers"]
    assert readiness["ledger"]["states"]["settlement_unknown"] == 1


@pytest.mark.asyncio
async def test_invalid_verify_releases_proof_for_safe_retry(tmp_path):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    header = payment_header(base)
    fake.verify_result = SimpleNamespace(is_valid=False, payer=None)

    invalid = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert invalid.code == "payment_invalid"
    fake.verify_result = SimpleNamespace(is_valid=True, payer=BASE_PAYER)
    retried = await gateway.begin(
        header,
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert retried.kind is DecisionKind.PROCEED
    assert fake.verify_calls == 2


@pytest.mark.asyncio
async def test_released_invalid_proofs_cannot_exhaust_ledger_capacity(tmp_path):
    config = make_config(tmp_path, replay_max_entries=1)
    gateway, fake, requirements = await ready_gateway(tmp_path, config=config)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    fake.verify_result = SimpleNamespace(is_valid=False, payer=None)

    first = await gateway.begin(
        payment_header(base, nonce_byte="61"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    second = await gateway.begin(
        payment_header(base, nonce_byte="62"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )

    assert first.code == "payment_invalid"
    assert second.code == "payment_invalid"
    assert fake.verify_calls == 2
    states = gateway.readiness_snapshot()["ledger"]["states"]
    assert states["released"] == 0
    assert states["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response, code",
    [
        (CachedResponse(500, {}, b"failure"), "cached_response_not_success"),
        (CachedResponse(200, {}, b"x" * 1025), "cached_response_too_large"),
        (
            CachedResponse(200, {"authorization": "Bearer secret-marker"}, b"{}"),
            "cached_response_sensitive_header",
        ),
    ],
)
async def test_response_is_bounded_and_sensitive_headers_are_never_cached(
    tmp_path,
    response,
    code,
):
    gateway, fake, requirements = await ready_gateway(tmp_path)
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    begun = await gateway.begin(
        payment_header(base),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert begun.ticket is not None

    decision = await gateway.settle_and_finalize(begun.ticket, response)

    assert decision.code == code
    assert fake.settle_calls == 0
    assert "secret-marker" not in repr(decision)


@pytest.mark.asyncio
async def test_ttl_cleanup_frees_only_safely_expired_finalized_rows(tmp_path):
    clock = MutableClock()
    config = make_config(
        tmp_path,
        replay_ttl_seconds=5,
        replay_max_entries=1,
        verification_lease_seconds=2,
    )
    fake = FakeFacilitator()
    gateway = CoinbaseX402Gateway(
        config,
        facilitator_client=fake,
        sdk_types=SDK,
        clock=clock,
    )
    await gateway.refresh_supported()
    requirements = gateway.prepare_requirements(raw_requirements(timeout=5))
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)

    first = await gateway.begin(
        payment_header(base, nonce_byte="51", valid_before="1800000005"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert first.ticket is not None
    finalized = await gateway.settle_and_finalize(
        first.ticket,
        CachedResponse(200, {}, b"one"),
    )
    assert finalized.kind is DecisionKind.FINALIZED

    at_capacity = await gateway.begin(
        payment_header(base, nonce_byte="52", valid_before="1800000005"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert at_capacity.code == "payment_ledger_capacity"
    clock.advance(6)
    after_ttl = await gateway.begin(
        payment_header(base, nonce_byte="52", valid_before="1800000011"),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    assert after_ttl.kind is DecisionKind.PROCEED


@pytest.mark.asyncio
async def test_readiness_and_counters_are_non_secret_and_auditable(tmp_path):
    clock = MutableClock()
    fake = FakeFacilitator()
    gateway = CoinbaseX402Gateway(
        make_config(tmp_path),
        facilitator_client=fake,
        sdk_types=SDK,
        clock=clock,
    )
    initial = gateway.readiness_snapshot()
    assert initial["ready"] is False
    assert initial["facilitator"]["checked"] is False

    await gateway.refresh_supported()
    requirements = gateway.prepare_requirements(raw_requirements())
    base = next(item for item in requirements if item["network"] == BASE_NETWORK)
    await gateway.begin(
        payment_header(base),
        method="GET",
        resource_url=RESOURCE_URL,
        body=b"",
        requirements=requirements,
        route_id="v1_vwap",
    )
    snapshot = gateway.readiness_snapshot()
    encoded = json.dumps(snapshot, sort_keys=True)
    assert snapshot["ready"] is False
    assert snapshot["counters"]["verify_calls_total"] == 1
    assert snapshot["ledger"]["states"]["pending"] == 1
    assert snapshot["ledger"]["unresolved"] == 1
    assert "payment_verification_in_progress" in snapshot["blockers"]
    assert "not-a-real-secret" not in encoded
    assert RESOURCE_URL not in encoded
    assert COINBASE_FACILITATOR_URL not in encoded

    clock.advance(31)
    stale = gateway.readiness_snapshot()
    assert stale["ready"] is False
    assert "facilitator_support_not_ready" in stale["blockers"]
