#!/usr/bin/env python3
"""Deterministically renormalize a captured RWA.xyz monitor report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.rwa_xyz_monitor import (
    DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
    _write_csv,
    _mixed_class_asset_ids,
    reclassify_rwa_xyz_monitor_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_RWA_XYZ_REPORT_JSON_PATH)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_RWA_XYZ_REPORT_JSON_PATH)
    parser.add_argument("--assets-csv-out", type=Path, default=DEFAULT_RWA_XYZ_ASSET_CSV_PATH)
    parser.add_argument("--tokens-csv-out", type=Path, default=DEFAULT_RWA_XYZ_TOKEN_CSV_PATH)
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    before_mixed_ids = _mixed_class_asset_ids(source.get("asset_rows") or [])
    report = reclassify_rwa_xyz_monitor_report(source)
    if reclassify_rwa_xyz_monitor_report(report) != report:
        raise RuntimeError("RWA.xyz captured-report normalization is not idempotent")
    for path in (args.json_out, args.assets_csv_out, args.tokens_csv_out):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.assets_csv_out, report.get("asset_rows") or [])
    _write_csv(args.tokens_csv_out, report.get("token_rows") or [])
    print(
        json.dumps(
            {
                "snapshot_generated_at": report.get("generated_at"),
                "mixed_class_asset_id_count_before": len(before_mixed_ids),
                "mixed_class_asset_ids_before": before_mixed_ids,
                "mixed_class_asset_id_count_after": report["summary"][
                    "identity_quality"
                ]["mixed_class_asset_id_count"],
                "identity_quality": report["summary"]["identity_quality"],
                "contract_identity_quality": report["summary"][
                    "contract_identity_quality"
                ],
                "yield_metric_quality": report["summary"]["yield_metric_quality"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
