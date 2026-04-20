"""
Pydantic models for "Decision-Ready" data responses.

These models transform raw Blocksize API output into token-efficient,
structured summaries optimized for LLM consumption. Agents receive
exactly what they need for reasoning — no wasted tokens on raw arrays.

Asset classes covered:
  - Crypto (RT VWAP, Bid/Ask, 30-Min VWAP, 24-Hr VWAP, State Price)
  - Equities (US + Chinese stocks)
  - FX (129 currency pairs)
  - Metals (Gold, Silver, Platinum, Palladium, Copper)
  - US Treasury Rates (1M–30Y yields)
  - Commodities
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# VWAP Responses (Real-Time, 30-Min, 24-Hour)
# ---------------------------------------------------------------------------

class VWAPData(BaseModel):
    """Volume Weighted Average Price — single pair snapshot."""

    pair: str = Field(..., description="Trading pair (e.g., BTC-USD)")
    vwap: float = Field(..., description="Current VWAP price")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    currency: str = Field("", description="Quote currency")
    source: str = Field("blocksize", description="Data provider identifier")

    def to_decision_summary(self) -> str:
        """Format as a concise string for LLM consumption."""
        return (
            f"VWAP [{self.pair}]: {self.vwap:.8g} {self.currency} "
            f"| Source: Blocksize Capital (Institutional) "
            f"| Time: {self.timestamp.isoformat()}"
        )


class VWAPResponse(BaseModel):
    """Response wrapper for VWAP tool calls."""

    status: str = "ok"
    data: VWAPData
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
        "quality": "institutional-grade",
        "methodology": "Real-time VWAP with outlier detection",
    })


class VWAP30MinData(BaseModel):
    """30-Minute VWAP snapshot for a crypto ticker."""

    ticker: str = Field(..., description="Base currency ticker (e.g., BTC)")
    vwap: float = Field(..., description="30-minute VWAP price")
    quote_currency: str = Field("EUR", description="Quote currency")
    timestamp: datetime = Field(..., description="Snapshot timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        return (
            f"30-MIN VWAP [{self.ticker}]: {self.vwap:.8g} {self.quote_currency} "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


class VWAP24HrData(BaseModel):
    """24-Hour VWAP (closing price) for a crypto pair."""

    pair: str = Field(..., description="Trading pair")
    vwap: float = Field(..., description="24-hour VWAP (closing)")
    volume: float = Field(0, description="24-hour volume")
    timestamp: datetime = Field(..., description="Close timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        return (
            f"24H VWAP [{self.pair}]: {self.vwap:.8g} "
            f"| Vol: {self.volume:.2f} "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


# ---------------------------------------------------------------------------
# Bid/Ask Responses (Crypto, Equities, FX, Metals)
# ---------------------------------------------------------------------------

class BidAskData(BaseModel):
    """Best bid/ask snapshot for a trading pair."""

    pair: str = Field(..., description="Trading pair")
    bid: float = Field(..., description="Best bid price")
    ask: float = Field(..., description="Best ask price")
    spread: float = Field(..., description="Absolute spread (ask - bid)")
    spread_pct: float = Field(..., description="Spread as percentage")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider identifier")

    def to_decision_summary(self) -> str:
        return (
            f"BID/ASK [{self.pair}]: Bid {self.bid:.8g} | Ask {self.ask:.8g} "
            f"| Spread {self.spread:.8g} ({self.spread_pct:.4f}%) "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


class BidAskResponse(BaseModel):
    """Response wrapper for bid/ask tool calls."""

    status: str = "ok"
    data: BidAskData
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
        "quality": "institutional-grade",
    })


# ---------------------------------------------------------------------------
# Equity Responses
# ---------------------------------------------------------------------------

class EquityData(BaseModel):
    """Equity (stock) snapshot data."""

    ticker: str = Field(..., description="Stock ticker symbol")
    open: Optional[float] = Field(None, description="Opening price")
    high: Optional[float] = Field(None, description="High price")
    low: Optional[float] = Field(None, description="Low price")
    last: Optional[float] = Field(None, description="Last trade price")
    bid: Optional[float] = Field(None, description="Best bid price")
    ask: Optional[float] = Field(None, description="Best ask price")
    volume: Optional[float] = Field(None, description="Trading volume")
    prev_close: Optional[float] = Field(None, description="Previous close")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        parts = [f"EQUITY [{self.ticker}]:"]
        if self.last is not None:
            parts.append(f"Last {self.last:.4f}")
        if self.bid is not None and self.ask is not None:
            parts.append(f"Bid {self.bid:.4f} / Ask {self.ask:.4f}")
        if self.volume is not None:
            parts.append(f"Vol {self.volume:,.0f}")
        if self.prev_close is not None and self.last is not None:
            change_pct = ((self.last - self.prev_close) / self.prev_close * 100)
            parts.append(f"Chg {change_pct:+.2f}%")
        parts.append(f"| Time: {self.timestamp.isoformat()}")
        return " | ".join(parts)


class EquityResponse(BaseModel):
    """Response wrapper for equity queries."""

    status: str = "ok"
    data: EquityData
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
        "asset_class": "equities",
        "markets": "US + Chinese exchanges",
    })


# ---------------------------------------------------------------------------
# FX & Metals Responses
# ---------------------------------------------------------------------------

class FXData(BaseModel):
    """Foreign exchange rate data."""

    pair: str = Field(..., description="FX pair (e.g., EUR/USD)")
    base_currency: str = Field(..., description="Base currency")
    quote_currency: str = Field(..., description="Quote currency")
    bid: Optional[float] = Field(None, description="Bid rate")
    ask: Optional[float] = Field(None, description="Ask rate")
    mid: Optional[float] = Field(None, description="Mid rate")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        rate = self.mid or self.bid or self.ask or 0
        parts = [f"FX [{self.pair.upper()}]: {rate:.6f}"]
        if self.bid and self.ask:
            spread = self.ask - self.bid
            parts.append(f"Bid {self.bid:.6f} / Ask {self.ask:.6f} (spread {spread:.6f})")
        parts.append(f"| Time: {self.timestamp.isoformat()}")
        return " | ".join(parts)


class MetalData(BaseModel):
    """Precious/base metal spot price data."""

    ticker: str = Field(..., description="Metal ticker (e.g., XAUUSD)")
    name: str = Field("", description="Metal name (e.g., Gold)")
    price: float = Field(..., description="Spot price in USD")
    currency: str = Field("USD", description="Quote currency")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        return (
            f"METAL [{self.name or self.ticker}]: {self.price:.4f} {self.currency} "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


# ---------------------------------------------------------------------------
# US Treasury Rate Responses
# ---------------------------------------------------------------------------

class TreasuryRateData(BaseModel):
    """US Treasury yield data."""

    ticker: str = Field(..., description="Rate identifier (e.g., Rates.US10Y)")
    maturity: str = Field(..., description="Maturity (e.g., 10Y, 2Y, 30Y)")
    yield_pct: float = Field(..., description="Yield in percent")
    currency: str = Field("USD", description="Yield currency")
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        return (
            f"US TREASURY [{self.maturity}]: {self.yield_pct:.4f}% "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


class TreasuryRateResponse(BaseModel):
    """Response wrapper for rate queries — supports multiple maturities."""

    status: str = "ok"
    rates: list[TreasuryRateData]
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
        "asset_class": "fixed_income",
        "description": "US Treasury yield curve",
    })


# ---------------------------------------------------------------------------
# State Price Responses
# ---------------------------------------------------------------------------

class StatePriceData(BaseModel):
    """State/reference price for a crypto asset."""

    pair: str = Field(..., description="Trading pair")
    price: float = Field(..., description="State/reference price")
    timestamp: datetime = Field(..., description="Price timestamp (UTC)")
    source: str = Field("blocksize", description="Data provider")

    def to_decision_summary(self) -> str:
        return (
            f"STATE PRICE [{self.pair}]: {self.price:.8g} "
            f"| Source: Blocksize Capital | Time: {self.timestamp.isoformat()}"
        )


# ---------------------------------------------------------------------------
# Pair Search Responses
# ---------------------------------------------------------------------------

class PairInfo(BaseModel):
    """Metadata for a single trading pair."""

    pair: str = Field(..., description="Pair identifier (e.g., BTC-USD)")
    base_currency: str = Field("", description="Base asset")
    quote_currency: str = Field("", description="Quote asset")
    asset_class: str = Field("crypto", description="Asset class (crypto, equity, fx, metal, rate)")
    services: list[str] = Field(
        default_factory=list,
        description="Available services (vwap, bidask, vwap30m, vwap24h, state)",
    )
    tier: str = Field("core", description="Pricing tier (core, extended, tradfi, equities, analytics)")


class PairSearchResponse(BaseModel):
    """Response wrapper for pair search tool calls."""

    status: str = "ok"
    query: str = Field(..., description="Original search query")
    total_matches: int = Field(..., description="Number of matching pairs")
    pairs: list[PairInfo] = Field(..., description="Matching pairs (max 50)")
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
        "total_coverage": "30,000+ instruments across crypto, equities, FX, metals, rates",
    })


# ---------------------------------------------------------------------------
# Instrument List Responses
# ---------------------------------------------------------------------------

class InstrumentListResponse(BaseModel):
    """Response wrapper for instrument listing tool calls."""

    status: str = "ok"
    service: str = Field(..., description="Service type")
    total_instruments: int = Field(..., description="Total count")
    instruments: list[str] = Field(..., description="Instrument identifiers")
    meta: dict = Field(default_factory=lambda: {
        "provider": "Blocksize Capital",
    })


# ---------------------------------------------------------------------------
# Error Responses
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Standardized error response for tool calls."""

    status: str = "error"
    error_code: str
    message: str
    details: Optional[str] = None
