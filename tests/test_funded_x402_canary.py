from __future__ import annotations

import base64
import json

import pytest
from solders.keypair import Keypair
from x402.schemas import PaymentRequired, PaymentRequirements

from scripts.run_funded_x402_canary import (
    CanaryError,
    SOLANA_MAINNET,
    SOLANA_USDC,
    _atomic_usdc,
    _parse_keypair,
    _read_key_once,
    _select_solana_requirement,
    _validate_vwap_payload,
    _write_lock_is_open,
)


def test_atomic_usdc_enforces_precision_and_positive_limit() -> None:
    assert _atomic_usdc("0.002") == 2_000
    with pytest.raises(CanaryError):
        _atomic_usdc("0")
    with pytest.raises(CanaryError):
        _atomic_usdc("0.0000001")


def test_keypair_parser_accepts_json_base58_and_base64() -> None:
    keypair = Keypair()
    raw = bytes(keypair)
    variants = (
        json.dumps(list(raw)).encode(),
        str(keypair).encode(),
        base64.b64encode(raw),
    )
    assert all(parsed.pubkey() == keypair.pubkey() for parsed in map(_parse_keypair, variants))


def test_keypair_parser_rejects_invalid_or_oversized_shapes() -> None:
    with pytest.raises(CanaryError):
        _parse_keypair(b"")
    with pytest.raises(CanaryError):
        _parse_keypair(json.dumps([1, 2, 3]).encode())
    with pytest.raises(CanaryError):
        _parse_keypair(base64.b64encode(b"too-short"))


def test_local_key_file_requires_explicit_macos_opt_in(tmp_path, monkeypatch) -> None:
    keypair = Keypair()
    key_file = tmp_path / "Test.json"
    key_file.write_text(json.dumps(list(bytes(keypair))))
    monkeypatch.setattr("scripts.run_funded_x402_canary.sys.platform", "darwin")

    with pytest.raises(CanaryError, match="under /Volumes"):
        _read_key_once(str(key_file))
    assert (
        _read_key_once(str(key_file), allow_local_key_file=True).pubkey()
        == keypair.pubkey()
    )


def _required(*, amount: str = "2000", pay_to: str | None = None) -> PaymentRequired:
    return PaymentRequired(
        resource={
            "url": "https://mcp.blocksize.info/v1/vwap/BTCUSD",
            "description": "canary",
            "mimeType": "application/json",
        },
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network=SOLANA_MAINNET,
                asset=SOLANA_USDC,
                amount=amount,
                payTo=pay_to or str(Keypair().pubkey()),
                maxTimeoutSeconds=60,
                extra={"feePayer": str(Keypair().pubkey())},
            )
        ],
    )


def test_select_requirement_enforces_amount_and_recipient() -> None:
    required = _required()
    selected = _select_solana_requirement(required, max_atomic=2_000, expected_pay_to=None)
    assert selected.amount == "2000"
    with pytest.raises(CanaryError):
        _select_solana_requirement(required, max_atomic=1_999, expected_pay_to=None)
    with pytest.raises(CanaryError):
        _select_solana_requirement(
            required,
            max_atomic=2_000,
            expected_pay_to=str(Keypair().pubkey()),
        )


def test_write_lock_must_be_explicitly_ready_and_open() -> None:
    assert _write_lock_is_open(
        {
            "checks": {
                "legacy_transaction_bridge": {
                    "ready": True,
                    "economic_writes_locked": False,
                }
            }
        }
    )
    assert not _write_lock_is_open(
        {
            "checks": {
                "legacy_transaction_bridge": {
                    "ready": True,
                    "economic_writes_locked": True,
                }
            }
        }
    )
    assert not _write_lock_is_open({})


def test_validate_vwap_payload_requires_real_typed_data() -> None:
    payload = {
        "status": "ok",
        "data": {
            "pair": "BTC-USD",
            "vwap": 123.45,
            "timestamp": "2026-08-26T22:45:00+00:00",
            "currency": "USD",
            "source": "blocksize",
        },
        "meta": {"provider": "Blocksize Capital"},
    }

    assert _validate_vwap_payload(payload) is payload
    with pytest.raises(CanaryError, match="VWAP"):
        _validate_vwap_payload({**payload, "data": {**payload["data"], "vwap": 0}})
    with pytest.raises(CanaryError, match="pair"):
        _validate_vwap_payload({**payload, "data": {**payload["data"], "pair": "ETHUSD"}})
