"""Acceptance checks for crawler-safe public discovery metadata."""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from src import public_metadata, resource_server
from src.rwa_coverage import build_rwa_asset_matrix


ROOT = Path(__file__).resolve().parents[1]
SITEMAP_NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.add(value)


def _package_by_id(package_id: str) -> dict[str, object]:
    return next(package for package in public_metadata.DATA_PACKAGES if package["id"] == package_id)


def test_generated_package_link_mesh_has_no_404s() -> None:
    """Every catalog URL rendered into every selector must resolve locally."""
    package_paths = {
        urlsplit(str(package["url"])).path for package in public_metadata.DATA_PACKAGES
    }
    rendered_hrefs: set[str] = set()
    for slug in public_metadata.SEO_LANDING_PAGES:
        parser = _HrefParser()
        parser.feed(public_metadata.build_seo_landing_page(slug))
        rendered_hrefs.update(parser.hrefs)

    assert package_paths <= rendered_hrefs

    client = TestClient(resource_server.app)
    try:
        failures = {
            path: response.status_code
            for path in sorted(package_paths)
            if (response := client.get(path, follow_redirects=False)).status_code != 200
        }
    finally:
        client.close()

    assert failures == {}


def test_distinct_products_have_pages_and_token_quality_has_one_canonical_url() -> None:
    assert "agent-data-provenance-api" in public_metadata.SEO_LANDING_PAGES
    assert "spend-controlled-market-monitor-api" in public_metadata.SEO_LANDING_PAGES

    provenance = _package_by_id("agent-data-provenance")
    monitor = _package_by_id("spend-controlled-market-monitor")
    token_quality = _package_by_id("token-market-quality-indicator")

    assert str(provenance["url"]).endswith("/agent-data-provenance-api")
    assert str(monitor["url"]).endswith("/spend-controlled-market-monitor-api")
    assert str(token_quality["url"]).endswith("/token-quality-indicator-api")
    assert "token-market-quality-indicator-api" not in {
        urlsplit(str(package["url"])).path.strip("/") for package in public_metadata.DATA_PACKAGES
    }


def test_robots_excludes_transport_and_operational_surfaces_from_crawling() -> None:
    robots = public_metadata.build_robots_txt()

    assert public_metadata.NON_CRAWLABLE_MCP_PATHS == (
        "/anthropic/mcp/",
        "/cursor/mcp/",
        "/openai/mcp/",
        "/mcp/server/",
    )
    assert public_metadata.NON_CRAWLABLE_OPERATIONAL_PATHS == (
        "/v1/",
        "/internal/",
    )
    assert public_metadata.NON_CRAWLABLE_PATHS == (
        *public_metadata.NON_CRAWLABLE_MCP_PATHS,
        *public_metadata.NON_CRAWLABLE_OPERATIONAL_PATHS,
    )
    for path in public_metadata.NON_CRAWLABLE_PATHS:
        assert f"Disallow: {path}" in robots

    for path in (
        "/llms.txt",
        "/data-packages.json",
        "/category-hubs.json",
        "/server.json",
        "/mcp/manifest.json",
        "/openapi.json",
    ):
        assert f"Allow: {path}" in robots


def test_sitemap_lastmod_is_versioned_content_metadata_not_wall_clock_time() -> None:
    sitemap = ET.fromstring(public_metadata.build_sitemap_xml())
    locations = [node.text for node in sitemap.findall("sm:url/sm:loc", SITEMAP_NAMESPACE)]
    last_modified = [node.text for node in sitemap.findall("sm:url/sm:lastmod", SITEMAP_NAMESPACE)]

    assert len(locations) == len(set(locations))
    assert len(last_modified) == len(locations)
    dates_by_location = dict(zip(locations, last_modified, strict=True))
    historical_urls = {
        public_metadata.RWA_COVERAGE_INDEX_URL,
        public_metadata.ORACLE_LINEAGE_INDEX_URL,
        public_metadata.RWA_COVERAGE_INDEX_PDF_URL,
        public_metadata.ORACLE_LINEAGE_INDEX_PDF_URL,
    }
    assert {
        dates_by_location[url] for url in historical_urls
    } == {public_metadata.HISTORICAL_EVIDENCE_LAST_MODIFIED}
    assert {
        modified
        for location, modified in dates_by_location.items()
        if location not in historical_urls
    } == {public_metadata.PUBLIC_CONTENT_LAST_MODIFIED}
    assert (
        public_metadata.PUBLIC_CONTENT_LAST_MODIFIED_BY_VERSION[public_metadata.APP_VERSION]
        == public_metadata.PUBLIC_CONTENT_LAST_MODIFIED
    )
    date.fromisoformat(public_metadata.PUBLIC_CONTENT_LAST_MODIFIED)


def test_public_rwa_counts_reconcile_to_captured_source_and_lossless_matrix() -> None:
    monitor = json.loads(
        (ROOT / "reports/rwa_xyz_new_asset_monitor.json").read_text(encoding="utf-8")
    )
    daily = json.loads((ROOT / "reports/rwa_daily_feed_agent.json").read_text(encoding="utf-8"))
    matrix_summary = build_rwa_asset_matrix()["summary"]
    snapshot = public_metadata.RWA_DISCOVERY_SNAPSHOT
    source_summary = monitor["summary"]
    identity = source_summary["identity_quality"]
    contracts = source_summary["contract_identity_quality"]

    assert snapshot["as_of"] == monitor["generated_at"][:10]
    assert snapshot["rwa_xyz_fetched_at"] == monitor["source"]["fetched_at"]
    assert snapshot["rwa_xyz_source_asset_rows"] == source_summary["asset_count"]
    assert snapshot["rwa_xyz_token_listing_rows"] == source_summary["token_count"]
    assert (
        snapshot["rwa_xyz_unique_contract_identities"]
        == contracts["unique_contract_identity_count"]
    )
    assert snapshot["rwa_xyz_identity_verified_asset_rows"] == identity["verified_asset_count"]
    assert snapshot["rwa_xyz_identity_unverified_asset_rows"] == identity["unverified_asset_count"]
    assert snapshot["canonical_asset_rows"] == matrix_summary["canonical_asset_count"]
    assert snapshot["venue_instrument_rows"] == matrix_summary["coverage_row_count"]
    assert (
        snapshot["decision_grade_canonical_asset_rows"]
        == matrix_summary["decision_grade_canonical_asset_count"]
    )
    assert (
        snapshot["manual_verification_canonical_asset_rows"]
        == matrix_summary["manual_verification_asset_count"]
    )
    assert (
        snapshot["ambiguous_source_scoped_asset_rows"]
        == matrix_summary["ambiguous_source_scoped_asset_count"]
    )
    assert (
        snapshot["rwa_xyz_identity_verified_asset_rows"]
        + snapshot["rwa_xyz_identity_unverified_asset_rows"]
        == snapshot["rwa_xyz_source_asset_rows"]
    )
    assert (
        snapshot["decision_grade_canonical_asset_rows"]
        + snapshot["manual_verification_canonical_asset_rows"]
        == snapshot["canonical_asset_rows"]
    )
    assert daily["summary"]["baseline_created"] is True
    assert daily["status"]["snapshot_reconciled"] is True
    assert snapshot["daily_comparison_state"] == "first_verified_baseline_only"


def test_public_rwa_copy_exposes_grains_and_fail_closed_freshness_boundaries() -> None:
    category_hubs = public_metadata.build_category_hubs_json()
    rwa_hub = next(hub for hub in category_hubs["hubs"] if hub["slug"] == "rwa-market-data")
    serialized = json.dumps(rwa_hub, sort_keys=True)
    llms = public_metadata.build_llms_txt()

    assert rwa_hub["source_snapshot"] == {
        **public_metadata.RWA_DISCOVERY_SNAPSHOT,
        "source": "RWA.xyz public new-asset monitor",
        "source_grain": "source_asset_and_token_listing",
        "canonical_matrix_grain": "canonical_asset_and_venue_instrument",
        "freshness_boundary": (
            "The RWA.xyz source was fetched at the stated timestamp. Other venue "
            "and derivative artifacts expose independent captured_at timestamps and "
            "are not made current by this refresh."
        ),
        "decision_boundary": (
            "Catalog presence, identity verification, canonicalization, and "
            "production promotion are separate states."
        ),
    }
    assert "second distinct verified snapshot" in rwa_hub["qualification_note"]
    assert "not production price feeds" in serialized
    assert "not live-feed readiness" in serialized
    assert "1,169" in llms
    assert "3,438" in llms
    assert "2,139" in llms
    assert "5,161" in llms
    for stale_count in ("1,025", "3,407", "1,150"):
        assert stale_count not in serialized
        assert stale_count not in llms

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "2,139 canonical assets" in readme
    assert "5,161 venue instruments" in readme
    assert "contains 1,025 canonical" not in readme


def test_public_portal_uses_canonical_repository_and_truthful_mirror_labels() -> None:
    portal = (ROOT / "docs" / "developer_portal.html").read_text(encoding="utf-8")

    assert '"codeRepository": "https://github.com/jf-cmyk/agentic-payments"' in portal
    assert "https://github.com/jf-cmyk/blocksize-agentic-payments-mcp" not in portal
    assert "Historical mirror; not an install source" in portal
    assert "Primary source repository" not in portal
    assert "Community listing (merged)" in portal


def test_public_readme_links_are_portable_and_manual_images_are_published() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/Users/" not in readme

    architecture = ROOT / "docs" / "assets" / "architecture_diagram.png"
    swimlane = ROOT / "docs" / "assets" / "swimlane_diagram.jpg"
    assert architecture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert swimlane.read_bytes().startswith(b"\xff\xd8\xff")

    manual = (ROOT / "docs" / "api_agent_manual.md").read_text(encoding="utf-8")
    assert "assets/architecture_diagram.png" in manual
    assert "assets/swimlane_diagram.jpg" in manual
