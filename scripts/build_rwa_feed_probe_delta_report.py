#!/usr/bin/env python3
"""Build the RWA feed probe delta and updated credential inventory."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_PREVIOUS = Path("reports/feed_quality_all_current_probe_livewired_retry.json")
DEFAULT_CURRENT = Path("reports/feed_quality_all_current_probe_next.json")
DEFAULT_GAP_INVENTORY = Path("reports/rwa_full_coverage_symbol_gap_inventory_2026-07-14.csv")
DEFAULT_CREDENTIALS = Path("reports/rwa_credentials_access_needed_2026-07-14.csv")
DEFAULT_MD = Path("reports/rwa_feed_quality_delta_next_2026-07-14.md")
DEFAULT_JSON = Path("reports/rwa_feed_quality_delta_next_2026-07-14.json")
DEFAULT_CREDENTIALS_OUT = Path("reports/rwa_credentials_access_needed_next_2026-07-14.csv")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _status_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(row.get("probe_status") or "") for row in rows)


def _ids_with_status(rows: list[dict[str, Any]], status: str) -> set[str]:
    return {str(row["feed_id"]) for row in rows if row.get("probe_status") == status}


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def _credential_status(package: str, current_rows: list[dict[str, Any]]) -> tuple[str, str, str]:
    rows = [row for row in current_rows if row.get("venue") == package]
    categories = Counter(str(row.get("blocker_category") or "live_candidate") for row in rows)
    ok = sum(1 for row in rows if row.get("probe_status") == "ok")
    errors = sum(1 for row in rows if row.get("probe_status") == "error")
    not_live = sum(1 for row in rows if row.get("probe_status") == "not_live_wired")
    evidence = f"next_probe ok={ok}; error={errors}; not_live_wired={not_live}; blockers={dict(categories)}"

    if package in {"jupiter_router", "raydium_clmm", "orca_whirlpool", "meteora_dlmm"}:
        return (
            "keyed_public_quote_access_present_but_production_quota_and_direct_pool_replay_needed",
            evidence,
            "Upgrade Jupiter quota/backoff for quote sweeps and add direct Raydium/Orca/Meteora pool-state replay before production.",
        )
    if package == "orderly":
        return (
            "public_mark_index_reference_confirmed_l2_depth_not_confirmed",
            evidence,
            "Confirm documented Orderly L2 endpoint or account/API entitlement; configure ORDERLY_ORDERBOOK_PATH_TEMPLATE and pacing.",
        )
    if package == "drift":
        return (
            "solana_rpc_present_adapter_blocked_on_drift_dlob_and_replay",
            evidence,
            "Wire Drift SDK/DLOB or market-account replay with websocket slot lag and raw account payload capture.",
        )
    if package in {"ostium", "gains"}:
        return (
            "not_live_wired_partner_api_or_contract_replay_needed",
            evidence,
            "Secure official API/partner access or implement contract/oracle replay with replayable raw payloads.",
        )
    if package in {"uniswap_v3_v4", "balancer_pools", "curve_stableswap", "aerodrome_slipstream"}:
        return (
            "evm_rpc_indexer_missing_direct_pool_state_needed",
            evidence,
            "Provision EVM RPC/indexers and implement direct pool-state/tick/balance quote simulation.",
        )
    if package in {"blocksize_state", "treasury_nav"}:
        return (
            "reference_coverage_only_state_or_issuer_confirmation_needed",
            evidence,
            "Confirm state/NAV instrument identity, entitlement, freshness, and issuer alignment before using as benchmarks.",
        )
    return (
        "unchanged_access_review_needed",
        evidence,
        "Keep credential package open until rights, freshness, replay, benchmark, and production scope are confirmed.",
    )


def build(args: argparse.Namespace) -> dict[str, Any]:
    previous_payload = _read_json(args.previous)
    current_payload = _read_json(args.current)
    previous_rows = previous_payload["results"]
    current_rows = current_payload["results"]
    previous_by_id = {str(row["feed_id"]): row for row in previous_rows}
    current_by_id = {str(row["feed_id"]): row for row in current_rows}
    previous_ok = _ids_with_status(previous_rows, "ok")
    current_ok = _ids_with_status(current_rows, "ok")
    newly_live = sorted(current_ok - previous_ok)
    lost_live = sorted(previous_ok - current_ok)
    added_ids = sorted(set(current_by_id) - set(previous_by_id))
    removed_ids = sorted(set(previous_by_id) - set(current_by_id))

    gap_rows = _read_csv(args.gap_inventory)
    gap_counts = Counter((row.get("gap_category", ""), row.get("credential_status", "")) for row in gap_rows)
    blocker_counts = Counter(str(row.get("blocker_category") or "live_candidate") for row in current_rows)
    venue_status_counts = Counter(
        (str(row.get("venue") or ""), str(row.get("probe_status") or ""))
        for row in current_rows
    )

    credentials = _read_csv(args.credentials)
    updated_credentials: list[dict[str, Any]] = []
    for row in credentials:
        package = row.get("credential_package", "")
        status, evidence, action = _credential_status(package, current_rows)
        updated_credentials.append(
            {
                **row,
                "confirmed_access_status": status,
                "latest_probe_evidence": evidence,
                "remaining_access_action": action,
                "production_use_allowed": "False",
            }
        )
    credential_fields = list(credentials[0].keys()) + [
        "confirmed_access_status",
        "latest_probe_evidence",
        "remaining_access_action",
        "production_use_allowed",
    ]
    _write_csv(args.credentials_out, updated_credentials, credential_fields)

    previous_counts = _status_counts(previous_rows)
    current_counts = _status_counts(current_rows)
    status_rows = []
    for status in sorted(set(previous_counts) | set(current_counts)):
        status_rows.append([status, previous_counts.get(status, 0), current_counts.get(status, 0), current_counts.get(status, 0) - previous_counts.get(status, 0)])

    venue_rows = []
    for venue in sorted({row.get("venue") for row in current_rows}):
        venue_rows.append(
            [
                venue,
                venue_status_counts.get((venue, "ok"), 0),
                venue_status_counts.get((venue, "error"), 0),
                venue_status_counts.get((venue, "not_live_wired"), 0),
            ]
        )

    blocker_rows = [[category, count] for category, count in blocker_counts.most_common()]
    gap_rows_table = [[gap, credential, count] for (gap, credential), count in gap_counts.most_common()]

    def _feed_line(feed_id: str) -> str:
        row = current_by_id.get(feed_id) or previous_by_id[feed_id]
        return f"{feed_id} ({row.get('venue')}, {row.get('symbol')}, {row.get('kind')})"

    md = "\n".join(
        [
            "# RWA feed probe delta - 2026-07-14",
            "",
            "## Summary",
            "",
            f"- Previous probe: `{args.previous}`.",
            f"- Current probe: `{args.current}`.",
            f"- Current rows: {len(current_rows)}; live-attempted: {current_payload['summary'].get('live_attempted_rows')}; production-promoted: {current_payload['summary'].get('production_promoted_rows')}.",
            f"- Current ok rows: {current_payload['summary'].get('ok_rows')} across {current_payload['summary'].get('unique_ok_symbols')} symbols and {current_payload['summary'].get('unique_ok_venues')} venues.",
            f"- Newly live rows: {len(newly_live)}; rows that were ok previously but blocked/error now: {len(lost_live)}.",
            f"- Feed ids added: {len(added_ids)}; feed ids removed: {len(removed_ids)}.",
            "",
            "No feeds are production-promoted. Every ok row remains candidate-only because raw payload replay, 5m/30m/24h windows, benchmark alignment, manipulation/depth checks, rights clearance, and multi-source consensus are still required.",
            "",
            "## Probe Status Delta",
            "",
            _table(["status", "previous", "current", "delta"], status_rows),
            "",
            "## Current Blockers",
            "",
            _table(["blocker_category", "rows"], blocker_rows),
            "",
            "## Venue Status",
            "",
            _table(["venue", "ok", "error", "not_live_wired"], venue_rows),
            "",
            "## Source Gap Inventory Grouping",
            "",
            _table(["gap_category", "credential_status", "rows"], gap_rows_table),
            "",
            "## Newly Live Rows",
            "",
            "\n".join(f"- {_feed_line(feed_id)}" for feed_id in newly_live) or "- None",
            "",
            "## Rows No Longer Live In This Probe",
            "",
            "\n".join(f"- {_feed_line(feed_id)} -> {current_by_id[feed_id].get('blocker_category')}: {current_by_id[feed_id].get('error')}" for feed_id in lost_live) or "- None",
            "",
            "## Remaining Credentials Needed",
            "",
            f"Updated credential inventory: `{args.credentials_out}`.",
            "",
            "- Jupiter: key is present, but this run still hit production quota/rate limits; direct Raydium/Orca/Meteora pool-state replay remains required for production.",
            "- Orderly: public futures mark/index is reference-only; L2 depth is blocked until a documented endpoint or account/API entitlement is configured.",
            "- Drift: Solana RPC is present, but Drift DLOB/market-account replay is not wired.",
            "- Ostium/Gains: still need official API/partner access or on-chain contract/oracle replay adapters.",
            "- EVM pools: Ethereum/Base RPC/indexers and direct pool-state/tick/balance adapters are still missing.",
            "- Blocksize state and issuer NAV: still reference/candidate-only until state-instrument confirmation, freshness, replay evidence, rights, and issuer NAV alignment pass.",
            "",
        ]
    )
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(md, encoding="utf-8")

    payload = {
        "previous": str(args.previous),
        "current": str(args.current),
        "summary": {
            "previous_status_counts": dict(previous_counts),
            "current_status_counts": dict(current_counts),
            "newly_live_rows": len(newly_live),
            "lost_live_rows": len(lost_live),
            "added_feed_ids": len(added_ids),
            "removed_feed_ids": len(removed_ids),
            "current_blocker_counts": dict(blocker_counts),
        },
        "newly_live": [_feed_line(feed_id) for feed_id in newly_live],
        "lost_live": [
            {
                "feed_id": feed_id,
                "previous_venue": previous_by_id[feed_id].get("venue"),
                "symbol": previous_by_id[feed_id].get("symbol"),
                "current_status": current_by_id[feed_id].get("probe_status"),
                "blocker_category": current_by_id[feed_id].get("blocker_category"),
                "error": current_by_id[feed_id].get("error"),
            }
            for feed_id in lost_live
        ],
        "added_feed_ids": added_ids,
        "removed_feed_ids": removed_ids,
        "credential_inventory": str(args.credentials_out),
        "report": str(args.output_md),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, default=DEFAULT_PREVIOUS)
    parser.add_argument("--current", type=Path, default=DEFAULT_CURRENT)
    parser.add_argument("--gap-inventory", type=Path, default=DEFAULT_GAP_INVENTORY)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--credentials-out", type=Path, default=DEFAULT_CREDENTIALS_OUT)
    return parser.parse_args()


def main() -> None:
    payload = build(parse_args())
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
