import sys
import base64
import json
import asyncio
import os
import time
from dotenv import load_dotenv

load_dotenv()
import httpx

try:
    from solana.rpc.async_api import AsyncClient
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from spl.token.instructions import get_associated_token_address, transfer_checked, TransferCheckedParams
    from solana.transaction import Transaction
    from solders.system_program import TransferParams, transfer
    from solana.rpc.commitment import Confirmed
except Exception:
    print("[-] Missing Solana libraries! Please run:")
    print("    pip install solana solders base58")
    sys.exit(1)

USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
WALLET_FILE = "agent_wallet.json"
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# --------------------------------------------------------------------------------
# WALLET MANAGEMENT
# --------------------------------------------------------------------------------
def load_secure_wallet():
    """Load the agent identity from secure environment variables."""
    encoded_key = os.getenv("AGENT_PRIVATE_KEY")
    if not encoded_key:
        print("🚨 SECURITY ERROR: AGENT_PRIVATE_KEY not found in .env!")
        print("   Please ensure you have set your rotated private key in your environment.")
        sys.exit(1)
    
    try:
        # Key is stored as base64 encoded bytes of the 64-byte keypair
        import base64
        kp_bytes = base64.b64decode(encoded_key)
        kp = Keypair.from_bytes(kp_bytes)
        print(f"🤖 [Agent]: Identity loaded securely from Environment")
        return kp
    except Exception as e:
        print(f"🚨 FAILED TO LOAD IDENTITY: {e}")
        sys.exit(1)

async def get_usdc_balance(rpc: AsyncClient, agent_pubkey: Pubkey):
    ata = get_associated_token_address(agent_pubkey, USDC_MINT)
    try:
        resp = await rpc.get_token_account_balance(ata)
        return float(resp.value.ui_amount)
    except Exception:
        return 0.0

async def get_sol_balance(rpc: AsyncClient, agent_pubkey: Pubkey):
    resp = await rpc.get_balance(agent_pubkey)
    return resp.value / 1_000_000_000

# --------------------------------------------------------------------------------
# AUTONOMOUS PAYMENT LOGIC
# --------------------------------------------------------------------------------
async def send_usdc(rpc: AsyncClient, kp: Keypair, destination: str, amount_usdc: float) -> str:
    print(f"   💸 Executing SOLANA Native USDC Transfer: ${amount_usdc} to {destination}")
    dest_pubkey = Pubkey.from_string(destination)
    
    agent_ata = get_associated_token_address(kp.pubkey(), USDC_MINT)
    dest_ata = get_associated_token_address(dest_pubkey, USDC_MINT)
    
    # Needs 6 decimals for USDC
    amount_in_micro_usdc = int(amount_usdc * 1_000_000)

    instruction = transfer_checked(
        TransferCheckedParams(
            program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
            source=agent_ata,
            mint=USDC_MINT,
            dest=dest_ata,
            owner=kp.pubkey(),
            amount=amount_in_micro_usdc,
            decimals=6,
        )
    )

    recent_blockhash_resp = await rpc.get_latest_blockhash()
    recent_blockhash = recent_blockhash_resp.value.blockhash

    txn = Transaction(recent_blockhash=recent_blockhash, fee_payer=kp.pubkey())
    txn.add(instruction)
    
    txn.sign(kp)
    
    # Send transaction
    resp = await rpc.send_transaction(txn, kp)
    tx_hash = str(resp.value)
    
    print(f"   ⏳ Broadcasted TX: {tx_hash} (waiting for confirmation...)")
    
    # Wait for confirmation
    confirm = await rpc.confirm_transaction(resp.value, commitment=Confirmed)
    # in solana 0.30.2, confirm.value is a list of SignatureStatus objects
    if isinstance(confirm.value, list) and len(confirm.value) > 0 and confirm.value[0].err:
        print(f"   ❌ Transaction failed on chain: {confirm.value[0].err}")
        return ""
        
    print(f"   ✅ Transaction Confirmed!")
    return tx_hash


# --------------------------------------------------------------------------------
# AGENT LOOP
# --------------------------------------------------------------------------------
async def run_agent_loop(base_url_input: str):
    import urllib.parse
    import random
    
    parsed = urllib.parse.urlparse(base_url_input)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    kp = load_secure_wallet()
    wallet_addr = str(kp.pubkey())
    
    print("\n" + "="*60)
    print("🟢 AUTONOMOUS AGENT WALLET STATUS")
    print("="*60)
    print(f"Agent Wallet Address: {wallet_addr}")
    
    async with AsyncClient(SOLANA_RPC_URL) as rpc:
        sol_bal = await get_sol_balance(rpc, kp.pubkey())
        usdc_bal = await get_usdc_balance(rpc, kp.pubkey())
        
        print(f"SOL Balance:  {sol_bal} SOL (gas)")
        print(f"USDC Balance: {usdc_bal} USDC")
        print("="*60 + "\n")
        
        if sol_bal < 0.001 or usdc_bal < 0.05:
            print("🚨 INSUFFICIENT FUNDS! 🚨")
            print("1. Open Phantom.")
            print(f"2. Send 0.005 SOL to: {wallet_addr} (for transaction fees)")
            print(f"3. Send 0.05 USDC to:  {wallet_addr}")
            print("4. Restart this script once funded.\n")
            return

        print("🤖 [Agent]: Budget secured. Contacting Discovery Node for Master Instrument List...\n")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Fetch Instruments (Free Search)
            discovery_url = f"{base_url}/v1/instruments/vwap"
            try:
                resp = await client.get(discovery_url)
                if resp.status_code == 200:
                    all_instruments = resp.json().get("instruments", [])
                else:
                    all_instruments = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "LINKUSD"]
            except Exception:
                all_instruments = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "LINKUSD"]
                
            if not all_instruments:
                all_instruments = ["BTCUSD", "ETHUSD", "SOLUSD"]
                
            # 2. Pick 10 random assets
            sample_size = min(10, len(all_instruments))
            chosen_pairs = random.sample(all_instruments, sample_size)
            
            endpoints = ["vwap", "bidask", "state", "vwap30m"]
            
            print(f"🤖 [Agent]: Discovered {len(all_instruments)} assets. Executing 10 autonomous loops.\n")
            
            for i, pair in enumerate(chosen_pairs, start=1):
                endpoint_type = random.choice(endpoints)
                target_url = f"{base_url}/v1/{endpoint_type}/{pair}"
                
                print(f"[{i}/10] Requesting endpoint: {target_url}")
                response = await client.get(target_url)

                if response.status_code == 402:
                    print(f"   ⛔️ Intercepted 402 Payment Required.")
                    
                    data = response.json()
                    price = float(data.get("price_usdc", 0.002))
                    
                    # Extract Destination
                    pay_to_address = ""
                    req_header = response.headers.get("PAYMENT-REQUIRED")
                    if req_header:
                        decoded = json.loads(base64.b64decode(req_header))
                        sol_req = next((r for r in decoded if "solana" in str(r.get("network", "")).lower()), None)
                        if sol_req:
                            pay_to_address = sol_req.get("payTo")
                            
                    # Autonomously pay
                    tx_hash = await send_usdc(rpc, kp, pay_to_address, price)
                    
                    if not tx_hash:
                        print("   ❌ Agent failed to settle payment. Stopping.")
                        break
                        
                    # Wrap cryptographic payload
                    signed_payload = {
                        "proof": tx_hash,
                        "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                        "timestamp": int(time.time()),
                        "agent_pubkey": wallet_addr,
                        "signature": "AUTONOMOUS_PYTHON_AGENT_EXECUTION"
                    }
                    signature_header = base64.b64encode(json.dumps(signed_payload).encode()).decode()
                    
                    # Request real data
                    print("   [*] Resubmitting request with cryptographic signature...")
                    final_resp = await client.get(target_url, headers={"PAYMENT-SIGNATURE": signature_header})
                    
                    if final_resp.status_code == 200:
                        print(f"   🎉 SUCCESS! Real-time payload retrieved:")
                        print(json.dumps(final_resp.json(), indent=2))
                    else:
                        print(f"   ❌ Verification failed: HTTP {final_resp.status_code} - {final_resp.text}")
                        break
                        
                elif response.status_code == 200:
                    print("   [+] Endpoint is Free.")
                else:
                    print(f"   [-] Failed. HTTP {response.status_code}")
                
                if i < 10:
                    print("\n--- Next request in 2 seconds... ---")
                    await asyncio.sleep(2)
                    
        print("\n✅ Agent run complete.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/autonomous_solana_agent.py [BASE_URL or ANY_URL]")
        sys.exit(1)
        
    asyncio.run(run_agent_loop(sys.argv[1]))
