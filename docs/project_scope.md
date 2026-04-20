# Project Scope: Monetizing Agentic Data Infrastructure

## 1. Executive Summary
The Blocksize Capital Agentic Data Infrastructure is a production-ready system designed to instantly monetize institutional-grade market data. By wrapping Blocksize Capital’s robust data pipelines with the model context protocol (MCP) and integrating the x402 HTTP payment standard, this project enables autonomous AI agents to seamlessly discover, purchase, and consume financial data without human intervention. 

Instead of traditional API subscriptions and credit card processing, the system enforces real-time, per-call micro-transactions directly on public blockchains (Solana and Base), settling payments instantaneously into the administrator's wallet.

## 2. Technical Architecture
The core system is deployed as a stateless, highly scalable cloud application on Railway.app, utilizing the following stack:

*   **FastAPI Resource Server:** The core web server handling incoming REST requests.
*   **x402 Payment Middleware:** A custom interceptor that blocks unauthorized access, dynamically calculating the cost of the requested endpoint and returning standard ``HTTP 402 Payment Required`` responses containing cryptographic payment requirements constraints.
*   **Blocksize Capital API Client:** An asynchronous Python wrapper that securely interfaces with the upstream Blocksize infrastructure, authenticating via a hidden API key to fetch market data.
*   **FastMCP Server:** An agent-facing interface that exposes the API functionality as structured "tools" for LLMs running locally (e.g., Claude Desktop, Cursor).

## 3. Data Integration Scope
The system currently exposes over 30,000 distinct financial instruments, structured into tiered pricing brackets based on data density and processing requirements:

*   **Discovery (FREE):** Asset lookup, instrument listing, and pricing summaries.
*   **Core Crypto ($0.002):** Real-time VWAP and Bid/Ask spreads for top-250 capitalized cryptocurrencies.
*   **Extended Crypto ($0.004):** VWAP and Bid/Ask for 550+ long-tail and niche token pairs.
*   **Traditional Finance ($0.005):** Spot Foreign Exchange (129 pairs), Precious Metals (5 assets), and US Treasury Rates (8 maturity buckets).
*   **Equities ($0.008):** High-level snapshot data for over 18,000 US and Chinese stocks.
*   **Deep Analytics ($0.008):** Complex historical metrics including 30-Minute VWAP, 24-Hour VWAP, and end-of-day State Prices.

## 4. Financial & Settlement Architecture
To support frictionless agentic purchasing, the infrastructure bypasses legacy banking rails:

*   **x402 Compliance:** The system fully adheres to the emerging x402 machine-to-machine payment protocol.
*   **Dual-Network Settlement:** Agents are provided an invoice dictating payment in USDC. They can autonomously settle the invoice on either:
    *   **Solana (Primary):** For sub-second finality and near-zero transaction costs.
    *   **Base L2 (Fallback):** For broader EVM framework compatibility.
*   **Stateless Verification:** Payment proofs (Transaction Hashes) provided by the agents are verified securely via the Coinbase Developer Platform (CDP) Facilitator, ensuring no loss of funds without requiring a local database of processed transactions.

## 5. Security & Deployment Posture
*   **Environment Hardening:** Secrets (Blocksize API Keys and Settlement Wallet Addresses) are strictly isolated using Pydantic Settings and injected at runtime via Railway's encrypted variable store.
*   **Error Masking:** Internal Blocksize upstream timeouts or exceptions are safely abstracted into standard `HTTP 502 Bad Gateway` errors, ensuring downstream agents do not receive stack traces or sensitive internal paths.
*   **Dependency Management:** The environment is deterministically locked using `uv` and `pyproject.toml`, resolving build-time fragmentation during containerization.

## 6. Future Capabilities (Phase II)
While the current scope completely fulfills the objective of a stateless monetization node, future enhancements could include:
1.  **WebSocket Streaming (x402w):** Implementing pay-per-minute streaming architecture for high-frequency trading bots.
2.  **Dynamic Pricing Oracle:** Adjusting the USDC price per-call automatically based on network congestion or upstream rate limits.
3.  **Agent Funding Escrow:** Allowing enterprise clients to deposit larger sums upfront into a smart contract to reduce per-call blockchain gas overhead for their agents.
