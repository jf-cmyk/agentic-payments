"""HTTP contracts for agent discovery; no upstream requests or payments."""
import hashlib
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from src import agent_discovery as discovery
from src import public_metadata as meta
from src.resource_server import app


@pytest.fixture
def client():
    instance = TestClient(app)
    yield instance
    instance.close()


@pytest.mark.parametrize("path", sorted(discovery.PUBLIC_DISCOVERY_PATHS))
def test_public_discovery_get_head_and_cache(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-robots-tag"] == "index, follow"
    assert "public" in response.headers["cache-control"]
    head = client.head(path)
    assert head.status_code == 200 and not head.content
    assert head.headers["etag"] == response.headers["etag"]
    assert head.headers["content-length"] == str(len(response.content))
    assert client.get(path, headers={"If-None-Match": "W/" + response.headers["etag"]}).status_code == 304


def test_catalog_references_resolve_and_skill_hash_matches(client):
    catalog = client.get("/.well-known/ai-catalog.json").json()
    assert catalog["specVersion"] == "1.0"
    for entry in catalog["entries"]:
        assert ("url" in entry) != ("data" in entry)
        result = client.get(urlsplit(entry["url"]).path)
        assert result.status_code == 200
        assert result.headers["content-type"].split(";")[0] == entry["type"]
    index = client.get("/.well-known/agent-skills/index.json").json()
    for skill in index["skills"]:
        result = client.get(urlsplit(skill["url"]).path)
        assert skill["digest"] == "sha256:" + hashlib.sha256(result.content).hexdigest()
        assert "{{PUBLIC_BASE_URL}}" not in result.text
        assert skill["description"] in result.text
    assert client.get("/.well-known/agent-skills/missing/SKILL.md").status_code == 404


def test_card_matches_runtime_identity_and_alias(client):
    from src.public_mcp_server import public_mcp
    card = client.get("/mcp/server/server-card").json()
    assert card == client.get("/.well-known/mcp/server-card.json").json()
    assert card["name"] == meta.OFFICIAL_REGISTRY_NAME
    assert card["title"] == public_mcp.name
    assert card["version"] == meta.APP_VERSION
    assert card["remotes"] == [{"type": "streamable-http", "url": meta.REMOTE_MCP_URL}]
    assert "tools" not in card and "capabilities" not in card


@pytest.mark.parametrize("accept, expected", [
    ("text/markdown", "text/markdown"),
    ("text/markdown;q=0", "text/html"),
    ("text/html,text/markdown;q=0.5", "text/html"),
    ("text/html;q=0.5,text/markdown", "text/markdown"),
    ("text/markdown;q=invalid", "text/html"),
    ("*/*", "text/html"),
])
def test_homepage_negotiates_without_poisoning_browser_cache(client, accept, expected):
    response = client.get("/", headers={"Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected)
    assert "Accept" in response.headers["vary"]
    assert 'rel="api-catalog"' in response.headers["link"]
    assert 'rel="ai-catalog"' in response.headers["link"]
    assert client.head("/", headers={"Accept": accept}).content == b""


def test_root_auth_alias_still_identifies_connector(client):
    root = client.get("/.well-known/oauth-protected-resource").json()
    resource_path = urlsplit(root["resource"]).path
    scoped = client.get("/.well-known/oauth-protected-resource" + resource_path).json()
    assert root == scoped
    assert root["resource"] != meta.PUBLIC_BASE_URL


def test_public_metadata_follows_configured_origin(monkeypatch, client):
    monkeypatch.setattr(meta, "PUBLIC_BASE_URL", "https://staging.example.org")
    catalog = client.get("/.well-known/ai-catalog.json").json()
    for entry in catalog["entries"]:
        assert entry["url"].startswith("https://staging.example.org/")
        assert "staging.example.org" in entry["identifier"]
    assert "https://staging.example.org/" in client.get(discovery.SKILL_PATH).text


def test_public_document_preflight_does_not_grant_api_access(client):
    headers = {"Origin": "https://reader.example", "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "If-None-Match"}
    response = client.options("/.well-known/ai-catalog.json", headers=headers)
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
    assert client.options("/internal/not-a-route", headers=headers).status_code != 204
