from unittest.mock import patch

from src.rwa_sourcing import _rwa_xyz_monitor_sourcing_jobs


def _row(network: str, slug: str, address: str, token_id: str) -> dict:
    return {
        "rwa_xyz_asset_id": "5266",
        "rwa_xyz_token_id": token_id,
        "asset_id": "AAPL",
        "symbol": "AAPL.d/USD",
        "asset_class": "equity",
        "network": network,
        "network_slug": slug,
        "address": address,
        "platform": "Dinari",
    }


def test_rwa_xyz_jobs_are_network_aware_and_deduplicate_same_chain_contract():
    rows = [
        _row("Avalanche C-Chain", "avalanche-c-chain", "0xABC", "1"),
        _row("Avalanche C-Chain", "avalanche-c-chain", "0xabc", "2"),
        _row("HyperEVM", "hyperevm", "0xABC", "3"),
        _row("Dinari Financial Network", "dinari-financial-network", "0xDEF", "4"),
        _row("Solana", "solana", "Mint111", "5"),
    ]
    with patch("src.rwa_sourcing.load_rwa_xyz_token_rows", return_value=rows):
        jobs = _rwa_xyz_monitor_sourcing_jobs({})

    assert len(jobs) == 4
    assert {job["sourcing_lane"] for job in jobs} == {
        "evm_token_pool_and_router_discovery",
        "manual_network_adapter_triage",
        "solana_token_route_and_pool_discovery",
    }
    assert len({job["metadata"]["contract_identity"] for job in jobs}) == 4


def test_rwa_xyz_catalog_jobs_are_never_feed_promoted():
    with patch(
        "src.rwa_sourcing.load_rwa_xyz_token_rows",
        return_value=[_row("Avalanche C-Chain", "avalanche-c-chain", "0xABC", "1")],
    ):
        job = _rwa_xyz_monitor_sourcing_jobs({})[0]

    assert job["metadata"]["production_eligible"] is False
    assert job["metadata"]["allowed_feed_semantics"] == ["supplemental_catalog_coverage"]
    assert job["metadata"]["prohibited_feed_semantics"] == ["vwap", "bid_ask", "consensus"]
    assert {
        "token_identity_and_decimals",
        "pool_or_route_liquidity",
        "fee_tiers_and_slot_or_block_state",
        "replayable_raw_payloads",
        "issuer_nav_or_primary_market_alignment",
        "blocksize_benchmark_alignment",
        "manipulation_and_concentration_checks",
    }.issubset(job["missing_source_types"])
