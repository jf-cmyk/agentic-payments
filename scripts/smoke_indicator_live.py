from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.credit_manager import CreditManager
from src.resource_server import app


def compact(payload: dict) -> dict:
    """Keep smoke output readable while preserving feed coverage and metrics."""
    if payload.get("status") != "ok":
        return payload

    product = payload.get("product")
    base = {
        "status": payload.get("status"),
        "product": product,
        "credit_cost": payload.get("credit_cost"),
        "credits_remaining": (
            payload.get("meta", {})
            .get("credits", {})
            .get("credits_remaining")
        ),
        "methodology": payload.get("methodology", {}).get("type"),
        "receipt_id": (
            payload.get("provenance", {}) or payload.get("receipt", {})
        ).get("receipt_id"),
    }
    if product == "token_market_quality_indicator":
        base["indicator"] = payload.get("indicator")
    elif product == "state_divergence_indicator":
        base["state"] = payload.get("state")
        base["basis"] = payload.get("basis")
        base["coverage"] = {
            name: item.get("status")
            for name, item in payload.get("components", {}).items()
        }
    elif product == "solana_token_brief":
        base["summary"] = payload.get("summary")
        base["ranked_symbols"] = payload.get("ranked_symbols")
        base["token_statuses"] = [
            {
                "symbol": item.get("symbol"),
                "status": item.get("status"),
                "score": (item.get("indicator") or {}).get("score"),
                "flags": (item.get("indicator") or {}).get("flags"),
            }
            for item in payload.get("tokens", [])
        ]
    elif product == "trader_alpha_pack":
        base["summary"] = payload.get("summary")
        base["ranked_signals"] = payload.get("ranked_signals")
        base["alerts"] = payload.get("alerts")
    return base


def main() -> None:
    Path("/tmp/blocksize-live-indicator-smoke.db").unlink(missing_ok=True)
    with TestClient(app) as client:
        app.state.credits = CreditManager("/tmp/blocksize-live-indicator-smoke.db")
        cases = [
            (
                "token_quality",
                "/v1/indicators/token-quality",
                {
                    "X-AGENT-ID": "live-indicator-token-quality-0001",
                    "X-DEVICE-ID": "live-device-token-quality-0001",
                    "X-SESSION-ID": "live-session-token-quality-0001",
                    "X-Forwarded-For": "198.51.100.10",
                    "User-Agent": "blocksize-live-indicator-smoke/1.0",
                },
                {"symbol": "SOLUSD", "include_state_coverage": True},
            ),
            (
                "state_divergence",
                "/v1/indicators/state-divergence",
                {
                    "X-AGENT-ID": "live-indicator-state-divergence-0001",
                    "X-DEVICE-ID": "live-device-state-divergence-0001",
                    "X-SESSION-ID": "live-session-state-divergence-0001",
                    "X-Forwarded-For": "198.51.100.11",
                    "User-Agent": "blocksize-live-indicator-smoke/1.0",
                },
                {"symbol": "MSOLUSD", "max_divergence_bps": 75, "include_state_coverage": True},
            ),
            (
                "solana_token_brief",
                "/v1/signals/solana-token-brief",
                {
                    "X-AGENT-ID": "live-indicator-solana-brief-0001",
                    "X-DEVICE-ID": "live-device-solana-brief-0001",
                    "X-SESSION-ID": "live-session-solana-brief-0001",
                    "X-Forwarded-For": "198.51.100.12",
                    "User-Agent": "blocksize-live-indicator-smoke/1.0",
                },
                {"symbols": ["SOLUSD", "JUPUSD", "PYTHUSD", "MSOLUSD"], "include_state_coverage": True},
            ),
            (
                "trader_alpha_pack",
                "/v1/signals/trader-alpha-pack",
                {
                    "X-AGENT-ID": "live-indicator-alpha-pack-0001",
                    "X-DEVICE-ID": "live-device-alpha-pack-0001",
                    "X-SESSION-ID": "live-session-alpha-pack-0001",
                    "X-Forwarded-For": "198.51.100.13",
                    "User-Agent": "blocksize-live-indicator-smoke/1.0",
                },
                {"watchlist": ["BTCUSD", "ETHUSD", "SOLUSD"], "include_state_coverage": True},
            ),
        ]
        for label, path, headers, body in cases:
            response = client.post(path, headers=headers, json=body)
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text}
            print(
                json.dumps(
                    {
                        "case": label,
                        "status_code": response.status_code,
                        "output": compact(payload) if isinstance(payload, dict) else payload,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
