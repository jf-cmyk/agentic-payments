"""Live Solana/Jupiter discovery for RWA token and route identifiers."""

from __future__ import annotations

import asyncio
import csv
import json
import os
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from src.rwa_adapters import JUPITER_DEFAULT_TOKEN_MINTS, JupiterRouterAdapter
from src.rwa_dex_allowlist import build_dex_allowlist
from src.runtime_data import RWA_REPORTS_DIR, resolve_required_rwa_report_path


DEFAULT_TOKEN_REGISTRY_PATH = resolve_required_rwa_report_path(
    "rwa_solana_token_mints.json"
)
DEFAULT_ROUTE_ALLOWLIST_PATH = resolve_required_rwa_report_path(
    "rwa_jupiter_route_allowlist.json"
)
DEFAULT_TOKEN_CSV_PATH = RWA_REPORTS_DIR / "rwa_solana_token_mints.csv"
DEFAULT_ROUTE_CSV_PATH = RWA_REPORTS_DIR / "rwa_jupiter_route_allowlist.csv"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _token_key(value: str) -> str:
    return value.strip().upper().replace("-", "").replace("/", "")


def _symbol_parts(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
    else:
        base, quote = symbol, "USD"
    if quote.upper() == "USD":
        quote = "USDC"
    return base, quote


def _jupiter_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key} if api_key else {}


def build_solana_token_targets() -> dict[str, Any]:
    """Return Solana token symbols that need mint identity for RWA DEX work."""
    allowlist = build_dex_allowlist()
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in allowlist["candidates"]:
        if row["chain"] != "solana":
            continue
        base, quote = _symbol_parts(str(row["symbol"]))
        for token_symbol, role in ((base, "base"), (quote, "quote")):
            token_key = _token_key(token_symbol)
            target = by_symbol.setdefault(
                token_key,
                {
                    "query_symbol": token_symbol,
                    "token_key": token_key,
                    "roles": set(),
                    "asset_ids": set(),
                    "asset_classes": set(),
                    "venues": set(),
                    "source_symbols": set(),
                },
            )
            target["roles"].add(role)
            target["asset_ids"].add(row["asset_id"] if role == "base" else quote.upper())
            target["asset_classes"].add(row["asset_class"])
            target["venues"].add(row["venue"])
            target["source_symbols"].add(row["symbol"])

    targets = []
    for item in by_symbol.values():
        targets.append(
            {
                **item,
                "roles": sorted(item["roles"]),
                "asset_ids": sorted(item["asset_ids"]),
                "asset_classes": sorted(item["asset_classes"]),
                "venues": sorted(item["venues"]),
                "source_symbols": sorted(item["source_symbols"]),
            }
        )
    return {
        "summary": {"target_count": len(targets)},
        "targets": sorted(targets, key=lambda row: str(row["token_key"])),
    }


def _rank_token_candidates(query_symbol: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    exact_key = _token_key(query_symbol)
    candidates = [item for item in payload if isinstance(item, dict)]
    candidates.sort(
        key=lambda item: (
            _token_key(str(item.get("symbol") or "")) != exact_key,
            not bool(item.get("isVerified")),
            -float(item.get("liquidity") or 0),
            -float(item.get("organicScore") or 0),
            str(item.get("id") or ""),
        )
    )
    return candidates


async def _get_json(
    client: httpx.AsyncClient,
    *,
    path: str,
    params: dict[str, Any],
    api_key: str,
) -> Any:
    for attempt in range(4):
        response = await client.get(path, params=params, headers=_jupiter_headers(api_key))
        if response.status_code == 429 and attempt < 3:
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"Jupiter {path} returned HTTP {response.status_code}: {response.text[:500]}") from exc
        return response.json()
    raise ValueError(f"Jupiter {path} retry loop ended without a response")


async def discover_solana_token_mints(
    *,
    api_key: str | None = None,
    base_url: str = "https://api.jup.ag",
    limit: int | None = None,
) -> dict[str, Any]:
    """Discover Solana token mint metadata for allowlisted RWA symbols."""
    key = (api_key if api_key is not None else os.getenv("JUPITER_API_KEY", "")).strip()
    targets = build_solana_token_targets()["targets"]
    if limit is not None:
        targets = targets[: max(1, int(limit))]

    tokens = []
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15) as client:
        for target in targets:
            query = str(target["query_symbol"])
            try:
                default_token = JUPITER_DEFAULT_TOKEN_MINTS.get(_token_key(query))
                if default_token is not None:
                    token_row = {
                        **target,
                        "status": "resolved",
                        "mint": default_token["mint"],
                        "symbol": default_token["symbol"],
                        "name": default_token["symbol"],
                        "decimals": default_token["decimals"],
                        "is_verified": True,
                        "tags": [],
                        "liquidity": None,
                        "organic_score": None,
                        "organic_score_label": None,
                        "holder_count": None,
                        "first_pool": None,
                        "updated_at": None,
                        "candidate_count": 1,
                        "candidate_mints": [
                            {
                                "mint": default_token["mint"],
                                "symbol": default_token["symbol"],
                                "name": default_token["symbol"],
                                "is_verified": True,
                                "liquidity": None,
                                "organic_score": None,
                                "tags": [],
                            }
                        ],
                        "review_status": "default_verified_quote_token",
                        "source": default_token.get("source", "jupiter_default_token_mints"),
                    }
                    tokens.append(token_row)
                    continue
                payload = await _get_json(
                    client,
                    path="/tokens/v2/search",
                    params={"query": query},
                    api_key=key,
                )
                candidates = _rank_token_candidates(query, payload)
                selected = candidates[0] if candidates else {}
                token_row = {
                    **target,
                    "status": "resolved" if selected else "unresolved",
                    "mint": selected.get("id"),
                    "symbol": selected.get("symbol"),
                    "name": selected.get("name"),
                    "decimals": selected.get("decimals"),
                    "is_verified": bool(selected.get("isVerified")),
                    "tags": selected.get("tags") or [],
                    "liquidity": selected.get("liquidity"),
                    "organic_score": selected.get("organicScore"),
                    "organic_score_label": selected.get("organicScoreLabel"),
                    "holder_count": selected.get("holderCount"),
                    "first_pool": selected.get("firstPool"),
                    "updated_at": selected.get("updatedAt"),
                    "candidate_count": len(candidates),
                    "candidate_mints": [
                        {
                            "mint": item.get("id"),
                            "symbol": item.get("symbol"),
                            "name": item.get("name"),
                            "is_verified": bool(item.get("isVerified")),
                            "liquidity": item.get("liquidity"),
                            "organic_score": item.get("organicScore"),
                            "tags": item.get("tags") or [],
                        }
                        for item in candidates[:5]
                    ],
                    "review_status": (
                        "jupiter_verified_needs_internal_review"
                        if selected.get("isVerified")
                        else "needs_manual_review"
                    ),
                    "source": "jupiter_tokens_v2_search",
                }
            except Exception as exc:  # pragma: no cover - live network defensive path.
                token_row = {
                    **target,
                    "status": "error",
                    "error": str(exc),
                    "review_status": "needs_manual_review",
                    "source": "jupiter_tokens_v2_search",
                }
            tokens.append(token_row)
            await asyncio.sleep(0.75)

    by_status = Counter(str(row["status"]) for row in tokens)
    by_venue = Counter(venue for row in tokens for venue in row["venues"])
    return {
        "product": "rwa_solana_token_mint_registry",
        "as_of": _utc_now_iso(),
        "summary": {
            "token_count": len(tokens),
            "resolved": by_status.get("resolved", 0),
            "unresolved": by_status.get("unresolved", 0),
            "errors": by_status.get("error", 0),
            "verified": sum(1 for row in tokens if row.get("is_verified")),
            "by_status": dict(sorted(by_status.items())),
            "by_venue": dict(sorted(by_venue.items())),
        },
        "quality_policy": {
            "source": "Jupiter Tokens V2 search",
            "promotion_status": "candidate_registry; internal issuer/underlying review still required before production promotion",
            "secret_policy": "API key is used only in the request header and is never persisted",
        },
        "tokens": sorted(tokens, key=lambda row: str(row["token_key"])),
    }


def _token_mints_for_adapter(token_registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mints: dict[str, dict[str, Any]] = {}
    for row in token_registry.get("tokens", []):
        if row.get("status") != "resolved" or not row.get("mint") or row.get("decimals") is None:
            continue
        symbol = str(row.get("query_symbol") or row.get("symbol") or row["token_key"])
        mints[_token_key(symbol)] = {
            "symbol": str(row.get("symbol") or symbol).upper(),
            "mint": str(row["mint"]),
            "decimals": int(row["decimals"]),
            "source": "rwa_solana_token_mint_registry",
            "status": "verified" if row.get("is_verified") else "unverified_registry_result",
        }
    return mints


def _route_steps(route_plan: Any) -> list[dict[str, Any]]:
    steps = []
    for item in route_plan or []:
        if not isinstance(item, dict):
            continue
        swap_info = item.get("swapInfo") or {}
        if not isinstance(swap_info, dict):
            swap_info = {}
        steps.append(
            {
                "label": swap_info.get("label"),
                "amm_key": swap_info.get("ammKey"),
                "input_mint": swap_info.get("inputMint"),
                "output_mint": swap_info.get("outputMint"),
                "percent": item.get("percent"),
                "bps": item.get("bps"),
            }
        )
    return steps


async def discover_jupiter_routes(
    *,
    token_registry: dict[str, Any] | None = None,
    api_key: str | None = None,
    base_url: str = "https://api.jup.ag",
    limit: int | None = None,
    include_order_book: bool = True,
    depth: int = 2,
) -> dict[str, Any]:
    """Probe Jupiter route candidates and capture route identifiers."""
    key = (api_key if api_key is not None else os.getenv("JUPITER_API_KEY", "")).strip()
    registry = token_registry or {"tokens": []}
    token_mints = _token_mints_for_adapter(registry)
    candidates = build_dex_allowlist(venue="jupiter_router")["candidates"]
    if limit is not None:
        candidates = candidates[: max(1, int(limit))]

    routes = []
    adapter = JupiterRouterAdapter(api_key=key, token_mints=token_mints, base_url=base_url)
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        try:
            bidask = await adapter.fetch_bidask(symbol)
            metadata = bidask.get("metadata") or {}
            ask_quote = metadata.get("ask_quote") or {}
            bid_quote = metadata.get("bid_quote") or {}
            route_row: dict[str, Any] = {
                "allowlist_id": candidate["allowlist_id"],
                "symbol": symbol,
                "asset_id": candidate["asset_id"],
                "asset_class": candidate["asset_class"],
                "venue": "jupiter_router",
                "status": "route_discovered",
                "source_type": "quote_sweep",
                "bid": bidask.get("bid"),
                "ask": bidask.get("ask"),
                "timestamp": bidask.get("timestamp"),
                "base_token": metadata.get("base_token"),
                "quote_token": metadata.get("quote_token"),
                "ask_context_slot": ask_quote.get("context_slot"),
                "bid_context_slot": bid_quote.get("context_slot"),
                "ask_price_impact_pct": ask_quote.get("price_impact_pct"),
                "bid_price_impact_pct": bid_quote.get("price_impact_pct"),
                "ask_route_steps": _route_steps(ask_quote.get("route_plan")),
                "bid_route_steps": _route_steps(bid_quote.get("route_plan")),
                "promotion_status": "candidate_route_requires_liquidity_volume_manipulation_and_benchmark_checks",
            }
            if include_order_book:
                order_book = await adapter.fetch_order_book(symbol, side="buy", depth=depth)
                route_row["sweep_levels"] = order_book.get("levels") or []
                route_row["sweep_quotes"] = (order_book.get("metadata") or {}).get("sweep_quotes") or []
            routes.append(route_row)
        except Exception as exc:  # pragma: no cover - live network defensive path.
            routes.append(
                {
                    "allowlist_id": candidate["allowlist_id"],
                    "symbol": symbol,
                    "asset_id": candidate["asset_id"],
                    "asset_class": candidate["asset_class"],
                    "venue": "jupiter_router",
                    "status": "error",
                    "error": str(exc),
                    "promotion_status": "needs_manual_review",
                }
            )
        await asyncio.sleep(0.1)
        await asyncio.sleep(0.75)

    by_status = Counter(str(row["status"]) for row in routes)
    by_label = Counter(
        str(step.get("label"))
        for row in routes
        for step in [*(row.get("ask_route_steps") or []), *(row.get("bid_route_steps") or [])]
        if step.get("label")
    )
    return {
        "product": "rwa_jupiter_route_allowlist",
        "as_of": _utc_now_iso(),
        "summary": {
            "route_count": len(routes),
            "route_discovered": by_status.get("route_discovered", 0),
            "errors": by_status.get("error", 0),
            "by_status": dict(sorted(by_status.items())),
            "by_route_label": dict(sorted(by_label.items())),
        },
        "quality_policy": {
            "source": "Jupiter /swap/v1/quote",
            "source_type": "quote_sweep",
            "promotion_status": "route evidence only; pool-specific adapters still require pool state and liquidity checks",
            "secret_policy": "API key is used only in the request header and is never persisted",
        },
        "routes": sorted(routes, key=lambda row: str(row["allowlist_id"])),
    }


def write_solana_discovery_reports(
    *,
    token_json_path: str | Path = DEFAULT_TOKEN_REGISTRY_PATH,
    route_json_path: str | Path = DEFAULT_ROUTE_ALLOWLIST_PATH,
    token_csv_path: str | Path = DEFAULT_TOKEN_CSV_PATH,
    route_csv_path: str | Path = DEFAULT_ROUTE_CSV_PATH,
    api_key: str | None = None,
    token_limit: int | None = None,
    route_limit: int | None = None,
    include_routes: bool = True,
) -> dict[str, Any]:
    """Run live Solana/Jupiter discovery and write JSON/CSV artifacts."""

    async def _run() -> tuple[dict[str, Any], dict[str, Any] | None]:
        token_registry = await discover_solana_token_mints(api_key=api_key, limit=token_limit)
        route_registry = None
        if include_routes:
            route_registry = await discover_jupiter_routes(
                token_registry=token_registry,
                api_key=api_key,
                limit=route_limit,
            )
        return token_registry, route_registry

    token_registry, route_registry = asyncio.run(_run())
    token_json = Path(token_json_path)
    route_json = Path(route_json_path)
    token_csv = Path(token_csv_path)
    route_csv = Path(route_csv_path)
    for path in (token_json, route_json, token_csv, route_csv):
        path.parent.mkdir(parents=True, exist_ok=True)

    token_json.write_text(json.dumps(token_registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with token_csv.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "token_key",
            "query_symbol",
            "symbol",
            "mint",
            "decimals",
            "status",
            "is_verified",
            "liquidity",
            "organic_score",
            "venues",
            "source_symbols",
            "review_status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in token_registry["tokens"]:
            writer.writerow(
                {
                    "token_key": row.get("token_key"),
                    "query_symbol": row.get("query_symbol"),
                    "symbol": row.get("symbol"),
                    "mint": row.get("mint"),
                    "decimals": row.get("decimals"),
                    "status": row.get("status"),
                    "is_verified": row.get("is_verified"),
                    "liquidity": row.get("liquidity"),
                    "organic_score": row.get("organic_score"),
                    "venues": json.dumps(row.get("venues") or [], sort_keys=True),
                    "source_symbols": json.dumps(row.get("source_symbols") or [], sort_keys=True),
                    "review_status": row.get("review_status"),
                }
            )

    if route_registry is not None:
        route_json.write_text(json.dumps(route_registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with route_csv.open("w", newline="", encoding="utf-8") as handle:
            fieldnames = [
                "allowlist_id",
                "symbol",
                "asset_id",
                "asset_class",
                "status",
                "bid",
                "ask",
                "ask_context_slot",
                "ask_price_impact_pct",
                "ask_route_labels",
                "promotion_status",
                "error",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in route_registry["routes"]:
                writer.writerow(
                    {
                        "allowlist_id": row.get("allowlist_id"),
                        "symbol": row.get("symbol"),
                        "asset_id": row.get("asset_id"),
                        "asset_class": row.get("asset_class"),
                        "status": row.get("status"),
                        "bid": row.get("bid"),
                        "ask": row.get("ask"),
                        "ask_context_slot": row.get("ask_context_slot"),
                        "ask_price_impact_pct": row.get("ask_price_impact_pct"),
                        "ask_route_labels": json.dumps(
                            [step.get("label") for step in row.get("ask_route_steps") or []],
                            sort_keys=True,
                        ),
                        "promotion_status": row.get("promotion_status"),
                        "error": row.get("error"),
                    }
                )

    return {
        "token_registry": token_registry,
        "route_allowlist": deepcopy(route_registry) if route_registry is not None else None,
    }
