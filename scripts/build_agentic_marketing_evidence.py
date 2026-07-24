#!/usr/bin/env python3
"""Build reproducible RWA coverage and oracle-lineage evidence artifacts."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "agentic_marketing"
PDF_DIR = ROOT / "output" / "pdf"
GENERATED_AT = "2026-07-22T00:00:00Z"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source(
    source_id: str,
    label: str,
    path: str,
    description: str,
    sql: str,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "python",
            "language": "python",
            "description": description,
            "sql": sql,
            "tables_used": [path],
            "filters": {},
            "executed_at": GENERATED_AT,
        },
    }


def coverage_model() -> dict[str, Any]:
    summary_path = ROOT / "reports" / "rwa_master_sourceability_source_all_summary_2026-07-16.json"
    mitigation_path = ROOT / "reports" / "rwa_discovery_mitigation_plan.json"
    live_check_path = ROOT / "reports" / "rwa_live_source_readiness_2026-07-22.json"
    summary = read_json(summary_path)
    mitigation = read_json(mitigation_path)
    live_check = read_json(live_check_path)
    canonical_assets = 1025
    candidate_assets = int(summary["sourceable_unique_asset_ids"])
    deployment_rows = int(summary["token_rows"])
    candidate_rows = int(summary["sourceable_token_rows"])

    disposition = [
        {"status": "Candidate source lane", "deployment_rows": candidate_rows},
        {
            "status": "Access or adapter required",
            "deployment_rows": int(
                summary["by_sourceability_status"]["not_fetched_lane_requires_access_or_adapter"]
            ),
        },
    ]
    blockers = [
        {
            "promotion_gate": issue["issue_id"].replace("_", " ").title(),
            "affected_feeds": int(issue["affected_feed_count"]),
            "clear_feeds": int(issue["clear_feed_count"]),
            "severity": issue["severity"],
        }
        for issue in mitigation["issues"]
        if int(issue["affected_feed_count"]) > 0
    ]
    blockers.sort(key=lambda row: (-row["affected_feeds"], row["promotion_gate"]))
    venues = [
        {"venue": venue.replace("_", " ").title(), "candidate_rows": count}
        for venue, count in summary["sourceable_by_venue"].items()
    ]
    venues.sort(key=lambda row: (-row["candidate_rows"], row["venue"]))
    asset_classes = [
        {"asset_class": asset_class.replace("_", " ").title(), "candidate_rows": count}
        for asset_class, count in summary["sourceable_by_asset_class"].items()
    ]
    live_source_checks = []
    for row in live_check["results"]:
        observation = row.get("observation") if isinstance(row.get("observation"), dict) else {}
        evidence = observation.get("endpoint") or observation.get("chain") or "Source-specific blocker"
        if observation.get("block_number"):
            evidence = f"{evidence}; block {observation['block_number']}"
        live_source_checks.append(
            {
                "venue": str(row["venue"]).replace("_", " ").title(),
                "symbol": row["requested_symbol"],
                "source_lane": str(row["source_lane"]).replace("_", " ").title(),
                "status": "Working point-in-time probe" if row["status"] == "ok" else "Blocked safely",
                "observed_mid": observation.get("mid"),
                "freshness_ms": observation.get("freshness_ms"),
                "source_evidence": evidence,
                "tiingo_dependency": "No",
                "promotion_status": "Candidate only; not production-promoted",
            }
        )
    metrics = [{
        "canonical_assets": canonical_assets,
        "deployment_rows": deployment_rows,
        "candidate_unique_assets": candidate_assets,
        "candidate_asset_share": candidate_assets / canonical_assets,
        "candidate_deployment_rows": candidate_rows,
        "candidate_row_share": candidate_rows / deployment_rows,
        "expansion_production_promoted": 0,
        "live_probe_successes": int(live_check["summary"]["success_count"]),
        "live_probe_count": int(live_check["summary"]["probe_count"]),
        "live_probe_success_rate": (
            int(live_check["summary"]["success_count"])
            / int(live_check["summary"]["probe_count"])
        ),
    }]
    return {
        "generated_at": GENERATED_AT,
        "claims_boundary": {
            "existing_blocksize_coverage": "Existing Blocksize production market-data coverage remains live and is not counted by the expansion promotion metric.",
            "expansion_catalog": "The RWA expansion catalog contains 1,025 canonical economic assets across 3,407 token-deployment rows.",
            "zero_promoted_scope": "Zero means no newly sourced third-party or onchain addition has completed every expansion-workflow promotion gate.",
        },
        "metrics": metrics,
        "disposition": disposition,
        "blockers": blockers,
        "venues": venues,
        "asset_classes": asset_classes,
        "live_source_checks": live_source_checks,
    }


def rights_evidence_status(value: str) -> str:
    lowered = value.lower()
    if "unknown" in lowered:
        return "Not externally documented"
    if any(word in lowered for word in ("agreement", "add-on", "licensed", "subject to")):
        return "Agreement or license required"
    if lowered.startswith("no ") or "not a grant" in lowered:
        return "Restricted / not granted"
    return "Review required"


def lineage_model() -> dict[str, Any]:
    official_path = ROOT / "reports" / "rwa_official_source_rights_lineage_matrix_2026-07-16.csv"
    internal_path = ROOT / "reports" / "rwa_source_rights.csv"
    official = read_csv(official_path)
    internal = read_csv(internal_path)
    lineage_rows = [
        {
            "provider": row["provider"],
            "category": row["category"],
            "category_label": row["category"].replace("_", " ").title(),
            "price_semantics": row["price_semantics"],
            "lineage_group": row["lineage_group"],
            "external_redistribution_evidence": rights_evidence_status(
                row["commercial_redistribution_rights"]
            ),
            "documented_rights_text": row["commercial_redistribution_rights"],
            "decision_note": row["decision_note"],
            "official_sources": row["official_sources"],
        }
        for row in official
    ]
    category_counts = [
        {"category": category.replace("_", " ").title(), "providers": count}
        for category, count in Counter(row["category"] for row in official).items()
    ]
    category_counts.sort(key=lambda row: (-row["providers"], row["category"]))
    external_status = [
        {"evidence_status": status, "providers": count}
        for status, count in Counter(
            row["external_redistribution_evidence"] for row in lineage_rows
        ).items()
    ]
    external_status.sort(key=lambda row: (-row["providers"], row["evidence_status"]))
    internal_readiness = [
        {"readiness": "Production access ready", "providers": sum(row["production_access_ready"] == "True" for row in internal)},
        {"readiness": "Production access not ready", "providers": sum(row["production_access_ready"] != "True" for row in internal)},
    ]
    metrics = [{
        "officially_researched_providers": len(official),
        "internal_policy_rows": len(internal),
        "externally_undocumented_redistribution": sum(
            row["external_redistribution_evidence"] == "Not externally documented"
            for row in lineage_rows
        ),
        "internal_production_access_ready": sum(
            row["production_access_ready"] == "True" for row in internal
        ),
        "distinct_lineage_groups": len({row["lineage_group"] for row in official}),
    }]
    return {
        "generated_at": GENERATED_AT,
        "claims_boundary": {
            "lineage": "Lineage groups identify economically dependent observations so aggregates, component venues, marks, and oracle references are not double-counted as independent sources.",
            "rights": "Public technical access and onchain availability do not themselves grant commercial redistribution rights.",
            "internal_vs_external": "The internal rights register is a policy-state input; it is not substituted for externally documented contractual permission in this index.",
        },
        "metrics": metrics,
        "category_counts": category_counts,
        "external_status": external_status,
        "internal_readiness": internal_readiness,
        "lineage_rows": lineage_rows,
    }


def coverage_artifact(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Blocksize RWA Coverage Index",
            "description": "Expansion-catalog sourceability and production-promotion evidence, explicitly separated from existing Blocksize production coverage.",
            "generatedAt": GENERATED_AT,
            "sources": [
                {"id": "sourceability_summary", "label": "RWA sourceability summary", "path": "reports/rwa_master_sourceability_source_all_summary_2026-07-16.json"},
                {"id": "mitigation_plan", "label": "RWA discovery mitigation plan", "path": "reports/rwa_discovery_mitigation_plan.json"},
                {"id": "coverage_reconciliation", "label": "RWA coverage reconciliation", "path": "reports/rwa_coverage_and_client_usage_reconciliation_2026-07-16.md"},
                {"id": "live_source_check", "label": "Read-only live RWA source check", "path": "reports/rwa_live_source_readiness_2026-07-22.json"},
            ],
            "cards": [
                {"id": "assets", "description": "Expansion catalog denominator.", "dataset": "metrics", "sourceId": "coverage_reconciliation", "metrics": [{"label": "Canonical economic assets", "field": "canonical_assets", "format": "number"}]},
                {"id": "candidate_share", "description": "Unique economic assets with at least one candidate source lane.", "dataset": "metrics", "sourceId": "sourceability_summary", "metrics": [{"label": "Assets with candidate lane", "field": "candidate_asset_share", "format": "percent"}]},
                {"id": "promoted", "description": "New third-party/onchain additions clearing every expansion gate; excludes existing Blocksize production coverage.", "dataset": "metrics", "sourceId": "mitigation_plan", "metrics": [{"label": "New expansion feeds promoted", "field": "expansion_production_promoted", "format": "number"}]},
                {"id": "live_probes", "description": "Representative source lanes returning usable point-in-time observations on July 22, 2026.", "dataset": "metrics", "sourceId": "live_source_check", "metrics": [{"label": "Working representative probes", "field": "live_probe_successes", "format": "number"}, {"label": "of probes run", "field": "live_probe_count", "format": "number"}]},
            ],
            "charts": [
                {"id": "disposition", "title": "Deployment-row sourceability disposition", "subtitle": "All 3,407 token-deployment rows use one denominator.", "type": "bar", "dataset": "disposition", "sourceId": "sourceability_summary", "encodings": {"x": {"field": "status", "type": "nominal", "label": "Disposition"}, "y": {"field": "deployment_rows", "type": "quantitative", "label": "Deployment rows"}}, "valueFormat": "number", "layout": "full"},
                {"id": "blockers", "title": "Expansion promotion gates with affected feeds", "subtitle": "Counts overlap because one candidate can fail multiple gates.", "type": "bar", "dataset": "blockers", "sourceId": "mitigation_plan", "encodings": {"x": {"field": "promotion_gate", "type": "nominal", "label": "Promotion gate"}, "y": {"field": "affected_feeds", "type": "quantitative", "label": "Affected feeds"}}, "valueFormat": "number", "layout": "full"},
            ],
            "tables": [
                {"id": "venues", "title": "Candidate deployment rows by venue", "subtitle": "A deployment row is not the same grain as an economic asset.", "dataset": "venues", "sourceId": "sourceability_summary", "defaultSort": {"field": "candidate_rows", "direction": "desc"}, "columns": [{"field": "venue", "label": "Venue"}, {"field": "candidate_rows", "label": "Candidate rows", "format": "number"}]},
                {"id": "live_sources", "title": "Representative live source verification", "subtitle": "Read-only checks on July 22, 2026; success proves reachable data, not production promotion.", "dataset": "live_source_checks", "sourceId": "live_source_check", "defaultSort": {"field": "venue", "direction": "asc"}, "columns": [{"field": "venue", "label": "Venue"}, {"field": "symbol", "label": "Symbol"}, {"field": "source_lane", "label": "Source lane"}, {"field": "status", "label": "Check result"}, {"field": "source_evidence", "label": "Evidence"}, {"field": "tiingo_dependency", "label": "Tiingo?"}, {"field": "promotion_status", "label": "Promotion"}]},
            ],
            "blocks": [
                {"id": "title", "type": "markdown", "body": "# Blocksize RWA Coverage Index\n\nEvidence as of July 22, 2026. This index measures the separate RWA expansion workflow; it does not negate or replace Blocksize's existing live production market-data coverage."},
                {"id": "summary", "type": "markdown", "body": "## Executive Summary\n\nBlocksize already supplies broad production market data. Separately, the RWA expansion catalog contains **1,025 canonical economic assets** and **3,407 token-deployment rows**. **90 assets (8.8%)** have at least one candidate source lane, while **709 deployment rows (20.8%)** are candidate-sourceable. A July 22 read-only verification returned working data from **6 of 7 representative checks** across venue APIs, a Solana router quote, and Ethereum/Base RPC pool state. The runtime RWA path did **not** use Tiingo. No newly sourced third-party or onchain addition has yet cleared every expansion-workflow promotion gate."},
                {"id": "metrics", "type": "metric-strip", "cardIds": ["assets", "candidate_share", "live_probes", "promoted"]},
                {"id": "live_finding", "type": "markdown", "body": "## Working data comes from venue APIs and chain infrastructure\n\nThe representative checks returned current Hyperliquid order-book data, Gains and Ostium venue prices, a Jupiter Solana route quote, and live Ethereum/Base pool state. The Raydium-labeled check was blocked because Jupiter routed through Byreal; that is the intended anti-mislabeling behavior. These RWA runtime checks have no Tiingo dependency."},
                {"id": "live_table", "type": "table", "tableId": "live_sources"},
                {"id": "finding1", "type": "markdown", "body": "## Key findings\n\nThe largest gap is operational source access and adapter readiness, not catalog breadth. The deployment-row view below preserves a consistent 3,407-row denominator."},
                {"id": "chart1", "type": "chart", "chartId": "disposition"},
                {"id": "finding2", "type": "markdown", "body": "Candidate sourceability is necessary but not sufficient for production. Promotion remains gated by sustained quality windows, manipulation controls, replay evidence, identifiers, benchmark alignment, and issuer/NAV checks. Gate counts overlap and must not be summed."},
                {"id": "chart2", "type": "chart", "chartId": "blockers"},
                {"id": "finding3", "type": "markdown", "body": "Venue concentration shows where adapter and access work can unlock the most deployment rows. It does not imply independent lineage, legal redistribution clearance, or production quality."},
                {"id": "table1", "type": "table", "tableId": "venues"},
                {"id": "next", "type": "markdown", "body": "## Recommended next steps\n\n1. Resolve native identifiers and source access for the highest-yield candidate venues.\n2. Run continuous quality windows and persist replayable payload evidence.\n3. Apply manipulation, concentration, and Blocksize benchmark gates before consensus inclusion.\n4. Promote only when every required technical, lineage, issuer, and rights gate passes."},
                {"id": "questions", "type": "markdown", "body": "## Further questions\n\nWhich candidate venues produce the fastest incremental asset coverage after rights and adapter effort? Which catalog assets are already covered by existing Blocksize products but need a clearer RWA taxonomy mapping?"},
                {"id": "caveats", "type": "markdown", "body": "## Caveats\n\nCatalog assets, ticker strings, token deployments, venue rows, and production feeds are different grains. Candidate-sourceable does not mean licensed, independent, manipulation-resistant, or production-promoted. A point-in-time success does not replace continuous freshness, replay, manipulation, benchmark, consensus, rights, and signoff windows. The zero-promotion statement applies only to new expansion sources."},
            ],
        },
        "snapshot": {"version": 1, "generatedAt": GENERATED_AT, "status": "ready", "datasets": {"metrics": model["metrics"], "disposition": model["disposition"], "blockers": model["blockers"], "venues": model["venues"], "live_source_checks": model["live_source_checks"]}},
        "sources": [
            source("sourceability_summary", "RWA sourceability summary", "reports/rwa_master_sourceability_source_all_summary_2026-07-16.json", "Aggregate sourceability counts by row, asset, venue, and asset class.", "SELECT * FROM sourceability_summary"),
            source("mitigation_plan", "RWA discovery mitigation plan", "reports/rwa_discovery_mitigation_plan.json", "Promotion-gate issue counts and execution phases.", "SELECT issue_id, affected_feed_count, clear_feed_count, severity FROM mitigation_issues WHERE affected_feed_count > 0 ORDER BY affected_feed_count DESC"),
            source("coverage_reconciliation", "RWA coverage reconciliation", "reports/rwa_coverage_and_client_usage_reconciliation_2026-07-16.md", "Reconciles the 1,025-asset, 1,150-ticker, and 3,407-deployment grains.", "SELECT canonical_assets, ticker_strings, deployment_rows FROM coverage_reconciliation"),
            source("live_source_check", "Read-only live RWA source check", "reports/rwa_live_source_readiness_2026-07-22.json", "Representative point-in-time probes across venue API, Solana router, and EVM RPC source lanes.", "SELECT venue, requested_symbol, source_lane, status, observation FROM live_source_readiness_check"),
        ],
    }


def lineage_artifact(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Blocksize Oracle Lineage & Rights Evidence Index",
            "description": "Source semantics, lineage-independence, and rights evidence for RWA and oracle candidates.",
            "generatedAt": GENERATED_AT,
            "sources": [
                {"id": "official_matrix", "label": "Official-source rights and lineage matrix", "path": "reports/rwa_official_source_rights_lineage_matrix_2026-07-16.csv"},
                {"id": "internal_register", "label": "Internal source-rights register", "path": "reports/rwa_source_rights.csv"},
            ],
            "cards": [
                {"id": "providers", "description": "Rows backed by linked public provider materials.", "dataset": "metrics", "sourceId": "official_matrix", "metrics": [{"label": "Officially researched providers", "field": "officially_researched_providers", "format": "number"}]},
                {"id": "undocumented", "description": "Provider rows whose public evidence does not state a commercial redistribution grant.", "dataset": "metrics", "sourceId": "official_matrix", "metrics": [{"label": "Redistribution not externally documented", "field": "externally_undocumented_redistribution", "format": "number"}]},
                {"id": "lineages", "description": "Economic/source dependency groups used to avoid double-counting.", "dataset": "metrics", "sourceId": "official_matrix", "metrics": [{"label": "Distinct lineage groups", "field": "distinct_lineage_groups", "format": "number"}]},
            ],
            "charts": [
                {"id": "categories", "title": "Providers by researched category", "subtitle": "Category is descriptive; it does not imply equivalent price semantics.", "type": "bar", "dataset": "category_counts", "sourceId": "official_matrix", "encodings": {"x": {"field": "category", "type": "nominal", "label": "Category"}, "y": {"field": "providers", "type": "quantitative", "label": "Providers"}}, "valueFormat": "number", "layout": "full"},
                {"id": "rights", "title": "Public commercial-redistribution evidence status", "subtitle": "Classification of the documented public evidence, not a legal opinion.", "type": "bar", "dataset": "external_status", "sourceId": "official_matrix", "encodings": {"x": {"field": "evidence_status", "type": "nominal", "label": "Evidence status"}, "y": {"field": "providers", "type": "quantitative", "label": "Providers"}}, "valueFormat": "number", "layout": "full"},
            ],
            "tables": [
                {"id": "lineage", "title": "Provider lineage and rights evidence", "subtitle": "Read price semantics and lineage group before treating sources as independent.", "dataset": "lineage_rows", "sourceId": "official_matrix", "defaultSort": {"field": "provider", "direction": "asc"}, "columns": [{"field": "provider", "label": "Provider"}, {"field": "category_label", "label": "Category"}, {"field": "lineage_group", "label": "Lineage group"}, {"field": "external_redistribution_evidence", "label": "External rights evidence"}]},
            ],
            "blocks": [
                {"id": "title", "type": "markdown", "body": "# Blocksize Oracle Lineage & Rights Evidence Index\n\nEvidence as of July 22, 2026. This is a product and sourcing control artifact, not legal advice."},
                {"id": "summary", "type": "markdown", "body": "## Executive Summary\n\nThe index covers **34 providers** and **34 distinct lineage groups**. For **25 providers**, a commercial redistribution grant is not stated in the reviewed public evidence. Public APIs, onchain contracts, and signed reports can establish technical access or provenance, but they do not by themselves grant Blocksize the right to redistribute the data."},
                {"id": "metrics", "type": "metric-strip", "cardIds": ["providers", "undocumented", "lineages"]},
                {"id": "finding1", "type": "markdown", "body": "## Key findings\n\nSource categories span venues, oracle networks, issuers, commercial market-data partners, and infrastructure. Their values are not interchangeable: executable books, reference marks, issuer NAV, and oracle aggregates need different labels and quality gates."},
                {"id": "chart1", "type": "chart", "chartId": "categories"},
                {"id": "finding2", "type": "markdown", "body": "Public documentation most often leaves commercial redistribution unstated or agreement-scoped. That is a diligence queue, not evidence that use is forbidden or granted."},
                {"id": "chart2", "type": "chart", "chartId": "rights"},
                {"id": "finding3", "type": "markdown", "body": "Lineage groups prevent false source diversity. A vendor aggregate, its component venues, and a downstream oracle report cannot automatically be counted as independent confirmations of the same price."},
                {"id": "table1", "type": "table", "tableId": "lineage"},
                {"id": "next", "type": "markdown", "body": "## Recommended next steps\n\n1. Attach executed contract or license references to each production source.\n2. Keep executable, indicative, oracle, NAV, and derived price semantics explicit in every receipt.\n3. Require lineage-group checks before consensus calculations.\n4. Reconcile the internal policy register against external documentary evidence and counsel-approved decisions."},
                {"id": "questions", "type": "markdown", "body": "## Further questions\n\nWhich high-value providers can supply a written redistribution and signing grant? Which apparent independent sources share an upstream index, venue set, issuer NAV, or oracle aggregate?"},
                {"id": "caveats", "type": "markdown", "body": "## Caveats\n\nThe external matrix summarizes public materials and is not legal advice. The internal rights register is a policy-state input and may encode approvals not visible in public documentation; it must not be presented as an external contractual grant without the supporting agreement."},
            ],
        },
        "snapshot": {"version": 1, "generatedAt": GENERATED_AT, "status": "ready", "datasets": {"metrics": model["metrics"], "category_counts": model["category_counts"], "external_status": model["external_status"], "lineage_rows": model["lineage_rows"]}},
        "sources": [
            source("official_matrix", "Official-source rights and lineage matrix", "reports/rwa_official_source_rights_lineage_matrix_2026-07-16.csv", "Provider-by-provider review of price semantics, access, public rights evidence, lineage, and official sources.", "SELECT provider, category, price_semantics, lineage_group, commercial_redistribution_rights, decision_note, official_sources FROM official_source_rights_lineage_matrix"),
            source("internal_register", "Internal source-rights register", "reports/rwa_source_rights.csv", "Internal policy and readiness state, kept separate from public documentary evidence.", "SELECT provider_id, rights_status, production_access_ready, can_redistribute_production FROM internal_source_rights_register"),
        ],
    }


def pdf_styles() -> dict[str, ParagraphStyle]:
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("Title", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=25, leading=28, textColor=colors.HexColor("#111827"), spaceAfter=12),
        "subtitle": ParagraphStyle("Subtitle", parent=styles["BodyText"], fontSize=10, leading=15, textColor=colors.HexColor("#5B6470"), spaceAfter=16),
        "h1": ParagraphStyle("H1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=colors.HexColor("#183A72"), spaceBefore=12, spaceAfter=8),
        "h2": ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=colors.HexColor("#111827"), spaceBefore=8, spaceAfter=5),
        "body": ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9, leading=13, textColor=colors.HexColor("#374151"), spaceAfter=7),
        "metric": ParagraphStyle("Metric", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=18, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#285CDB")),
        "metric_label": ParagraphStyle("MetricLabel", parent=styles["BodyText"], fontSize=7.5, leading=10, alignment=TA_CENTER, textColor=colors.HexColor("#5B6470")),
        "small": ParagraphStyle("Small", parent=styles["BodyText"], fontSize=7, leading=9, textColor=colors.HexColor("#4B5563")),
    }


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D7DCE2"))
    canvas.line(0.65 * inch, 0.48 * inch, 7.85 * inch, 0.48 * inch)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#6B7280"))
    canvas.drawString(0.65 * inch, 0.30 * inch, "Blocksize evidence artifact · 2026-07-22")
    canvas.drawRightString(7.85 * inch, 0.30 * inch, f"Page {doc.page}")
    canvas.restoreState()


def metric_strip(items: list[tuple[str, str]], styles: dict[str, ParagraphStyle]) -> Table:
    cells = [
        [Paragraph(value, styles["metric"]), Paragraph(label, styles["metric_label"])]
        for value, label in items
    ]
    table = Table([[cell for cell in cells]], colWidths=[6.9 * inch / len(cells)] * len(cells))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F7FA")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DCE2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DCE2")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return table


def horizontal_chart(labels: list[str], values: list[int], width: float = 6.8 * inch) -> Drawing:
    height = max(1.8 * inch, 0.28 * inch * len(labels) + 0.55 * inch)
    drawing = Drawing(width, height)
    chart = HorizontalBarChart()
    chart.x = 2.15 * inch
    chart.y = 0.3 * inch
    chart.width = width - 2.45 * inch
    chart.height = height - 0.55 * inch
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(values) * 1.12 if values else 1
    chart.valueAxis.valueStep = max(1, round(max(values) / 4)) if values else 1
    chart.bars[0].fillColor = colors.HexColor("#285CDB")
    chart.bars[0].strokeColor = None
    drawing.add(chart)
    return drawing


def styled_table(headers: list[str], rows: list[list[Any]], widths: list[float], styles: dict[str, ParagraphStyle]) -> Table:
    data = [[Paragraph(header, styles["small"]) for header in headers]]
    for row in rows:
        data.append([Paragraph(str(value), styles["small"]) for value in row])
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EFFB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#183A72")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DCE2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFBFC")]),
    ]))
    return table


def build_coverage_pdf(model: dict[str, Any], output: Path) -> None:
    styles = pdf_styles()
    story: list[Any] = [
        Paragraph("Blocksize RWA Coverage Index", styles["title"]),
        Paragraph("Expansion-catalog sourceability and promotion evidence · Evidence as of July 22, 2026", styles["subtitle"]),
        Paragraph("Executive Summary", styles["h1"]),
        Paragraph("Blocksize already supplies broad production market data. Separately, the RWA expansion catalog contains <b>1,025 canonical economic assets</b> and <b>3,407 token-deployment rows</b>. Of those, 90 assets have at least one candidate source lane. No newly sourced third-party or onchain addition has yet completed every expansion-workflow promotion gate.", styles["body"]),
        metric_strip([("1,025", "Canonical expansion assets"), ("8.8%", "Assets with candidate lane"), ("6/7", "Working representative probes"), ("0", "New expansion feeds promoted")], styles),
        Spacer(1, 10),
        Paragraph("Key findings", styles["h1"]),
        Paragraph("Deployment-row sourceability", styles["h2"]),
        Paragraph("The largest gap is source access and adapter readiness. Both bars use the same 3,407 deployment-row denominator; a row is not the same grain as an economic asset.", styles["body"]),
        horizontal_chart([row["status"] for row in model["disposition"]], [row["deployment_rows"] for row in model["disposition"]]),
        KeepTogether([
            Paragraph("Promotion blockers", styles["h2"]),
            Paragraph("Candidate sourceability is not production readiness. A feed can fail multiple gates, so the affected-feed counts below overlap and must not be added together.", styles["body"]),
            horizontal_chart([row["promotion_gate"] for row in model["blockers"]], [row["affected_feeds"] for row in model["blockers"]]),
        ]),
        Paragraph("Candidate venue concentration", styles["h1"]),
        Paragraph("These counts identify where adapter and access work can unlock deployment rows. They do not prove independent lineage, redistribution rights, or production quality.", styles["body"]),
        styled_table(["Venue", "Candidate rows"], [[row["venue"], row["candidate_rows"]] for row in model["venues"]], [5.6 * inch, 1.3 * inch], styles),
        KeepTogether([
            Paragraph("Working source evidence", styles["h1"]),
            Paragraph("Six of seven read-only checks returned usable data from venue APIs, a Solana router quote, and Ethereum/Base RPC pool state. No check depended on Tiingo. The Raydium-specific check safely rejected a route that was actually labeled Byreal.", styles["body"]),
            styled_table(["Venue", "Symbol", "Source lane", "Result"], [[row["venue"], row["symbol"], row["source_lane"], row["status"]] for row in model["live_source_checks"]], [1.55 * inch, 1.05 * inch, 2.65 * inch, 1.65 * inch], styles),
        ]),
        Paragraph("Recommended next steps", styles["h1"]),
        Paragraph("1. Resolve native identifiers and source access for high-yield venues.<br/>2. Run continuous quality windows and persist replayable payload evidence.<br/>3. Apply manipulation, concentration, and Blocksize benchmark gates.<br/>4. Promote only when every required technical, lineage, issuer, and rights gate passes.", styles["body"]),
        Paragraph("Further questions", styles["h1"]),
        Paragraph("Which candidate venues produce the fastest incremental asset coverage after rights and adapter effort? Which expansion assets already map to existing Blocksize production products but need clearer RWA taxonomy?", styles["body"]),
        Paragraph("Caveats", styles["h1"]),
        Paragraph("Catalog assets, ticker strings, token deployments, venue rows, and production feeds are different grains. Candidate-sourceable does not mean licensed, independent, manipulation-resistant, or promoted. A point-in-time success does not replace continuous quality and signoff gates. The zero-promotion statement applies only to new expansion sources, not existing Blocksize production coverage.", styles["body"]),
        Paragraph("Sources", styles["h1"]),
        Paragraph("reports/rwa_master_sourceability_source_all_summary_2026-07-16.json<br/>reports/rwa_discovery_mitigation_plan.json<br/>reports/rwa_coverage_and_client_usage_reconciliation_2026-07-16.md<br/>reports/rwa_live_source_readiness_2026-07-22.json", styles["small"]),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=letter, rightMargin=0.65 * inch, leftMargin=0.65 * inch, topMargin=0.62 * inch, bottomMargin=0.62 * inch, title="Blocksize RWA Coverage Index", author="Blocksize")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def build_lineage_pdf(model: dict[str, Any], output: Path) -> None:
    styles = pdf_styles()
    story: list[Any] = [
        Paragraph("Blocksize Oracle Lineage & Rights Evidence Index", styles["title"]),
        Paragraph("Source semantics, independence, and documentary rights evidence · Evidence as of July 22, 2026", styles["subtitle"]),
        Paragraph("Executive Summary", styles["h1"]),
        Paragraph("The index covers <b>34 providers</b> and <b>34 distinct lineage groups</b>. For 25 providers, a commercial redistribution grant is not stated in the reviewed public evidence. Public APIs, onchain contracts, and signed reports establish technical access or provenance; they do not themselves grant Blocksize redistribution rights.", styles["body"]),
        metric_strip([("34", "Researched providers"), ("25", "Redistribution not documented"), ("34", "Distinct lineage groups")], styles),
        Spacer(1, 10),
        Paragraph("Key findings", styles["h1"]),
        Paragraph("Provider categories", styles["h2"]),
        Paragraph("The researched set spans venues, oracles, issuers, commercial data partners, and infrastructure. Their executable, indicative, NAV, reference, and aggregate values are not interchangeable.", styles["body"]),
        horizontal_chart([row["category"] for row in model["category_counts"]], [row["providers"] for row in model["category_counts"]]),
        KeepTogether([
            Paragraph("Public redistribution evidence", styles["h2"]),
            Paragraph("Most public materials leave commercial redistribution unstated or agreement-scoped. This is a diligence queue, not a legal conclusion that use is forbidden or granted.", styles["body"]),
            horizontal_chart([row["evidence_status"] for row in model["external_status"]], [row["providers"] for row in model["external_status"]]),
        ]),
        Paragraph("Provider lineage and rights evidence", styles["h1"]),
        Paragraph("Lineage groups prevent false diversity: a vendor aggregate, its component venues, and a downstream oracle report cannot automatically count as independent confirmations.", styles["body"]),
        styled_table(["Provider", "Category", "Lineage group", "External rights evidence"], [[row["provider"], row["category_label"], row["lineage_group"], row["external_redistribution_evidence"]] for row in model["lineage_rows"]], [1.45 * inch, 1.25 * inch, 2.35 * inch, 1.85 * inch], styles),
        PageBreak(),
        Paragraph("Recommended next steps", styles["h1"]),
        Paragraph("1. Attach executed contract or license references to each production source.<br/>2. Preserve executable, indicative, oracle, NAV, and derived semantics in receipts.<br/>3. Require lineage-group checks before consensus calculations.<br/>4. Reconcile internal policy state against external documentary evidence and counsel-approved decisions.", styles["body"]),
        Paragraph("Further questions", styles["h1"]),
        Paragraph("Which providers can supply written redistribution and signing grants? Which apparently independent sources share an upstream index, venue set, issuer NAV, or oracle aggregate?", styles["body"]),
        Paragraph("Caveats", styles["h1"]),
        Paragraph("This is not legal advice. The external matrix summarizes public materials. The internal rights register is a policy-state input and may encode approvals not visible publicly; it must not be represented as an external contractual grant without supporting documentation.", styles["body"]),
        Paragraph("Sources", styles["h1"]),
        Paragraph("reports/rwa_official_source_rights_lineage_matrix_2026-07-16.csv<br/>reports/rwa_source_rights.csv", styles["small"]),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=letter, rightMargin=0.65 * inch, leftMargin=0.65 * inch, topMargin=0.62 * inch, bottomMargin=0.62 * inch, title="Blocksize Oracle Lineage and Rights Evidence Index", author="Blocksize")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> None:
    coverage = coverage_model()
    lineage = lineage_model()

    write_json(REPORT_DIR / "rwa_coverage_index_2026-07-22.json", coverage)
    write_csv(REPORT_DIR / "rwa_coverage_venues_2026-07-22.csv", coverage["venues"])
    write_json(REPORT_DIR / "oracle_lineage_index_2026-07-22.json", lineage)
    write_csv(REPORT_DIR / "oracle_lineage_index_2026-07-22.csv", lineage["lineage_rows"])
    write_json(REPORT_DIR / "rwa_coverage_report_artifact.json", coverage_artifact(coverage))
    write_json(REPORT_DIR / "oracle_lineage_report_artifact.json", lineage_artifact(lineage))
    build_coverage_pdf(coverage, PDF_DIR / "Blocksize_RWA_Coverage_Index_2026-07-22.pdf")
    build_lineage_pdf(lineage, PDF_DIR / "Blocksize_Oracle_Lineage_Index_2026-07-22.pdf")
    print(json.dumps({"coverage_assets": coverage["metrics"][0]["canonical_assets"], "lineage_providers": lineage["metrics"][0]["officially_researched_providers"], "report_dir": str(REPORT_DIR), "pdf_dir": str(PDF_DIR)}, indent=2))


if __name__ == "__main__":
    main()
