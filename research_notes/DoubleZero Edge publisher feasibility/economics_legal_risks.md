# DoubleZero Edge third-party publisher: business, token-economic, legal and strategic risks (as of 16 October 2026)

Scope note: notes for BlockSize Capital (German/EU market-data vendor, ~110 exchanges aggregated, supplier to Pyth and Chainlink) judging whether to publish feeds on DoubleZero Edge, including possibly operating the Binance feed on DoubleZero's behalf. Every number carries a date. Three evidence classes are kept apart throughout: VERIFIED (URL), CALL CLAIM (what was said in the call with DoubleZero, unverified) and INFERENCE.

Access limits during research: doublezero.xyz journal pages, docs.doublezero.xyz, docs.malbeclabs.com, the MiCA white paper PDF, DefiLlama and the Kairos Research X article all returned 403/404/402 to the fetcher; findings from those are via secondary coverage and are labelled as such.

## Key question 1: What are the actual bond and reward rules for Edge publishers, and what happens to the bond on downtime or bad data?

### Takeaway
No public document describing an Edge publisher bond, a slashing rule or an uptime commitment could be found; the only published reward mechanics are the Solana-shred split (10% burn, then 50% fibre contributors / 32.5% validators / 17.5% client teams) and a generic statement that publishers "typically receive a percentage share of post-burn subscription fees". The call's "10% burn, then 50/50 publisher vs network contributors" is therefore unconfirmed and differs from the only published split.

### Cited Findings
- Edge launched publicly on 16 April 2026 with "350 data publishers" (Solana validators); Jito, Temporal, Staking Facilities and Triton named as participants; traders pay through a "validator-aligned subscription model" — [The Block, 16 Apr 2026](https://www.theblock.co/post/397715/doublezero-rolls-out-edge-a-low-latency-onchain-data-delivery-service-on-its-global-fiber-depin-infrastructure)
- Solana-shred revenue split (as of a Triton One post last updated 20 May 2026): after a protocol burn, fibre operators 50%, validators 32.5%, client development teams (e.g. Jito, Anza) 17.5%; subscribers pay on-chain in USDC — [Triton One blog](https://blog.triton.one/rethinking-the-network-beneath-the-blockchain/)
- Same split reported at Edge launch: "50% to network contributors, 32.5% to validators originating shreds, 17.5% to protocol clients, with 10% going to a protocol burn mechanism" — [search summary of launch coverage incl. crypto.jobs / MEXC news](https://crypto.jobs/news/doublezero-launches-edge-platform-for-low-latency-blockchain-data-delivery)
- Kairos Research (X article, paywalled/402) is cited in search results as saying fibre contributors' allocation dropped "from 90% to 50%" under the new Edge model, with the 10% burn unchanged — [Kairos Research on X](https://x.com/Kairos_Res/article/2041637367463710957) (content not independently retrievable; treat as secondary)
- Datawallet (audit dated 20 Aug 2026): Edge payments in USDC; protocol revenue buys 2Z on the open market; 10% of acquired tokens burned, 90% to fibre and hardware contributors; "participating validators receive a share of Edge revenue for publishing their shreds" with no percentage given — [Datawallet](https://www.datawallet.com/crypto/doublezero-explained)
- Kalshi arrangement (Aug 2026): data publishers "typically get a percentage share of the post-burn subscription fees"; Kalshi waives its publisher share for one year, subscribers still pay network fees — [crypto.news](https://crypto.news/kalshi-brings-live-market-data-to-doublezero-edge/); confirmed in [crypto.news, 12 Oct 2026](https://crypto.news/doublezero-federa-says-fast-market-data-private/)
- Blockworks Token Transparency filing (v1.1, 23 Jun 2026): "Contributors and some data publishers earn 2Z based on the performance and usage of their infrastructure"; a portion of network fees is programmatically burned; "some tokens are burned for integrity, to discourage inorganic traffic that could inflate rewards"; no bond mechanism is mentioned anywhere in the filing — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Hyperion DeFi (Hyperliquid validator operator) "obtained 10 million DoubleZero 2Z tokens as part of the initial arrangement" and "will earn additional future revenue by publishing Hyperliquid market data" — [RTTNews, 24 Sep 2026](https://www.rttnews.com/3693733/hyperion-defi-supports-doublezero-edge-launch-of-hyperliquid-data-feeds.aspx?type=bio)
- The Hyperliquid docs page says DoubleZero runs "multiple publishers" itself; the peering service carries a "99.9% monthly" availability target but the page explicitly excludes Edge market-data feeds from that service; no publisher documentation or bond terms on the page — [docs.doublezero.xyz/hyperliquid](https://docs.doublezero.xyz/hyperliquid/)
- Multicast group definitions, transport configuration and permissioning are "programmable onchain via the DoubleZero ledger" (testnet-era description) — [RockawayX](https://rockawayx.com/insights/doublezero-launches-native-multicast-for-decentralized-systems)
- Enforcement precedent: on 9 Sep 2026 DoubleZero removed "unauthorized retransmitters" (RPCs forwarding the licensed shred feed without a licence) from Edge; Federa announced it 10 Sep; Node1 disputed being a retransmitter; parties were not named; no fine or other penalty described beyond removal — [Solana Compass](https://solanacompass.com/news/doublezero-removes-unauthorized-shred-retransmitters-claims-70-plus-lead-over-all-competitors)

### Inferences
- CALL CLAIM vs VERIFIED: the call's "10% burn, remainder 50/50 publisher vs contributors" would give a publisher 45% of gross. The only published split (Solana shreds) gives the data originators 32.5% post-burn = 29.25% of gross, with 17.5% post-burn going to client teams. For exchange/venue feeds there may be no "client team" bucket, which would make a 50/50 split plausible, but nothing public confirms it. Get the split in writing before committing.
- Rewards are paid in 2Z bought with subscriber USDC, so a publisher carries 2Z price risk between accrual and sale. The Hyperion deal (10M 2Z up front, worth roughly $0.6M at the ~$0.06 price of late Sept 2026 and roughly $0.32M at $0.032 on 16 Oct 2026) shows DoubleZero also pays onboarding grants in 2Z.
- Because no bond, slashing or SLA document is public, any bond terms offered in the call are bespoke contract terms; downtime consequences would be governed by that contract and by DoubleZero's demonstrated willingness to de-list participants unilaterally (Sept 2026 retransmitter removal).

### Gaps
- No public source for an Edge publisher bond amount, bond currency (2Z or USDC), lock-up, or slashing conditions on downtime/bad data. The docs site (docs.doublezero.xyz/edge) and the "Validator's Guide to DoubleZero Edge" journal post were not retrievable (404/403).
- No public uptime/SLA commitment for Edge market-data publishers; the 99.9% target applies to peering only.
- Whether the 90%→50% contributor-share change was voted on or decided by the Foundation: no governance record found (see question 6).

## Key question 2: Realistic monthly publisher revenue for a cross-venue crypto price feed (USD and 2Z) under the stated split

### Takeaway
With published Hyperliquid list prices of $900–$5,000 per feed per month and the call's own figures (Hyperliquid feed ~$10,000/month total, Solana ~$3,000, Phoenix <$100), a publisher share of 45% implies low single-digit thousands of USD per month per feed (roughly $4,500/month for a Hyperliquid-sized feed, about 140,000 2Z at $0.032), and the Binance feed was announced as included "with an existing Edge subscription", i.e. apparently without a separate fee.

### Cited Findings
- Hyperliquid feed list prices (24 Sep 2026): Top-of-Book + Trades $900/month Tokyo or $1,500 elsewhere; Market-by-Order (L4) $3,000 Tokyo or $5,000 elsewhere; full bundle $5,500 Tokyo / $9,000 elsewhere (~30% off); two receiving hosts per feed per metro included — [Crypto Briefing, 24 Sep 2026](https://cryptobriefing.com/doublezero-hyperliquid-order-book-feeds/); [crypto.news](https://crypto.news/doublezero-launches-hyperliquid-order-book-feeds-edge/)
- Solana shred subscriptions: "traders subscribe in USDC per device per epoch, with prices from $30 to $100 depending on city" (launch coverage, Apr 2026) — [crypto.jobs](https://crypto.jobs/news/doublezero-launches-edge-platform-for-low-latency-blockchain-data-delivery)
- Binance data (live 7 Oct 2026): spot and futures, top-of-book and trades for all symbols, ingested in Tokyo; "available with an existing Edge subscription", no separate fee mentioned; "2000+ user connections to date" for Edge overall — [PR Newswire, 7 Oct 2026](http://www.prnewswire.com/news-releases/doublezero-adds-binance-market-data-to-edge-302901849.html)
- Edge subscriber counts: "447 distinct subscribers since launch" and 434 validators publishing shreds by end of the June 2026 quarter — [Datawallet, 20 Aug 2026](https://www.datawallet.com/crypto/doublezero-explained); "more than 2,000 user connections since its April launch" (connections, not customers) — [crypto.news, 12 Oct 2026](https://crypto.news/doublezero-federa-says-fast-market-data-private/)
- Edge fee volume (DefiLlama snapshot, undated, via search): "$94,874 in fees over the past 30 days, of which $9,486 was protocol revenue" (the 10% burn) — [DefiLlama Edge page](https://defillama.com/protocol/doublezero-edge) (page itself returned 403)
- Secondary report of Q2 2026 Edge subscription revenue "$330,000 in USDC across 39 epochs, converting to roughly 4.35 million 2Z" — cited in search results attributed to a blog; could not be located in the Triton One post itself — treat as unverified
- Validator seat-fee revenue (the pre-Edge model) collapsed on Tokenomics.com's table: gross $9.5M (Nov 2025) → $5.4M (Mar 2026) → $450K (Aug 2026) → $290K (Sep 2026) → $134K (Oct 2026 partial); "net"/protocol revenue $599K (Nov 2025) → $15K (Sep 2026); "holder" revenue $234 in Sep 2026 — [Tokenomics.com revenue page](https://app.tokenomics.com/tokenomics/doublezero/revenue) (methodology not disclosed; Edge USDC revenue not shown)
- 2Z price 16 Oct 2026: $0.0322–0.0323 — [CoinGecko](https://www.coingecko.com/en/coins/doublezero); [CoinMarketCap](https://coinmarketcap.com/currencies/doublezero/)

### Inferences
- CALL CLAIM: ~150–200 trader subscriptions; Hyperliquid feed ~$10,000/month; Solana ~$3,000/month; Phoenix <$100/month. Cross-check: $10,000/month for Hyperliquid at $1,500–$5,000 list implies only 2–7 paying Hyperliquid subscribers (or heavy discounting), consistent with a very young product. $3,000/month for Solana shreds vs the DefiLlama ~$95K/30-day snapshot (probably Q2/Q3 2026) suggests either a sharp decline, a different counting basis, or that the call figure refers to something else; flag this inconsistency and ask DoubleZero for a dashboard export.
- Revenue scenarios under the CALL CLAIM split (publisher = 0.9 × 0.5 = 45% of gross):
  - Hyperliquid-sized feed ($10,000/month gross): publisher $4,500/month ≈ 140,000 2Z at $0.032.
  - Solana-sized ($3,000): $1,350/month ≈ 42,000 2Z. Phoenix-sized (<$100): <$45/month.
  - A hypothetical cross-venue top-of-book feed priced at $1,500/month with 10 subscribers: $15,000 gross → $6,750/month publisher share; with 30 subscribers → $20,250/month. Under the published Solana split (29.25% of gross) the same 10-subscriber case yields $4,387/month.
- The Binance press release's "available with an existing Edge subscription" wording implies the Binance feed is bundled rather than separately priced; a third party operating it would then depend on an internal attribution formula that is not public. This is the single biggest economic unknown for the "operate Binance on DoubleZero's behalf" option.
- Since rewards arrive in 2Z after an open-market buy, a publisher's USD revenue is fixed at subscription time but realised in 2Z; a 10% move in 2Z between accrual and sale (routine in Oct 2026, see question 3) changes effective revenue by the same 10%.
- Incremental cost to DoubleZero of a new subscriber is near zero ("adding a subscriber... costs the publisher almost nothing" per launch messaging), so pricing power sits with DoubleZero's list prices, not with the publisher.

### Gaps
- No public price list for Binance, Kalshi or Phoenix feeds; no per-venue subscriber counts; no public Edge revenue dashboard retrievable (DefiLlama 403).
- Whether publishers can set their own prices or are price-takers: not documented.
- Whether publisher revenue is attributed per feed, per multicast group, or pro rata across a bundle: not documented.

## Key question 3: 2Z token — launch, price history, supply, market cap, venues, volatility

### Takeaway
2Z launched with mainnet-beta on 2 October 2025, peaked at $0.75–0.89 within hours, and hit an all-time low of about $0.0315 on 15 October 2026 (down ~96%); the 2 October 2026 cliff unlock of ~1.66B tokens (~48% of prior float) took circulating supply from 3.47B to ~5.18B, and a further ~31M 2Z per week vests linearly to 2029.

### Cited Findings
- Launch: mainnet-beta and 2Z token launched 2 October 2025 — [Blockworks](https://blockworks.com/news/doublezero-launches-mainnet-beta); vesting runs from 2 Oct 2025 with a one-year cliff then four-year gradual unlock — [Blockworks filing, 23 Jun 2026](https://blockworks.com/token-transparency/filing/doublezero)
- All-time high: $0.7502 on 2 Oct 2025 (CMC) — [CoinMarketCap](https://coinmarketcap.com/currencies/doublezero/); CoinGecko shows ATH $0.8937 (date not stated) — [CoinGecko](https://www.coingecko.com/en/coins/doublezero); Datawallet: "nearly $0.75 in its first week and later fell more than 90% to around $0.05" (as of 20 Aug 2026) — [Datawallet](https://www.datawallet.com/crypto/doublezero-explained)
- All-time low: $0.0315 on 15 Oct 2026 (CMC, "2d ago" at fetch on 16 Oct 2026) — [CoinMarketCap](https://coinmarketcap.com/currencies/doublezero/); CoinGecko ATL $0.03151 — [CoinGecko](https://www.coingecko.com/en/coins/doublezero)
- Price 16 Oct 2026: $0.032301 (24h range $0.03227–$0.0337, −4.2%), 24h volume $4.93M (CMC); $0.03224, 24h volume $3.82M, 7-day −10.8%, 30-day move of 28.3% magnitude (CoinGecko widget omits sign; given the Sept 26 level of ~$0.06–0.075, the 30-day move is a decline) — same sources
- Pre-unlock prices: on 26 Sep 2026 2Z rallied 29% through $0.060 and $0.075 (uptrend since 16 Sep), open interest +113%, volume >$50M — [AMBCrypto, 26 Sep 2026](https://ambcrypto.com/doublezero-jumps-29-as-oi-climbs-113-can-2z-outrun-its-124m-unlock/); "right before the unlock, the token fell 13% over the past 24 hours and traded near $0.055" — [edgeX news aggregation](https://pro.edgex.exchange/en-US/news/article/doublezero-2z-token-unlock-october-2)
- Interim lows reported earlier in 2026: $0.066 on 24 Feb 2026 / ~€0.057 in Mar 2026 (secondary, inconsistent) — [search summary incl. Bison](https://bisonapp.com/en/crypto/coins/doublezero/)
- Supply: 10B genesis; 3,471,417,500 (34.71%) circulating pre-unlock — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero); CoinGecko on 16 Oct 2026 shows circulating 5.184B, total 9.998B, market cap $167.1M, FDV $322.4M — [CoinGecko](https://www.coingecko.com/en/coins/doublezero); CMC on the same day still shows 3.47B circulating, market cap $112.1M (rank #184), FDV $323.0M — [CoinMarketCap](https://coinmarketcap.com/currencies/doublezero/) (sources disagree on whether the Oct 2 unlock is counted)
- 2 Oct 2026 cliff: ~1.66B 2Z, $113.23M, 47.7% of then-circulating supply; largest recipients Jump Crypto and Malbec Labs; ~8% to validators/contributors; thereafter linear vesting ~31.1M 2Z per week (~0.9% of pre-cliff float) through 2029 — [Tokenomist, 28 Sep 2026](https://tokenomist.ai/research/weekly-unlock-digest-sep-28-oct-4-2026-2z-faces-a-114m-cliff-worth-47-of-float-2); AMBCrypto puts the cliff at ~1.77B / $124M / 17.79% of max supply — [AMBCrypto](https://ambcrypto.com/doublezero-jumps-29-as-oi-climbs-113-can-2z-outrun-its-124m-unlock/); next listed unlock 2 Nov 2026 of 135.41M 2Z — [DropsTab](https://dropstab.com/coins/double-zero-crypto-coin)
- Allocation: Foundation & Ecosystem 29% (unlocked), Jump Crypto 28%, Malbec Labs 14%, Institutions 12%, Team 10%, Contributors 4%, Builders 2%, Validators 1% — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Prior sales: Builder Dec 2024 2% for $3.9M at $200M FDV; Institutional Mar 2025 12% for $28.5M at $400M FDV (token warrants); Contributor Mar 2025 4% for $10M at $400–600M FDV; Validator Apr 2025 1% for $6M at $750M FDV — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Venues (CoinGecko, 16 Oct 2026; 54 listed): GroveX 2Z/USDT $1.09M, Binance 2Z/USDT $0.52M and 2Z/TRY $0.26M, OKX $0.20M, Coinbase 2Z/USD $0.15M, Bybit $0.11M, Bitunix, MEXC, Gate, Kraken $26K — [CoinGecko](https://www.coingecko.com/en/coins/doublezero); market makers Wintermute, Auros, Da Vinci Trading — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)

### Inferences
- A 2Z-denominated reward stream has lost roughly half its USD value since late September 2026 (~$0.06–0.075 → $0.032). A publisher budgeting in USD must either sell rewards immediately or accept crypto-beta exposure on a thin token (~$4–5M daily volume on 16 Oct 2026, i.e. 3–4% of market cap).
- Ongoing weekly vesting of ~31M 2Z (~$1M/week at $0.032) is structural sell pressure relative to Edge buybacks that DefiLlama put at roughly $95K/30 days of gross fees; Edge revenue at current scale cannot offset unlock flow.
- Circulating-supply and market-cap figures differ by source on 16 Oct 2026 ($112M vs $167M) purely because of how the Oct 2 unlock is counted; use 5.18B / ~$167M as the post-unlock figure.

### Gaps
- No daily volatility series retrieved (CoinGecko/CMC pages do not expose it to the fetcher); volatility statements above are from point prices.
- Exact ATH date/value disagree ($0.7502 CMC vs $0.8937 CoinGecko vs €0.94 Bison).

## Key question 4: What has DoubleZero said publicly about neutrality, first-party vs third-party feeds, and Binance's involvement?

### Takeaway
DoubleZero frames Edge as "open access" read-only distribution where anyone can subscribe to the same stream; the Binance feed is described as "sourced from the exchange's first party published data", ingested by DoubleZero in Tokyo, with no Binance quote or licensing statement in the announcement and no published neutrality or third-party-publisher policy.

### Cited Findings
- Federa (12 Oct 2026): "Our answer to the imbalance concern is open access: the fastest data path shouldn't be private rails"; anyone from anywhere can subscribe to the same Binance stream; Edge is read-only, does not execute or route orders — [crypto.news, 12 Oct 2026](https://crypto.news/doublezero-federa-says-fast-market-data-private/)
- The Binance feed is "sourced from the exchange's first party published data"; Edge "draws on multiple sources to make sure Binance-authored data reaches subscribers accurately"; ingestion point Tokyo — same source
- Binance is Edge's first centralised exchange and fifth venue after Solana, Kalshi, Hyperliquid and Phoenix; press release contains no Binance quote, no pricing, no licensing statement; Federa: "Binance is one of the few venues where price discovery happens" — [PR Newswire, 7 Oct 2026](http://www.prnewswire.com/news-releases/doublezero-adds-binance-market-data-to-edge-302901849.html)
- For Hyperliquid, DoubleZero "runs its own publisher across multiple hosts" and sources order-book data "directly from HyperCore through partnerships" with validator operators Hyperion DeFi, MAVAN and Kinetiq, "without an intermediary reseller" — [docs.doublezero.xyz/hyperliquid](https://docs.doublezero.xyz/hyperliquid/); [Crypto Briefing](https://cryptobriefing.com/doublezero-hyperliquid-order-book-feeds/); [RTTNews](https://www.rttnews.com/3693733/hyperion-defi-supports-doublezero-edge-launch-of-hyperliquid-data-feeds.aspx?type=bio)
- Kalshi feed built by "DoubleZero and Malbec Labs engineers"; audiences named: market makers, professional traders, bot and arbitrage builders; Federa: "Traditional finance got this concept exactly right: data access is a critical part of market structure" — [Markets Media, 13 Aug 2026](https://www.marketsmedia.com/institutional-grade-kalshi-market-data-now-on-doublezero-edge/)
- DoubleZero removed unauthorised retransmitters from Edge on 9 Sep 2026, accusing "disreputable RPCs" of "stealing from validators" — [Solana Compass](https://solanacompass.com/news/doublezero-removes-unauthorized-shred-retransmitters-claims-70-plus-lead-over-all-competitors)

### Inferences
- "First-party published data" most plausibly means Binance's public WebSocket/API streams rather than a bilateral Binance licence; nothing public indicates Binance endorsed, licensed or operates the feed. CALL CLAIM that DoubleZero would prefer a third party to operate the Binance feed is consistent with DoubleZero wanting to keep itself a neutral transport layer and shift sourcing/licensing exposure to the publisher.
- DoubleZero's "neutrality" is about subscriber access (anyone can buy), not about publisher exclusivity: for Hyperliquid and Kalshi DoubleZero itself (with Malbec Labs) is the publisher. A third-party publisher therefore competes with, and depends on, the platform owner as both operator and rule-setter.
- The 9 Sep 2026 enforcement shows DoubleZero acts unilaterally on licence violations; a publisher re-sourcing exchange data would be exposed to the same discretion if an exchange complained.

### Gaps
- No published neutrality policy, publisher terms of service, or statement on whether DoubleZero will itself compete with third-party publishers on the same venue.
- No statement from Binance about the Edge feed; no X thread from Federa or McConnell with the "first-party vs third-party" framing was retrievable.

## Key question 5: What licensing do Binance and other large exchanges require to redistribute real-time market data commercially, and are there cut-off precedents?

### Takeaway
Binance's historical-archive terms are CC BY-NC-SA with commercial licensing "strictly excluded", and the (archived, 2023) Terms of Use list "data feeding or streaming services that make use of any market data of Binance" among commercial uses barred without written consent; Coinbase, OKX and Bybit all bar redistribution without written consent even via public endpoints, so a cross-venue feed sold via Edge needs enterprise licences or a defensible position per exchange.

### Cited Findings
- Binance Vision Dataset Terms v1.0 (26 Aug 2026): datasets under CC BY-NC-SA 4.0; commercial licensing "strictly excluded under this agreement"; redistributed derivatives must stay CC BY-NC-SA; sub-licensing or commercially exploiting the data barred; commercial use requires a separate written enterprise data licence (no published price) — [cryptomarkets.tools guide, updated 19 Sep 2026](https://cryptomarkets.tools/guides/exchange-api-terms); PDF at [data.binance.vision](https://data.binance.vision/Binance_Vision-Terms_of_Use.pdf) (text not extractable by the fetcher)
- Binance Terms of Use section III.1.b (archived 4 Feb 2023): commercial uses without written consent prohibited, including "Data feeding or streaming services that make use of any market data of Binance" — [cryptomarkets.tools](https://cryptomarkets.tools/guides/exchange-api-terms) citing [archived terms](https://web.archive.org/web/20230204015424id_/https://www.binance.com/en/terms); the live terms page could not be fetched (JS shell only) — [binance.com/en/terms](https://www.binance.com/en/terms)
- Binance terms also bar using services "for resale or commercial purposes... unless expressly agreed by us in writing" and prohibit trading services using Binance quotes without consent — [search summary incl. GitHub issue](https://github.com/binance/binance-public-data/issues/482)
- Binance API Key Terms say nothing about republishing prices — [cryptomarkets.tools](https://cryptomarkets.tools/guides/exchange-api-terms)
- Binance is hiring a "Market Data Lead" to own licensing agreements (display/non-display, per-user, enterprise) and "data licensing and redistribution terms" for its TradFi programme — a buyer-side role; no public Binance enterprise data-sales page found — [Built In job posting](https://www.builtincolorado.com/job/market-data-lead/10922926)
- Coinbase Market Data Terms (7 Aug 2026): licence "non-sublicensable, revocable, limited", for personal/research use; data "may not be used to build an application intended for use by end users"; sharing outside your organisation needs written consent; restriction extends to derived data incl. charts/analytics; no indexes/fixings/benchmarks; eleven authorised redistribution partners listed — [cryptomarkets.tools](https://cryptomarkets.tools/guides/exchange-api-terms); [Coinbase terms](https://www.coinbase.com/legal/market_data)
- OKX API Agreement (28 Jul 2026) ss. 9.1/9.4: market data "solely for your own personal, non-commercial trading and account management purposes"; reselling/displaying to third parties barred "whether for commercial gain or otherwise"; applies "equally to Market Data accessed through public endpoints"; commercial scale needs "a separate data licensing agreement" — [OKX API agreement](https://www.okx.com/help/okx-api-agreement)
- Bybit API Terms (16 Jan 2026) ss. 6.7/6.9: no "repackaging or reselling" of Service Data; no commercial exploitation; unauthenticated access not addressed — [Bybit API terms PDF](https://www.bybit.com/common-static/compliance/legal/BYBIT/df1923006718fbba8ba70d7d762b9866.pdf)
- CoinAPI states it "does not sell a license for raw data redistribution" because "exchanges impose strict redistribution restrictions" — [CoinAPI](https://www.coinapi.io/blog/coinapi-data-commercial-use-policy)
- TradFi comparator: ICE licenses redistribution separately as an "Authorized Vendor" use case — [ICE Futures Europe Market Data Policy](https://www.theice.com/publicdocs/futures/ICE_Futures_Europe_Market_Data_Policy.pdf)
- Only cut-off precedent found: DoubleZero itself removing unauthorised retransmitters from Edge (9 Sep 2026) — [Solana Compass](https://solanacompass.com/news/doublezero-removes-unauthorized-shred-retransmitters-claims-70-plus-lead-over-all-competitors)

### Inferences
- BlockSize already redistributes exchange data to Pyth and Chainlink; the Edge use case differs in that it sells a raw, real-time, venue-attributed stream (top-of-book and trades) to traders rather than an aggregated index to oracles. Exchanges' terms treat derived/aggregated data and raw streams differently in practice, and raw-stream resale is the case most explicitly prohibited (Binance "data feeding or streaming services", OKX "displaying to third parties").
- Operating the Binance feed "on DoubleZero's behalf" transfers the licensing exposure to the operator. Since DoubleZero's own announcement gives no sign of a Binance licence, the operator should assume none exists and either obtain a written enterprise licence from Binance or obtain an indemnity from DoubleZero.
- Enforcement in crypto has historically been by IP/API-key blocking rather than litigation; no litigated or publicised vendor cut-off by Binance, Coinbase or OKX was found, which cuts both ways (low observed enforcement, but no safe-harbour precedent either).

### Gaps
- Current live text of Binance Terms of Use (2026 version) not retrievable; the "data feeding or streaming" clause is confirmed only in the 2023 archive.
- Kraken API/data terms not covered by any retrieved source.
- No documented case of an exchange cutting off a crypto data vendor for redistribution (Binance, CoinMarketCap, Kaiko searches returned nothing).
- Whether Binance has an institutional data-licensing programme at all (no public page found).

## Key question 6: DoubleZero Foundation and Malbec Labs — governance, funding, team, investors, runway; counterparty and platform risk

### Takeaway
There is no DAO or on-chain governance: fee-model changes (waiving the 5% validator fee at epoch 939, cutting the contributor share from 90% to 50%) were decided by the Cayman-domiciled Foundation and Malbec Labs, which together with Jump Crypto hold 71% of supply; treasury size and runway are undisclosed.

### Cited Findings
- Entities: DoubleZero Foundation is a memberless Cayman Islands foundation company holding protocol IP; Malbec Labs is the primary development services provider, also Cayman; other contributors Jump Crypto, Anza, Galaxy Digital; "No DAO or on-chain governance body exists at the time of this filing"; 2Z confers no dividends, profit sharing, ownership or voting rights; staking/delegation "expected in due course"; "no protocol revenue or fees go to the Foundation, its directors, or Malbec Labs"; treasury size and runway not disclosed — [Blockworks filing, 23 Jun 2026](https://blockworks.com/token-transparency/filing/doublezero)
- Team: Malbec Labs — Mateo Ward (CEO), Andrew McConnell (CTO), Max Randolph (COO); Foundation — Austin Federa (President and Director, also CEO of DZ USA), Karen Kersey (Supervisor), Oliver Bell and Teemu Paivinen (Directors) — same source; Federa was formerly head of strategy at Solana Foundation — [The Block, Mar 2025](https://www.theblock.co/post/344846/doublezero-foundation-raises-28-million-to-expand-a-global-fiber-network-for-blockchains)
- Funding: $28M token round (Mar 2025) co-led by Multicoin Capital and Dragonfly, with Foundation Capital, Reciprocal Ventures, DBA, Borderless Capital, Superscrypt, Frictionless; token warrants; valuation reported at $400M by CoinDesk sources, not confirmed by DoubleZero; a follow-on at $600M was being sought — [The Block](https://www.theblock.co/post/344846/doublezero-foundation-raises-28-million-to-expand-a-global-fiber-network-for-blockchains); [Fortune, 15 Mar 2025](https://fortune.com/crypto/2025/03/15/doublezero-malbec-labs-jump-crypto-solana-hft-trading-private-internet/); [MoneyCheck](https://moneycheck.com/doublezero-valued-at-400-million-in-latest-funding-round/)
- Total token-sale proceeds per the filing: $3.9M + $28.5M + $10M + $6M = $48.4M across Dec 2024–Apr 2025 — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Fee-model change for validators: 5% block-reward fee (base, voting, priority fees) planned at launch — [DoubleZero journal](https://doublezero.xyz/journal/the-short-and-long-of-validator-economics) (403; via search); waived for all connected validators from Epoch 939 in connection with the Edge launch — [xgram.io](https://xgram.io/blog/doublezero-on-solana-what-the-rollout-means-for-validators-and-users); the fee had generated ~$3.5M annualised from ~455 validators — [Datawallet](https://www.datawallet.com/crypto/doublezero-explained) and [Kairos Research (402)](https://x.com/Kairos_Res/article/2041637367463710957)
- Governance intent only: Foundation "has stated an intent to eventually enable on-chain governance for staked 2Z holders" covering validator admittance, fee-rate changes and development-fund decisions — [altFINS](https://altfins.com/crypto-screener/2z-doublezero)
- Risk disclosures in filing: dependence on demand for network services ("insufficient demand could reduce contributor rewards"), large unlocks ahead, possible future inflation, Cayman regulatory change — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Edge is DoubleZero's replacement revenue line after the validator fee was retired — [Datawallet](https://www.datawallet.com/crypto/doublezero-explained)

### Inferences
- Counterparty risk is concentrated: two Cayman entities with no token-holder vote can and did change economics for validators (fee to zero) and for fibre contributors (90% → 50%) within the first year. A publisher's revenue share is equally changeable.
- Runway: $48.4M raised (2024–25) plus the Foundation's 29% unlocked allocation (2.9B 2Z ≈ $93M at $0.032 on 16 Oct 2026, but illiquid relative to $4–5M daily volume). Operating cost of a 30-metro fibre network is paid to contributors in 2Z, so the Foundation's solvency is coupled to the 2Z price.
- Platform-substitution risk: an exchange could launch its own low-latency distribution or license a competing network; the Hyperliquid and Kalshi models (venue-aligned partners, Kalshi waiving its share) show venues can extract most of the economics when they engage directly, leaving an independent third-party publisher with the residual.

### Gaps
- No Foundation treasury report, burn rate, or runway statement found.
- No governance forum or proposal record found for the validator-fee waiver or the contributor-share change.
- Fortune article (Jump Crypto's role as co-engineering entity) not fetched in full.

## Key question 7: Demand side — who subscribes to Edge feeds?

### Takeaway
Public statements name market makers, professional/HFT traders and arbitrage-bot builders as the targets, and DoubleZero reports 447 distinct subscribers (to Q2 2026) and 2,000+ connections (Oct 2026), but no customer names, per-venue counts or HIP-3/Kalshi-trader breakdown are public.

### Cited Findings
- Target audiences for Kalshi feed: market makers, professional traders, bot and arbitrage builders — [Markets Media, 13 Aug 2026](https://www.marketsmedia.com/institutional-grade-kalshi-market-data-now-on-doublezero-edge/)
- Edge launch: "sophisticated traders, market makers, and protocols subscribe to receive the fastest available view of the chain's state" — [crypto.jobs launch coverage](https://crypto.jobs/news/doublezero-launches-edge-platform-for-low-latency-blockchain-data-delivery)
- 447 distinct subscribers since launch (to end June 2026 quarter) — [Datawallet](https://www.datawallet.com/crypto/doublezero-explained); 2,000+ user connections (7 Oct 2026) — [PR Newswire](http://www.prnewswire.com/news-releases/doublezero-adds-binance-market-data-to-edge-302901849.html)
- Hyperliquid feeds include HIP-3 RWA perpetuals (gold, silver, oil) and dedicated feeds for Entropy IO's Anthropic/OpenAI pre-IPO perps; expansion to HIP-4 Outcome Markets could enable cross-venue arbitrage with Kalshi — [RTTNews, 24 Sep 2026](https://www.rttnews.com/3693733/hyperion-defi-supports-doublezero-edge-launch-of-hyperliquid-data-feeds.aspx?type=bio)
- HIP-3 deployers must stake 500k HYPE, define and update the oracle, and face slashing up to their full stake; Pyth offers "HIP-3 as a Service" pushing aggregated prices every 3 seconds — [Hyperliquid docs](https://hyperliquid.gitbook.io/Hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals); [Pyth docs](https://docs.pyth.network/price-feeds/hip-3-service)
- Solana subscriptions sold "per device per epoch" at $30–100 — [crypto.jobs](https://crypto.jobs/news/doublezero-launches-edge-platform-for-low-latency-blockchain-data-delivery); subscriptions bind a cryptographic identity to an IP with a tunnel connector — [crypto.news, 12 Oct 2026](https://crypto.news/doublezero-federa-says-fast-market-data-private/)

### Inferences
- The 447 "distinct subscribers" to Q2 2026 are mostly Solana-shred buyers at $30–100 per device-epoch; the CALL CLAIM of 150–200 trader subscriptions may describe concurrently active paying entities, which is plausible against 447 cumulative.
- HIP-3 deployers are a plausible buyer of a cross-venue CEX price feed (they need oracle inputs), but they consume via pushers like Pyth's service, not via multicast fibre; Edge's latency advantage matters to market makers quoting on Hyperliquid/Kalshi, not to 3-second oracle updates. This limits the overlap with BlockSize's oracle customers.

### Gaps
- No named Edge customers, no HFT/market-maker counts, no revenue by customer type.

## Key question 8: Regulatory considerations for an EU (German) company earning crypto-token rewards for data publishing

### Takeaway
Under the BMF letter of 6 March 2025, tokens received by a business are business income valued at market price at receipt with full AO/HGB record-keeping; receiving 2Z as consideration for a data service is not itself a MiCA crypto-asset service (inference; no authority found), and 2Z has a US SEC no-action letter (29 Sep 2025) and a MiCA white paper published by DoubleZero, but no EU-authority classification was retrievable.

### Cited Findings
- BMF letter of 6 Mar 2025 (IV C 1 – S 2256/00042/064/043) replaced the 10 May 2022 letter; distinguishes payment, utility and security tokens by actual function, hybrids possible; for business cases "the general principles of business assets apply" — [Der Betrieb](https://der-betrieb.de/meldungen/bmf-schreiben-praezisiert-steuerpflichten-fuer-kryptowerte); [CMS, 26 Jun 2025](https://cms.law/de/deu/legal-updates/update-des-bmf-schreibens-zu-kryptowerten)
- Business treatment: block rewards and transaction fees from commercial mining/forging are Betriebseinnahmen; staking, lending and airdrop returns on business assets are business income; assets are capitalised under Betriebsvermögensvergleich; general AO/HGB bookkeeping duties apply — [Deubner summary of BMF 6.3.2025](https://www.deubner-steuern.de/themen/bitcoin-steuer/bmf-632025.html)
- Valuation: market price of a trading platform or web price list acceptable; consistency of valuation method required; burden of proof on taxpayer; public key alone insufficient as evidence; extended cooperation duties (§ 90 Abs. 2 AO) for decentralised platforms; missing records can lead to estimation under § 162 AO; "Zugangsfiktion": unclaimed staking rewards deemed received at latest at year-end — [CMS](https://cms.law/de/deu/legal-updates/update-des-bmf-schreibens-zu-kryptowerten)
- Corporate rates: 15% KSt + 5.5% SolZ on KSt + ~14% trade tax (municipality-dependent); the one-year private holding exemption does not apply to business assets — [search summary of German crypto tax guides](https://tangem.com/en/blog/post/crypto-taxes-in-germany/)
- MiCA: crypto-asset service providers (custody, trading platforms, exchange, advice, etc.) need BaFin or passported authorisation; MiCA applies from 30 Dec 2024 with grandfathering under Art. 143(3); MiCA overlaps but is not identical to KWG — [KPMG Law](https://kpmg-law.de/micar-neue-eu-verordnung-fuer-krypto-dienstleister-und-emittenten); BaFin has licensed e.g. Crypto Finance, Bitpanda, Baader Bank — [boerse-online](https://www.boerse-online.de/dpa-afx/eqs-news-baader-bank-erhaelt-micar-erlaubnis-der-bafin-deutsch-454459.html)
- 2Z US status: SEC Division of Corporation Finance no-action letter 29 Sep 2025 — staff will not recommend enforcement for "Programmatic Transfers" of 2Z without Securities Act s.5 registration and 2Z need not register under Exchange Act s.12(g); staff guidance, not a Commission ruling — [Free Writings & Perspectives (Mayer Brown)](https://www.freewritings.law/2025/10/division-of-corporation-finance-issues-no-action-letter-to-doublezero-foundation/); [Axios, 29 Sep 2025](https://www.axios.com/2025/09/29/sec-doublezero-crypto-no-action); Coinbase added 2Z to its roadmap following the letter — [CoinGape](https://coingape.com/coinbase-announces-doublezero-2z-listing-following-us-sec-no-action/)
- DoubleZero publishes a MiCA white paper at doublezero.xyz/whitepaper-mica.pdf (returned 403; content unverified) — [DoubleZero](https://doublezero.xyz/whitepaper-mica.pdf)
- Filing: "Tokenholders are responsible for their own tax obligations"; access restricted for sanctioned jurisdictions — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)

### Inferences
- For a GmbH, 2Z received as publisher rewards is ordinary business income (Betriebseinnahme) at the EUR market value on receipt (likely at each epoch/payout), subsequently a current asset with gains/losses on disposal also taxable; no holding-period exemption. Any 2Z bond posted would be a financial asset on the balance sheet (not income), with impairment risk if 2Z falls (it fell ~50% in the three weeks to 16 Oct 2026), and slashing would be a deductible loss subject to documentation.
- Converting 2Z rewards to EUR via an exchange is proprietary dealing, not a service to third parties, so no MiCA CASP licence is triggered by receiving and selling rewards (inference from the MiCA service list; no BaFin guidance specific to token-denominated service fees found). Selling a market-data feed is not a MiCA service either. A CASP licence would only become relevant if BlockSize held 2Z for others or operated a trading venue.
- The existence of a MiCA white paper implies DoubleZero treats 2Z as an "other crypto-asset" (Title II) rather than an ART/EMT, which supports the utility-token reading for German tax classification, but the document could not be read.
- US no-action relief covers DoubleZero's programmatic distributions, not a German recipient's position; it reduces but does not eliminate re-characterisation risk if 2Z gains governance/revenue rights later ("staking and delegation... in due course").

### Gaps
- No German or EU authority statement on 2Z; MiCA white paper content (offeror entity, notified NCA, token classification, rights) not retrievable.
- No law-firm note specifically on tokens received as consideration for B2B services by a German company; the BMF letter's exact Randnummern for valuation at receipt in Betriebsvermögen were not retrievable (only summaries).
- DAC8/CARF reporting implications for 2Z payouts from a Cayman foundation not researched.

## Key question 9: Operational risks — multicast key management, uptime, bond slashing

### Takeaway
Public material describes on-chain programmable multicast groups and permissioning and a subscriber identity/IP-binding model, but no publisher-side key-management guide, uptime commitment or slashing rule was retrievable; the one documented operational sanction is removal from the network.

### Cited Findings
- Multicast transport configuration, group definitions and permissioning are programmable on-chain via the DoubleZero ledger — [RockawayX](https://rockawayx.com/insights/doublezero-launches-native-multicast-for-decentralized-systems)
- Subscriber side: subscription links a cryptographic identity to the machine IP; a software connector sets up a private tunnel; two receiving IPs per feed per metro — [crypto.news, 12 Oct 2026](https://crypto.news/doublezero-federa-says-fast-market-data-private/); [Crypto Briefing](https://cryptobriefing.com/doublezero-hyperliquid-order-book-feeds/)
- Hyperliquid feeds are UDP multicast groups (hyper-hl-tob, hyper-hl-mbo, hyper-xyz-tob, hyper-xyz-mbo); DoubleZero runs "multiple publishers" for redundancy; peering availability target 99.9% monthly, Edge feeds excluded from that target — [docs.doublezero.xyz/hyperliquid](https://docs.doublezero.xyz/hyperliquid/)
- Binance feed ingested at a single stated point (Tokyo) with Edge drawing "on multiple sources to make sure Binance-authored data reaches subscribers accurately" — [crypto.news](https://crypto.news/doublezero-federa-says-fast-market-data-private/)
- Integrity burns exist "to discourage inorganic traffic that could inflate rewards" — [Blockworks filing](https://blockworks.com/token-transparency/filing/doublezero)
- Unauthorised retransmitters removed 9 Sep 2026 with disputed accuracy (Node1 denial) — [Solana Compass](https://solanacompass.com/news/doublezero-removes-unauthorized-shred-retransmitters-claims-70-plus-lead-over-all-competitors)

### Inferences
- A third-party Binance publisher would need Tokyo-colocated redundant hosts (Binance matching engine region), redundant upstream connections to Binance's public streams within Binance API rate limits, and multiple publisher instances per DoubleZero's own practice; cost of this is the publisher's, while pricing is DoubleZero's.
- Because reward attribution is usage/performance-based and integrity burns exist, a publisher's income can be reduced by DoubleZero's measurement logic without a formal slashing event.
- Removal risk is real and discretionary (Sept 2026), and there is no public appeal process.

### Gaps
- No publisher key-management, allowlisting or onboarding documentation retrievable (docs.doublezero.xyz/edge 404; doublezero.xyz/dz-edge 403).
- No published SLA, uptime, data-quality or bond-slashing terms for Edge publishers.
- The call's statements on bonds and slashing remain the only source and are unverified.
