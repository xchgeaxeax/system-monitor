"""Compression middleware tests.

These drive the ASGI app directly (manual scope/receive/send) rather than
through TestClient, so httpx's own Accept-Encoding negotiation can't interfere
and we can inspect the raw content-encoding / content-length headers and
decompress the body ourselves.

Async helpers are wrapped with asyncio.run() so the suite needs no
pytest-asyncio dependency.
"""
import asyncio
import gzip

import pytest


async def _call_app(app, path="/", accept_encoding=""):
    """Run one ASGI request, return (status, headers-dict, body-bytes)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"accept-encoding", accept_encoding.encode())] if accept_encoding else [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    status = None
    headers = {}
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status, headers, body
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, headers, body


def _call(app, path, ae):
    return asyncio.run(_call_app(app, path, ae))


# ── Negotiation (pure function) ─────────────────────────────────────────
def test_negotiate_prefers_zstd(server_mod):
    assert server_mod._negotiate_encoding("zstd, gzip", "/") == "zstd"
    assert server_mod._negotiate_encoding("gzip", "/") == "gzip"
    assert server_mod._negotiate_encoding("br", "/") is None
    # /sw.js is always identity
    assert server_mod._negotiate_encoding("zstd, gzip", "/sw.js") is None
    # empty accept-encoding -> identity
    assert server_mod._negotiate_encoding("", "/") is None


def test_negotiate_zstd_unavailable_falls_back(server_mod):
    orig = server_mod.ZSTD_AVAILABLE
    server_mod.ZSTD_AVAILABLE = False
    try:
        assert server_mod._negotiate_encoding("zstd, gzip", "/") == "gzip"
    finally:
        server_mod.ZSTD_AVAILABLE = orig


# ── End-to-end through the ASGI app ─────────────────────────────────────
def test_zstd_compresses_dashboard(server_mod):
    import zstandard as zstd
    status, headers, body = _call(server_mod.app, "/", "zstd, gzip")
    assert status == 200
    assert headers.get("content-encoding") == "zstd"
    assert int(headers.get("content-length", "0")) == len(body)
    assert "accept-encoding" in headers.get("vary", "").lower()
    raw = zstd.ZstdDecompressor().decompress(body)
    assert b"System Monitor" in raw
    assert len(body) < len(raw)


def test_gzip_fallback(server_mod):
    status, headers, body = _call(server_mod.app, "/", "gzip")
    assert status == 200
    assert headers.get("content-encoding") == "gzip"
    assert int(headers.get("content-length", "0")) == len(body)
    raw = gzip.decompress(body)
    assert b"System Monitor" in raw


def test_identity_when_no_encoding(server_mod):
    status, headers, body = _call(server_mod.app, "/", "br")
    assert status == 200
    assert "content-encoding" not in headers
    assert b"System Monitor" in body


def test_swjs_never_compressed(server_mod):
    for ae in ("zstd, gzip", "gzip"):
        status, headers, body = _call(server_mod.app, "/sw.js", ae)
        assert status == 200
        assert "content-encoding" not in headers
        assert b"cache" in body.lower()


def test_small_body_not_compressed(server_mod):
    # /api/health returns tiny JSON (< min size) -> identity even if asked.
    status, headers, body = _call(server_mod.app, "/api/health", "zstd, gzip")
    assert status == 200
    assert "content-encoding" not in headers
    assert b"status" in body


def test_zstd_beats_gzip_on_json(server_mod):
    # The dashboard is the canonical large payload; compare zstd vs gzip sizes
    # for it to confirm zstd is at least as good (it's smaller for JSON).
    _, _, zbody = _call(server_mod.app, "/", "zstd")
    _, _, gbody = _call(server_mod.app, "/", "gzip")
    # zstd should not be larger than gzip (it's smaller for this HTML+JSON mix)
    assert len(zbody) <= len(gbody)


def test_compress_disabled_via_none(server_mod):
    orig = server_mod.COMPRESS_ENCODINGS
    server_mod.COMPRESS_ENCODINGS = ["none"]
    try:
        # With "none" the middleware short-circuits: no content-encoding even
        # when the client offers zstd.
        status, headers, body = _call(server_mod.app, "/", "zstd, gzip")
        assert status == 200
        assert "content-encoding" not in headers
    finally:
        server_mod.COMPRESS_ENCODINGS = orig
