"""Deterministic instrument intent resolution for human and agent discovery."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Iterable

from src.models import PairInfo


SERVICE_TO_ENDPOINT = {
    "vwap": "/v1/vwap/{symbol}",
    "bidask": "/v1/bidask/{symbol}",
    "state": "/v1/state/{symbol}",
    "vwap30m": "/v1/vwap30m/{symbol}",
    "vwap24h": "/v1/vwap24h/{symbol}",
    "fx": "/v1/fx/{symbol}",
    "metal": "/v1/metal/{symbol}",
}

SERVICE_TO_TOOL = {
    "vwap": "get_vwap",
    "bidask": "get_bid_ask",
    "state": "get_state_price",
    "vwap30m": "get_vwap_30min",
    "vwap24h": "get_vwap_24h",
    "fx": "get_fx_rate",
    "metal": "get_metal_price",
}

# Only high-confidence names and market-language aliases belong here. Ambiguous
# words intentionally remain ordinary substring searches.
INSTRUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "bitcoin": ("BTC", "BTCUSD", "BTCUSDT", "BTCUSDC"),
    "ethereum": ("ETH", "ETHUSD", "ETHUSDT", "ETHUSDC"),
    "ether": ("ETH", "ETHUSD", "ETHUSDT", "ETHUSDC"),
    "solana": ("SOL", "SOLUSD", "SOLUSDT", "SOLUSDC"),
    "apple": ("AAPL", "AAPLX"),
    "microsoft": ("MSFT", "MSFTX"),
    "nvidia": ("NVDA", "NVDAX"),
    "tesla": ("TSLA", "TSLAX"),
    "gold": ("XAUUSD", "XAU"),
    "silver": ("XAGUSD", "XAG"),
    "platinum": ("XPTUSD", "XPT"),
    "palladium": ("XPDUSD", "XPD"),
    "copper": ("COPPERUSD", "COPPER"),
    "euro dollar": ("EURUSD",),
    "euro usd": ("EURUSD",),
    "pound dollar": ("GBPUSD",),
    "sterling dollar": ("GBPUSD",),
}

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_SERVICE_WORD_RE = re.compile(
    r"\b(?:latest|current|live|price|quote|rate|market|data|spot|snapshot|feed|"
    r"vwap|volume\s+weighted|bid\s*/?\s*ask|spread|best\s+bid|best\s+ask|"
    r"state|amm|pool|oracle|reference|30\s*(?:m|min|minute)|thirty\s+minute|"
    r"24\s*(?:h|hour)|daily)\b",
    flags=re.IGNORECASE,
)


def normalize_instrument_text(value: str) -> str:
    """Normalize a symbol or natural-language term for deterministic matching."""
    return _NON_ALNUM_RE.sub("", value.upper())


def extract_instrument_intent(query: str) -> str:
    """Remove feed-language words while retaining the requested instrument."""
    stripped = _SERVICE_WORD_RE.sub(" ", query)
    stripped = " ".join(stripped.split()).strip(" -_/")
    return stripped or query.strip()


def _alias_targets(query: str) -> tuple[str, ...]:
    lowered = " ".join(query.lower().replace("_", " ").split())
    phrase_text = " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())
    normalized = normalize_instrument_text(lowered)
    targets: list[str] = []
    for alias, values in INSTRUMENT_ALIASES.items():
        alias_normalized = normalize_instrument_text(alias)
        alias_pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        if (
            lowered == alias
            or normalized == alias_normalized
            or re.search(alias_pattern, phrase_text)
        ):
            targets.extend(normalize_instrument_text(value) for value in values)
    return tuple(dict.fromkeys(targets))


def _match_instrument(
    query: str,
    *,
    symbol: str,
    base_currency: str = "",
    quote_currency: str = "",
) -> tuple[int, str] | None:
    intent = extract_instrument_intent(query)
    query_key = normalize_instrument_text(intent)
    symbol_key = normalize_instrument_text(symbol)
    base_key = normalize_instrument_text(base_currency)
    quote_key = normalize_instrument_text(quote_currency)
    if not query_key:
        return 100, "catalog"

    if query_key == symbol_key:
        return 1000, "exact_symbol"

    alias_targets = _alias_targets(intent)
    if symbol_key in alias_targets or base_key in alias_targets:
        # A natural-language alias expresses canonical intent. Prefer its
        # configured market symbol (for example bitcoin -> BTCUSD) over a
        # literal long-form catalog base such as BITCOINUSD. This keeps the
        # cheapest, most familiar conversion path at the top of discovery.
        return 980, "alias"
    if query_key == base_key:
        return 960, "exact_base"
    if any(
        symbol_key.startswith(target) or base_key.startswith(target)
        for target in alias_targets
    ):
        return 900, "alias"

    searchable = tuple(value for value in (symbol_key, base_key, quote_key) if value)
    if any(value.startswith(query_key) for value in searchable):
        return 760, "prefix"
    if any(query_key in value for value in searchable):
        return 600, "substring"
    return None


def recommended_service(pair: PairInfo) -> str | None:
    """Choose a general-use service without inventing unverified coverage."""
    preferred = {
        "equity": ("bidask",),
        "fx": ("fx",),
        "metal": ("metal",),
        "crypto": ("vwap", "bidask", "state"),
    }.get(pair.asset_class, tuple(pair.services))
    for service in preferred:
        if service in pair.services:
            return service
    return pair.services[0] if pair.services else None


def commercialize_pair(pair: PairInfo, pricing: Any) -> PairInfo:
    """Attach the current purchase path and price to a catalog-confirmed result."""
    service = pair.recommended_service or recommended_service(pair)
    if service in {"fx", "metal"}:
        price = pricing.tradfi
    elif pair.asset_class == "equity":
        price = pricing.equities
    elif pair.tier == "extended":
        price = pricing.extended_crypto
    else:
        price = pricing.core_crypto
    endpoint = SERVICE_TO_ENDPOINT.get(service or "")
    canonical = pair.canonical_symbol or normalize_instrument_text(pair.pair)
    return pair.model_copy(
        update={
            "canonical_symbol": canonical,
            "recommended_service": service,
            "recommended_tool": SERVICE_TO_TOOL.get(service or ""),
            "endpoint_path": endpoint.format(symbol=canonical) if endpoint else None,
            "price_usdc": str(price) if service else None,
            "readiness": "catalog_confirmed" if service else "metadata_only",
        }
    )


def _merge_pair_rows(pairs: Iterable[PairInfo]) -> list[PairInfo]:
    merged: dict[tuple[str, str], PairInfo] = {}
    for pair in pairs:
        key = (pair.asset_class, normalize_instrument_text(pair.pair))
        current = merged.get(key)
        if current is None:
            merged[key] = pair
            continue
        merged[key] = current.model_copy(
            update={
                "services": list(dict.fromkeys([*current.services, *pair.services])),
                "capability_check_services": list(
                    dict.fromkeys(
                        [
                            *current.capability_check_services,
                            *pair.capability_check_services,
                        ]
                    )
                ),
            }
        )
    return list(merged.values())


def rank_pair_candidates(
    query: str,
    pairs: Iterable[PairInfo],
    *,
    limit: int | None = None,
    diversify_asset_classes: bool = True,
) -> list[PairInfo]:
    """Rank exact and alias matches first while preserving catalog diversity."""
    ranked: list[PairInfo] = []
    alias_targets = _alias_targets(extract_instrument_intent(query))
    for pair in _merge_pair_rows(pairs):
        match = _match_instrument(
            query,
            symbol=pair.pair,
            base_currency=pair.base_currency,
            quote_currency=pair.quote_currency,
        )
        if match is None:
            continue
        score, match_type = match
        if alias_targets and match_type in {"prefix", "substring"}:
            continue
        service = recommended_service(pair)
        endpoint = SERVICE_TO_ENDPOINT.get(service or "")
        ranked.append(
            pair.model_copy(
                update={
                    "canonical_symbol": normalize_instrument_text(pair.pair),
                    "recommended_service": service,
                    "recommended_tool": SERVICE_TO_TOOL.get(service or ""),
                    "endpoint_path": (
                        endpoint.format(symbol=normalize_instrument_text(pair.pair))
                        if endpoint
                        else None
                    ),
                    "match_type": match_type,
                    "relevance_score": score,
                }
            )
        )

    quote_priority = {"USD": 0, "USDC": 1, "USDT": 2, "EUR": 3, "BTC": 4, "ETH": 5}
    ranked.sort(
        key=lambda pair: (
            -pair.relevance_score,
            quote_priority.get(pair.quote_currency.upper(), 6),
            pair.asset_class,
            normalize_instrument_text(pair.pair),
        )
    )
    if not diversify_asset_classes or len({pair.asset_class for pair in ranked}) <= 1:
        return ranked if limit is None else ranked[:limit]

    selected: list[PairInfo] = [pair for pair in ranked if pair.relevance_score >= 900]
    selected_keys = {(pair.asset_class, pair.pair) for pair in selected}
    buckets: dict[str, deque[PairInfo]] = defaultdict(deque)
    for pair in ranked:
        if (pair.asset_class, pair.pair) not in selected_keys:
            buckets[pair.asset_class].append(pair)
    class_order = [asset for asset in ("crypto", "equity", "fx", "metal") if buckets[asset]]
    target = len(ranked) if limit is None else limit
    while len(selected) < target and class_order:
        next_order: list[str] = []
        for asset_class in class_order:
            bucket = buckets[asset_class]
            if bucket and len(selected) < target:
                selected.append(bucket.popleft())
            if bucket:
                next_order.append(asset_class)
        class_order = next_order
    return selected[:target]
