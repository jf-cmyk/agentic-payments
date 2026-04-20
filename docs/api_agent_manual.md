# Blocksize Agentic Data API Manual

Welcome to the Blocksize Agentic Data node. This documentation provides AI Agents and Developers the instructions necessary to autonomously discover, purchase, and consume institutional-grade market data via the x402 payment protocol.

This API diverges from traditional, subscription-based data vendors. You do not need to register for an account, negotiate enterprise bounds, or provision API keys. Instead, your Agent simply executes a micro-transaction directly on the blockchain in real-time, per-request, for the exact data constraint it requires.

---

## 1. The Autonomous Flow (x402 Protocol)

When interacting with our data endpoints, your Agent will follow a deterministic 3-step lifecycle:

### Step 1: Demand & Discovery
Your Agent makes an unauthenticated HTTP `GET` request to a secure data endpoint (e.g., `/v1/vwap/BTC-USD`).
Our secure middleware intercepts this request. Because the Agent has not attached cryptographic proof of payment, the server responds with an `HTTP 402 Payment Required` status.

Contained within the response body (and the `PAYMENT-REQUIRED` header) is a machine-readable JSON invoice specifying the real-time cost of the endpoint, the accepted blockchain networks, and the required destination wallets.

```json
{
  "error": "Payment Required",
  "message": "This endpoint requires a payment of $0.002 USDC.",
  "price_usdc": "0.002",
  "networks": [
    {"name": "Solana", "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
    {"name": "Base", "caip2": "eip155:8453"}
  ]
}
```

### Step 2: The Agentic Settlement
Your Agent parses the JSON invoice. Utilizing an embedded funding mechanism (such as a Coinbase Developer Platform AgentKit wallet, or an abstracted LangChain tool), the Agent automatically executes a blockchain transaction transferring the required USDC amount to the designated `payTo` address on the chosen blockchain (Solana or Base L2).

### Step 3: Cryptographic Fulfillment
Once the transaction settles on the blockchain, the Agent extracts the Transaction Hash. The Agent then constructs a base64-encoded x402 valid payload dictating the network, signature format, and proof (transaction hash). 
Finally, the Agent resubmits the exact same `GET` request, this time appending the signature in the headers:

```http
GET /v1/vwap/BTC-USD HTTP/1.1
Host: agentic-payments-production.up.railway.app
PAYMENT-SIGNATURE: eyJhbGciOiJFZERTQ...<agent_cryptographic_signature>
```

Our server natively decodes the payload and instantly validates the transaction completely on-chain via a secure Solana RPC node, preventing playback attacks. Upon verification, it seamlessly fulfills the market data payload as standard JSON.

---

## 2. API Endpoints Reference

All endpoints natively support the Model Context Protocol (MCP) or direct HTTP access. Endpoints marked **FREE** require no x402 settlement.

### 2.1 Asset Discovery (FREE)

**Search Instruments**
`GET /v1/search?q={query}&asset_class={all|crypto|equity}`
Returns all matching instrument pairs based on string queries.

**List Instruments by Service**
`GET /v1/instruments/{service}`
Where `{service}` is one of `vwap`, `bidask`, `equity`, or `fx`. Returns a definitive list of active trading pairs.

### 2.2 Cryptocurrency Data (Dynamic Pricing)

Prices fluctuate based on the capitalization and liquidity indexing requirements of the network requested. Top 250 assets default to **$0.002 USDC**. Niche and long-tail listings default to **$0.004 USDC**.

**Real-Time VWAP, Bid/Ask, and State Price**
`GET /v1/vwap/{pair}`
`GET /v1/bidask/{pair}`
`GET /v1/state/{pair}`
*Returns:* A consolidated top-of-book market depth snapshot across global liquidity pools.

### 2.3 Traditional Finance ($0.005 USDC)

**Foreign Exchange (FX)**
`GET /v1/fx/{pair}`
*Returns:* Spot rates for 129 institutional pairings.
*Example:* `/v1/fx/EUR-USD`

**Metals**
`GET /v1/metal/{ticker}`
*Returns:* Spot rates for institutional stores of value.
*Example:* `/v1/metal/XAU-USD` (Gold)

**US Treasury Rates**
`GET /v1/rate/{maturity}`
*Returns:* Government debt yields over 8 maturity brackets (e.g., 10Y, 2Y).

### 2.4 Equities ($0.008 USDC)

**Equities Snapshot**
`GET /v1/equity/{ticker}`
*Returns:* Market snapshot for 18,071 global stocks.

### 2.5 Historical Analytics ($0.001 USDC)

**Historical/Time-Series VWAP**
`GET /v1/vwap30m/{ticker}` (30-Minute)
`GET /v1/vwap24h/{ticker}` (24-Hour)

---

## 3. Developer Implementation Examples

### Coinbase AgentKit + LangChain
Integrating this server via Coinbase AgentKit eliminates the need to manually build transaction signing logic. 

```typescript
import { AgentKit } from "@coinbase/agentkit";
import { BlocksizeX402Client } from "blocksize-agent-sdk";

// Initialize the Agent's onboard CDP Wallet
const agentWallet = await AgentKit.fundWallet("solana", "5.00");

// The client automatically manages 402 interceptions
const apiClient = new BlocksizeX402Client({
    baseUrl: "https://agentic-payments-production.up.railway.app",
    wallet: agentWallet,
});

// Request data. The client autonomously executes the USDC transfer under the hood.
const btcVwap = await apiClient.getVWAP("BTC-USD");
console.log(btcVwap.price); 
```

### Testing the Paywall
If you are developing non-agentic software or testing natively, you can verify the `402` integration using cURL:

```bash
curl -i https://agentic-payments-production.up.railway.app/v1/fx/JPY-USD
```

You will observe the raw JSON validation constraints block routing appropriately.
