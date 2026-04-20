import asyncio
import os
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solana.transaction import Transaction

async def main():
    old_bytes = [126, 3, 83, 75, 208, 170, 234, 86, 177, 172, 187, 140, 23, 87, 208, 79, 124, 254, 96, 250, 23, 119, 237, 93, 200, 2, 195, 96, 239, 169, 42, 124, 68, 125, 195, 2, 83, 234, 163, 68, 250, 23, 58, 184, 210, 181, 107, 240, 81, 83, 84, 25, 166, 146, 14, 109, 163, 59, 81, 193, 176, 77, 208, 87]
    old_kp = Keypair.from_bytes(bytes(old_bytes))
    target = Pubkey.from_string("89xvyfLae5s1BgWBmdrsWQfJ3fYDQFywYBJev8fgtMEu")
    
    async with AsyncClient("https://mainnet.helius-rpc.com/?api-key=2a2801c5-01ca-458f-9aaa-5aafc1886571") as client:
        bal = (await client.get_balance(old_kp.pubkey())).value
        if bal < 6000: return
        
        # Leave a tiny bit of SOL to avoid the "fully empty account" simulation quirks
        amount = bal - 10000
        recent = await client.get_latest_blockhash()
        params = TransferParams(from_pubkey=old_kp.pubkey(), to_pubkey=target, lamports=amount)
        txn = Transaction(recent_blockhash=recent.value.blockhash, fee_payer=old_kp.pubkey())
        txn.add(transfer(params))
        txn.sign(old_kp)
        
        try:
            resp = await client.send_transaction(txn, old_kp)
            print(f"Sweep Succesful to Main: {resp.value}")
        except Exception as e:
            print(f"Failed: {e}")

asyncio.run(main())
