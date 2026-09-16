"""Public, credential-free discovery documents generated from service metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from src import public_metadata as meta

router = APIRouter()
SKILL_NAME = "discover-blocksize-market-data"
SKILL_DESCRIPTION = "Discover Blocksize instruments, check data readiness, and choose MCP or paid HTTP access."
SKILL_PATH = f"/.well-known/agent-skills/{SKILL_NAME}/SKILL.md"
PUBLIC_DISCOVERY_PATHS = {
    "/.well-known/api-catalog", "/.well-known/ai-catalog.json",
    "/.well-known/mcp/server-card.json", "/mcp/server/server-card",
    "/.well-known/agent-skills/index.json", SKILL_PATH, "/auth.md",
}


def public_response(request: Request, payload: dict | str, media_type: str) -> Response:
    body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) if isinstance(payload, dict)
            else payload).encode("utf-8")
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    headers = {
        "Cache-Control": "public, max-age=300", "ETag": etag,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, If-None-Match",
        "Access-Control-Expose-Headers": "ETag", "X-Robots-Tag": "index, follow",
    }
    validators = [v.strip().removeprefix("W/") for v in
                  request.headers.get("if-none-match", "").split(",")]
    if etag in validators or "*" in validators:
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(body))
    return Response(b"" if request.method == "HEAD" else body,
                    media_type=media_type, headers=headers)


def server_card() -> dict:
    # Use the canonical registry identity; primitives stay in runtime tools/list.
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
        "name": meta.OFFICIAL_REGISTRY_NAME, "title": meta.PUBLIC_DISPLAY_NAME,
        "version": meta.APP_VERSION,
        "description": "Free discovery of Blocksize market data, instruments, pricing, and integration documentation.",
        "websiteUrl": f"{meta.PUBLIC_BASE_URL}/",
        "remotes": [{"type": "streamable-http", "url": meta.REMOTE_MCP_URL}],
    }


@router.api_route("/.well-known/mcp/server-card.json", methods=["GET", "HEAD"])
@router.api_route("/mcp/server/server-card", methods=["GET", "HEAD"])
async def get_server_card(request: Request):
    return public_response(request, server_card(), "application/mcp-server-card+json")


@router.api_route("/.well-known/api-catalog", methods=["GET", "HEAD"])
async def get_api_catalog(request: Request):
    base = meta.PUBLIC_BASE_URL
    return public_response(request, {"linkset": [{
        "anchor": base,
        "service-desc": [{"href": meta.OPENAPI_URL, "type": "application/json"}],
        "service-doc": [{"href": meta.SWAGGER_URL, "type": "text/html"}],
        "status": [{"href": f"{base}/health", "type": "application/json"}],
    }]}, "application/linkset+json")


@router.api_route("/.well-known/ai-catalog.json", methods=["GET", "HEAD"])
async def get_ai_catalog(request: Request):
    base = meta.PUBLIC_BASE_URL
    domain = urlsplit(base).hostname
    entries = [
        ("mcp", "market-data", "Blocksize public MCP discovery",
         "application/mcp-server-card+json", "/mcp/server/server-card"),
        ("api", "market-data", "Blocksize market data HTTP API",
         "application/json", "/openapi.json"),
        ("skill", SKILL_NAME, "Discover and access Blocksize market data",
         "text/markdown", SKILL_PATH),
    ]
    return public_response(request, {
        "specVersion": "1.0",
        "host": {"displayName": "Blocksize", "documentationUrl": meta.QUICKSTART_URL},
        "entries": [{"identifier": f"urn:air:{domain}:{kind}:{name}",
                     "displayName": title, "type": media_type, "url": base + path}
                    for kind, name, title, media_type, path in entries],
    }, "application/ai-catalog+json")


def skill_text() -> str:
    return (Path(__file__).parent / "agent_skills" / SKILL_NAME / "SKILL.md").read_text(
        encoding="utf-8").replace("{{PUBLIC_BASE_URL}}", meta.PUBLIC_BASE_URL)


@router.api_route("/.well-known/agent-skills/index.json", methods=["GET", "HEAD"])
async def get_skills_index(request: Request):
    return public_response(request, {
        "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        "skills": [{"name": SKILL_NAME, "type": "skill-md", "description": SKILL_DESCRIPTION,
                    "url": meta.PUBLIC_BASE_URL + SKILL_PATH,
                    "digest": "sha256:" + hashlib.sha256(skill_text().encode()).hexdigest()}],
    }, "application/json")


@router.api_route("/.well-known/agent-skills/{name}/SKILL.md", methods=["GET", "HEAD"])
async def get_skill(request: Request, name: str):
    if name != SKILL_NAME:
        raise HTTPException(404, "Skill not found")
    return public_response(request, skill_text(), "text/markdown")


@router.api_route("/auth.md", methods=["GET", "HEAD"])
async def get_auth_guide(request: Request):
    base = meta.PUBLIC_BASE_URL
    text = f"""# Blocksize authentication and access

Public discovery at {meta.REMOTE_MCP_URL} requires no credentials and cannot fetch paid data.
Use {base}/openapi.json to discover HTTP operations and {base}/.well-known/x402 for payment metadata.

## Authenticated connectors

Connect with the client-specific MCP URL and follow the client's OAuth sign-in flow:

| Client | MCP URL | Protected-resource metadata |
| --- | --- | --- |
"""
    for client, path in [("Claude", "anthropic"), ("Cursor", "cursor"), ("OpenAI", "openai")]:
        text += (f"| {client} | {base}/{path}/mcp/ | "
                 f"{base}/.well-known/oauth-protected-resource/{path}/mcp/ |\n")
    text += f"""
Read the authorization server metadata advertised by the protected resource. Use its advertised
registration and authorization mechanisms; availability depends on deployment configuration.
The root OAuth metadata is a compatibility alias for the configured default connector.
Its resource identifies that connector, not the public homepage. Preserve resource/audience matching.

Eligible authenticated connector identities can receive starter credits. Public HTTP identity
headers cannot claim or spend them. Check credit balance before live calls.

## Direct HTTP payment

Paid HTTP routes use signed x402 v2 payments. An unpaid request to a paid route returns its
payment requirements. Read the current challenge for price, network, asset, and recipient.
Only authorize payment within the user's approved budget. Never put wallet secrets in requests.
There is no separate auth.md autonomous registration API. Account plans are available by agreement.

Integration guide: {meta.QUICKSTART_URL}
First live price: {meta.FIRST_PRICE_QUICKSTART_URL}
Support: {meta.SUPPORT_URL}
"""
    return public_response(request, text, "text/markdown")


def prefers_markdown(accept: str) -> bool:
    """Require explicit Markdown acceptance and honor relative quality preferences."""
    qualities = {}
    for item in accept.lower().split(","):
        media, *params = item.strip().split(";")
        quality = 1.0
        for param in params:
            if param.strip().startswith("q="):
                try:
                    quality = float(param.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[media] = quality if 0 <= quality <= 1 else 0.0
    markdown = qualities.get("text/markdown", 0)
    html = qualities.get("text/html", qualities.get("text/*", qualities.get("*/*", 0)))
    return markdown > 0 and markdown >= html
