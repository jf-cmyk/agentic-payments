# 5-Minute Quickstart And Demo Outline

Purpose: create one reliable path from curiosity to first successful agentic payment concept.

## Demo promise

"Watch an AI agent discover a paid market-data resource, receive an HTTP 402 invoice, understand the USDC price and accepted networks, and fulfill the request through a payment-proof flow."

Until proof verification is hardened, demos should clearly distinguish:

- Live 402 challenge and pricing behavior.
- Mock/testnet payment proof behavior.
- Planned production-grade verification enhancements.

## Known-good path

Use this path first:

```text
GET /v1/vwap/BTC-USD
```

Why:

- Core crypto is the cleanest ICP demo.
- Price is low and easy to understand.
- Resource-server tests confirm the payment gate is active for `/v1/vwap`.
- BTC-USD is familiar to every prospect.

## 5-minute flow

1. Start with free discovery.

```bash
curl -s "https://mcp.blocksize.info/v1/search?q=btc"
```

2. Request paid data without payment.

```bash
curl -i "https://mcp.blocksize.info/v1/vwap/BTC-USD"
```

Expected:

- HTTP 402.
- `PAYMENT-REQUIRED` response header.
- JSON body with `price_usdc` and supported networks.

3. Decode the payment requirement locally.

```bash
python3 - <<'PY'
import base64, json, os
value = os.environ["PAYMENT_REQUIRED"]
print(json.dumps(json.loads(base64.b64decode(value)), indent=2))
PY
```

4. Explain the agent decision.

The agent checks:

- Price.
- Network.
- `payTo`.
- Asset.
- Timeout.
- Its own budget policy.

5. Show mock/testnet proof retry.

Use a deterministic fixture or approved testnet transaction. Do not imply mock signatures are valid production payments.

6. Close with business value.

One autonomous agent can discover, price, pay for, and consume institutional data without a sales call, subscription, invoice, API key exchange, or manual checkout.

## Mission Control demo storyboard

| Scene | What Johann/prospect sees | Proof artifact |
| --- | --- | --- |
| Agent needs BTC VWAP | Agent explains data need and policy budget | Task list |
| Agent discovers endpoint | Agent calls free search/pricing | Terminal output |
| Agent hits 402 | Agent parses payment requirement | Decoded invoice |
| Agent chooses rail | Solana preferred, Base fallback | Decision note |
| Agent retries with proof | Mock/testnet proof until production hardening | Response artifact |
| Agent records audit | Who/what/why/how much/which endpoint | Audit log preview |

## Demo readiness checklist

- [ ] Only demo endpoints that pass tests or are manually verified.
- [ ] Fix `scripts/agent_loop.py` to read `payTo`, not `address`.
- [ ] Add mock/testnet mode.
- [ ] Add copy-paste env setup.
- [ ] Add short "verified today vs planned" note.
- [ ] Capture screenshot/video artifact after local verification.

