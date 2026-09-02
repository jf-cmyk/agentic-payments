#!/usr/bin/env python3
"""Buy one Blocksize response with Base USDC using the official x402 client."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any

from eth_account import Account
from x402 import max_amount, prefer_network, x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client


BASE_MAINNET = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_URL = (
    "https://mcp.blocksize.info/v1/vwap/BTCUSD"
    "?selection_source=published_example_path"
    "&utm_source=github&utm_medium=buyer_example&utm_campaign=first_price"
)
USDC_DECIMALS = 6


class BuyerError(RuntimeError):
    """Fail-closed buyer configuration or response error."""


def _atomic_usdc(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise BuyerError(f"invalid USDC cap: {value!r}") from exc
    atomic = amount * (Decimal(10) ** USDC_DECIMALS)
    if amount <= 0 or atomic != atomic.to_integral_value():
        raise BuyerError("USDC cap must be positive with at most 6 decimal places")
    return int(atomic)


def _json_body(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BuyerError("--json-body must be one JSON object") from exc
    if not isinstance(parsed, dict):
        raise BuyerError("--json-body must be one JSON object")
    return parsed


async def _buy(args: argparse.Namespace) -> dict[str, Any]:
    if not args.pay:
        raise BuyerError("payment is disabled; pass --pay after reviewing URL and cap")
    raw_key = os.environ.get("EVM_PRIVATE_KEY", "").strip()
    if not raw_key:
        raise BuyerError("EVM_PRIVATE_KEY is required")

    signer = EthAccountSigner(Account.from_key(raw_key))
    cap_atomic = _atomic_usdc(args.max_usdc)
    client = x402Client()
    register_exact_evm_client(client, signer, networks=[BASE_MAINNET])
    client.register_policy(prefer_network(BASE_MAINNET))
    client.register_policy(max_amount(cap_atomic))
    client.register_policy(
        lambda _version, requirements: [
            requirement
            for requirement in requirements
            if requirement.asset.lower() == BASE_USDC.lower()
        ]
    )

    body = _json_body(args.json_body)
    async with x402HttpxClient(client, timeout=args.timeout) as http:
        response = await http.request(args.method, args.url, json=body)
        try:
            payload: Any = response.json()
        except json.JSONDecodeError:
            payload = {"body": response.text}
    if response.status_code != 200:
        raise BuyerError(
            f"request returned HTTP {response.status_code}: "
            f"{json.dumps(payload, sort_keys=True)}"
        )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise BuyerError("response did not contain a successful Blocksize data payload")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--json-body")
    parser.add_argument("--max-usdc", default="0.002")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--pay",
        action="store_true",
        help="explicitly authorize one request within --max-usdc",
    )
    return parser


def main() -> int:
    try:
        payload = asyncio.run(_buy(_parser().parse_args()))
    except (BuyerError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        return 1
    print(json.dumps({"passed": True, "data": payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
