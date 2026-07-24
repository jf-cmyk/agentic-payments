import pytest

from src.rwa_clmm_replay import (
    Q96,
    decode_signed_word,
    decode_swap_log,
    encode_signed_argument,
    simulate_exact_input,
    summarize_swap_logs,
)


def _word(value: int) -> str:
    return encode_signed_argument(value, 256)


def _swap_log(*, amount0: int, amount1: int, block: int = 100) -> dict:
    data = "0x" + "".join(
        (
            _word(amount0),
            _word(amount1),
            f"{int(Q96):064x}",
            f"{1_000_000:064x}",
            _word(0),
        )
    )
    return {
        "blockNumber": hex(block),
        "transactionHash": "0xabc",
        "logIndex": "0x2",
        "topics": [
            "0xtopic",
            "0x" + "0" * 24 + "1" * 40,
            "0x" + "0" * 24 + "2" * 40,
        ],
        "data": data,
    }


@pytest.mark.parametrize("value,bits", [(-1, 16), (-12345, 24), (0, 24), (32767, 16)])
def test_signed_abi_argument_round_trip(value, bits):
    encoded = encode_signed_argument(value, bits)
    assert len(encoded) == 64
    assert decode_signed_word(encoded) == value


def test_decode_and_summarize_common_swap_event():
    log = _swap_log(amount0=2 * 10**18, amount1=-630 * 10**6)

    decoded = decode_swap_log(log)
    summary = summarize_swap_logs(
        [log],
        token0="0xbase",
        token1="0xquote",
        decimals0=18,
        decimals1=6,
        base_token="0xbase",
        quote_token="0xquote",
    )

    assert decoded["amount0"] == 2 * 10**18
    assert decoded["amount1"] == -630 * 10**6
    assert decoded["tick"] == 0
    assert summary["swap_count"] == 1
    assert summary["base_volume"] == 2.0
    assert summary["quote_volume_usd"] == 630.0
    assert summary["unique_sender_recipient_proxy_count"] == 2


def _replay_state() -> dict:
    return {
        "token0": "0xbase",
        "token1": "0xquote",
        "base_token": "0xbase",
        "quote_token": "0xquote",
        "decimals0": 6,
        "decimals1": 6,
        "fee_tier": 500,
        "sqrt_price_x96": int(Q96),
        "tick": 0,
        "liquidity": 10**15,
        "price": 1.0,
        "initialized_ticks": [
            {"tick": -100, "liquidity_net": 0, "initialized": True},
            {"tick": 100, "liquidity_net": 0, "initialized": True},
        ],
        "max_ticks_crossed": 16,
    }


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_exact_input_replay_fills_small_block_inside_captured_tick_range(side):
    fill = simulate_exact_input(_replay_state(), side=side, target_notional_usd=10_000)

    assert fill["fill_ratio"] == 1.0
    assert fill["captured_range_sufficient"] is True
    assert fill["stop_reason"] == "target_filled"
    assert fill["slippage_bps"] is not None


def test_exact_input_replay_retains_partial_fill_when_tick_range_is_exhausted():
    state = _replay_state()
    state["liquidity"] = 10**6

    fill = simulate_exact_input(state, side="buy", target_notional_usd=1_000_000)

    assert fill["fill_ratio"] < 1.0
    assert fill["captured_range_sufficient"] is False
    assert fill["stop_reason"] == "captured_tick_range_exhausted"
