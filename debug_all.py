import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def debug():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    async with httpx.AsyncClient() as client:
        res = await client.post(
            url,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "method": "bidask_getSnapshot",
                "params": {"ticker": "AAPL"},  # See if it accepts AAPL
                "id": 1
            }
        )
        snapshot = res.json().get('result', {}).get('snapshot', [])
        print("TICKER: AAPL")
        for item in snapshot:
            if item.get("ticker", "").upper() == "AAPL":
                print("FOUND AAPL:", json.dumps(item, indent=2))
                break
        
        # Also check EURUSD
        for item in snapshot:
            if item.get("ticker", "").upper() == "EURUSD":
                print("FOUND EURUSD:", json.dumps(item, indent=2))
                break

        # Also check Rates.US10Y
        for item in snapshot:
            if item.get("ticker", "").upper() == "RATES.US10Y":
                print("FOUND US10Y:", json.dumps(item, indent=2))
                break

asyncio.run(debug())
