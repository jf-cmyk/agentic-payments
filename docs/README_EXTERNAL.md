# 🪙 Blocksize Capital x402 Data Economy
### High-Performance Market Data for Autonomous Agents

This server provides institutional-grade market data (Crypto, Equities, FX, Metals, Rates) gated behind the **x402 Payment Protocol**. It is designed for autonomous agents that can pay for their own data consumption in real-time.

---

## 🚀 Quick Start for Agent Developers

### 1. Discover the Tools
The server is **MCP-Compatible**. If you use Claude Desktop or any MCP-compliant client, point it to our manifest:
- **Manifest URL:** `https://agentic-payments-production.up.railway.app/mcp/manifest.json`

### 2. The Payment Protocol (x402)
Our API uses a **Pay-Per-Call** model. There are no monthly subscriptions.
1.  **Request:** Your agent makes a standard GET request to a data endpoint.
2.  **Challenge:** The server returns `HTTP 402 Payment Required`.
3.  **Metadata:** Look at the `PAYMENT-REQUIRED` header. It contains a Base64-encoded JSON array specifying the amount (USDC) and the destination wallet.
4.  **Payment:** Your agent sends the USDC (on Solana or Base L2).
5.  **Proof:** Re-submit the request with the `PAYMENT-SIGNATURE` header containing base64-encoded JSON with at least `proof` and `network` fields.
6.  **Data:** The server verifies the transaction and returns the institutional data instantly.

### 3. Accepted Networks
- **Solana (Mainnet):** $0.002 - $0.008 USDC per call.
- **Base L2:** Supported fallback for EVM-native agents.

---

## 🛠 Integration Example (Python)

We provide a bare-bones integration script in this repository:
👉 `scripts/examples/client_boilerplate.py`

Simply provide your private key and the script handles the entire loop: **Request -> Pay -> Retrieve**.

---

## 📊 Current Pricing (USDC)

| Service | Price | Description |
| :--- | :--- | :--- |
| **Real-Time VWAP** | $0.002 - $0.004 | Tiered by asset liquidity |
| **Bid/Ask Snapshot** | $0.002 - $0.004 | Deep liquidity order book data |
| **Equities** | $0.008 | US & Chinese stock snapshots |
| **FX & Metals** | $0.005 | Gold, Silver, and 120+ FX pairs |
| **Batch Query** | Dynamic | Sum of assets (Cheaper than individual calls) |
| **Search** | FREE | Find valid tickers and instrument lists |

---

## 👩‍💻 Support
For API key upgrades or custom throughput limits, contact the project maintainer.
