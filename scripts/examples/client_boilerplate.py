"""
🪙 Blocksize x402 Client Boilerplate
A minimal, copy-pasteable example of how to pay for and retrieve data.

Usage:
    export AGENT_PRIVATE_KEY="your_solana_private_key_base64"
    python client_boilerplate.py
"""

import os
import json
import base64
import asyncio
import httpx
from solders.keypair import Keypair # type: ignore
from solana.rpc.async_api import AsyncClient # type: ignore
from solana.transaction import Transaction # type: ignore
from solders.system_program import TransferParams, transfer # type: ignore
from solders.pubkey import Pubkey # type: ignore

# CONFIG
BASE_URL = "https://agentic-payments-production.up.railway.app"
SOLANA_RPC = "https://api.mainnet-beta.solana.com" # Use Helius for production

async def get_data_autonomously(endpoint: str):
    # 1. Load Identity
    try:
        pk_bytes = base64.b64decode(os.getenv("AGENT_PRIVATE_KEY", ""))
        keypair = Keypair.from_bytes(pk_bytes)
    except Exception:
        print("❌ Error: Please set AGENT_PRIVATE_KEY environment variable.")
        return

    async with httpx.AsyncClient() as client:
        # 2. Initial Request
        url = f"{BASE_URL}{endpoint}"
        print(f"[*] Requesting: {url}")
        res = await client.get(url)

        if res.status_code == 200:
            print("✅ Data retrieved for free!")
            print(json.dumps(res.json(), indent=2))
            return

        if res.status_code == 402:
            print("💰 Payment Required Intercepted.")
            
            # 3. Parse Payment Requirements
            req_header = res.headers.get("PAYMENT-REQUIRED")
            if not req_header:
                print("❌ No payment instructions found.")
                return
            
            reqs = json.loads(base64.b64decode(req_header))
            # Find Solana requirement
            sol_req = next((r for r in reqs if "solana" in str(r.get("network")).lower()), None)
            
            if not sol_req:
                print("❌ This server doesn't accept Solana payments.")
                return

            pay_to = sol_req["payTo"]
            amount_usdc = int(sol_req["maxAmountRequired"]) / 1_000_000
            print(f"💸 Paying {amount_usdc} USDC to {pay_to}...")

            # 4. Execute Native Transfer (Note: Simplified for SOL native here)
            # In production, use spl-token transfer for USDC.
            sol_client = AsyncClient(SOLANA_RPC)
            # (Payment logic abbreviated for boilerplate clarity)
            # tx_hash = "PAYMENT_TX_HASH_GOES_HERE"
            
            print("⚠️ Boilerplate Note: Implement your standard Solana transfer here.")
            print("⚠️ Once confirmed, re-submit with 'PAYMENT-SIGNATURE' header.")
            
            # 5. Proof Transmission
            # proof_header = base64.b64encode(json.dumps({
            #     "proof": tx_hash,
            #     "network": sol_req["network"],
            # }).encode()).decode()
            # final_res = await client.get(url, headers={"PAYMENT-SIGNATURE": proof_header})
            # print(final_res.json())

if __name__ == "__main__":
    # Example: Fetch BTC VWAP
    asyncio.run(get_data_autonomously("/v1/vwap/BTCUSD"))
