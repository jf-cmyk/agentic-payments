"""Machine-readable RWA rights-clearance evidence.

This module deliberately stores acknowledgement metadata, not credentials or
license text. The legal/commercial artifact is used only to clear the rights
gate; feed promotion still requires replay, liquidity, freshness, manipulation,
and benchmark evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.runtime_data import resolve_required_rwa_report_path


DEFAULT_RIGHTS_CLEARANCE_PATH = resolve_required_rwa_report_path(
    "rwa_rights_clearance.json"
)
RIGHTS_CLEARANCE_PATH_ENV = "RWA_RIGHTS_CLEARANCE_PATH"
ACK_VALUES = {"1", "true", "yes", "y", "ack", "approved", "signed", "cleared"}


def rights_clearance_path() -> Path:
    """Return the configured rights-clearance evidence path."""
    return resolve_required_rwa_report_path("rwa_rights_clearance.json")


def load_rights_clearance(path: str | Path | None = None) -> dict[str, Any]:
    """Load rights-clearance evidence, returning an empty dict when absent."""
    target = Path(path).expanduser() if path else rights_clearance_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ACK_VALUES


def env_or_clearance_ack(name: str, *, clearance: dict[str, Any] | None = None) -> bool:
    """Return whether an acknowledgement env var is set or cleared in evidence."""
    if _truthy(os.getenv(name)):
        return True
    evidence = clearance if clearance is not None else load_rights_clearance()
    acknowledgements = evidence.get("policy_acknowledgements")
    if isinstance(acknowledgements, dict) and _truthy(acknowledgements.get(name)):
        return True
    global_ack = evidence.get("rights_cleared")
    applies_to_all = _truthy(evidence.get("applies_to_all_registered_sources"))
    return applies_to_all and _truthy(global_ack)


def rights_cleared_for_venue(venue: str, *, clearance: dict[str, Any] | None = None) -> bool:
    """Return whether production redistribution rights are cleared for a venue."""
    evidence = clearance if clearance is not None else load_rights_clearance()
    if not evidence:
        return False
    venue_key = str(venue or "").strip()
    venue_overrides = evidence.get("venue_overrides")
    if isinstance(venue_overrides, dict) and venue_key in venue_overrides:
        override = venue_overrides.get(venue_key)
        if isinstance(override, dict) and "rights_cleared" in override:
            return _truthy(override.get("rights_cleared"))
        return _truthy(override)
    return _truthy(evidence.get("rights_cleared")) and _truthy(
        evidence.get("applies_to_all_registered_sources", True)
    )


def rights_clearance_summary(clearance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a secret-safe summary for API and report evidence blocks."""
    evidence = clearance if clearance is not None else load_rights_clearance()
    acknowledgements = evidence.get("policy_acknowledgements")
    ack_names = sorted(acknowledgements) if isinstance(acknowledgements, dict) else []
    return {
        "path": str(rights_clearance_path()),
        "artifact_present": bool(evidence),
        "rights_cleared": _truthy(evidence.get("rights_cleared")),
        "applies_to_all_registered_sources": _truthy(
            evidence.get("applies_to_all_registered_sources", True)
        ),
        "clearance_id": evidence.get("clearance_id"),
        "cleared_at": evidence.get("cleared_at"),
        "cleared_by": evidence.get("cleared_by"),
        "acknowledgement_count": len(ack_names),
        "acknowledgement_names": ack_names,
        "not_legal_advice": True,
    }
