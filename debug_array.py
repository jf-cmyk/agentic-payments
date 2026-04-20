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
                "params": {"ticker": ""},
                "id": 1
            }
        )
        snapshot = res.json().get('result', {}).get('snapshot', [])
        
        tickers = [item.get("ticker", "").upper() for item in snapshot]
        
        # Are there any typical equities? e.g. TSLA, MSFT?
        for t in ["TSLA", "MSFT", "AAPL", "GOOGL", "NVDA", "Rates.US10Y", "US10Y", "RATES.US2Y"]:
            if t in tickers:
                print(f"FOUND EXACT: {t}")
                
        # Let's see if there are ANY rates
        for t in tickers:
            if "US10Y" in t or "RATE" in t:
                print(f"FOUND MATCH: {t}")
        
        print(f"Total Tickers: {len(tickers)}")

asyncio.run(debug())
