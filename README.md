# Blocksize Capital MCP Server

**Institutional-grade multi-asset market data for AI agents — pay per call via x402.**

[![x402](https://img.shields.io/badge/payment-x402-blue)](https://x402.org)
[![MCP](https://img.shields.io/badge/protocol-MCP-green)](https://modelcontextprotocol.io)
[![Solana](https://img.shields.io/badge/network-Solana-14F195)](https://solana.com)
[![Base](https://img.shields.io/badge/fallback-Base%20L2-0052FF)](https://base.org)

## Overview

This MCP server wraps [Blocksize Capital](https://blocksize.capital)'s institutional market data API and makes it available to AI agents as callable tools. Agents can discover, pay for, and consume data across 30,000+ instruments with zero subscription setup.

**Payment** is handled via the [x402 protocol](https://x402.org) — agents pay per data call using USDC, settled on **Solana (Primary)** or **Base L2 (Fallback)**. All payments are verified and settled by the CDP Facilitator.

### Why Blocksize over free alternatives?

| Feature | Blocksize MCP | CoinGecko MCP | Pyth MCP (Community) |
|---------|--------------|---------------|---------------------|
| **Asset Coverage** | 30k+ Crypto, Equities, FX, Metals, Rates | Crypto only | Crypto, some TradFi |
| **Equities Data** | 18,000+ US/Chinese stocks | ❌ | ❌ |
| **VWAP Quality** | Real-time, outlier-filtered | Daily aggregate string | Raw oracle price |
| **Analytics** | 30-Min & 24Hr VWAP, State Price | ❌ | ❌ |
| **Price** | Tiered ($0.002 - $0.008/call) | Free (rate-limited) | Gas fees |

## Quick Start

### 1. Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Blocksize Capital API key
- USDC wallet on Solana (to receive payments)

### 2. Install

```bash
# Clone and enter the project
cd "Agentic Payments"

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Blocksize Auth
BLOCKSIZE_API_KEY=your_blocksize_api_key

# Solana Payment Setup (Primary)
X402_SOLANA_WALLET_ADDRESS=YourSolanaWalletAddressHere

# Base Payment Setup (Fallback - Optional)
X402_EVM_WALLET_ADDRESS=0xYourBaseWalletAddressHere
```

### 4. Run the MCP Server

**Local (STDIO) — for Claude Desktop / Cursor:**

```bash
source .venv/bin/activate
python -m src.mcp_server
```

### 5. Connect to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "blocksize-capital": {
      "command": "/path/to/Agentic Payments/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/Agentic Payments",
      "env": {
        "BLOCKSIZE_API_KEY": "your_key",
        "X402_SOLANA_WALLET_ADDRESS": "YourSolanaWallet",
        "X402_EVM_WALLET_ADDRESS": "0xYourFallbackBaseWallet"
      }
    }
  }
}
```

## Available Tools & Tiered Pricing

Discovery tools are **FREE**, ensuring zero-friction onboarding for agents. Data endpoints use dynamic tier pricing based on the asset requested.

| Tier | Services | Assets Covered | Price/Call |
|------|----------|----------------|------------|
| **🆓 Discovery** | `search_pairs`, `list_instruments`, `get_pricing_info` | All | **FREE** |
| **📊 Core Crypto** | `get_vwap`, `get_bid_ask` | Top ~250 crypto by market cap | **$0.002** |
| **📊 Extended Crypto**| `get_vwap`, `get_bid_ask` | 550+ long-tail / niche crypto | **$0.004** |
| **🏦 TradFi** | `get_fx_rate`, `get_metal_price`, `get_treasury_rate`, `get_yield_curve` | FX, Metals, US Treasury Rates | **$0.005** |
| **📈 Equities** | `get_equity` | 18,071 US + Chinese stocks | **$0.008** |
| **⏱️ Analytics** | `get_vwap_30min`, `get_vwap_24hr`, `get_state_price` | Time-series and settlement data | **$0.008** |

## Architecture

```
AI Agent (Claude, GPT, LangChain)
    ↓ MCP Protocol (stdio)
FastMCP Server (src/mcp_server.py)
    ↓ Request data
FastAPI Resource Server (src/resource_server.py)
    ↓ x402 middleware: 402 Payment Required (Solana or Base)
    ↓ Payment verified via CDP Facilitator
    ↓ x-api-key authentication
Blocksize Capital REST & WS API
```

## Resource Server (REST API)

For direct HTTP access (non-MCP clients), you can run the FastAPI resource server:

```bash
source .venv/bin/activate
uvicorn src.resource_server:app --port 8402
```

All paid endpoints return a `402 Payment Required` status if the `PAYMENT-SIGNATURE` header is missing.
The `PAYMENT-REQUIRED` response header contains base64-encoded payment requirements listing accepted networks (Solana priority, then Base).

## Development

```bash
# Run tests (with test env vars)
BLOCKSIZE_API_KEY=test X402_SOLANA_WALLET_ADDRESS=TestSolWallet X402_EVM_WALLET_ADDRESS=0xTestWallet python -m pytest tests/ -v

# Code Quality
python -m ruff check src/ tests/
```

## License

MIT
