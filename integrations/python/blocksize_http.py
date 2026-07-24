"""Small dependency-free HTTP client shared by the Python agent examples."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class BlocksizePaymentRequired(RuntimeError):
    """Raised when Blocksize returns a valid x402 payment challenge."""

    def __init__(self, message: str, *, payment_required: str | None = None) -> None:
        super().__init__(message)
        self.payment_required = payment_required


class BlocksizeHTTPClient:
    """Read-only Blocksize HTTP client for agent framework tools."""

    def __init__(
        self,
        agent_id: str,
        *,
        base_url: str = "https://mcp.blocksize.info",
        timeout: float = 20.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        clean_agent_id = agent_id.strip()
        if len(clean_agent_id) < 8:
            raise ValueError("agent_id must be a stable identifier with at least 8 characters")
        self.agent_id = clean_agent_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener

    @staticmethod
    def _instrument(value: str) -> str:
        clean = value.strip().lower().replace("_", "-").replace("/", "-")
        if not clean or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in clean):
            raise ValueError("instrument must contain only letters, numbers, slash, underscore, or hyphen")
        return quote(clean, safe="-")

    def _get(self, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        suffix = f"?{urlencode(query)}" if query else ""
        request = Request(
            f"{self.base_url}{path}{suffix}",
            headers={
                "Accept": "application/json",
                "X-Agent-ID": self.agent_id,
                "User-Agent": "blocksize-framework-integration/1.0",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 402:
                raise BlocksizePaymentRequired(
                    "Blocksize starter credits are unavailable or exhausted; complete the returned x402 challenge.",
                    payment_required=exc.headers.get("PAYMENT-REQUIRED"),
                ) from exc
            body = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"Blocksize returned HTTP {exc.code}: {body}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Blocksize returned a non-object JSON response")
        return payload

    def search(self, query: str, *, asset_class: str | None = None) -> dict[str, Any]:
        params = {"q": query.strip()}
        if asset_class:
            params["asset_class"] = asset_class.strip().lower()
        return self._get("/v1/search", params)

    def get_vwap(self, pair: str) -> dict[str, Any]:
        return self._get(f"/v1/vwap/{self._instrument(pair)}")

    def get_bid_ask(self, pair: str) -> dict[str, Any]:
        return self._get(f"/v1/bidask/{self._instrument(pair)}")
