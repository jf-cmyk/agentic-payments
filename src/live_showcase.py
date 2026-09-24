"""Free live showcase: one real, attributed price an agent can inspect before paying.

The showcase exists so an agent can see actual Blocksize data quality (multi-venue
VWAP, bid/ask spread, freshness, and a recomputable provenance digest) without a
payment or an account. It is deliberately bounded: an allowlist of symbols, a
short server-side cache, and the public discovery rate limit. It is never a
production feed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlencode

from src.config import settings
from src.public_metadata import PUBLIC_BASE_URL

LIVE_SHOWCASE_PATH = "/v1/samples/live-showcase"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")


def normalise_showcase_symbol(value: str) -> str | None:
    """Return the upstream-safe symbol, or None when it cannot be a symbol."""
    clean = value.strip().replace("-", "").replace("/", "").replace("_", "").upper()
    return clean if _SYMBOL_RE.fullmatch(clean) else None


def showcase_enabled() -> bool:
    return bool(settings.server.showcase_live_enabled) and bool(allowed_showcase_symbols())


def allowed_showcase_symbols() -> tuple[str, ...]:
    """Configured allowlist, normalised and de-duplicated, in configured order."""
    symbols: list[str] = []
    for raw in str(settings.server.showcase_live_symbols or "").split(","):
        clean = normalise_showcase_symbol(raw)
        if clean and clean not in symbols:
            symbols.append(clean)
    return tuple(symbols)


def showcase_url(symbol: str | None = None) -> str | None:
    """Public URL of the free showcase call, or None when it is disabled."""
    allowed = allowed_showcase_symbols()
    if not settings.server.showcase_live_enabled or not allowed:
        return None
    chosen = symbol if symbol in allowed else allowed[0]
    return f"{PUBLIC_BASE_URL}{LIVE_SHOWCASE_PATH}?{urlencode({'symbol': chosen})}"


def showcase_handoff() -> dict[str, Any] | None:
    """Agent-facing description of the showcase for discovery surfaces."""
    url = showcase_url()
    if url is None:
        return None
    return {
        "url": url,
        "symbols": list(allowed_showcase_symbols()),
        "purpose": (
            "One free, real, attributed price with multi-venue VWAP, bid/ask spread, "
            "freshness, and a recomputable provenance digest. Call it to inspect "
            "Blocksize data quality before spending credits or paying."
        ),
        "returns_live_data": True,
        "starts_payment": False,
        "licence": "Evaluation only; attribution 'Data by Blocksize' required.",
    }


def canonical_digest(payload: Any) -> str:
    """Unsalted, recomputable sha256 over the canonical JSON of a payload."""
    stable = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(stable.encode('utf-8')).hexdigest()}"


DIGEST_VERIFY_INSTRUCTIONS = (
    "Recompute sha256 over json.dumps(data, sort_keys=True, separators=(',', ':'), "
    "default=str) encoded as UTF-8 and compare it with payload_digest."
)
