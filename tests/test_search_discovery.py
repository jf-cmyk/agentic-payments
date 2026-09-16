import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.submit_indexnow import public_urls
from src.search_discovery import router


def test_proof_disabled_and_validated(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for key in ("", "short", "bad\nheader", "x" * 129):
            monkeypatch.setenv("INDEXNOW_KEY", key)
            assert client.get("/indexnow-key.txt").status_code == 404
        monkeypatch.setenv("INDEXNOW_KEY", "test-only-indexnow-key")
        response = client.get("/indexnow-key.txt")
        assert response.text == "test-only-indexnow-key"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        head = client.head("/indexnow-key.txt")
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(response.content))


def sitemap(*urls):
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            ''.join(f'<url><loc>{url}</loc></url>' for url in urls) + '</urlset>').encode()


def test_submission_excludes_operational_urls_and_deduplicates():
    assert public_urls(sitemap('https://mcp.blocksize.info/', 'https://mcp.blocksize.info/',
                               'https://mcp.blocksize.info/v1/price/BTC-USD',
                               'https://mcp.blocksize.info/internal/stats')) == ['https://mcp.blocksize.info/']


@pytest.mark.parametrize('url', ['http://mcp.blocksize.info/', 'https://other.example/',
                                'https://mcp.blocksize.info.evil.example/',
                                'https://mcp.blocksize.info/path?token=secret',
                                'https://mcp.blocksize.info/%2e%2e/private'])
def test_noncanonical_urls_are_rejected(url):
    with pytest.raises(ValueError):
        public_urls(sitemap(url))


def test_empty_and_entity_sitemaps_are_rejected():
    for xml in (sitemap(), b'<!DOCTYPE x><urlset/>', b'<sitemapindex/>'):
        with pytest.raises(ValueError):
            public_urls(xml)
