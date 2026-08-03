-- Safeguard 6 RWA data-quality acceptance synthesis.
-- SQLite-compatible and reviewed against the executed notebook snapshot.

WITH endpoint_profile(
  endpoint,
  before_bytes,
  after_bytes,
  before_seconds,
  after_seconds,
  byte_limit,
  local_seconds_limit,
  status
) AS (
  VALUES
    ('/v1/rwa/registry',          15161226, 293545,  1.3890, 1.0138, 1000000, 2.5, 'pass'),
    ('/v1/rwa/sourcing/jobs',     12903824,  53023,  1.1590, 1.2706, 1000000, 2.5, 'pass'),
    ('/v1/rwa/registry/venues',    5210277, 143749,  0.4740, 0.8944, 1000000, 2.5, 'pass'),
    ('/v1/rwa/discovery',          3372877, 224844,  0.7240, 0.9459, 1000000, 2.5, 'pass'),
    ('/v1/rwa/derivative-venues',  2879550, 101262,  0.0360, 0.3575, 1000000, 2.5, 'pass'),
    ('/v1/rwa/identity-audit',     1619023,  70345,  0.4630, 0.9830, 1000000, 2.5, 'pass'),
    ('/v1/rwa/non-crypto-feeds',    794705, 167978,  0.4510, 0.8717, 1000000, 2.5, 'pass'),
    ('/v1/rwa/provider-catalog',    302830, 302830,  0.0090, 0.0091, 1000000, 2.5, 'pass'),
    ('/v1/rwa/assets',              288601, 480457,  0.5160, 0.9873, 1000000, 2.5, 'pass'),
    ('/v1/rwa/coverage',             94842, 132710,  0.2300, 0.4857, 1000000, 2.5, 'pass'),
    ('/v1/rwa/consensus/sources',     57169,  54741, 18.4530, 1.4763, 1000000, 2.5, 'pass'),
    ('/v1/rwa/market-expansion',      26743,  26743, 17.6900, 0.9833, 1000000, 2.5, 'pass'),
    ('/v1/rwa/equity-universes',      25902,  25902,  7.5150, 0.9500, 1000000, 2.5, 'pass')
)
SELECT
  endpoint,
  before_bytes,
  after_bytes,
  ROUND(100.0 * (before_bytes - after_bytes) / before_bytes, 2) AS byte_reduction_pct,
  before_seconds,
  after_seconds,
  byte_limit,
  local_seconds_limit,
  status
FROM endpoint_profile
ORDER BY after_bytes DESC;

WITH quality_gates(
  gate_order,
  gate,
  status,
  evidence,
  acceptance_or_next_action
) AS (
  VALUES
    (1, 'Lossless venue-instrument matrix', 'pass', '5,161 source rows and 5,161 nested instruments', 'Retain authoritative venues.<venue>.instruments[] grain'),
    (2, 'RWA.xyz contract identity', 'pass', '3,438 token rows preserved; 3,435 contracts; zero cross-asset collisions', 'Continue blocking incomplete or cross-asset identities'),
    (3, 'Cross-class canonical identity', 'pass', '55 raw mixed ids normalized to zero canonical mixed ids; two ambiguities source-scoped', 'Keep ambiguous bare tickers out of decision-grade counts'),
    (4, 'RWA.xyz verification boundary', 'pass_with_candidates', '93 verified and 1,076 unverified assets', 'Do not promote unverified mappings as canonical underlyings'),
    (5, 'Yield metric semantics', 'pass', '24 YTM and 175 trailing-30-day APY values finite with raw trends retained', 'Preserve basis and units in every consumer'),
    (6, 'Daily snapshot reconciliation', 'pass', 'Canonical SHA-256 reconciled; explicit baseline_created state', 'Capture a second distinct verified snapshot before delta claims'),
    (7, 'Default endpoint budgets', 'pass', '13 of 13 HTTP 200; every response below 1,000,000 bytes and 2.5 seconds locally', 'Require clients to follow deterministic pagination'),
    (8, 'Pilot evidence ledger', 'pass', 'Schema v3 accepts valid evidence and rejects crossed claims and auto-promotion', 'Zero-reject production history migration remains a release gate'),
    (9, 'Dynamic source freshness', 'blocked', 'Hyperliquid and derivative catalog snapshots are stale', 'Refresh and reconcile both artifacts without changing the source boundary'),
    (10, 'Production promotion', 'blocked', '14-day window, rights, benchmark, manipulation/depth, independence, and human approval remain open', 'No deployment or production promotion from this local acceptance')
)
SELECT *
FROM quality_gates
ORDER BY gate_order;
