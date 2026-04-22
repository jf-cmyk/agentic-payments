"""
End-to-end agent flow simulation.

Simulates what an AI agent would do when connecting to the Blocksize
MCP server: discover tools, check pricing, search pairs, and fetch
VWAP/bid-ask data.

Usage:
    python scripts/test_agent_flow.py

This script calls the Blocksize API directly (bypassing x402 for testing).
Set BLOCKSIZE_API_KEY in your .env to run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError
from src.config import settings


async def simulate_agent_flow() -> None:
    """Simulate a typical agent interaction flow."""
    print("=" * 60)
    print("  Blocksize MCP — Agent Flow Simulation")
    print("=" * 60)
    print()

    client = BlocksizeClient()

    try:
        # Step 1: Discover available instruments
        print("🔍 Step 1: Listing VWAP instruments...")
        try:
            instruments = await client.list_vwap_instruments()
            print(f"   ✅ Found {len(instruments)} VWAP instruments")
            if instruments:
                print(f"   Sample: {instruments[:5]}")
        except BlocksizeAPIError as e:
            print(f"   ⚠️  Could not list instruments: {e}")

        print()

        # Step 2: Search for specific pairs
        print("🔍 Step 2: Searching for BTC pairs...")
        try:
            pairs = await client.search_pairs("btc")
            print(f"   ✅ Found {len(pairs)} BTC pairs")
            for p in pairs[:5]:
                print(f"      • {p.pair} (services: {', '.join(p.services)})")
        except BlocksizeAPIError as e:
            print(f"   ⚠️  Search failed: {e}")

        print()

        # Step 3: Get VWAP for BTC-USD
        print("📊 Step 3: Fetching VWAP for btc-usd...")
        try:
            vwap = await client.get_vwap_latest("btc-usd")
            print(f"   ✅ {vwap.to_decision_summary()}")
        except BlocksizeAPIError as e:
            print(f"   ⚠️  VWAP request failed: {e}")
            print("   (This is expected if your API key doesn't have VWAP access)")

        print()

        # Step 4: Get bid/ask for BTC-USD
        print("📊 Step 4: Fetching bid/ask for btc-usd...")
        try:
            bidask = await client.get_bidask_snapshot("btc-usd")
            print(f"   ✅ {bidask.to_decision_summary()}")
        except BlocksizeAPIError as e:
            print(f"   ⚠️  Bid/ask request failed: {e}")
            print("   (This is expected if your API key doesn't have bid/ask access)")

        print()

        # Step 5: Pricing summary
        print("💰 Step 5: Pricing info")
        for tier, details in settings.pricing_summary.items():
            print(f"   {tier}: {details['price']} USDC/call — {details['includes']}")
        print(f"   Primary network: {settings.x402.primary_network}")
        print(f"   Primary wallet:  {settings.x402.primary_wallet or '(not configured)'}")

        print()
        print("=" * 60)
        print("  Simulation complete!")
        print("=" * 60)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(simulate_agent_flow())
