"""Resolve source-checkout and installed-wheel runtime data without CWD fallback."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import sysconfig
import tomllib
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.public_metadata import APP_VERSION


DISTRIBUTION_NAME = "blocksize-mcp-x402"
PACKAGE_DATA_ANCHOR = "share/blocksize-mcp/server.json"
RWA_REPORTS_OVERRIDE_ENV = "RWA_REPORTS_DIR"
REQUIRED_RWA_REPORT_FILENAMES = (
    "hyperliquid_tradeable_feeds.json",
    "rwa_blocksize_state_discovery.json",
    "rwa_daily_feed_agent.json",
    "rwa_derivative_venue_discovery.json",
    "rwa_evm_pool_allowlist.json",
    "rwa_hyperliquid_paxg_probe.json",
    "rwa_jupiter_route_allowlist.json",
    "rwa_rights_clearance.json",
    "rwa_solana_pool_allowlist.json",
    "rwa_solana_token_mints.json",
    "rwa_xyz_new_asset_monitor.json",
)
RWA_REPORT_OVERRIDE_ENVS: Mapping[str, str | None] = MappingProxyType(
    {
        "hyperliquid_tradeable_feeds.json": None,
        "rwa_blocksize_state_discovery.json": None,
        "rwa_daily_feed_agent.json": None,
        "rwa_derivative_venue_discovery.json": None,
        "rwa_evm_pool_allowlist.json": "RWA_EVM_POOL_ALLOWLIST_PATH",
        "rwa_hyperliquid_paxg_probe.json": None,
        "rwa_jupiter_route_allowlist.json": "RWA_JUPITER_ROUTE_ALLOWLIST_PATH",
        "rwa_rights_clearance.json": "RWA_RIGHTS_CLEARANCE_PATH",
        "rwa_solana_pool_allowlist.json": "RWA_SOLANA_POOL_ALLOWLIST_PATH",
        "rwa_solana_token_mints.json": "RWA_SOLANA_TOKEN_MINTS_PATH",
        "rwa_xyz_new_asset_monitor.json": None,
    }
)
MAX_RWA_REPORT_BYTES = 64 * 1024 * 1024
SOLANA_TOKEN_LOADER_STATUSES = frozenset({"resolved", "verified", "configured"})


class RWAReportRowValueKind(Enum):
    """Supported primitive constraints for runtime-consumed report rows."""

    NONEMPTY_STRING = "nonempty_string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class RWAReportRowFieldRule:
    kind: RWAReportRowValueKind
    minimum: int | float | None = None
    maximum: int | float | None = None
    minimum_exclusive: bool = False
    allowed_values: frozenset[str] | None = None
    normalize_string_for_allowed_values: bool = False


@dataclass(frozen=True)
class RWAReportRowContract:
    fields: Mapping[str, RWAReportRowFieldRule]


@dataclass(frozen=True)
class RWAReportRowValueCountContract:
    rows_field: str
    value_field: str
    counts_field: str
    selected_count_fields: Mapping[str, str]
    normalize_strings: bool = False


ROW_TEXT = RWAReportRowFieldRule(RWAReportRowValueKind.NONEMPTY_STRING)
ROW_BOOLEAN = RWAReportRowFieldRule(RWAReportRowValueKind.BOOLEAN)
ROW_POSITIVE_INTEGER = RWAReportRowFieldRule(
    RWAReportRowValueKind.INTEGER,
    minimum=1,
)
ROW_NONNEGATIVE_NUMBER = RWAReportRowFieldRule(
    RWAReportRowValueKind.NUMBER,
    minimum=0,
)
ROW_POSITIVE_NUMBER = RWAReportRowFieldRule(
    RWAReportRowValueKind.NUMBER,
    minimum=0,
    minimum_exclusive=True,
)
ROW_TOKEN_DECIMALS = RWAReportRowFieldRule(
    RWAReportRowValueKind.INTEGER,
    minimum=0,
    maximum=255,
)
ROW_SOLANA_TOKEN_STATUS = RWAReportRowFieldRule(
    RWAReportRowValueKind.NONEMPTY_STRING,
    allowed_values=SOLANA_TOKEN_LOADER_STATUSES,
    normalize_string_for_allowed_values=True,
)


def _row_contract(
    fields: Mapping[str, RWAReportRowFieldRule],
) -> RWAReportRowContract:
    return RWAReportRowContract(fields=MappingProxyType(dict(fields)))


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = Path(__file__).resolve()


def _record_hash_matches(path: Path, hash_spec: str, size_text: str) -> bool:
    """Verify an installed data file against its wheel RECORD entry."""
    if not path.is_file() or not hash_spec.startswith("sha256="):
        return False
    try:
        expected_size = int(size_text)
        payload = path.read_bytes()
    except (OSError, ValueError):
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return len(payload) == expected_size and digest.decode("ascii") == hash_spec[7:]


def _installed_distribution_layout() -> tuple[bool, Path | None]:
    """Locate this module's signed wheel data across prefix/target/user schemes."""
    try:
        package_distribution = distribution(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return False, None
    recorded_module = Path(package_distribution.locate_file("src/runtime_data.py")).resolve(
        strict=False
    )
    if recorded_module != _MODULE_PATH:
        return False, None

    record_text = package_distribution.read_text("RECORD") or ""
    anchor_record = next(
        (
            row
            for row in csv.reader(io.StringIO(record_text))
            if len(row) >= 3 and row[0].replace("\\", "/").endswith(PACKAGE_DATA_ANCHOR)
        ),
        None,
    )
    distribution_root = Path(package_distribution.locate_file("")).resolve(strict=False)
    if anchor_record is None:
        return True, distribution_root / ".blocksize-mcp-package-data-missing"

    record_path, hash_spec, size_text = anchor_record[:3]
    candidates = [
        Path(package_distribution.locate_file(record_path)).resolve(strict=False),
        distribution_root / PACKAGE_DATA_ANCHOR,
        (Path(sysconfig.get_path("data")) / PACKAGE_DATA_ANCHOR).resolve(strict=False),
    ]
    for candidate in dict.fromkeys(candidates):
        if _record_hash_matches(candidate, hash_spec, size_text):
            return True, candidate.parent
    return True, distribution_root / ".blocksize-mcp-package-data-invalid"


def _validated_source_checkout() -> bool:
    """Require this module to belong to a version-matched source checkout."""
    manifest_path = PROJECT_ROOT / "pyproject.toml"
    if (PROJECT_ROOT / "src" / "runtime_data.py").resolve() != _MODULE_PATH:
        return False
    try:
        project = tomllib.loads(manifest_path.read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return False
    return project.get("name") == DISTRIBUTION_NAME and project.get("version") == APP_VERSION


_DISTRIBUTION_INSTALLED, _DISTRIBUTION_DATA_ROOT = _installed_distribution_layout()
SOURCE_CHECKOUT = not _DISTRIBUTION_INSTALLED and _validated_source_checkout()
INSTALLED_DISTRIBUTION = not SOURCE_CHECKOUT
PACKAGED_DATA_ROOT = (
    _DISTRIBUTION_DATA_ROOT
    if _DISTRIBUTION_DATA_ROOT is not None
    else (
        _MODULE_PATH.parent.parent / ".blocksize-mcp-package-data-missing"
        if INSTALLED_DISTRIBUTION
        else (Path(sysconfig.get_path("data")) / "share" / "blocksize-mcp").resolve(strict=False)
    )
)


def resolve_data_directory(source_name: str, *, override_env: str | None = None) -> Path:
    """Resolve an immutable data directory; an explicit override never falls back."""
    if override_env:
        configured = os.environ.get(override_env, "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
    if SOURCE_CHECKOUT:
        return (PROJECT_ROOT / source_name).resolve()
    return (PACKAGED_DATA_ROOT / source_name).resolve()


def resolve_rwa_reports_dir() -> Path:
    """Resolve the canonical RWA report directory for this runtime layout."""
    return resolve_data_directory("reports", override_env=RWA_REPORTS_OVERRIDE_ENV)


RWA_REPORTS_DIR = resolve_rwa_reports_dir()


def resolve_rwa_report_path(
    filename: str,
    *,
    override_env: str | None = None,
) -> Path:
    """Resolve one report, honoring a file-specific operator override when set."""
    if override_env:
        configured = os.environ.get(override_env, "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("RWA report filename must stay within the report directory")
    if relative.parts[:1] == ("reports",):
        relative = Path(*relative.parts[1:])
    return (RWA_REPORTS_DIR / relative).resolve()


def effective_rwa_report_paths(
    *,
    reports_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Return the exact required report paths used by runtime readers.

    File-specific operator overrides are authoritative. ``reports_dir`` is an
    injectable base used by readiness tests and explicit runtime layouts; it
    never suppresses a configured file-specific override.
    """
    base = Path(reports_dir).expanduser().resolve() if reports_dir is not None else RWA_REPORTS_DIR
    paths: dict[str, Path] = {}
    for filename in REQUIRED_RWA_REPORT_FILENAMES:
        override_env = RWA_REPORT_OVERRIDE_ENVS[filename]
        configured = os.environ.get(override_env, "").strip() if override_env else ""
        paths[filename] = (
            Path(configured).expanduser().resolve() if configured else (base / filename).resolve()
        )
    return paths


def resolve_required_rwa_report_path(filename: str) -> Path:
    """Resolve one required report from the centralized effective-path map."""
    if filename not in RWA_REPORT_OVERRIDE_ENVS:
        raise ValueError("unknown required RWA report")
    return effective_rwa_report_paths()[filename]


def _nested_value(payload: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


_RWA_REPORT_CONTRACTS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "hyperliquid_tradeable_feeds.json": {
            "product": "hyperliquid_tradeable_feed_discovery",
            "objects": ("summary",),
            "lists": ("perp_markets", "spot_pairs", "coverage_rows"),
            "nonempty_lists": ("perp_markets", "spot_pairs", "coverage_rows"),
            "positive_numbers": (
                "summary.perp_market_count",
                "summary.spot_pair_count",
                "summary.coverage_row_count",
            ),
            "count_matches": (
                ("summary.perp_market_count", "perp_markets"),
                ("summary.spot_pair_count", "spot_pairs"),
                ("summary.coverage_row_count", "coverage_rows"),
            ),
            "row_contracts": {
                "perp_markets": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
                "spot_pairs": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
                "coverage_rows": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
            },
        },
        "rwa_blocksize_state_discovery.json": {
            "product": "rwa_blocksize_state_discovery",
            "objects": ("summary",),
            "lists": ("symbols",),
            "nonempty_lists": ("symbols",),
            "positive_numbers": ("summary.target_count",),
            "count_matches": (("summary.target_count", "symbols"),),
            "row_contracts": {
                "symbols": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "state_symbol", "status")}
                ),
            },
        },
        "rwa_daily_feed_agent.json": {
            "product": "rwa_daily_feed_discovery_agent",
            "objects": ("summary", "status", "source", "source_snapshot"),
            "lists": (
                "new_assets",
                "new_tokens",
                "removed_assets",
                "removed_tokens",
                "sourcing_actions",
            ),
            "positive_numbers": (
                "summary.current_asset_count",
                "summary.current_token_count",
                "summary.current_coverage_row_count",
            ),
            "exact": (
                ("status.acceptance", "passed"),
                ("status.decision_usable", True),
                ("status.snapshot_reconciled", True),
            ),
            "equal_fields": (
                ("summary.current_asset_count", "source_snapshot.asset_count"),
                ("summary.current_token_count", "source_snapshot.token_count"),
                (
                    "summary.current_coverage_row_count",
                    "source_snapshot.coverage_row_count",
                ),
            ),
            "count_matches": (
                ("summary.new_asset_count", "new_assets"),
                ("summary.new_token_count", "new_tokens"),
                ("summary.removed_asset_count", "removed_assets"),
                ("summary.removed_token_count", "removed_tokens"),
                ("summary.new_token_count", "sourcing_actions"),
            ),
            "row_contracts": {
                "new_assets": _row_contract({"asset_id": ROW_TEXT, "symbol": ROW_TEXT}),
                "new_tokens": _row_contract(
                    {
                        field: ROW_TEXT
                        for field in ("token_row_id", "asset_id", "network", "address")
                    }
                ),
                "removed_assets": _row_contract({"asset_id": ROW_TEXT, "symbol": ROW_TEXT}),
                "removed_tokens": _row_contract(
                    {
                        field: ROW_TEXT
                        for field in ("token_row_id", "asset_id", "network", "address")
                    }
                ),
                "sourcing_actions": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "lane", "priority")}
                ),
            },
            "persisted_reference": "source.current_report",
        },
        "rwa_derivative_venue_discovery.json": {
            "product": "rwa_derivative_venue_discovery",
            "objects": ("summary",),
            "lists": ("venues", "market_rows", "coverage_rows"),
            "nonempty_lists": ("venues", "market_rows", "coverage_rows"),
            "positive_numbers": (
                "summary.venue_count",
                "summary.market_row_count",
                "summary.coverage_row_count",
            ),
            "count_matches": (
                ("summary.venue_count", "venues"),
                ("summary.market_row_count", "market_rows"),
                ("summary.coverage_row_count", "coverage_rows"),
            ),
            "row_contracts": {
                "venues": _row_contract(
                    {field: ROW_TEXT for field in ("venue_id", "name", "discovery_status")}
                ),
                "market_rows": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
                "coverage_rows": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
            },
        },
        "rwa_evm_pool_allowlist.json": {
            "product": "rwa_evm_pool_allowlist",
            "objects": ("summary",),
            "lists": ("pools",),
            "nonempty_lists": ("pools",),
            "positive_numbers": ("summary.pool_count",),
            "count_matches": (("summary.pool_count", "pools"),),
            "row_contracts": {
                "pools": _row_contract(
                    {
                        **{
                            field: ROW_TEXT
                            for field in ("allowlist_id", "symbol", "venue", "chain", "pool_id")
                        },
                        "block_number": ROW_POSITIVE_INTEGER,
                    }
                ),
            },
        },
        "rwa_hyperliquid_paxg_probe.json": {
            "product": "rwa_hyperliquid_paxg_live_probe",
            "objects": ("result", "result.summary", "result.quality"),
            "lists": ("result.results", "result.quality.observations"),
            "nonempty_lists": ("result.results", "result.quality.observations"),
            "positive_numbers": (
                "result.summary.jobs_selected",
                "result.summary.jobs_succeeded",
                "result.summary.observations",
            ),
            "count_matches": (
                ("result.summary.jobs_selected", "result.results"),
                ("result.summary.observations", "result.quality.observations"),
            ),
            "row_contracts": {
                "result.results": _row_contract(
                    {
                        **{
                            field: ROW_TEXT
                            for field in (
                                "status",
                                "job.job_id",
                                "job.symbol",
                                "job.venue",
                                "bidask.symbol",
                                "bidask.venue",
                                "block_vwap.status",
                            )
                        },
                        "block_vwap.block_size_usd": ROW_POSITIVE_NUMBER,
                    }
                ),
                "result.quality.observations": _row_contract(
                    {
                        **{
                            field: ROW_TEXT
                            for field in ("symbol", "venue", "status", "source_type")
                        },
                        "age_ms": ROW_NONNEGATIVE_NUMBER,
                        "usable_for_realtime": ROW_BOOLEAN,
                    }
                ),
            },
        },
        "rwa_jupiter_route_allowlist.json": {
            "product": "rwa_jupiter_route_allowlist",
            "objects": ("summary",),
            "lists": ("routes",),
            "nonempty_lists": ("routes",),
            "positive_numbers": ("summary.route_count",),
            "count_matches": (("summary.route_count", "routes"),),
            "row_contracts": {
                "routes": _row_contract(
                    {field: ROW_TEXT for field in ("allowlist_id", "symbol", "venue", "status")}
                ),
            },
        },
        "rwa_rights_clearance.json": {
            "objects": ("scope", "policy_acknowledgements"),
            "lists": (
                "scope.allowed_uses",
                "scope.registered_source_scope",
                "still_requires_technical_gates",
            ),
            "nonempty_lists": (
                "scope.allowed_uses",
                "scope.registered_source_scope",
                "still_requires_technical_gates",
            ),
            "nonempty_objects": ("policy_acknowledgements",),
            "exact": (("rights_cleared", True),),
            "string_rows": (
                "scope.allowed_uses",
                "scope.registered_source_scope",
                "still_requires_technical_gates",
            ),
        },
        "rwa_solana_pool_allowlist.json": {
            "product": "rwa_solana_pool_allowlist",
            "objects": ("summary",),
            "lists": ("pools",),
            "nonempty_lists": ("pools",),
            "positive_numbers": ("summary.pool_count",),
            "count_matches": (("summary.pool_count", "pools"),),
            "row_contracts": {
                "pools": _row_contract(
                    {
                        **{
                            field: ROW_TEXT
                            for field in ("allowlist_id", "symbol", "venue", "chain", "pool_id")
                        },
                        "slot": ROW_POSITIVE_INTEGER,
                    }
                ),
            },
        },
        "rwa_solana_token_mints.json": {
            "product": "rwa_solana_token_mint_registry",
            "objects": ("summary", "summary.by_status"),
            "lists": ("tokens",),
            "nonempty_lists": ("tokens",),
            "positive_numbers": ("summary.token_count",),
            "count_matches": (("summary.token_count", "tokens"),),
            "row_contracts": {
                "tokens": _row_contract(
                    {
                        "token_key": ROW_TEXT,
                        "mint": ROW_TEXT,
                        "decimals": ROW_TOKEN_DECIMALS,
                        "status": ROW_SOLANA_TOKEN_STATUS,
                    }
                ),
            },
            "row_value_counts": (
                RWAReportRowValueCountContract(
                    rows_field="tokens",
                    value_field="status",
                    counts_field="summary.by_status",
                    selected_count_fields=MappingProxyType({"resolved": "summary.resolved"}),
                    normalize_strings=True,
                ),
            ),
        },
        "rwa_xyz_new_asset_monitor.json": {
            "objects": ("summary", "source"),
            "lists": ("asset_rows", "token_rows", "coverage_rows"),
            "nonempty_lists": ("asset_rows", "token_rows", "coverage_rows"),
            "positive_numbers": (
                "summary.asset_count",
                "summary.token_count",
                "summary.coverage_row_count",
            ),
            "count_matches": (
                ("summary.asset_count", "asset_rows"),
                ("summary.token_count", "token_rows"),
                ("summary.coverage_row_count", "coverage_rows"),
            ),
            "row_contracts": {
                "asset_rows": _row_contract(
                    {
                        field: ROW_TEXT
                        for field in (
                            "rwa_xyz_asset_id",
                            "asset_id",
                            "symbol",
                            "asset_class",
                        )
                    }
                ),
                "token_rows": _row_contract(
                    {
                        field: ROW_TEXT
                        for field in ("token_row_id", "asset_id", "network", "address")
                    }
                ),
                "coverage_rows": _row_contract(
                    {field: ROW_TEXT for field in ("asset_id", "symbol", "venue", "source_type")}
                ),
            },
        },
    }
)


def _row_value_matches_rule(value: Any, rule: RWAReportRowFieldRule) -> bool:
    if rule.kind is RWAReportRowValueKind.NONEMPTY_STRING:
        if not isinstance(value, str) or not value.strip():
            return False
        if rule.allowed_values is None:
            return True
        comparison_value = (
            value.strip().lower() if rule.normalize_string_for_allowed_values else value
        )
        return comparison_value in rule.allowed_values
    if rule.kind is RWAReportRowValueKind.BOOLEAN:
        return isinstance(value, bool)
    if rule.kind is RWAReportRowValueKind.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        numeric_value: int | float = value
    elif rule.kind is RWAReportRowValueKind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
        numeric_value = value
    else:
        return False
    if rule.minimum is not None:
        if rule.minimum_exclusive and numeric_value <= rule.minimum:
            return False
        if not rule.minimum_exclusive and numeric_value < rule.minimum:
            return False
    return rule.maximum is None or numeric_value <= rule.maximum


def _row_matches_contract(row: Any, contract: RWAReportRowContract) -> bool:
    return isinstance(row, Mapping) and all(
        _row_value_matches_rule(_nested_value(row, field), rule)
        for field, rule in contract.fields.items()
    )


def validate_rwa_report_payload(filename: str, payload: Any) -> tuple[str, ...]:
    """Validate one required report against stable, non-secret invariants."""
    contract = _RWA_REPORT_CONTRACTS.get(filename)
    if contract is None:
        return ("unknown_report",)
    if not isinstance(payload, Mapping):
        return ("root_not_object",)

    errors: set[str] = set()
    expected_product = contract.get("product")
    if expected_product is not None and payload.get("product") != expected_product:
        errors.add("schema_invalid")
    for field in contract.get("objects", ()):
        if not isinstance(_nested_value(payload, field), Mapping):
            errors.add("schema_invalid")
    for field in contract.get("lists", ()):
        if not isinstance(_nested_value(payload, field), list):
            errors.add("schema_invalid")
    for field in contract.get("nonempty_lists", ()):
        value = _nested_value(payload, field)
        if not isinstance(value, list) or not value:
            errors.add("structurally_empty")
    for field in contract.get("nonempty_objects", ()):
        value = _nested_value(payload, field)
        if not isinstance(value, Mapping) or not value:
            errors.add("structurally_empty")
    for field in contract.get("positive_numbers", ()):
        value = _nested_value(payload, field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            errors.add("structurally_empty")
    for field, expected in contract.get("exact", ()):
        if _nested_value(payload, field) != expected:
            errors.add("schema_invalid")
    for left, right in contract.get("equal_fields", ()):
        if _nested_value(payload, left) != _nested_value(payload, right):
            errors.add("count_mismatch")
    for count_field, rows_field in contract.get("count_matches", ()):
        rows = _nested_value(payload, rows_field)
        count = _nested_value(payload, count_field)
        if not isinstance(rows, list) or isinstance(count, bool) or count != len(rows):
            errors.add("count_mismatch")
    for rows_field, row_contract in contract.get("row_contracts", {}).items():
        rows = _nested_value(payload, rows_field)
        if not isinstance(rows, list):
            errors.add("schema_invalid")
            continue
        for row in rows:
            if not _row_matches_contract(row, row_contract):
                errors.add("row_invalid")
    for value_count_contract in contract.get("row_value_counts", ()):
        rows = _nested_value(payload, value_count_contract.rows_field)
        reported_counts = _nested_value(payload, value_count_contract.counts_field)
        if not isinstance(rows, list) or not isinstance(reported_counts, Mapping):
            errors.add("count_mismatch")
            continue
        actual_counts: dict[str, int] = {}
        categories_valid = True
        for row in rows:
            category = (
                _nested_value(row, value_count_contract.value_field)
                if isinstance(row, Mapping)
                else None
            )
            if not isinstance(category, str) or not category.strip():
                categories_valid = False
                continue
            normalized_category = (
                category.strip().lower() if value_count_contract.normalize_strings else category
            )
            actual_counts[normalized_category] = actual_counts.get(normalized_category, 0) + 1
        reported_counts_valid = all(
            isinstance(category, str)
            and category
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for category, count in reported_counts.items()
        )
        if (
            not categories_valid
            or not reported_counts_valid
            or dict(reported_counts) != actual_counts
        ):
            errors.add("count_mismatch")
        for category, count_field in value_count_contract.selected_count_fields.items():
            selected_count = _nested_value(payload, count_field)
            if (
                not isinstance(selected_count, int)
                or isinstance(selected_count, bool)
                or selected_count != actual_counts.get(category, 0)
            ):
                errors.add("count_mismatch")
    for rows_field in contract.get("string_rows", ()):
        rows = _nested_value(payload, rows_field)
        if not isinstance(rows, list) or any(
            not isinstance(row, str) or not row.strip() for row in rows
        ):
            errors.add("row_invalid")

    reference_field = contract.get("persisted_reference")
    if reference_field:
        reference = _nested_value(payload, str(reference_field))
        if not isinstance(reference, str) or not reference.strip():
            errors.add("schema_invalid")
        else:
            reference_path = Path(reference)
            normalized_parts = (
                reference_path.parts[1:]
                if reference_path.parts[:1] == ("reports",)
                else reference_path.parts
            )
            if reference_path.is_absolute() or ".." in normalized_parts or not normalized_parts:
                errors.add("unsafe_persisted_reference")
    return tuple(sorted(errors))


@lru_cache(maxsize=64)
def _cached_rwa_report_integrity(
    filename: str,
    path_text: str,
    _device: int,
    _inode: int,
    _size: int,
    _mtime_ns: int,
    _ctime_ns: int,
) -> tuple[str, ...]:
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ("invalid_json",)
    return validate_rwa_report_payload(filename, payload)


def inspect_required_rwa_report(filename: str, path: Path) -> tuple[str, ...]:
    """Return secret-safe integrity failures, caching by path and file stat."""
    if filename not in RWA_REPORT_OVERRIDE_ENVS:
        return ("unknown_report",)
    try:
        stat = path.stat()
    except OSError:
        return ("missing",)
    if not path.is_file():
        return ("not_regular_file",)
    if stat.st_size <= 0:
        return ("structurally_empty",)
    if stat.st_size > MAX_RWA_REPORT_BYTES:
        return ("report_too_large",)
    return _cached_rwa_report_integrity(
        filename,
        str(path),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@lru_cache(maxsize=32)
def _cached_daily_xyz_reconciliation(
    daily_path_text: str,
    _daily_device: int,
    _daily_inode: int,
    _daily_size: int,
    _daily_mtime_ns: int,
    _daily_ctime_ns: int,
    xyz_path_text: str,
    _xyz_device: int,
    _xyz_inode: int,
    _xyz_size: int,
    _xyz_mtime_ns: int,
    _xyz_ctime_ns: int,
    reports_root_text: str,
) -> tuple[str, ...]:
    try:
        daily_payload = json.loads(Path(daily_path_text).read_text(encoding="utf-8"))
        xyz_payload = json.loads(Path(xyz_path_text).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ("cross_report_unreadable",)
    if not isinstance(daily_payload, dict) or not isinstance(xyz_payload, dict):
        return ("cross_report_schema_invalid",)

    source = daily_payload.get("source") if isinstance(daily_payload.get("source"), dict) else {}
    try:
        persisted_target = resolve_rwa_report_reference(
            source.get("current_report"),
            default_filename="rwa_xyz_new_asset_monitor.json",
            reports_dir=reports_root_text,
        )
    except ValueError:
        return ("daily_source_target_invalid",)
    if persisted_target != Path(xyz_path_text).resolve():
        return ("daily_source_target_mismatch",)

    # Imported lazily to avoid a module cycle: the daily agent itself uses this
    # runtime-data module for its immutable paths.
    from src.rwa_daily_feed_agent import validate_daily_feed_agent_report

    if validate_daily_feed_agent_report(
        daily_payload,
        current_report=xyz_payload,
    ):
        return ("daily_snapshot_mismatch",)
    return ()


def inspect_daily_xyz_reconciliation(
    daily_path: Path,
    xyz_path: Path,
    *,
    reports_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Validate the daily agent against its exact effective RWA.xyz snapshot."""
    try:
        daily_stat = daily_path.stat()
        xyz_stat = xyz_path.stat()
    except OSError:
        return ("cross_report_unavailable",)
    reports_root = (
        Path(reports_dir).expanduser().resolve()
        if reports_dir is not None
        else RWA_REPORTS_DIR.resolve()
    )
    return _cached_daily_xyz_reconciliation(
        str(daily_path.resolve()),
        daily_stat.st_dev,
        daily_stat.st_ino,
        daily_stat.st_size,
        daily_stat.st_mtime_ns,
        daily_stat.st_ctime_ns,
        str(xyz_path.resolve()),
        xyz_stat.st_dev,
        xyz_stat.st_ino,
        xyz_stat.st_size,
        xyz_stat.st_mtime_ns,
        xyz_stat.st_ctime_ns,
        str(reports_root),
    )


def resolve_rwa_report_reference(
    value: str | os.PathLike[str] | None,
    *,
    default_filename: str,
    reports_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a persisted report reference independently of the process CWD."""
    reports_root = (
        Path(reports_dir).expanduser().resolve()
        if reports_dir is not None
        else RWA_REPORTS_DIR.resolve()
    )
    if value in {None, ""}:
        relative_default = Path(default_filename)
        if relative_default.is_absolute() or ".." in relative_default.parts:
            raise ValueError("default RWA report reference must stay relative")
        return (reports_root / relative_default).resolve()
    path = Path(value).expanduser()
    if path.is_absolute():
        raise ValueError("persisted RWA report reference must be relative")
    if ".." in path.parts:
        raise ValueError("RWA report reference must not escape the report directory")
    if path.parts[:1] == ("reports",):
        path = Path(*path.parts[1:])
    resolved = (reports_root / path).resolve()
    if not resolved.is_relative_to(reports_root):
        raise ValueError("RWA report reference must stay within the report directory")
    return resolved


def persisted_rwa_report_reference(
    value: str | os.PathLike[str],
) -> str | None:
    """Return a safe relative reference, omitting trusted external inputs."""
    path = Path(value).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
        reports_root = RWA_REPORTS_DIR.resolve()
        if not resolved.is_relative_to(reports_root):
            return None
        path = resolved.relative_to(reports_root)
    elif path.parts[:1] == ("reports",):
        path = Path(*path.parts[1:])
    if not path.parts or ".." in path.parts:
        return None
    return path.as_posix()
