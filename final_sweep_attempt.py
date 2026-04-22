import asyncio
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solana.transaction import Transaction
from solana.rpc.commitment import Confirmed

async def main():
    # Reconstructing the compromised key (it was 5cMwy... which corresponds to these bytes)
    old_bytes = [126, 3, 83, 75, 208, 170, 234, 86, 177, 172, 187, 140, 23, 87, 208, 79, 124, 254, 96, 250, 23, 119, 237, 93, 200, 2, 195, 96, 239, 169, 42, 124, 68, 125, 195, 2, 83, 234, 163, 68, 250, 23, 58, 184, 210, 181, 107, 240, 81, 83, 84, 25, 166, 146, 14, 109, 163, 59, 81, 193, 176, 77, 208, 87]
    old_kp = Keypair.from_bytes(bytes(old_bytes))
    old_addr = old_kp.pubkey()
    
    # New Account to fund
    new_addr = Pubkey.from_string("49FF6NWognLXj751gV6hzD4wYiXFA8SqmFhnALaJc5mQ")
    
    rpc_url = "https://mainnet.helius-rpc.com/?api-key=2a2801c5-01ca-458f-9aaa-5aafc1886571"
    async with AsyncClient(rpc_url) as client:
        bal_resp = await client.get_balance(old_addr)
        bal = bal_resp.value
        print(f"[*] Starting balance of compromised wallet: {bal} lamports (~{bal/1e9:.8f} SOL)")
        
        # We need to leave a buffer. Simple 5000 is failing. Let's try leaving 15,000 for safety.
        # However, the REAL problem is probably rent-exemption for the new account.
        # Rent-exemption is ~2,039,280 lamports. 
        # 1,447,680 (current bal) < 2,039,280.
        # Solana protects beginners by preventing transfers that create non-rent-exempt accounts.
        
        if bal < 10000:
            print("[!] Balance is way too low.")
            return

        # Attempting aggressive sweep minus a larger fee buffer
        amount = bal - 10000 
        
        recent = await client.get_latest_blockhash()
        params = TransferParams(from_pubkey=old_addr, to_pubkey=new_addr, lamports=amount)
        instruction = transfer(params)
        
        txn = Transaction(recent_blockhash=recent.value.blockhash, fee_payer=old_addr)
        txn.add(instruction)
        txn.sign(old_kp)
        
        try:
            print(f"[*] Attempting to transfer {amount} lamports...")
            # We skip_preflight to see if we can force it through (though it will likely fail on chain if simulation fails)
            resp = await client.send_transaction(txn, old_kp)
            print(f"[*] Transaction Sent: {resp.value}")
            await client.confirm_transaction(resp.value, commitment=Confirmed)
            print("[+] SUCCESS! Remaining SOL moved to the new address.")
        except Exception as e:
            print(f"[!] FAILED: {e}")
            if "InsufficientFundsForFee" in str(e):
                print("    - Analysis: Solana requires a minimum balance to keep accounts 'alive' or to pay fees.")
            if "AccountDoesNotExists" in str(e) or "rent" in str(e).lower():
                print("    - Analysis: This most likely means the amount (0.0014 SOL) is below the RENT-EXEMPT threshhold (~0.0020 SOL) required to initialize a new account on-chain.")

asyncio.run(main())
