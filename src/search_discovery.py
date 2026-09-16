"""Search-engine ownership proof, disabled unless explicitly configured."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter()


def indexnow_key() -> str:
    key = os.environ.get("INDEXNOW_KEY", "").strip()
    return key if re.fullmatch(r"[A-Za-z0-9-]{8,128}", key) else ""


@router.api_route("/indexnow-key.txt", methods=["GET", "HEAD"])
async def get_indexnow_key(request: Request) -> Response:
    key = indexnow_key()
    if not key:
        raise HTTPException(404, "Not found")
    return Response(
        key if request.method == "GET" else b"",
        media_type="text/plain",
        headers={"Content-Length": str(len(key)), "Cache-Control": "public, max-age=300",
                 "X-Robots-Tag": "noindex, nofollow"},
    )
