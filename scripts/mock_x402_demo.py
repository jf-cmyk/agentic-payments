"""
Local x402 demo client.

This script proves the 402 challenge/retry loop without moving funds. Run the
resource server with X402_ALLOW_MOCK_PAYMENTS=true, then point this script at a
known-good paid endpoint.

Example:
    X402_ALLOW_MOCK_PAYMENTS=true uvicorn src.resource_server:app --port 8402
    python scripts/mock_x402_demo.py http://localhost:8402/v1/vwap/BTC-USD
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time

import httpx


def normalise_payment_requirements(decoded: object) -> list[dict]:
    if isinstance(decoded, list):
        return decoded
    if not isinstance(decoded, dict) or not isinstance(decoded.get("accepts"), list):
        return []
    resource = decoded.get("resource") if isinstance(decoded.get("resource"), dict) else {}
    requirements = []
    for accept in decoded["accepts"]:
        if not isinstance(accept, dict):
            continue
        requirements.append({
            "scheme": accept.get("scheme", "exact"),
            "network": accept.get("network", ""),
            "maxAmountRequired": str(accept.get("maxAmountRequired") or accept.get("amount") or "0"),
            "resource": accept.get("payTo", ""),
            "description": resource.get("description", "Blocksize Capital institutional market data"),
            "mimeType": resource.get("mimeType", "application/json"),
            "payTo": accept.get("payTo", ""),
            "maxTimeoutSeconds": accept.get("maxTimeoutSeconds", 60),
            "asset": accept.get("asset", ""),
            "extra": accept.get("extra") if isinstance(accept.get("extra"), dict) else {},
        })
    return requirements


def decode_requirements(header_value: str) -> list[dict]:
    return normalise_payment_requirements(json.loads(base64.b64decode(header_value)))


def build_mock_signature(network: str) -> str:
    payload = {
        "proof": f"mock_demo_{int(time.time())}",
        "network": network,
        "timestamp": int(time.time()),
        "agent_pubkey": "mock_demo_agent",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/mock_x402_demo.py http://localhost:8402/v1/vwap/BTC-USD")
        sys.exit(1)

    url = sys.argv[1]

    async with httpx.AsyncClient(timeout=20.0) as client:
        first = await client.get(url)
        print(f"Initial response: HTTP {first.status_code}")

        if first.status_code != 402:
            print(first.text)
            return

        header = first.headers.get("PAYMENT-REQUIRED")
        if not header:
            raise RuntimeError("Server returned 402 without PAYMENT-REQUIRED header")

        requirements = decode_requirements(header)
        solana_req = next(
            (req for req in requirements if "solana" in str(req.get("network", "")).lower()),
            requirements[0],
        )
        print("Decoded payment requirement:")
        print(json.dumps(solana_req, indent=2))

        signature = build_mock_signature(solana_req["network"])
        second = await client.get(url, headers={"PAYMENT-SIGNATURE": signature})
        print(f"Retry response: HTTP {second.status_code}")
        print(json.dumps(second.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
