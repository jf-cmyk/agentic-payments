import httpx
import json

tx_hash = "cmjPfabJtFMCKrzoY5qdX42eSCPRM4YxnLWb5TQQk7ygngbXSQ8A7akh4Ea9vUc3d7NzYJBMEW8TvSt9g77oMd1"
rpc_url = "https://api.mainnet-beta.solana.com"

res = httpx.post(rpc_url, json={
    "jsonrpc": "2.0",
    "id": 1,
    "method": "getTransaction",
    "params": [tx_hash, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
})
data = res.json()
print(data)
