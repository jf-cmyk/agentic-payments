#!/usr/bin/env python3
"""Discover Solana RWA token mints and Jupiter route evidence."""

from __future__ import annotations

import argparse

from src.rwa_solana_discovery import (
    DEFAULT_ROUTE_ALLOWLIST_PATH,
    DEFAULT_ROUTE_CSV_PATH,
    DEFAULT_TOKEN_CSV_PATH,
    DEFAULT_TOKEN_REGISTRY_PATH,
    write_solana_discovery_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-json-out", default=DEFAULT_TOKEN_REGISTRY_PATH)
    parser.add_argument("--route-json-out", default=DEFAULT_ROUTE_ALLOWLIST_PATH)
    parser.add_argument("--token-csv-out", default=DEFAULT_TOKEN_CSV_PATH)
    parser.add_argument("--route-csv-out", default=DEFAULT_ROUTE_CSV_PATH)
    parser.add_argument("--token-limit", type=int, default=None)
    parser.add_argument("--route-limit", type=int, default=None)
    parser.add_argument("--skip-routes", action="store_true")
    args = parser.parse_args()

    result = write_solana_discovery_reports(
        token_json_path=args.token_json_out,
        route_json_path=args.route_json_out,
        token_csv_path=args.token_csv_out,
        route_csv_path=args.route_csv_out,
        token_limit=args.token_limit,
        route_limit=args.route_limit,
        include_routes=not args.skip_routes,
    )
    token_summary = result["token_registry"]["summary"]
    route_summary = (result.get("route_allowlist") or {}).get("summary", {})
    print(
        "wrote Solana discovery reports: "
        f"{token_summary['resolved']}/{token_summary['token_count']} tokens resolved, "
        f"{route_summary.get('route_discovered', 0)}/{route_summary.get('route_count', 0)} routes discovered"
    )


if __name__ == "__main__":
    main()
