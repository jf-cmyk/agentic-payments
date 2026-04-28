"""Safe SOL sweep utility.

Requires:
  SWEEP_PRIVATE_KEY_BASE64 - base64-encoded 64-byte Solana keypair
  SWEEP_TARGET_ADDRESS     - destination public key

Optional:
  SOLANA_RPC_URL
  SWEEP_FEE_BUFFER_LAMPORTS
"""

from __future__ import annotations

import asyncio
import base64
import os

from solana.rpc.async_api import AsyncClient
from solana.transaction import Transaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer


def _load_keypair() -> Keypair:
    encoded = os.environ.get("SWEEP_PRIVATE_KEY_BASE64")
    if not encoded:
        raise SystemExit("Set SWEEP_PRIVATE_KEY_BASE64 in your environment.")
    return Keypair.from_bytes(base64.b64decode(encoded))


async def main() -> None:
    keypair = _load_keypair()
    target_raw = os.environ.get("SWEEP_TARGET_ADDRESS")
    if not target_raw:
        raise SystemExit("Set SWEEP_TARGET_ADDRESS in your environment.")

    target = Pubkey.from_string(target_raw)
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    fee_buffer = int(os.environ.get("SWEEP_FEE_BUFFER_LAMPORTS", "10000"))

    async with AsyncClient(rpc_url) as client:
        balance = (await client.get_balance(keypair.pubkey())).value
        if balance <= fee_buffer:
            print("Balance is below the configured fee buffer; nothing swept.")
            return

        amount = balance - fee_buffer
        recent = await client.get_latest_blockhash()
        params = TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=target,
            lamports=amount,
        )
        transaction = Transaction(
            recent_blockhash=recent.value.blockhash,
            fee_payer=keypair.pubkey(),
        )
        transaction.add(transfer(params))
        transaction.sign(keypair)

        response = await client.send_transaction(transaction, keypair)
        print(f"Sweep submitted: {response.value}")


if __name__ == "__main__":
    asyncio.run(main())
