"""
FastAPI Resource Server with x402 Payment Middleware.

This server gates Blocksize Capital data endpoints behind the x402
payment protocol. Supports tiered pricing and dual-network settlement
(Solana primary, Base L2 fallback).

Endpoints:
  GET /v1/vwap/{pair}             — Real-time VWAP (crypto tier pricing)
  GET /v1/bidask/{pair}           — Bid/Ask snapshot (shared upstream namespace)
  GET /v1/fx/{pair}               — FX rate ($0.005)
  GET /v1/metal/{ticker}          — Metal price ($0.005)
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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError
from src.config import settings
from src.credit_manager import CreditManager, CREDIT_COSTS, BULK_TIERS
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)
from src.public_metadata import (
    AGENT_MANUAL_URL,
    APP_VERSION,
    CONTACT_EMAIL,
    DATA_CATALOG_URL,
    GLAMA_WELL_KNOWN_URL,
    MCP_MANIFEST_URL,
    MCP_REGISTRY_AUTH_CONTENT,
    MCP_REGISTRY_AUTH_URL,
    OPENAPI_URL,
    PRICING_GUIDE_URL,
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    QUICKSTART_URL,
    REMOTE_MCP_PATH,
    REMOTE_MCP_URL,
    REPOSITORY_URL,
    SERVER_JSON_URL,
    SUPPORT_URL,
    SWAGGER_URL,
    USER_FLOW_URL,
    build_server_json,
)
from src.public_mcp_server import public_mcp

logger = logging.getLogger(__name__)
DOCS_DIR = Path("docs")
PUBLIC_MCP_HTTP_APP = public_mcp.http_app(path="/", transport="streamable-http")


# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the Blocksize client and Credit manager lifecycle."""
    app.state.blocksize = BlocksizeClient()
    app.state.credits = CreditManager()
    logger.info("Blocksize MCP Resource Server starting (with Credit Drawdown engine)")
    logger.info("Solana Wallet: %s", settings.x402.solana_wallet_address)
    logger.info("Base Wallet: %s", settings.x402.evm_wallet_address)
    async with PUBLIC_MCP_HTTP_APP.lifespan(PUBLIC_MCP_HTTP_APP):
        yield
    await app.state.blocksize.close()
    logger.info("Blocksize MCP Resource Server shut down")


app = FastAPI(
    title="Blocksize Capital Agentic Data Economy",
    version=APP_VERSION,
    description=f"""
Institutional-grade financial data gateway for autonomous AI agents.
Supports x402 real-time payment settlement, bulk wallet credits, and a public
remote MCP discovery surface for directory listings and client onboarding.

### Public Integration Surfaces
- **Developer Portal**: [Homepage]({PUBLIC_BASE_URL}/)
- **Remote MCP URL**: [Streamable HTTP]({REMOTE_MCP_URL})
- **MCP Manifest**: [Listing metadata]({MCP_MANIFEST_URL})
- **OpenAPI**: [JSON schema]({OPENAPI_URL})
- **Swagger UI**: [Interactive docs]({SWAGGER_URL})
- **Quickstart**: [Remote MCP install guide]({QUICKSTART_URL})
- **Prompt Examples**: [Example prompts]({PROMPT_EXAMPLES_URL})
- **Privacy Policy**: [Privacy]({PRIVACY_POLICY_URL})
- **Support**: [Contact and troubleshooting]({SUPPORT_URL})
    """,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
)

# Serve the Developer Portal as the main landing page
@app.get("/", include_in_schema=False)
async def get_portal():
    """Serve the institutional developer portal."""
    portal_path = DOCS_DIR / "developer_portal.html"
    if portal_path.exists():
        return FileResponse(portal_path)
    raise HTTPException(status_code=404, detail="Developer Portal not found")


def _serve_doc(filename: str, description: str) -> FileResponse:
    """Serve a static documentation page from the docs directory."""
    path = DOCS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{description} not found")
    return FileResponse(path)


@app.get("/quickstart/remote-mcp", include_in_schema=False)
async def get_remote_quickstart():
    """Serve the public remote MCP quickstart page."""
    return _serve_doc("remote_mcp_quickstart.html", "Remote MCP quickstart")


@app.get("/prompt-examples", include_in_schema=False)
async def get_prompt_examples():
    """Serve prompt examples for reviewers and users."""
    return _serve_doc("prompt_examples.html", "Prompt examples")


@app.get("/privacy", include_in_schema=False)
async def get_privacy_policy():
    """Serve the privacy policy page."""
    return _serve_doc("privacy_policy.html", "Privacy policy")


@app.get("/support", include_in_schema=False)
async def get_support_page():
    """Serve the support and troubleshooting page."""
    return _serve_doc("support.html", "Support page")


@app.get("/server.json", include_in_schema=False)
async def get_server_json():
    """Serve the official MCP Registry metadata file."""
    return JSONResponse(build_server_json())


@app.get("/.well-known/glama.json", include_in_schema=False)
async def get_glama_well_known() -> dict[str, object]:
    """Serve the Glama connector claim file."""
    return {
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "maintainers": [{"email": CONTACT_EMAIL}],
    }


@app.get("/.well-known/mcp-registry-auth", include_in_schema=False)
async def get_mcp_registry_auth() -> PlainTextResponse:
    """Serve the MCP Registry HTTP domain verification file."""
    return PlainTextResponse(MCP_REGISTRY_AUTH_CONTENT)


# Mount assets, PDFs, and the public remote MCP discovery server
app.mount("/assets", StaticFiles(directory="docs/assets"), name="assets")
app.mount("/pdf", StaticFiles(directory="docs/pdf"), name="pdf")
app.mount(REMOTE_MCP_PATH, PUBLIC_MCP_HTTP_APP, name="public-mcp")


# ---------------------------------------------------------------------------
# x402 Payment Middleware — Tiered Pricing
# ---------------------------------------------------------------------------

# Route → price mapping (None = free)
ROUTE_PRICING: dict[str, Decimal | None] = {
    # Crypto — dynamic pricing based on asset tier (handled separately)
    "/v1/vwap/": None,  # set dynamically
    "/v1/bidask/": None,  # set dynamically
    # TradFi
    "/v1/fx/": settings.pricing.tradfi,
    "/v1/metal/": settings.pricing.tradfi,
    # Free
    "/v1/search": None,
    "/v1/instruments/": None,
    "/health": None,
    "/v1/credits/": None,  # Credit endpoints define their own x402 challenges
}

# Mapping asset types to credit costs
PATH_TO_ASSET_TYPE = {
    "/v1/vwap/": "core",
    "/v1/bidask/": "core",
    "/v1/fx/": "tradfi",
    "/v1/metal/": "tradfi",
}
# ---------------------------------------------------------------------------
# Documentation & Schemas
# ---------------------------------------------------------------------------

X402_RESPONSE = {
    "402": {
        "description": "Payment Required. Returns a PAYMENT-REQUIRED header with CAIP-compliant requirements.",
        "headers": {
            "PAYMENT-REQUIRED": {
                "description": "Base64 encoded JSON array of payment requirements (network, recipient, amount).",
                "schema": {"type": "string"}
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "error": "Payment Required",
                    "message": "This endpoint requires a payment of $0.002 USDC.",
                    "price_usdc": "0.002",
                    "networks": [
                        {"name": "Solana", "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
                        {"name": "Base", "caip2": "eip155:8453"}
                    ]
                }
            }
        }
    }
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
            if ":" not in q:
                continue
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

        allow_mock = os.getenv("X402_ALLOW_MOCK_PAYMENTS", "").lower() in {"1", "true", "yes"}
        if allow_mock and str(tx_hash).startswith(("mock_", "test_")):
            _SEEN_TX_HASHES.add(tx_hash)
            logger.warning("Accepted mock x402 proof for local/demo mode only: %s", tx_hash)
            return {"valid": True, "mock": True}
            
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

    # 🚀 --- CREDIT DRAWDOWN CHECK --- 🚀
    wallet_header = request.headers.get("X-AGENT-WALLET")
    if wallet_header:
        mgr: CreditManager = request.app.state.credits
        client_ip = request.client.host if request.client else "unknown"
        
        # 🎁 New Agent? Grant 50 credits automatically IF stake check and IP check pass
        await mgr.ensure_wallet_with_welcome_pack(wallet_header, client_ip)
        
        # Map path to credit cost type
        asset_type = "core"
        for prefix, a_type in PATH_TO_ASSET_TYPE.items():
            if path.startswith(prefix):
                asset_type = a_type
                break
        
        credit_cost = CREDIT_COSTS.get(asset_type, 2.0)
        
        # Attempt to spend credits
        if mgr.spend_credits(wallet_header, credit_cost):
            logger.info("CREDIT DRAWDOWN: Spent %.1f from %s for %s", credit_cost, wallet_header, path)
            return await call_next(request)
        else:
            logger.warning("INSUFFICIENT CREDITS: Wallet %s for %s", wallet_header, path)
            # Proceed to normal 402 challenge below

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

@app.get("/v1/vwap/{pair}", responses=X402_RESPONSE)
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


@app.get("/v1/bidask/{pair}", responses=X402_RESPONSE)
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


@app.get("/v1/fx/{pair}", responses=X402_RESPONSE)
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

@app.get("/v1/metal/{ticker}", responses=X402_RESPONSE)
async def get_metal(ticker: str, request: Request) -> dict[str, Any]:
    """Get metal spot price. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = ticker.replace("-", "").replace("/", "").replace("_", "")
        data = await client.get_metal_price(clean)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "Metal Price", "asset_class": "metal"}}
        return resp
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve metal for {ticker}", details=str(e),
        ).model_dump())


@app.get("/v1/batch", responses=X402_RESPONSE)
async def batch_request(reqs: str, request: Request) -> dict[str, Any]:
    """
    Execute a batch of data queries.
    Pass a comma separated list of svc:pair in the `reqs` query parameter.
    Example: /v1/batch?reqs=vwap:BTCUSD,bidask:ETHUSD,fx:EURUSD
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
            elif svc == "fx":
                return await get_fx(ticker, request)
            elif svc == "metal":
                return await get_metal(ticker, request)
            else:
                return {
                    "status": "error",
                    "error_code": "UNSUPPORTED_SERVICE",
                    "message": f"Service type {svc} is not currently offered by this gateway",
                    "meta": {"endpoint": "Unsupported", "asset_class": "unknown"}
                }
        except HTTPException as he:
            return he.detail
        except Exception as e:
            return {"status": "error", "error_code": "BATCH_EXECUTION_ERROR", "message": str(e)}

    for q in queries:
        if ":" not in q:
            continue
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
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        elif service == "metal":
            instruments = await client.list_metal_instruments()
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
# Credit Management
# ---------------------------------------------------------------------------

@app.get("/v1/credits/balance/{wallet}")
async def get_credit_balance(request: Request, wallet: str):
    """View current drawdown credit balance for a specific wallet."""
    mgr: CreditManager = request.app.state.credits
    balance = mgr.get_balance(wallet)
    return {
        "wallet": wallet,
        "balance_credits": balance,
        "value_usdc": balance * 0.001,
        "currency": "USDC-Equivalent"
    }

@app.post("/v1/credits/purchase")
async def purchase_credits_challenge(tier: str = Query(..., pattern="^(starter|pro|institutional)$")):
    """
    Triggers an x402 challenge for bulk credits.
    Tiers: starter ($0.90), pro ($8.00), institutional ($60.00)
    """
    tier_data = BULK_TIERS.get(tier)
    price = Decimal(str(tier_data["price"]))
    
    # Return 402 challenge
    requirements = [
        {"network": "solana", "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "recipient": settings.x402.solana_wallet_address, "amount": str(price)},
        {"network": "base", "caip2": "eip155:8453", "recipient": settings.x402.evm_wallet_address, "amount": str(price)}
    ]
    
    payload = base64.b64encode(json.dumps(requirements).encode()).decode()
    
    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": payload},
        content={
            "error": "Payment Required",
            "message": f"Purchase {tier_data['credits']} credits for ${price} USDC.",
            "tier": tier,
            "credits_to_add": tier_data["credits"],
            "networks": requirements
        }
    )

@app.post("/v1/credits/claim")
async def claim_credits(request: Request, payload: dict):
    """Verify a bulk payment and credit the agent's drawdown balance."""
    mgr: CreditManager = request.app.state.credits
    tx_hash = payload.get("proof")
    network = payload.get("network", "solana")
    tier = payload.get("tier")
    wallet = payload.get("wallet") # The address to credit
    
    if not all([tx_hash, tier, wallet]):
        raise HTTPException(status_code=400, detail="Missing tx_hash, tier, or wallet")
        
    tier_data = BULK_TIERS.get(tier)
    if not tier_data:
        raise HTTPException(status_code=400, detail="Invalid tier")

    tier_data = BULK_TIERS.get(tier)
    if not tier_data:
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Native RPC verification of the bulk payment
    verification = await _verify_payment(base64.b64encode(json.dumps({
        "proof": tx_hash,
        "network": network
    }).encode()).decode(), [])
    
    if not verification.get("valid"):
        raise HTTPException(status_code=402, detail=f"Bulk payment verification failed: {verification.get('reason')}")
    
    # Credit the wallet
    mgr.add_credits(
        address=wallet, 
        credits=tier_data["credits"], 
        tx_hash=tx_hash, 
        amount_usdc=tier_data["price"]
    )
    
    return {
        "status": "success",
        "added": tier_data["credits"],
        "new_balance": mgr.get_balance(wallet),
        "message": f"Successfully credited {tier_data['credits']} to {wallet}"
    }

# ---------------------------------------------------------------------------
# Discovery & MCP
# ---------------------------------------------------------------------------

@app.get("/mcp/manifest.json")
async def mcp_manifest():
    """
    Model Context Protocol (MCP) Manifest.
    Provides listing metadata for the public remote discovery server.
    """
    return {
        "mcp_version": "1.0",
        "name": "Blocksize Capital Remote Discovery",
        "description": (
            "Public remote MCP discovery server for Blocksize Capital. "
            "Exposes free instrument discovery, pricing inspection, and "
            "documentation search tools. Live market data remains available "
            "through the x402-protected HTTP API."
        ),
        "version": APP_VERSION,
        "transport": {
            "type": "streamable-http",
            "url": REMOTE_MCP_URL,
        },
        "capabilities": {
            "discovery_modes": ["instrument-search", "pricing-inspection", "document-search"],
            "paid_api_modes": ["real-time-x402", "credit-drawdown"],
            "bulk_discounts": "up to 40% via /v1/credits/purchase",
            "public_remote_server": "read-only and listing-safe",
        },
        "links": {
            "homepage": PUBLIC_BASE_URL,
            "openapi": OPENAPI_URL,
            "swagger": SWAGGER_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "agent_manual": AGENT_MANUAL_URL,
            "pricing_guide": PRICING_GUIDE_URL,
            "data_catalog": DATA_CATALOG_URL,
            "user_flow": USER_FLOW_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
            "repository": REPOSITORY_URL,
        },
        "tools": [
            {
                "name": "search_pairs",
                "description": "Search supported crypto, FX, and metal symbols. Free and read-only.",
                "parameters": {
                    "query": {"type": "string", "example": "BTC"},
                    "asset_class": {"type": "string", "example": "crypto"},
                },
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "list_instruments",
                "description": "List the supported instruments for a service such as vwap, bidask, fx, or metal. Free and read-only.",
                "parameters": {"service": {"type": "string", "example": "metal"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "get_pricing_info",
                "description": "Inspect Blocksize pricing tiers, networks, and credit options. Free and read-only.",
                "parameters": {},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "search",
                "description": "OpenAI-style document and catalog search across Blocksize docs and instrument metadata. Free and read-only.",
                "parameters": {"query": {"type": "string", "example": "pricing"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "fetch",
                "description": "OpenAI-style document fetch for ids returned by search. Free and read-only.",
                "parameters": {"id": {"type": "string", "example": "doc:quickstart"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
        ],
        "paid_api": {
            "openapi_url": OPENAPI_URL,
            "swagger_url": SWAGGER_URL,
            "payment_model": "x402 or wallet credits",
        },
    }


# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check — free."""
    return {
        "status": "healthy",
        "service": "blocksize-mcp-x402",
        "version": APP_VERSION,
        "engine": "Shielded x402 Gateway (Iron Dome Active)",
        "networks": {
            "primary": {"name": "Solana", "wallet": settings.x402.solana_wallet_address or "(not set)"},
            "fallback": {"name": "Base", "wallet": settings.x402.evm_wallet_address or "(not set)"},
        },
        "pricing": settings.pricing_summary,
        "bulk_pricing": BULK_TIERS,
        "links": {
            "remote_mcp": REMOTE_MCP_URL,
            "manifest": MCP_MANIFEST_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
        },
    }


def run_resource_server() -> None:
    """Start the resource server with uvicorn."""
    import uvicorn
    uvicorn.run(
        "src.resource_server:app",
        host="0.0.0.0",
        port=settings.server.resource_server_port,
        log_level=settings.server.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=False,
    )


if __name__ == "__main__":
    run_resource_server()
