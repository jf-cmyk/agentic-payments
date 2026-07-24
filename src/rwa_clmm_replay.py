"""Pure concentrated-liquidity replay and swap-log decoding helpers."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any


Q96 = Decimal(2**96)
ONE_MILLION = Decimal(1_000_000)


def decode_signed_word(value: str | int, bits: int = 256) -> int:
    """Decode an ABI signed integer from a hex word or integer."""
    parsed = int(value, 16) if isinstance(value, str) else int(value)
    return parsed - 2**bits if parsed >= 2 ** (bits - 1) else parsed


def encode_signed_argument(value: int, bits: int) -> str:
    """ABI-encode a signed integer as one sign-extended 256-bit word."""
    if value < -(2 ** (bits - 1)) or value >= 2 ** (bits - 1):
        raise ValueError(f"value is outside int{bits}")
    encoded = value if value >= 0 else 2**256 + value
    return f"{encoded:064x}"


def sqrt_ratio_at_tick_x96(tick: int) -> Decimal:
    """Return sqrt(1.0001**tick) in Q64.96 form with high precision."""
    with localcontext() as context:
        context.prec = 90
        return (Decimal("1.0001").sqrt() ** int(tick)) * Q96


def decode_swap_log(log: dict[str, Any]) -> dict[str, Any]:
    """Decode the common Uniswap-v3/Slipstream Swap event data payload."""
    raw = str(log.get("data") or "")
    if not raw.startswith("0x") or len(raw) < 2 + 64 * 5:
        raise ValueError("swap log data is missing five ABI words")
    words = [raw[2 + index * 64 : 2 + (index + 1) * 64] for index in range(5)]
    topics = log.get("topics") if isinstance(log.get("topics"), list) else []
    return {
        "block_number": int(str(log.get("blockNumber") or "0x0"), 16),
        "transaction_hash": log.get("transactionHash"),
        "log_index": int(str(log.get("logIndex") or "0x0"), 16),
        "sender": f"0x{str(topics[1])[-40:]}".lower() if len(topics) > 1 else None,
        "recipient": f"0x{str(topics[2])[-40:]}".lower() if len(topics) > 2 else None,
        "amount0": decode_signed_word(words[0]),
        "amount1": decode_signed_word(words[1]),
        "sqrt_price_x96": int(words[2], 16),
        "liquidity": int(words[3], 16),
        "tick": decode_signed_word(words[4]),
    }


def summarize_swap_logs(
    logs: list[dict[str, Any]],
    *,
    token0: str,
    token1: str,
    decimals0: int,
    decimals1: int,
    base_token: str,
    quote_token: str,
) -> dict[str, Any]:
    """Summarize organic pool activity using decoded quote-token turnover."""
    decoded = [decode_swap_log(log) for log in logs]
    token0_key = token0.lower()
    token1_key = token1.lower()
    base_key = base_token.lower()
    quote_key = quote_token.lower()
    if {base_key, quote_key} != {token0_key, token1_key}:
        raise ValueError("base/quote tokens do not match the pool token pair")
    quote_is_token0 = quote_key == token0_key
    base_is_token0 = base_key == token0_key
    quote_decimals = decimals0 if quote_is_token0 else decimals1
    base_decimals = decimals0 if base_is_token0 else decimals1
    quote_volume = Decimal(0)
    base_volume = Decimal(0)
    addresses: set[str] = set()
    for row in decoded:
        quote_atoms = abs(row["amount0"] if quote_is_token0 else row["amount1"])
        base_atoms = abs(row["amount0"] if base_is_token0 else row["amount1"])
        quote_volume += Decimal(quote_atoms) / Decimal(10**quote_decimals)
        base_volume += Decimal(base_atoms) / Decimal(10**base_decimals)
        addresses.update(
            address for address in (row.get("sender"), row.get("recipient")) if address
        )
    return {
        "swap_count": len(decoded),
        "quote_volume_usd": float(quote_volume),
        "base_volume": float(base_volume),
        "unique_sender_recipient_proxy_count": len(addresses),
        "first_block": min((row["block_number"] for row in decoded), default=None),
        "last_block": max((row["block_number"] for row in decoded), default=None),
        "decoded_swaps": decoded,
    }


def _amount_to_boundary(
    *,
    sqrt_current: Decimal,
    sqrt_target: Decimal,
    liquidity: Decimal,
    zero_for_one: bool,
) -> tuple[Decimal, Decimal]:
    if zero_for_one:
        amount_in = liquidity * Q96 * (sqrt_current - sqrt_target) / (
            sqrt_current * sqrt_target
        )
        amount_out = liquidity * (sqrt_current - sqrt_target) / Q96
    else:
        amount_in = liquidity * (sqrt_target - sqrt_current) / Q96
        amount_out = liquidity * Q96 * (sqrt_target - sqrt_current) / (
            sqrt_target * sqrt_current
        )
    return max(Decimal(0), amount_in), max(Decimal(0), amount_out)


def _partial_step(
    *,
    sqrt_current: Decimal,
    liquidity: Decimal,
    net_input: Decimal,
    zero_for_one: bool,
) -> tuple[Decimal, Decimal]:
    if zero_for_one:
        sqrt_next = Decimal(1) / (
            Decimal(1) / sqrt_current + net_input / (liquidity * Q96)
        )
        output = liquidity * (sqrt_current - sqrt_next) / Q96
    else:
        sqrt_next = sqrt_current + net_input * Q96 / liquidity
        output = liquidity * Q96 * (sqrt_next - sqrt_current) / (
            sqrt_next * sqrt_current
        )
    return sqrt_next, max(Decimal(0), output)


def simulate_exact_input(
    state: dict[str, Any],
    *,
    side: str,
    target_notional_usd: float,
) -> dict[str, Any]:
    """Walk captured initialized ticks for one exact-input block-size quote."""
    clean_side = side.lower().strip()
    if clean_side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    base_token = str(state["base_token"]).lower()
    quote_token = str(state["quote_token"]).lower()
    token0 = str(state["token0"]).lower()
    token1 = str(state["token1"]).lower()
    if {base_token, quote_token} != {token0, token1}:
        raise ValueError("base/quote tokens do not match replay token pair")
    base_is_token0 = base_token == token0
    input_is_token0 = base_is_token0 if clean_side == "sell" else not base_is_token0
    zero_for_one = input_is_token0
    decimals0 = int(state["decimals0"])
    decimals1 = int(state["decimals1"])
    input_decimals = decimals0 if input_is_token0 else decimals1
    output_decimals = decimals1 if input_is_token0 else decimals0
    mid = Decimal(str(state["price"] or 0))
    if mid <= 0:
        raise ValueError("replay price must be positive")
    if clean_side == "buy":
        target_input_human = Decimal(str(target_notional_usd))
    else:
        target_input_human = Decimal(str(target_notional_usd)) / mid
    target_input_atoms = target_input_human * Decimal(10**input_decimals)
    fee_pips = Decimal(int(state.get("fee_tier") or 0))
    fee_factor = (ONE_MILLION - fee_pips) / ONE_MILLION
    if fee_factor <= 0:
        raise ValueError("fee tier must be less than one million pips")

    sqrt_current = Decimal(int(state["sqrt_price_x96"]))
    liquidity = Decimal(int(state["liquidity"]))
    current_tick = int(state["tick"])
    ticks = {
        int(row["tick"]): int(row["liquidity_net"])
        for row in (state.get("initialized_ticks") or [])
        if isinstance(row, dict) and row.get("initialized") is True
    }
    remaining_gross = target_input_atoms
    consumed_gross = Decimal(0)
    output_atoms = Decimal(0)
    crossed: list[int] = []
    stop_reason = "target_filled"

    with localcontext() as context:
        context.prec = 90
        for _ in range(max(1, int(state.get("max_ticks_crossed") or 128))):
            if remaining_gross <= 0:
                break
            candidates = (
                [tick for tick in ticks if tick <= current_tick]
                if zero_for_one
                else [tick for tick in ticks if tick > current_tick]
            )
            if not candidates:
                stop_reason = "captured_tick_range_exhausted"
                break
            next_tick = max(candidates) if zero_for_one else min(candidates)
            sqrt_target = sqrt_ratio_at_tick_x96(next_tick)
            net_to_boundary, output_to_boundary = _amount_to_boundary(
                sqrt_current=sqrt_current,
                sqrt_target=sqrt_target,
                liquidity=liquidity,
                zero_for_one=zero_for_one,
            )
            gross_to_boundary = net_to_boundary / fee_factor
            if remaining_gross < gross_to_boundary or gross_to_boundary <= 0:
                net_input = remaining_gross * fee_factor
                sqrt_current, output = _partial_step(
                    sqrt_current=sqrt_current,
                    liquidity=liquidity,
                    net_input=net_input,
                    zero_for_one=zero_for_one,
                )
                output_atoms += output
                consumed_gross += remaining_gross
                remaining_gross = Decimal(0)
                break
            remaining_gross -= gross_to_boundary
            consumed_gross += gross_to_boundary
            output_atoms += output_to_boundary
            sqrt_current = sqrt_target
            liquidity_net = Decimal(ticks.pop(next_tick))
            liquidity = liquidity - liquidity_net if zero_for_one else liquidity + liquidity_net
            crossed.append(next_tick)
            current_tick = next_tick - 1 if zero_for_one else next_tick
            if liquidity <= 0:
                stop_reason = "liquidity_exhausted"
                break
        else:
            stop_reason = "maximum_tick_crossings_reached"

    consumed_input_human = consumed_gross / Decimal(10**input_decimals)
    output_human = output_atoms / Decimal(10**output_decimals)
    fill_ratio = min(Decimal(1), consumed_gross / target_input_atoms) if target_input_atoms else Decimal(0)
    vwap = None
    filled_notional = None
    slippage_bps = None
    if clean_side == "buy" and output_human > 0:
        vwap = consumed_input_human / output_human
        filled_notional = consumed_input_human
        slippage_bps = (vwap / mid - Decimal(1)) * Decimal(10_000)
    elif clean_side == "sell" and consumed_input_human > 0:
        vwap = output_human / consumed_input_human
        filled_notional = consumed_input_human * mid
        slippage_bps = (Decimal(1) - vwap / mid) * Decimal(10_000)
    return {
        "side": clean_side,
        "target_notional_usd": target_notional_usd,
        "filled_notional_usd": float(filled_notional or 0),
        "fill_ratio": float(fill_ratio),
        "vwap": float(vwap) if vwap is not None else None,
        "slippage_bps": float(slippage_bps) if slippage_bps is not None else None,
        "ticks_crossed": crossed,
        "stop_reason": stop_reason,
        "captured_range_sufficient": remaining_gross <= 0,
        "ending_sqrt_price_x96": int(sqrt_current),
        "ending_liquidity": int(liquidity),
    }
