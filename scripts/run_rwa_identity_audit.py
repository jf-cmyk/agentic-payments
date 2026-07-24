#!/usr/bin/env python3
"""Generate the RWA ticker identity audit report."""

from __future__ import annotations

import argparse

from src.rwa_asset_identity import write_rwa_ticker_identity_audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_ticker_identity_audit.json",
        help="Path for JSON audit output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_ticker_identity_audit.csv",
        help="Path for CSV audit output.",
    )
    args = parser.parse_args()

    audit = write_rwa_ticker_identity_audit(
        json_path=args.json_out,
        csv_path=args.csv_out,
    )
    print(
        "wrote identity audit: "
        f"{audit['summary']['asset_count']} assets, "
        f"{audit['summary']['by_classification_action']}"
    )


if __name__ == "__main__":
    main()
