"""Bounded request models for operator-only RWA collection workflows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.rwa_security import validate_component_size, validate_json_shape


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("source timestamp must be ISO 8601") from exc
    else:
        raise ValueError("source timestamp is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("source timestamp is too far in the future")
    return parsed


class RWAObservationEnvelope(BaseModel):
    """One bounded, replayable observation submitted by an authenticated operator."""

    model_config = ConfigDict(extra="forbid")

    raw_payload: dict[str, Any]
    normalized_observation: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    realtime_quality: dict[str, Any] = Field(default_factory=dict)
    blocksize_benchmark: dict[str, Any] = Field(default_factory=dict)
    promotion: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    symbol: str | None = Field(None, min_length=1, max_length=64)
    venue: str | None = Field(None, min_length=1, max_length=64)
    asset_class: str | None = Field(None, max_length=64)
    source_type: str | None = Field(None, max_length=64)
    timestamp: datetime | None = None
    idempotency_key: str | None = Field(
        None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "RWAObservationEnvelope":
        normalized = self.normalized_observation or self.observation
        if not isinstance(normalized, dict) or not normalized:
            raise ValueError("normalized_observation or observation is required")
        components = {
            "raw_payload": self.raw_payload,
            "normalized_observation": normalized,
            "realtime_quality": self.realtime_quality,
            "blocksize_benchmark": self.blocksize_benchmark,
            "promotion": self.promotion,
            "metadata": self.metadata,
        }
        for name, value in components.items():
            validate_json_shape(value, path=name)
            validate_component_size(name, value)
        source_timestamp = (
            self.timestamp
            or normalized.get("timestamp")
            or self.raw_payload.get("timestamp")
        )
        _parse_timestamp(source_timestamp)
        return self

    def as_store_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if self.normalized_observation is None and self.observation is not None:
            payload["normalized_observation"] = payload.pop("observation")
        return payload


class RWAProbeRequest(BaseModel):
    """Bounded adapter-probe controls accepted from an authenticated operator."""

    model_config = ConfigDict(extra="forbid")

    include_completed_targets: bool = False
    venues: list[str] = Field(default_factory=list, max_length=10)
    symbols: list[str] = Field(default_factory=list, max_length=10)
    job_ids: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(5, ge=1, le=10)
    include_order_book: bool = True
    persist: bool = False
    block_size_usd: float = Field(10_000, gt=0, le=100_000_000, allow_inf_nan=False)
    side: Literal["buy", "sell"] = "buy"
    depth: int = Field(100, ge=1, le=200)

    @model_validator(mode="after")
    def validate_filters(self) -> "RWAProbeRequest":
        for field_name in ("venues", "symbols", "job_ids"):
            values = getattr(self, field_name)
            if any(not value.strip() or len(value) > 128 for value in values):
                raise ValueError(f"{field_name} contains an invalid value")
        return self

    def as_probe_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python")
