import httpx, base64, json
reqs = [{"scheme": "exact", "version": "1.0", "network": "solana"}]
res = httpx.post("https://x402.org/facilitator/verify", json={"paymentPayload": "dummy", "paymentRequirements": reqs}, follow_redirects=True)
print("Status:", res.status_code, res.text)
