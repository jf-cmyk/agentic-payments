#!/usr/bin/env python3
"""Run one bounded, standards-compliant x402 payment canary.

The payer key is read exactly once from a removable-volume file and is never
printed, copied, or written by this program.  The production write lock must
already be open; when it is locked, this program exits before reading the key.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from decimal import Decimal, InvalidOperation
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from x402 import max_amount, prefer_network, x402Client
from x402.http import x402HTTPClient
from x402.mechanisms.svm import KeypairSigner
from x402.mechanisms.svm.exact.register import register_exact_svm_client
from x402.schemas import PaymentRequired, PaymentRequirements


DEFAULT_URL = "https://mcp.blocksize.info/v1/vwap/BTCUSD"
MAX_USDC = "0.002"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MAX_KEY_FILE_BYTES = 4096


class CanaryError(RuntimeError):
    """A fail-closed canary validation error."""


def _atomic_usdc(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise CanaryError(f"invalid USDC amount: {value!r}") from exc
    atomic = amount * (Decimal(10) ** USDC_DECIMALS)
    if amount <= 0 or atomic != atomic.to_integral_value():
        raise CanaryError("USDC limit must be positive with at most 6 decimal places")
    return int(atomic)


def _parse_keypair(raw: bytes) -> Keypair:
    """Accept Solana CLI JSON, base58 text, or base64-encoded 64-byte keys."""
    stripped = raw.strip()
    if not stripped:
        raise CanaryError("key file is empty")

    try:
        decoded_json = json.loads(stripped)
    except (json.JSONDecodeError, UnicodeDecodeError):
        decoded_json = None

    if isinstance(decoded_json, list):
        if len(decoded_json) != 64 or any(
            not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 255
            for item in decoded_json
        ):
            raise CanaryError("Solana JSON keypair must contain exactly 64 byte values")
        return Keypair.from_bytes(bytes(decoded_json))

    try:
        text = stripped.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CanaryError("unsupported key format; expected JSON, base58, or base64 text") from exc

    try:
        return Keypair.from_base58_string(text)
    except ValueError:
        pass

    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CanaryError("unsupported key format; expected JSON, base58, or base64 text") from exc
    if len(decoded) != 64:
        raise CanaryError("base64 key must decode to exactly 64 bytes")
    return Keypair.from_bytes(decoded)


def _read_key_once(path_value: str, *, allow_local_key_file: bool = False) -> Keypair:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise CanaryError("key file path must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise CanaryError("key path is not a regular file")
    if (
        sys.platform == "darwin"
        and not allow_local_key_file
        and not resolved.is_relative_to(Path("/Volumes"))
    ):
        raise CanaryError("on macOS the key file must remain on a volume under /Volumes")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_KEY_FILE_BYTES:
        raise CanaryError(f"key file must be between 1 and {MAX_KEY_FILE_BYTES} bytes")

    with resolved.open("rb") as handle:
        raw = bytearray(handle.read(MAX_KEY_FILE_BYTES + 1))
    try:
        if len(raw) > MAX_KEY_FILE_BYTES:
            raise CanaryError("key file is unexpectedly large")
        return _parse_keypair(bytes(raw))
    finally:
        raw[:] = b"\x00" * len(raw)


def _select_solana_requirement(
    required: PaymentRequired,
    *,
    max_atomic: int,
    expected_pay_to: str | None,
) -> PaymentRequirements:
    matches = [
        item
        for item in required.accepts
        if item.scheme == "exact"
        and str(item.network) == SOLANA_MAINNET
        and item.asset == SOLANA_USDC
    ]
    if len(matches) != 1:
        raise CanaryError("invoice must contain exactly one Solana-mainnet USDC exact requirement")
    selected = matches[0]
    try:
        amount = int(selected.amount)
    except ValueError as exc:
        raise CanaryError("invoice amount is not an integer") from exc
    if amount <= 0 or amount > max_atomic:
        raise CanaryError(
            f"invoice amount {amount} exceeds the authorized maximum {max_atomic} atomic USDC"
        )
    try:
        Pubkey.from_string(selected.pay_to)
        Pubkey.from_string(str(selected.extra.get("feePayer", "")))
    except ValueError as exc:
        raise CanaryError("invoice recipient or facilitator fee payer is invalid") from exc
    if expected_pay_to and selected.pay_to != expected_pay_to:
        raise CanaryError("invoice recipient does not match --expected-pay-to")
    return selected


def _write_lock_is_open(readiness: dict[str, Any]) -> bool:
    try:
        bridge = readiness["checks"]["legacy_transaction_bridge"]
        return bridge["ready"] is True and bridge["economic_writes_locked"] is False
    except (KeyError, TypeError):
        return False


def _validate_vwap_payload(payload: Any) -> dict[str, Any]:
    """Validate the paid product body, not only its HTTP and settlement status."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise CanaryError("paid response is not a successful JSON product payload")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CanaryError("paid response is missing its data object")

    pair = str(data.get("pair") or "")
    canonical_pair = pair.replace("-", "").replace("/", "").replace("_", "").upper()
    if canonical_pair != "BTCUSD":
        raise CanaryError("paid response pair does not match BTCUSD")

    vwap = data.get("vwap")
    if (
        not isinstance(vwap, (int, float))
        or isinstance(vwap, bool)
        or not math.isfinite(float(vwap))
        or float(vwap) <= 0
    ):
        raise CanaryError("paid response VWAP must be a positive finite number")

    timestamp = data.get("timestamp")
    if not isinstance(timestamp, str):
        raise CanaryError("paid response timestamp is missing")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanaryError("paid response timestamp is not ISO-8601") from exc
    if parsed_timestamp.tzinfo is None:
        raise CanaryError("paid response timestamp must include a timezone")

    if str(data.get("currency") or "").upper() != "USD":
        raise CanaryError("paid response quote currency does not match USD")
    if not str(data.get("source") or "").strip():
        raise CanaryError("paid response source is missing")
    meta = payload.get("meta")
    if not isinstance(meta, dict) or meta.get("provider") != "Blocksize Capital":
        raise CanaryError("paid response provider metadata is missing")
    return payload


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    max_atomic = _atomic_usdc(MAX_USDC)
    parsed_url = httpx.URL(DEFAULT_URL)
    if parsed_url.scheme != "https" or parsed_url.host != "mcp.blocksize.info":
        raise CanaryError("canary URL must use https://mcp.blocksize.info")

    base_url = f"{parsed_url.scheme}://{parsed_url.host}"
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        readiness_response = await http.get(f"{base_url}/readyz")
        readiness_response.raise_for_status()
        readiness = readiness_response.json()
        if readiness.get("ready") is not True:
            raise CanaryError("production readiness is not green")
        if not _write_lock_is_open(readiness):
            raise CanaryError(
                "production economic writes are locked; key was not read and no payment was signed"
            )

        challenge = await http.get(DEFAULT_URL)
        if challenge.status_code != 402:
            raise CanaryError(f"unsigned request returned {challenge.status_code}, expected 402")

        parser_client = x402HTTPClient(x402Client())
        body: Any = None
        try:
            body = challenge.json()
        except json.JSONDecodeError:
            pass
        required = parser_client.get_payment_required_response(
            lambda name: challenge.headers.get(name), body
        )
        if not isinstance(required, PaymentRequired) or required.x402_version != 2:
            raise CanaryError("production challenge is not x402 v2")
        selected = _select_solana_requirement(
            required,
            max_atomic=max_atomic,
            expected_pay_to=None,
        )
        if required.resource and str(required.resource.url) != str(parsed_url):
            raise CanaryError("invoice resource URL does not exactly match the requested URL")

        keypair = _read_key_once(
            args.key_file,
            allow_local_key_file=args.allow_local_key_file,
        )
        signer = KeypairSigner(keypair)
        payer = signer.address

        payment_client = x402Client()
        register_exact_svm_client(
            payment_client,
            signer,
            networks=SOLANA_MAINNET,
            rpc_url=args.rpc_url,
        )
        payment_client.register_policy(prefer_network(SOLANA_MAINNET))
        payment_client.register_policy(max_amount(max_atomic))
        payment_http = x402HTTPClient(payment_client)
        payload = await payment_client.create_payment_payload(required)
        if payload.accepted != selected:
            raise CanaryError("official client selected an unexpected payment requirement")
        payment_headers = payment_http.encode_payment_signature_header(payload)

        paid = await http.get(DEFAULT_URL, headers=payment_headers)
        if paid.status_code != 200:
            detail = ""
            try:
                error_code = paid.json().get("error_code")
                if error_code:
                    detail = f" ({error_code})"
            except (json.JSONDecodeError, AttributeError):
                pass
            raise CanaryError(f"paid request returned {paid.status_code}{detail}")
        try:
            paid_payload = _validate_vwap_payload(paid.json())
        except json.JSONDecodeError as exc:
            raise CanaryError("paid response is not JSON") from exc
        settlement = payment_http.get_payment_settle_response(
            lambda name: paid.headers.get(name)
        )
        if not settlement.success:
            raise CanaryError("facilitator settlement did not report success")
        if str(settlement.network) != SOLANA_MAINNET or settlement.payer != payer:
            raise CanaryError("settlement network or payer does not match the signed payment")
        if settlement.amount is not None and int(settlement.amount) != int(selected.amount):
            raise CanaryError("settled amount does not match the invoice")

        replay = await http.get(DEFAULT_URL, headers=payment_headers)
        if replay.status_code != 200:
            raise CanaryError(f"idempotent replay returned {replay.status_code}, expected 200")
        try:
            replay_payload = _validate_vwap_payload(replay.json())
        except json.JSONDecodeError as exc:
            raise CanaryError("idempotent replay response is not JSON") from exc
        if replay_payload != paid_payload:
            raise CanaryError("idempotent replay did not return the original data payload")
        replay_settlement = payment_http.get_payment_settle_response(
            lambda name: replay.headers.get(name)
        )
        if (
            not replay_settlement.success
            or replay_settlement.transaction != settlement.transaction
        ):
            raise CanaryError("replay did not return the original settlement transaction")

    return {
        "passed": True,
        "url": DEFAULT_URL,
        "payer": payer,
        "pay_to": selected.pay_to,
        "network": str(selected.network),
        "amount_atomic_usdc": int(selected.amount),
        "amount_usdc": str(Decimal(selected.amount) / (Decimal(10) ** USDC_DECIMALS)),
        "transaction": settlement.transaction,
        "data": paid_payload["data"],
        "data_meta": paid_payload["meta"],
        "replay_transaction": replay_settlement.transaction,
        "replay_was_idempotent": True,
        "replay_data_identical": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one $0.002 x402 v2 Solana payment using a key file on removable media."
        )
    )
    parser.add_argument("key_file", help="Absolute key file path on the removable volume")
    parser.add_argument(
        "--allow-local-key-file",
        action="store_true",
        help="Explicitly allow a key file outside /Volumes on macOS",
    )
    parser.add_argument("--rpc-url", default=os.getenv("SOLANA_RPC_URL"))
    parser.add_argument("--timeout", type=float, default=45.0)
    return parser


def main() -> None:
    try:
        result = asyncio.run(_run(_parser().parse_args()))
    except (CanaryError, httpx.HTTPError, OSError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
