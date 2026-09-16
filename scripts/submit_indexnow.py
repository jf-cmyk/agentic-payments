"""Submit public sitemap URLs to IndexNow. Preview by default; --submit sends them.

INDEXNOW_KEY is read from the environment and never written to the report.
Only the canonical production origin is supported; redirects are rejected.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

BASE = "https://mcp.blocksize.info"
KEY_LOCATION = BASE + "/indexnow-key.txt"
API = "https://api.indexnow.org/indexnow"
MAX_BYTES = 2_000_000
EXCLUDED = ("/internal", "/v1", "/anthropic", "/cursor", "/openai", "/mcp/server",
            "/agent-auth", "/oauth", "/indexnow-key.txt")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected redirect; submission stopped")


def read_response(url: str, *, data: bytes | None = None) -> tuple[int, bytes]:
    headers = {"User-Agent": "Blocksize-IndexNow/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Response exceeds size limit")
            return response.status, body
    except HTTPError as error:
        # Do not copy response bodies or URLs that could echo the ownership key.
        raise ValueError(f"HTTP request failed with status {error.code}") from None


def public_urls(sitemap: bytes) -> list[str]:
    if len(sitemap) > MAX_BYTES or b"<!DOCTYPE" in sitemap.upper() or b"<!ENTITY" in sitemap.upper():
        raise ValueError("Unsupported sitemap")
    root = ElementTree.fromstring(sitemap)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag != ns + "urlset":
        raise ValueError("Expected a sitemap urlset")
    urls = []
    for node in root.findall(ns + "url/" + ns + "loc"):
        url = (node.text or "").strip()
        parsed = urlsplit(url)
        path = unquote(parsed.path).lower()
        if (parsed.scheme != "https" or parsed.netloc != "mcp.blocksize.info"
                or parsed.query or parsed.fragment or "\\" in path
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("Sitemap contains a noncanonical URL")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in EXCLUDED):
            continue
        urls.append(url)
    result = sorted(set(urls))
    if not result or len(result) > 10_000:
        raise ValueError("Expected 1–10,000 public URLs")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    _, sitemap = read_response(BASE + "/sitemap.xml")
    urls = public_urls(sitemap)
    report = {"host": "mcp.blocksize.info", "url_count": len(urls), "urls": urls,
              "submitted": False, "indexed": "not verified"}
    if args.submit:
        key = os.environ.get("INDEXNOW_KEY", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
            raise ValueError("Set a valid INDEXNOW_KEY before submitting")
        _, proof = read_response(KEY_LOCATION)
        if proof.decode("utf-8").strip() != key:
            raise ValueError("Live ownership proof does not match configured key")
        payload = {"host": "mcp.blocksize.info", "key": key,
                   "keyLocation": KEY_LOCATION, "urlList": urls}
        status, _ = read_response(API, data=json.dumps(payload).encode())
        if status not in {200, 202}:
            raise ValueError(f"Unexpected IndexNow response: {status}")
        report.update(submitted=True, http_status=status,
                      result="received" if status == 200 else "key validation pending")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
