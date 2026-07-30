#!/usr/bin/env python3
"""Audit public HTTP payment and security boundaries without submitting payment."""

from __future__ import annotations

import argparse
import base64
import json
from typing import Any

import httpx


PAID_ENDPOINTS = (
    "/v1/vwap/BTC-USD",
    "/v1/bidask/AAPL",
    "/v1/fx/EURUSD",
    "/v1/metal/XAUUSD",
    "/v1/batch?reqs=vwap:BTCUSD",
)
SECURITY_HEADERS = (
    "strict-transport-security",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
)


def _decode_payment_required(value: str) -> dict[str, Any]:
    padded = value + "=" * (-len(value) % 4)
    payload = json.loads(base64.b64decode(padded))
    if not isinstance(payload, dict):
        raise ValueError("Payment-Required did not decode to an object")
    return payload


def audit(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    origin = base
    endpoint_results: dict[str, Any] = {}
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        for path in PAID_ENDPOINTS:
            response = client.get(f"{base}{path}", headers={"Origin": origin})
            body = response.json()
            header_value = response.headers.get("payment-required", "")
            challenge = _decode_payment_required(header_value) if header_value else {}
            body_accepts = body.get("accepts", []) if isinstance(body, dict) else []
            header_accepts = challenge.get("accepts", [])
            resource_url = challenge.get("resource", {}).get("url", "")
            endpoint_results[path] = {
                "http_status": response.status_code,
                "challenge_version": challenge.get("x402Version"),
                "body_version": body.get("x402Version") if isinstance(body, dict) else None,
                "header_accept_count": len(header_accepts),
                "body_accept_count": len(body_accepts),
                "networks": sorted(
                    str(item.get("network"))
                    for item in header_accepts
                    if isinstance(item, dict)
                ),
                "resource_is_canonical_https": resource_url.startswith(f"{base}/"),
                "resource_matches_request_path": resource_url.split("?", 1)[0]
                == f"{base}{path.split('?', 1)[0]}",
                "body_and_header_accepts_match": body_accepts == header_accepts,
                "all_accepts_have_payment_fields": all(
                    isinstance(item, dict)
                    and bool(item.get("payTo"))
                    and bool(item.get("asset"))
                    and int(str(item.get("amount", "0"))) > 0
                    and item.get("scheme") == "exact"
                    for item in header_accepts
                ),
                "cache_control_no_store": "no-store"
                in response.headers.get("cache-control", "").lower(),
                "cors_origin_matches": response.headers.get("access-control-allow-origin")
                == origin,
                "payment_header_exposed": "payment-required"
                in response.headers.get("access-control-expose-headers", "").lower(),
                "security_headers_present": all(
                    bool(response.headers.get(header)) for header in SECURITY_HEADERS
                ),
            }

        preflight = client.options(
            f"{base}/v1/vwap/BTC-USD",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": (
                    "content-type,x-payment,payment-signature,authorization"
                ),
            },
        )
        invalid_method = client.post(f"{base}/v1/vwap/BTC-USD", json={})
        private_probes = {
            path: client.get(f"{base}{path}").status_code
            for path in ("/.env", "/.git/config")
        }

    endpoint_checks = [
        result["http_status"] == 402
        and result["challenge_version"] == 2
        and result["body_version"] == 2
        and result["header_accept_count"] >= 1
        and result["body_and_header_accepts_match"]
        and result["resource_is_canonical_https"]
        and result["resource_matches_request_path"]
        and result["all_accepts_have_payment_fields"]
        and result["cache_control_no_store"]
        and result["cors_origin_matches"]
        and result["payment_header_exposed"]
        and result["security_headers_present"]
        for result in endpoint_results.values()
    ]
    allow_headers = preflight.headers.get("access-control-allow-headers", "").lower()
    return {
        "paid_endpoints": endpoint_results,
        "all_paid_endpoint_challenges_passed": all(endpoint_checks),
        "preflight": {
            "http_status": preflight.status_code,
            "allows_origin": preflight.headers.get("access-control-allow-origin") == origin,
            "allows_required_headers": all(
                header in allow_headers
                for header in (
                    "content-type",
                    "x-payment",
                    "payment-signature",
                    "authorization",
                )
            ),
        },
        "invalid_method_status": invalid_method.status_code,
        "invalid_method_rejected_before_payment": invalid_method.status_code == 405,
        "private_file_probes": private_probes,
        "private_files_not_exposed": all(status == 404 for status in private_probes.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="https://mcp.blocksize.info")
    args = parser.parse_args()
    result = audit(args.base_url)
    result["passed"] = bool(
        result["all_paid_endpoint_challenges_passed"]
        and result["preflight"]["http_status"] == 200
        and result["preflight"]["allows_origin"]
        and result["preflight"]["allows_required_headers"]
        and result["invalid_method_rejected_before_payment"]
        and result["private_files_not_exposed"]
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
