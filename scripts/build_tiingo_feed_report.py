#!/usr/bin/env python3
"""Build the canonical Data Analytics artifact for the Tiingo feed review."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "reports/tiingo_feed_inventory_2026-07-21.csv"
FREQUENCY_PATH = ROOT / "reports/tiingo_feed_frequency_observed_2026-07-21.csv"
OUTPUT_PATH = ROOT / "reports/tiingo_feed_realtime_review_2026-07-21_artifact.json"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _number(value: str) -> float | int | None:
    if value == "":
        return None
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _frequency_rows() -> list[dict[str, Any]]:
    numeric_fields = {
        "duration_seconds",
        "events_or_timestamp_updates",
        "events_per_minute",
        "median_gap_seconds",
        "p95_gap_seconds",
        "max_gap_seconds",
        "share_gaps_at_or_below_500ms",
    }
    rows = _read_csv(FREQUENCY_PATH)
    for row in rows:
        for field in numeric_fields:
            row[field] = _number(str(row[field]))
        row["chart_label"] = f"{row['ticker']} — {row['profile']}"
    return rows


def build() -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    inventory = _read_csv(INVENTORY_PATH)
    frequency = _frequency_rows()
    chart_profiles = {
        "Direct derived equity reference",
        "Consolidated equity reference",
        "Crypto top and trades",
        "Forex top-of-book",
    }
    chart_rows = [
        row
        for row in frequency
        if row["profile"] in chart_profiles
        and (
            row["asset_class"] != "Equity"
            or (row["ticker"] == "LCID" and row["session"] == "Regular session")
        )
    ]
    chart_rows.sort(key=lambda row: float(row["events_per_minute"] or 0), reverse=True)

    frequency_source = {
        "id": "observed_frequency",
        "label": "Tiingo WebSocket and snapshot cadence benchmarks",
        "path": "reports/tiingo_feed_frequency_observed_2026-07-21.csv",
        "query": {
            "description": "Deterministic summary of authenticated Tiingo feed captures made on July 21, 2026, plus the earlier synchronized one-minute LCID snapshot comparison.",
            "engine": "duckdb",
            "language": "sql",
            "sql": "SELECT * FROM read_csv_auto('reports/tiingo_feed_frequency_observed_2026-07-21.csv', header = true)",
            "tables_used": [
                "reports/tiingo_feed_frequency_observed_2026-07-21.csv",
                "reports/blocksize_tiingo_LCID_20260721T180528Z.json",
                "reports/tiingo_iex_reference_20260721_afterhours.json",
                "reports/tiingo_consolidated_reference_20260721_afterhours.json",
                "reports/tiingo_consolidated_liquidity_20260721_afterhours.json",
                "reports/tiingo_crypto_top_trade_20260721.json",
                "reports/tiingo_crypto_trade_20260721.json",
                "reports/tiingo_fx_top_20260721.json",
            ],
            "filters": [
                "Equity WebSocket tests were after regular-session close",
                "Crypto and FX tests were live during their documented trading hours",
                "WebSocket windows lasted 20-30 seconds; LCID REST snapshot window lasted 60 seconds",
                "Only messageType=A data events were counted",
            ],
            "metric_definitions": [
                "events per minute = received data events / observed seconds * 60",
                "REST source timestamp updates per minute = distinct source timestamp advances after the first observed timestamp / scheduled minutes",
                "share gaps at or below 500ms = interarrival gaps no longer than 0.5 seconds / all observed interarrival gaps",
            ],
        },
    }
    inventory_source = {
        "id": "feed_inventory",
        "label": "Tiingo official product and documentation inventory",
        "path": "reports/tiingo_feed_inventory_2026-07-21.csv",
        "query": {
            "description": "Product-by-product classification compiled from Tiingo's official documentation and product pages; each row retains its source URL.",
            "engine": "duckdb",
            "language": "sql",
            "sql": "SELECT * FROM read_csv_auto('reports/tiingo_feed_inventory_2026-07-21.csv', header = true)",
            "tables_used": ["reports/tiingo_feed_inventory_2026-07-21.csv"],
            "filters": [
                "Publicly documented REST and WebSocket feed families as of July 21, 2026",
                "Sub-second capable does not mean a contractual per-symbol update guarantee",
            ],
            "metric_definitions": [
                "True event-level real time = eligible venue/source events are pushed as received",
                "Derived real time = values are calculated or thresholded and published only when provider logic triggers an update",
                "Sub-second guaranteed = a documented contractual promise of a new eligible value at least every 500ms; none was found",
            ],
        },
    }
    entitlement_source = {
        "id": "equity_entitlement_check",
        "label": "Tiingo full direct-equity entitlement check",
        "path": "reports/tiingo_iex_full_20260721_afterhours.json",
        "query": {
            "description": "Authenticated thresholdLevel 0 WebSocket subscription attempt using the configured credential.",
            "language": "python",
            "tables_used": ["reports/tiingo_iex_full_20260721_afterhours.json"],
            "filters": ["thresholdLevel=0", "eight representative US equity symbols"],
        },
    }
    chart_source = {
        "id": "observed_frequency_chart_source",
        "label": "Selected Tiingo cadence comparison rows",
        "path": "reports/tiingo_feed_frequency_observed_2026-07-21.csv",
        "query": {
            "description": "Comparable observed rows used in the update-frequency chart, retaining session, gap, and threshold context.",
            "engine": "duckdb",
            "language": "sql",
            "sql": "SELECT *, ticker || ' — ' || profile AS chart_label FROM read_csv_auto('reports/tiingo_feed_frequency_observed_2026-07-21.csv', header = true) WHERE profile IN ('Direct derived equity reference', 'Consolidated equity reference', 'Crypto top and trades', 'Forex top-of-book') AND (asset_class <> 'Equity' OR (ticker = 'LCID' AND session = 'Regular session')) ORDER BY events_per_minute DESC",
            "tables_used": ["reports/tiingo_feed_frequency_observed_2026-07-21.csv"],
            "filters": [
                "Primary top/reference profile per asset class",
                "Equity limited to the regular-session LCID comparison",
            ],
            "metric_definitions": [
                "events per minute = received data events / observed seconds * 60",
            ],
        },
    }

    manifest = {
        "version": 1,
        "surface": "report",
        "title": "Which Tiingo Feeds Are Actually Real Time?",
        "generatedAt": generated_at,
        "description": "Seller-facing classification of Tiingo feed semantics, entitlements, and observed update cadence.",
        "sources": [frequency_source, inventory_source, entitlement_source, chart_source],
        "charts": [
            {
                "id": "observed_frequency_chart",
                "title": "Observed updates per minute",
                "description": "20-60 second spot tests; event counts are source- and session-dependent, not an SLA.",
                "type": "bar",
                "dataset": "frequency_chart",
                "encodings": {
                    "x": {"field": "chart_label", "type": "nominal"},
                    "y": {"field": "events_per_minute", "type": "quantitative"},
                    "color": {"field": "asset_class", "type": "nominal"},
                },
                "options": {"orientation": "horizontal"},
                "sourceId": "observed_frequency_chart_source",
            }
        ],
        "tables": [
            {
                "id": "feed_inventory_table",
                "title": "Tiingo feed classification",
                "description": "Current public products, their update rules, and whether they support a hard 500ms pricing requirement.",
                "dataset": "feed_inventory",
                "columns": [
                    {"field": "product", "label": "Feed", "type": "text"},
                    {"field": "coverage_examples", "label": "Coverage", "type": "text"},
                    {"field": "source_semantics", "label": "What it represents", "type": "text"},
                    {"field": "official_update_rule", "label": "Update rule", "type": "text"},
                    {"field": "realtime_class", "label": "Real-time class", "type": "text"},
                    {"field": "subsecond_capable", "label": "Sub-second capable", "type": "text"},
                    {"field": "subsecond_guaranteed", "label": "500ms guaranteed", "type": "text"},
                    {"field": "current_access", "label": "Current access", "type": "text"},
                    {"field": "fit_for_500ms_primary_price", "label": "500ms primary-price fit", "type": "text"},
                    {"field": "source_url", "label": "Official source", "type": "text"},
                ],
                "defaultSort": {"field": "product", "direction": "asc"},
                "sourceId": "feed_inventory",
            },
            {
                "id": "ticker_frequency_table",
                "title": "Observed ticker frequency",
                "description": "Authenticated spot measurements on July 21, 2026; blank gap statistics mean fewer than two events were observed.",
                "dataset": "observed_frequency",
                "columns": [
                    {"field": "profile", "label": "Feed", "type": "text"},
                    {"field": "ticker", "label": "Ticker", "type": "text"},
                    {"field": "asset_class", "label": "Asset class", "type": "text"},
                    {"field": "session", "label": "Session", "type": "text"},
                    {"field": "transport", "label": "Transport", "type": "text"},
                    {"field": "events_or_timestamp_updates", "label": "Events/updates", "type": "number"},
                    {"field": "events_per_minute", "label": "Per minute", "type": "number"},
                    {"field": "median_gap_seconds", "label": "Median gap, s", "type": "number"},
                    {"field": "p95_gap_seconds", "label": "P95 gap, s", "type": "number"},
                    {"field": "max_gap_seconds", "label": "Max gap, s", "type": "number"},
                    {"field": "share_gaps_at_or_below_500ms", "label": "Share gaps ≤500ms", "type": "number"},
                    {"field": "interpretation", "label": "Interpretation", "type": "text"},
                ],
                "defaultSort": {"field": "events_per_minute", "direction": "desc"},
                "sourceId": "observed_frequency",
            },
        ],
        "blocks": [
            {
                "id": "title",
                "type": "markdown",
                "body": "# Which Tiingo Feeds Are Actually Real Time?",
            },
            {
                "id": "summary",
                "type": "markdown",
                "body": "## Executive Summary\n\n- **Tiingo has true event-level streams, but not all products labeled real time are equivalent.** Full direct-venue equities, BOATS overnight, crypto, and FX are designed as event streams. The derived and consolidated equity products publish only when provider algorithms detect or republish a qualifying change.\n- **The configured credential does not have full direct-equity TOPS access.** A threshold-level 0 subscription returned an error and closed. Tiingo's documentation says the full feed requires a separate exchange agreement; the accessible equity products are derived/reference feeds.\n- **No tested or documented product supports the claim “a new price every 500ms” as a universal SLA.** Crypto top-and-trade was the fastest accessible stream, but median per-symbol gaps were 0.74-0.99 seconds and only 36%-47% of gaps were at or below 500ms in the short sample.\n- **For LCID, the accessible equity source remains reference-grade.** During the earlier regular-session minute, its source timestamp advanced 4 times on the direct-derived endpoint and 3 times on the consolidated endpoint. That is directionally consistent with the customer's low-cadence finding.",
            },
            {
                "id": "real_time_definition",
                "type": "markdown",
                "body": "## Real time describes delivery latency, not a fixed update clock\n\nA genuine event stream publishes promptly when an eligible source event occurs. It does not promise that every symbol will trade or change top-of-book every 500ms. For sales and SLA language, keep three measures separate: event-to-delivery latency, unique source events per minute, and source age. A fast WebSocket can satisfy the first while failing the other two for a quiet asset.",
            },
            {
                "id": "chart_narrative",
                "type": "markdown",
                "body": "## Crypto is the fastest accessible feed, but still not a universal 2Hz source\n\nIncluding top-of-book roughly doubled or tripled observed crypto cadence relative to trades alone, yet the liquid pairs still had p95 gaps of 1.27-2.44 seconds. FX was much quieter in this late-US-session sample. LCID's regular-session reference timestamps advanced only 3-4 times per minute. The chart is a spot comparison rather than a provider SLA.",
            },
            {
                "id": "frequency_chart_block",
                "type": "chart",
                "chartId": "observed_frequency_chart",
            },
            {
                "id": "inventory_narrative",
                "type": "markdown",
                "body": "## Only the raw venue streams are candidates for sub-second trading use\n\nThe full direct-equity stream can be sub-second for active names, but it represents one venue rather than national NBBO and is currently entitlement-blocked. The consolidated equity reference and liquidity feeds add broader inputs, but their outputs are provider-derived and thresholded; the liquidity bid/ask fields are statistical valuation metrics, not executable NBBO. Historical, fundamental, fee, corporate-action, and news products are not sub-second price feeds.",
            },
            {
                "id": "inventory_table_block",
                "type": "table",
                "tableId": "feed_inventory_table",
            },
            {
                "id": "ticker_narrative",
                "type": "markdown",
                "body": "## The measured ticker table preserves the session and threshold context\n\nFrequency changes with the asset, selected threshold, exchange coverage, and time of day. A zero in the equity WebSocket rows means no event arrived during the short after-hours window; it is not evidence of an outage. The regular-session rerun is scheduled because the after-hours equity sample cannot support a fair production conclusion.",
            },
            {
                "id": "ticker_table_block",
                "type": "table",
                "tableId": "ticker_frequency_table",
            },
            {
                "id": "recommendations",
                "type": "markdown",
                "body": "## Recommended next steps\n\n1. **Do not sell the accessible LCID feed as 500ms or NBBO.** Position it as a normalized reference, valuation, monitoring, or fallback source.\n2. **If a single venue is sufficient, complete the exchange agreement and benchmark full TOPS.** This may unlock much higher event cadence, but it still will not represent the national best bid and offer.\n3. **If the customer requires national execution pricing, source licensed SIP/NBBO or a true multi-venue top-of-book feed.** A WebSocket transport upgrade alone cannot turn a thresholded reference value into full-market quotes.\n4. **For crypto and FX, negotiate the SLA around event-to-delivery latency and p95/p99 source age.** Avoid promising a new event every 500ms unless it is explicitly guaranteed for the target pairs and venues.\n5. **Run at least five regular sessions on the customer's symbol basket** before committing to asset-level cadence or freshness thresholds.",
            },
            {
                "id": "questions",
                "type": "markdown",
                "body": "## Further questions\n\n- Does the customer need a response every 500ms, delivery within 500ms of an exchange event, or a genuinely new market value every 500ms?\n- Is single-venue equity data acceptable, or must the price represent national NBBO?\n- Which symbols, sessions, and after-hours windows are contractually in scope?\n- Does the executed Tiingo agreement permit redistribution and external publication for this product?",
            },
            {
                "id": "caveats",
                "type": "markdown",
                "body": "## Caveats and assumptions\n\nThe live WebSocket samples lasted 20-30 seconds and the regular-session LCID snapshot comparison lasted one minute, so these figures describe observed windows rather than long-run frequency. Regular-session equity results are incomplete because the current WebSocket test ran after the close; an automated rerun is scheduled. BOATS overnight was identified in current Tiingo documentation but was not tested during its trading session. The credential's commercial and redistribution agreement was not reviewed, so technical access does not establish resale rights. This report is an internal evaluation, not a public performance benchmark or contractual SLA.",
            },
        ],
    }
    return {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "status": "ready",
            "generatedAt": generated_at,
            "datasets": {
                "feed_inventory": inventory,
                "observed_frequency": frequency,
                "frequency_chart": chart_rows,
            },
        },
        "sources": [frequency_source, inventory_source, entitlement_source, chart_source],
    }


def main() -> None:
    OUTPUT_PATH.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
