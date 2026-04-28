import base64
import json
import sys
import time

import asyncio
import httpx


def _normalise_payment_requirements(decoded):
    if isinstance(decoded, list):
        return decoded
    if not isinstance(decoded, dict) or not isinstance(decoded.get("accepts"), list):
        return []
    resource = decoded.get("resource") if isinstance(decoded.get("resource"), dict) else {}
    requirements = []
    for accept in decoded["accepts"]:
        if not isinstance(accept, dict):
            continue
        requirements.append({
            "scheme": accept.get("scheme", "exact"),
            "network": accept.get("network", ""),
            "maxAmountRequired": str(accept.get("maxAmountRequired") or accept.get("amount") or "0"),
            "resource": accept.get("payTo", ""),
            "description": resource.get("description", "Blocksize Capital institutional market data"),
            "mimeType": resource.get("mimeType", "application/json"),
            "payTo": accept.get("payTo", ""),
            "maxTimeoutSeconds": accept.get("maxTimeoutSeconds", 60),
            "asset": accept.get("asset", ""),
            "extra": accept.get("extra") if isinstance(accept.get("extra"), dict) else {},
        })
    return requirements


def _decode_payment_requirements(response):
    req_header = response.headers.get("PAYMENT-REQUIRED")
    if not req_header:
        return []
    return _normalise_payment_requirements(json.loads(base64.b64decode(req_header)))


def _build_payment_signature(tx_hash: str, network: str) -> str:
    payload = {
        "proof": tx_hash,
        "network": network,
        "timestamp": int(time.time()),
        "agent_pubkey": "manual_agent",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def fetch_data(client, url, iteration, mock_mode=False):
    print(f"\n[{iteration}/3] Requesting data from {url}...")
    response = await client.get(url)

    # Step 1: Handle the 402 Payment Required intercept
    if response.status_code == 402:
        print("[!] HTTP 402 Payment Required intercepted.")
        data = response.json()
        
        price = data.get("price_usdc")
        
        # Step 2: Extract the destination wallet address
        pay_to_address = "UNKNOWN"
        network = "solana"
        decoded_reqs = _decode_payment_requirements(response)
        sol_req = next((r for r in decoded_reqs if "solana" in str(r.get("network", "")).lower()), None)
        if sol_req:
            pay_to_address = sol_req.get("payTo", "UNKNOWN")
            network = sol_req.get("network", network)

        # Step 3: Prompt the user to use Phantom
        print("\n" + "="*55)
        print(f"💰 MANUAL PAYMENT {iteration}/3 (PHANTOM WALLET)")
        print("="*55)
        print("1. Open your Phantom Wallet extension in Chrome.")
        print("2. Click 'Send' and select USDC (on Solana).")
        print(f"3. Send exactly: {price} USDC")
        print(f"4. Send to address: {pay_to_address}")
        print("5. Wait for confirmation, view the transaction on Solscan,")
        print("   and copy the Transaction Signature (Hash).")
        print("="*55 + "\n")

        if mock_mode:
            tx_hash = f"mock_agent_loop_{int(time.time())}_{iteration}"
            print(f"Mock mode enabled. Using proof: {tx_hash}")
        else:
            tx_hash = input("Paste your Phantom Transaction Signature here: ").strip()

        if not tx_hash:
            print("[-] No transaction signature provided. Stopping agent.")
            return False

        # Step 4: Resubmit the request with the proof
        print("\n[*] Resubmitting request with payment proof...")
        headers = {"PAYMENT-SIGNATURE": _build_payment_signature(tx_hash, network)}
        
        final_response = await client.get(url, headers=headers)
        
        # Step 5: Deliver the data
        if final_response.status_code == 200:
            print("\n[+] SUCCESS! Cryptographic Fulfillment complete:")
            print(json.dumps(final_response.json(), indent=2))
        else:
            print(f"\n[-] FAILED. Server returned HTTP {final_response.status_code}")
            try:
                print(json.dumps(final_response.json(), indent=2))
            except json.JSONDecodeError:
                print(final_response.text)

    elif response.status_code == 200:
        print(f"\n[+] Endpoint {url} is FREE. Data Payload:")
        print(json.dumps(response.json(), indent=2))
    else:
        print(f"\n[-] Unexpected HTTP {response.status_code}:")
        print(response.text)

    return True

async def main():
    if len(sys.argv) < 2:
        print("Usage: python agent_loop.py [URL] [--mock]")
        print("Example: python agent_loop.py http://localhost:8402/v1/vwap/BTC-USD --mock")
        print("Mock mode requires the server to run with X402_ALLOW_MOCK_PAYMENTS=true.")
        sys.exit(1)

    url = sys.argv[1]
    mock_mode = "--mock" in sys.argv[2:]
    print("🤖 Agent starting. Target: fetch data 3 times.")

    async with httpx.AsyncClient() as client:
        for i in range(1, 4):
            success = await fetch_data(client, url, i, mock_mode=mock_mode)
            if not success:
                break
            
            if i < 3:
                print("\n--- Waiting 2 seconds before the next agentic request... ---")
                await asyncio.sleep(2)
                
    print("\n✅ Agent run complete.")


if __name__ == "__main__":
    asyncio.run(main())
