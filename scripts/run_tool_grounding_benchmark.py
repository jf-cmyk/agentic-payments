#!/usr/bin/env python3
"""Run a deterministic evidence-completeness and unsupported-symbol grounding benchmark."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "reports" / "agentic_marketing"
RUN_DATE = "2026-07-22"

CASES = [
    {"case_id": "btc_vwap", "supported": True, "instrument": "BTC-USD", "product": "vwap", "baseline_price": 100000.0},
    {"case_id": "eth_vwap", "supported": True, "instrument": "ETH-USD", "product": "vwap", "baseline_price": 3500.0},
    {"case_id": "aapl_bidask", "supported": True, "instrument": "AAPL", "product": "bidask", "baseline_price": 225.0},
    {"case_id": "eurusd_fx", "supported": True, "instrument": "EURUSD", "product": "fx", "baseline_price": 1.09},
    {"case_id": "xauusd_metal", "supported": True, "instrument": "XAUUSD", "product": "metal", "baseline_price": 2400.0},
    {"case_id": "unsupported_equity", "supported": False, "instrument": "ZZZQ", "product": "bidask", "baseline_price": 42.0},
    {"case_id": "unsupported_crypto", "supported": False, "instrument": "NOTREAL-USD", "product": "vwap", "baseline_price": 0.17},
    {"case_id": "invalid_symbol", "supported": False, "instrument": "../BAD", "product": "vwap", "baseline_price": 12.0},
]


def sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def evaluate() -> dict[str, Any]:
    rows = []
    for index, case in enumerate(CASES, start=1):
        baseline = {
            "instrument": case["instrument"],
            "price": case["baseline_price"],
            "source": None,
            "source_timestamp": None,
            "methodology": None,
            "citation": None,
            "support_checked": False,
        }
        if case["supported"]:
            grounded = {
                "instrument": case["instrument"],
                "price": case["baseline_price"],
                "source": "Blocksize",
                "source_timestamp": f"2026-07-22T12:{index:02d}:00Z",
                "methodology": f"blocksize_{case['product']}_snapshot_v1",
                "citation": f"https://mcp.blocksize.info/{case['product'].replace('bidask', 'bid-ask-price-api')}",
                "support_checked": True,
                "status": "ok",
            }
        else:
            grounded = {
                "instrument": case["instrument"],
                "price": None,
                "source": "Blocksize coverage check",
                "source_timestamp": f"2026-07-22T12:{index:02d}:00Z",
                "methodology": "instrument_search_then_refuse_v1",
                "citation": "https://mcp.blocksize.info/openapi.json",
                "support_checked": True,
                "status": "unsupported_or_invalid",
            }

        required_evidence = ("source", "source_timestamp", "methodology", "citation")
        baseline_evidence = sum(bool(baseline[field]) for field in required_evidence)
        grounded_evidence = sum(bool(grounded[field]) for field in required_evidence)
        rows.append({
            "case_id": case["case_id"],
            "instrument": case["instrument"],
            "supported_fixture": case["supported"],
            "baseline_evidence_fields": baseline_evidence,
            "grounded_evidence_fields": grounded_evidence,
            "baseline_unsupported_price_assertion": bool(not case["supported"] and baseline["price"] is not None),
            "grounded_unsupported_price_assertion": bool(not case["supported"] and grounded["price"] is not None),
            "baseline_support_checked": baseline["support_checked"],
            "grounded_support_checked": grounded["support_checked"],
            "baseline_response_hash": sha256(baseline),
            "grounded_response_hash": sha256(grounded),
        })

    unsupported = [row for row in rows if not row["supported_fixture"]]
    supported = [row for row in rows if row["supported_fixture"]]
    return {
        "benchmark": "blocksize_tool_grounding_harness_v1",
        "run_date": RUN_DATE,
        "status": "methodology_validation_only",
        "publication_status": "not_a_competitive_model_result",
        "methodology": {
            "baseline": "Deterministic ungrounded static fixture that asserts a numeric value without checking support or attaching evidence.",
            "grounded": "Deterministic Blocksize-shaped fixture that checks support, attaches source/timestamp/methodology/citation evidence, and refuses unsupported or invalid symbols.",
            "scope": "Validates benchmark scoring and response contracts. It does not measure a named LLM, live price accuracy, latency, or production uptime.",
        },
        "summary": {
            "cases": len(rows),
            "supported_cases": len(supported),
            "unsupported_or_invalid_cases": len(unsupported),
            "baseline_evidence_completeness": sum(row["baseline_evidence_fields"] for row in rows) / (len(rows) * 4),
            "grounded_evidence_completeness": sum(row["grounded_evidence_fields"] for row in rows) / (len(rows) * 4),
            "baseline_unsupported_price_assertion_rate": sum(row["baseline_unsupported_price_assertion"] for row in unsupported) / len(unsupported),
            "grounded_unsupported_price_assertion_rate": sum(row["grounded_unsupported_price_assertion"] for row in unsupported) / len(unsupported),
            "grounded_support_check_rate": sum(row["grounded_support_checked"] for row in rows) / len(rows),
        },
        "rows": rows,
        "next_valid_run": [
            "Freeze prompts and current truth timestamps.",
            "Run named models without tools and with Blocksize tools under identical prompts.",
            "Score price error only against timestamp-aligned source truth.",
            "Publish confidence intervals, failures, and the complete prompt/output corpus.",
        ],
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    result = evaluate()
    json_path = OUTPUT_DIR / f"tool_grounding_benchmark_{RUN_DATE}.json"
    csv_path = OUTPUT_DIR / f"tool_grounding_benchmark_{RUN_DATE}.csv"
    md_path = OUTPUT_DIR / f"tool_grounding_benchmark_{RUN_DATE}.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result["rows"][0]))
        writer.writeheader()
        writer.writerows(result["rows"])
    summary = result["summary"]
    md_path.write_text(
        "# Blocksize tool-grounding benchmark harness\n\n"
        "Status: **methodology validation only — not a competitive model result**.\n\n"
        "This deterministic run validates the scoring contract for source completeness and unsupported-symbol refusal. It does not claim real LLM performance or live price accuracy.\n\n"
        "## Results\n\n"
        f"- Cases: {summary['cases']}\n"
        f"- Baseline evidence completeness: {summary['baseline_evidence_completeness']:.0%}\n"
        f"- Grounded evidence completeness: {summary['grounded_evidence_completeness']:.0%}\n"
        f"- Baseline unsupported-price assertion rate: {summary['baseline_unsupported_price_assertion_rate']:.0%}\n"
        f"- Grounded unsupported-price assertion rate: {summary['grounded_unsupported_price_assertion_rate']:.0%}\n"
        f"- Grounded support-check rate: {summary['grounded_support_check_rate']:.0%}\n\n"
        "## Publication gate\n\n"
        "A publishable comparison still requires named model runs, identical frozen prompts, timestamp-aligned truth, and full prompt/output disclosure.\n",
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
