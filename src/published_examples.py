"""Published example instruments: rotate them, and mark copies of them.

Usage insights showed the top three tickers mirroring the examples in docs and
listings, so copied examples looked like demand. This module is the one place
that decides which instrument an example shows (rotating weekly on generated
surfaces) and tags every published example URL so a copy carries
``selection_source=published_example_path`` into the usage ledger.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SELECTION_SOURCE = "published_example_path"
QUERY_KEY = "selection_source"

# Rotation pools per raw-data service. Index 0 is the spelling the static docs
# use, so a reader of the manual and a reader of a generated listing see the
# same instrument in the first week of a cycle.
EXAMPLE_POOLS: dict[str, tuple[str, ...]] = {
    "vwap": ("BTCUSD", "ETHUSD", "SOLUSD"),
    "bidask": ("AAPLXUSD", "ETHUSD", "TSLAXUSD"),
    "state": ("MSOLUSD", "JUPSOLUSD", "WSTETHUSD"),
    "vwap30m": ("SOLUSD", "ETHUSD", "BTCUSD"),
    "vwap24h": ("BTCUSD", "SOLUSD", "ETHUSD"),
    "fx": ("EURUSD", "GBPUSD", "USDJPY"),
    "metal": ("XAUUSD", "XAGUSD", "XPTUSD"),
}
RAW_SERVICES = tuple(EXAMPLE_POOLS)


def normalise_symbol(symbol: str) -> str:
    """Fold the spellings published material uses (BTC-USD, btc/usd) into one key."""
    return "".join(ch for ch in str(symbol).upper() if ch.isalnum())


PUBLISHED_SYMBOLS: frozenset[tuple[str, str]] = frozenset(
    (service, normalise_symbol(symbol))
    for service, pool in EXAMPLE_POOLS.items()
    for symbol in pool
)


def _week_index(when: date | None) -> int:
    today = when or datetime.now(UTC).date()
    year, week, _ = today.isocalendar()
    return year * 53 + week


def example_symbol(service: str, *, when: date | None = None) -> str:
    """Return the example instrument a generated listing shows this week."""
    pool = EXAMPLE_POOLS[service]
    return pool[_week_index(when) % len(pool)]


def example_path(service: str, *, when: date | None = None) -> str:
    return f"/v1/{service}/{example_symbol(service, when=when)}"


def tag_url(url: str) -> str:
    """Append the published-example tag unless the URL already carries a source."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == QUERY_KEY for key, _ in query):
        return url
    query.append((QUERY_KEY, SELECTION_SOURCE))
    return urlunsplit(parts._replace(query=urlencode(query)))


def service_and_symbol(path: str) -> tuple[str, str] | None:
    """Split ``/v1/{service}/{symbol}`` for the raw-data services, else None."""
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "v1" or parts[1] not in EXAMPLE_POOLS:
        return None
    symbol = normalise_symbol(parts[2])
    return (parts[1], symbol) if symbol else None


def is_published_example(service: str, symbol: str) -> bool:
    """True when the instrument is one that published material shows for the service."""
    return (service, normalise_symbol(symbol)) in PUBLISHED_SYMBOLS


def is_published_example_path(path: str) -> bool:
    found = service_and_symbol(path)
    return bool(found) and found in PUBLISHED_SYMBOLS
