# Revenue operating scorecard

The authenticated Product Usage Command Center exposes
`revenue_operating_scorecard`. It turns the existing payment, delivery, discovery,
and retention evidence into one operating model without treating test traffic,
wallet top-ups, marketplace reachability, or raw proof noise as revenue.

## Primary outcomes

1. **Decision-grade recognized revenue (USDC).** Finalized, deduplicated payments
   joined to delivery evidence. No arbitrary revenue target is set until four
   comparable weekly observations establish a baseline.
2. **Verified purchase reliability.** Correlated settlements divided by correlated
   purchase attempts. Target: at least 90%.
3. **Seven-day repeat usage.** Mature verified identities with at least two valid
   deliveries in their first seven days. Target: at least 25%.

## Drivers

- Search-to-resolution rate tests whether users and agents can find covered data.
- Resolver-to-delivery rate tests whether discovery reaches delivered value.
- Prompt-to-proof rate tests whether the x402 purchase handoff is usable.
- Starter-to-paid rate tests whether introductory value creates paid conversion.

## Guardrails

- Unreconciled settlements must remain zero.
- Charged delivery failures must remain zero.
- Known ecosystem monitors must be separated from customer demand; a 50% or higher
  monitor share fails the current operating guardrail.

## Weekly decision sequence

1. Stop and repair any failed payment-integrity guardrail.
2. If purchase reliability is below target, fix payment completion before buying
   more traffic.
3. If discovery rates are weak, improve listings, aliases, examples, and catalog
   coverage around observed unsupported-symbol demand.
4. If discovery works but prompt-to-proof is weak, improve the official x402 client
   handoff and recovery guidance.
5. If conversion works but repeat usage is weak, ship recurring agent workflows and
   higher-value packages before changing price.
6. Compare recognized revenue only across equivalent, synthetic-excluded windows.

Marketplace performance feeds remain a separate acquisition input. Public listing
health proves availability only and never counts as demand or commercial success.
