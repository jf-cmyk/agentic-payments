"""
FastAPI Resource Server with x402 Payment Middleware.

This server gates Blocksize Capital data endpoints behind the x402
payment protocol. Supports tiered pricing and dual-network settlement
(Solana primary, Base L2 fallback).

Endpoints:
  GET /v1/vwap/{pair}             — Real-time VWAP (crypto tier pricing)
  GET /v1/bidask/{pair}           — Bid/Ask snapshot (crypto tier pricing)
  GET /v1/vwap30m/{ticker}        — 30-Min VWAP ($0.001 analytics)
  GET /v1/vwap24h/{pair}          — 24-Hr VWAP ($0.001 analytics)
  GET /v1/state/{pair}            — State Price (crypto tier pricing)
  GET /v1/equity/{ticker}         — Equity snapshot ($0.008)
  GET /v1/fx/{pair}               — FX rate ($0.005)
  GET /v1/metal/{ticker}          — Metal price ($0.005)
  GET /v1/rate/{maturity}         — Treasury rate ($0.005)
  GET /v1/search?q={query}        — Pair search (FREE)
  GET /v1/instruments/{service}   — Instrument list (FREE)
  GET /health                     — Health check (FREE)
"""

from __future__ import annotations

import os
import base64
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError
from src.config import settings
from src.models import (
    BidAskResponse,
    EquityResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the Blocksize client lifecycle."""
    app.state.blocksize = BlocksizeClient()
    logger.info("Blocksize MCP Resource Server starting")
    logger.info("Solana Wallet: %s", settings.x402.solana_wallet_address)
    logger.info("Base Wallet: %s", settings.x402.evm_wallet_address)
    yield
    await app.state.blocksize.close()
    logger.info("Blocksize MCP Resource Server shut down")


app = FastAPI(
    title="Blocksize Capital x402 Data Server",
    description="Institutional-grade multi-asset market data, pay-per-call via x402",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
)


# ---------------------------------------------------------------------------
# x402 Payment Middleware — Tiered Pricing
# ---------------------------------------------------------------------------

# Route → price mapping (None = free)
ROUTE_PRICING: dict[str, Decimal | None] = {
    # Crypto — dynamic pricing based on asset tier (handled separately)
    "/v1/vwap/": None,  # set dynamically
    "/v1/bidask/": None,  # set dynamically
    # Analytics tier
    "/v1/vwap30m/": settings.pricing.analytics,
    "/v1/vwap24h/": settings.pricing.analytics,
    # Equities
    "/v1/equity/": settings.pricing.equities,
    # TradFi
    "/v1/fx/": settings.pricing.tradfi,
    "/v1/metal/": settings.pricing.tradfi,
    "/v1/rate/": settings.pricing.tradfi,
    "/v1/yield": settings.pricing.tradfi,
    # Free
    "/v1/search": None,
    "/v1/instruments/": None,
    "/health": None,
}


def _get_price_for_request(request: Request) -> Decimal | None:
    """Determine the price for a given request."""
    path = request.url.path
    
    # Handle Batch endpoint dynamically
    if path.startswith("/v1/batch"):
        reqs = request.query_params.get("reqs", "")
        if not reqs:
            return None
        
        total = Decimal("0.0")
        queries = reqs.split(",")
        for q in queries:
            if ":" not in q: continue
            svc, pair = q.split(":", 1)
            # Find the mock path for this service to calculate price
            mock_path = f"/v1/{svc}/{pair}"
            
            # Crypto dynamic pricing
            if svc in ["vwap", "bidask"]:
                base = pair.split("-")[0].upper() if "-" in pair else pair[:len(pair)//2].upper()
                svc_price = settings.pricing.get_crypto_price(base)
                total += svc_price
            else:
                for prefix, price in ROUTE_PRICING.items():
                    if mock_path.startswith(prefix) and price is not None:
                        total += price
                        break
        return total if total > 0 else None

    # Crypto uses dynamic tier pricing
    if path.startswith("/v1/vwap/") or path.startswith("/v1/bidask/"):
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            pair = parts[3]
            base = pair.split("-")[0].upper() if "-" in pair else pair[:len(pair)//2].upper()
            return settings.pricing.get_crypto_price(base)
        return settings.pricing.core_crypto

    # Check static route pricing
    for route_prefix, price in ROUTE_PRICING.items():
        if path.startswith(route_prefix):
            return price

    return None  # Free endpoint


_SEEN_TX_HASHES: set[str] = set()

async def _verify_payment(payment_payload: str, payment_requirements: list[dict]) -> dict:
    """Natively verify the transaction on the Solana or Base blockchain via RPC."""
    try:
        decoded_str = base64.b64decode(payment_payload).decode()
        payload = json.loads(decoded_str)
        tx_hash = payload.get("proof")
        network = payload.get("network", "solana")
        
        if not tx_hash:
            return {"valid": False, "reason": "Missing tx_hash/proof in payload"}
            
        if tx_hash in _SEEN_TX_HASHES:
            return {"valid": False, "reason": "Transaction hash has already been used (Replay Attack)"}
            
        if "solana" in network:
            # Use Helius as standard default to bypass rate-limiting and environment variable syncing delays
            rpc_url = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=2a2801c5-01ca-458f-9aaa-5aafc1886571")
            logger.info(f"Verifying Solana payment via RPC: {rpc_url.split('?')[0]}")
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            tx_hash,
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}
                        ]
                    }
                )
                res.raise_for_status()
                data = res.json()
                
            result = data.get("result")
            if not result:
                return {"valid": False, "reason": "Transaction not found on chain or not yet finalized"}
                
            meta = result.get("meta")
            if meta and meta.get("err") is not None:
                return {"valid": False, "reason": f"Transaction reverted on chain: {meta['err']}"}
                
        elif "eip155" in network or "base" in network:
            rpc_url = "https://mainnet.base.org"
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getTransactionReceipt",
                        "params": [tx_hash]
                    }
                )
                res.raise_for_status()
                data = res.json()
            
            result = data.get("result")
            if not result:
                return {"valid": False, "reason": "EVM Transaction not found or not yet finalized"}
                
            status = result.get("status")
            if status not in ("0x1", 1):
                return {"valid": False, "reason": f"EVM Transaction reverted on chain: status={status}"}
        
        else:
            return {"valid": False, "reason": f"Unsupported network: {network}"}

        # Mark as used securely
        _SEEN_TX_HASHES.add(tx_hash)
        logger.info("NATIVELY VERIFIED %s: %s", network, tx_hash)
        return {"valid": True}
        
    except Exception as e:
        logger.error("Native RPC verification failed: %s", e)
        return {"valid": False, "reason": f"Native RPC failure: {str(e)}"}

async def _settle_payment(payment_payload: str, payment_requirements: list[dict]) -> dict:
    """Payment has inherently settled on chain during Native RPC Verification."""
    return {"success": True}


@app.middleware("http")
async def x402_payment_middleware(request: Request, call_next):
    """
    x402 payment gate middleware with tiered pricing.

    Flow:
    1. Check if the route is priced (None = free, pass through)
    2. Check for PAYMENT-SIGNATURE header
    3. If missing → 402 with payment requirements for BOTH networks
    4. If present → verify → proceed or reject
    5. After response → settle payment
    """
    path = request.url.path
    price = _get_price_for_request(request)

    # Free endpoints pass through
    if price is None:
        return await call_next(request)

    # Build multi-network payment requirements
    payment_reqs = settings.payment_requirements(price)

    # Check for payment signature
    payment_header = (
        request.headers.get("PAYMENT-SIGNATURE")
        or request.headers.get("payment-signature")
    )

    if not payment_header:
        requirements_b64 = base64.b64encode(
            json.dumps(payment_reqs).encode()
        ).decode()

        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment Required",
                "message": (
                    f"This endpoint requires a payment of ${price} USDC. "
                    f"Send a signed x402 payment in the PAYMENT-SIGNATURE header. "
                    f"Accepted networks: Solana (preferred), Base L2."
                ),
                "price_usdc": str(price),
                "networks": [
                    {"name": "Solana", "caip2": settings.x402.solana_network},
                    {"name": "Base", "caip2": settings.x402.base_network},
                ],
            },
            headers={
                "PAYMENT-REQUIRED": requirements_b64,
            },
        )

    # Verify the payment
    try:
        verification = await _verify_payment(payment_header, payment_reqs)
        if not verification.get("valid", False):
            return JSONResponse(
                status_code=402,
                content={
                    "error": "Payment Invalid",
                    "message": "Payment verification failed.",
                    "details": verification.get("reason", "Unknown"),
                },
            )
    except httpx.HTTPError as e:
        logger.error("Facilitator verification failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={
                "error": "Payment Verification Unavailable",
                "message": "Could not reach payment facilitator.",
            },
        )

    # Payment verified — serve the request
    response = await call_next(request)

    # Settle payment (best-effort)
    try:
        settlement = await _settle_payment(payment_header, payment_reqs)
        if settlement.get("success"):
            settlement_b64 = base64.b64encode(json.dumps(settlement).encode()).decode()
            response.headers["PAYMENT-RESPONSE"] = settlement_b64
            logger.info("Payment settled: %s USDC for %s", price, path)
    except httpx.HTTPError as e:
        logger.error("Payment settlement failed: %s", e)

    return response


# ---------------------------------------------------------------------------
# Data Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/vwap/{pair}")
async def get_vwap(pair: str, request: Request) -> dict[str, Any]:
    """Get real-time VWAP for a crypto pair. Cost: $0.002–$0.004 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = pair.replace("-", "").replace("/", "").replace("_", "")
        vwap_data = await client.get_vwap_latest(clean)
        resp = VWAPResponse(data=vwap_data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Real-Time VWAP", "asset_class": "crypto"}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/bidask/{pair}")
async def get_bidask(pair: str, request: Request) -> dict[str, Any]:
    """Get bid/ask snapshot for a crypto pair. Cost: $0.002–$0.004 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = pair.replace("-", "").replace("/", "").replace("_", "")
        bidask_data = await client.get_bidask_snapshot(clean)
        resp = BidAskResponse(data=bidask_data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Bid/Ask Snapshot", "asset_class": "crypto"}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve bid/ask for {pair}", details=str(e),
        ).model_dump())


# Note: /v1/state and /v1/vwap30m have been removed as they are currently unsupported by upstream RPC


@app.get("/v1/equity/{ticker}")
async def get_equity(ticker: str, request: Request) -> dict[str, Any]:
    """Get equity snapshot. Cost: $0.008 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = ticker.replace("-", "").replace("/", "").replace("_", "")
        data = await client.get_equity_snapshot(clean)
        resp = EquityResponse(data=data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Equity Data", "asset_class": "equity"}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve equity for {ticker}", details=str(e),
        ).model_dump())


@app.get("/v1/fx/{pair}")
async def get_fx(pair: str, request: Request) -> dict[str, Any]:
    """Get FX rate. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = pair.replace("-", "").replace("/", "").replace("_", "")
        data = await client.get_fx_rate(clean)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "FX Rate", "asset_class": "fx"}}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve FX for {pair}", details=str(e),
        ).model_dump())

@app.get("/v1/metal/{ticker}")
async def get_metal(ticker: str, request: Request) -> dict[str, Any]:
    """Get metal spot price. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = ticker.replace("-", "").replace("/", "").replace("_", "")
        data = await client.get_metal_price(clean)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "commodity", "asset_class": "metal"}}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve metal for {ticker}", details=str(e),
        ).model_dump())

@app.get("/v1/rate/{maturity}")
async def get_rate(maturity: str, request: Request) -> dict[str, Any]:
    """Get US Treasury rate. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        data = await client.get_treasury_rate(maturity)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "Treasury Yield", "asset_class": "rate"}}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve rate for {maturity}", details=str(e),
        ).model_dump())

@app.get("/v1/batch")
async def batch_request(reqs: str, request: Request) -> dict[str, Any]:
    """
    Execute a batch of data queries.
    Pass a comma separated list of svc:pair in the `reqs` query parameter.
    Example: /v1/batch?reqs=vwap:BTCUSD,bidask:ETHUSD,equity:AAPL
    """
    import asyncio
    
    queries = reqs.split(",")
    results = []
    
    # We execute requests concurrently for extreme speed
    tasks = []
    
    async def _safe_fetch(svc: str, ticker: str):
        try:
            if svc == "vwap":
                return await get_vwap(ticker, request)
            elif svc == "bidask":
                return await get_bidask(ticker, request)
            elif svc == "equity":
                return await get_equity(ticker, request)
            elif svc == "fx":
                return await get_fx(ticker, request)
            elif svc == "metal":
                return await get_metal(ticker, request)
            elif svc == "rate":
                return await get_rate(ticker, request)
            else:
                return {
                    "status": "error",
                    "error_code": "UNSUPPORTED_SERVICE",
                    "message": f"Service type {svc} is not supported or was deprecated by upstream RPC",
                    "meta": {"endpoint": "Unsupported", "asset_class": "unknown"}
                }
        except HTTPException as he:
            return he.detail
        except Exception as e:
            return {"status": "error", "error_code": "BATCH_EXECUTION_ERROR", "message": str(e)}

    for q in queries:
        if ":" not in q: continue
        svc, ticker = q.split(":", 1)
        tasks.append(_safe_fetch(svc, ticker))
        
    gathered_results = await asyncio.gather(*tasks)
    
    for q, result in zip(queries, gathered_results):
        results.append({
            "request": q,
            "response": result
        })
        
    return {
        "status": "ok",
        "batch_size": len(results),
        "results": results,
    }


@app.get("/v1/search")
async def search_pairs(
    q: str = Query(..., description="Search query"),
    asset_class: str = Query("all", description="Asset class filter"),
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Search instruments. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        pairs = await client.search_pairs(q, asset_class)
        return PairSearchResponse(query=q, total_matches=len(pairs), pairs=pairs).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Search failed for '{q}'", details=str(e),
        ).model_dump())


@app.get("/v1/instruments/{service}")
async def list_instruments(service: str, request: Request) -> dict[str, Any]:
    """List instruments for a service. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "equity":
            instruments = await client.list_equity_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        return InstrumentListResponse(
            service=service, total_instruments=len(instruments), instruments=instruments,
        ).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to list for {service}", details=str(e),
        ).model_dump())


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check — free."""
    return {
        "status": "healthy",
        "service": "blocksize-mcp-x402",
        "version": "0.2.0",
        "networks": {
            "primary": {"name": "Solana", "wallet": settings.x402.solana_wallet_address or "(not set)"},
            "fallback": {"name": "Base", "wallet": settings.x402.evm_wallet_address or "(not set)"},
        },
        "pricing": settings.pricing_summary,
    }


def run_resource_server() -> None:
    """Start the resource server with uvicorn."""
    import uvicorn
    uvicorn.run(
        "src.resource_server:app",
        host="0.0.0.0",
        port=settings.server.resource_server_port,
        log_level=settings.server.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    run_resource_server()
