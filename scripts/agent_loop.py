import sys
import base64
import json
import httpx
import asyncio

async def fetch_data(client, url, iteration):
    print(f"\n[{iteration}/3] Requesting data from {url}...")
    response = await client.get(url)

    # Step 1: Handle the 402 Payment Required intercept
    if response.status_code == 402:
        print("[!] HTTP 402 Payment Required intercepted.")
        data = response.json()
        
        price = data.get("price_usdc")
        
        # Step 2: Extract the destination wallet address
        pay_to_address = "UNKNOWN"
        req_header = response.headers.get("PAYMENT-REQUIRED")
        
        if req_header:
            decoded_reqs = json.loads(base64.b64decode(req_header))
            sol_req = next((r for r in decoded_reqs if "solana" in str(r.get("network", "")).lower()), None)
            if sol_req:
                pay_to_address = sol_req.get("address")

        # Step 3: Prompt the user to use Phantom
        print("\n" + "="*55)
        print(f"💰 MANUAL PAYMENT {iteration}/3 (PHANTOM WALLET)")
        print("="*55)
        print(f"1. Open your Phantom Wallet extension in Chrome.")
        print(f"2. Click 'Send' and select USDC (on Solana).")
        print(f"3. Send exactly: {price} USDC")
        print(f"4. Send to address: {pay_to_address}")
        print("5. Wait for confirmation, view the transaction on Solscan,")
        print("   and copy the Transaction Signature (Hash).")
        print("="*55 + "\n")

        tx_hash = input("Paste your Phantom Transaction Signature here: ").strip()

        if not tx_hash:
            print("[-] No transaction signature provided. Stopping agent.")
            return False

        # Step 4: Resubmit the request with the proof
        print("\n[*] Resubmitting request with payment proof...")
        headers = {
            "PAYMENT-SIGNATURE": tx_hash 
        }
        
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
        print("Usage: python agent_loop.py [URL]")
        print("Example: python agent_loop.py https://agentic-payments-production.up.railway.app/v1/vwap/BTC-USD")
        sys.exit(1)

    url = sys.argv[1]
    print("🤖 Agent starting. Target: fetch data 3 times.")

    async with httpx.AsyncClient() as client:
        for i in range(1, 4):
            success = await fetch_data(client, url, i)
            if not success:
                break
            
            if i < 3:
                print("\n--- Waiting 2 seconds before the next agentic request... ---")
                await asyncio.sleep(2)
                
    print("\n✅ Agent run complete.")


if __name__ == "__main__":
    asyncio.run(main())
