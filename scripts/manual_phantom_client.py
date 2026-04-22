import sys
import base64
import json
import httpx
import asyncio
import time


def _decode_payment_requirements(response):
    req_header = response.headers.get("PAYMENT-REQUIRED")
    if not req_header:
        return []
    return json.loads(base64.b64decode(req_header))


def _build_payment_signature(tx_hash: str, network: str) -> str:
    payload = {
        "proof": tx_hash,
        "network": network,
        "timestamp": int(time.time()),
        "agent_pubkey": "manual_phantom_client",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()

async def main():
    if len(sys.argv) < 2:
        print("Usage: python manual_phantom_client.py [URL]")
        print("Example: python manual_phantom_client.py http://localhost:8402/v1/vwap/BTC-USD")
        sys.exit(1)

    url = sys.argv[1]
    
    print(f"[*] Requesting data from {url}...")
    async with httpx.AsyncClient() as client:
        response = await client.get(url)

        # Step 1: Handle the 402 Payment Required intercept
        if response.status_code == 402:
            print("[!] HTTP 402 Payment Required intercepted.")
            data = response.json()
            
            price = data.get("price_usdc")
            
            # Step 2: Extract the destination wallet address from the base64 headers
            pay_to_address = "UNKNOWN"
            network = "solana"
            decoded_reqs = _decode_payment_requirements(response)
            sol_req = next((r for r in decoded_reqs if "solana" in str(r.get("network", "")).lower()), None)
            if sol_req:
                pay_to_address = sol_req.get("payTo", "UNKNOWN")
                network = sol_req.get("network", network)

            # Step 3: Prompt the user to use Phantom
            print("\n" + "="*55)
            print("💰 MANUAL PAYMENT INSTRUCTIONS (PHANTOM WALLET)")
            print("="*55)
            print("1. Open your Phantom Wallet extension in Chrome.")
            print("2. Click 'Send' and select USDC (on Solana).")
            print(f"3. Send exactly: {price} USDC")
            print(f"4. Send to address: {pay_to_address}")
            print("5. Wait for confirmation, view the transaction on Solscan,")
            print("   and copy the Transaction Signature (Hash).")
            print("   For local demos only, run the server with X402_ALLOW_MOCK_PAYMENTS=true")
            print("   and enter a proof beginning with mock_ instead of a real transaction.")
            print("="*55 + "\n")

            tx_hash = input("Paste your Phantom Transaction Signature here: ").strip()

            if not tx_hash:
                print("[-] No transaction signature provided. Exiting.")
                sys.exit(1)

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
            print("\n[+] Endpoint is FREE. Data Payload received:")
            print(json.dumps(response.json(), indent=2))
        else:
            print(f"\n[-] Unexpected HTTP {response.status_code}:")
            print(response.text)

if __name__ == "__main__":
    asyncio.run(main())
