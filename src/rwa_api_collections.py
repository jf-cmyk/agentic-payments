"""Deterministic collection paging for public RWA API responses."""

from __future__ import annotations

from typing import Any, Sequence


def paginate_rows(
    rows: Sequence[dict[str, Any]],
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a stable slice plus reconciliation metadata."""
    if limit < 1:
        raise ValueError("limit must be greater than or equal to 1")
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")
    total = len(rows)
    page = list(rows[offset : offset + limit])
    next_offset = offset + len(page)
    has_more = next_offset < total
    return page, {
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }
