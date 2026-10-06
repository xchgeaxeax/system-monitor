"""Tests for the v4.4 additions: history downsampling, dashboard ETag/304,
PWA icon/manifest endpoints, and Accept-Encoding q-value negotiation."""
import time


# ── History.sampled (T13) ───────────────────────────────────────────────
def test_sampled_passthrough_small(server_mod):
    h = server_mod.History(window_s=3600, max_points=1000)
    for i in range(10):
        h.append((time.time() - (10 - i), float(i), 0.0))
    assert len(h.sampled(points=100)) == 10


def test_sampled_downsamples_and_keeps_spikes(server_mod):
    h = server_mod.History(window_s=3600, max_points=5000)
    base = time.time() - 1000
    for i in range(2400):
        v = 1.0
        if i == 1750:  # a single-sample traffic spike
            v = 999.0
        h.append((base + i * 0.4, v, 0.0))
    out = h.sampled(points=400)
    assert len(out) == 400
    # max-pooling must preserve the spike, not average it away
    assert max(p[1] for p in out) == 999.0
    # strictly increasing timestamps
    assert all(out[i][0] < out[i + 1][0] for i in range(len(out) - 1))


def test_sampled_window_filter(server_mod):
    h = server_mod.History(window_s=3600, max_points=5000)
    now = time.time()
    for i in range(100):
        h.append((now - 3600 + i * 36, 1.0, 0.0))  # spread over 1h
    out = h.sampled(points=1000, window_s=600)  # last 10 minutes only
    assert out, "expected points inside the window"
    assert all(p[0] >= now - 601 for p in out)


def test_sampled_dict_points_magnitude(server_mod):
    # disk-io history points are (ts, {disk: (r, w)}) — magnitude must handle dicts
    h = server_mod.History(window_s=3600, max_points=5000)
    base = time.time() - 1000
    for i in range(1200):
        d = {"sda": (1.0, 1.0)}
        if i == 600:
            d = {"sda": (500.0, 0.0)}
        h.append((base + i * 0.8, d))
    out = h.sampled(points=100)
    assert any(p[1]["sda"][0] == 500.0 for p in out)


# ── Accept-Encoding q-values (T15) ──────────────────────────────────────
def test_negotiate_qvalue_prefers_gzip(server_mod):
    assert server_mod._negotiate_encoding("gzip;q=1.0, zstd;q=0.5", "/x") == "gzip"


def test_negotiate_qvalue_zero_excludes(server_mod):
    assert server_mod._negotiate_encoding("zstd;q=0, gzip", "/x") == "gzip"
    assert server_mod._negotiate_encoding("zstd;q=0, gzip;q=0", "/x") is None


def test_negotiate_identity_only(server_mod):
    assert server_mod._negotiate_encoding("identity", "/x") is None
    assert server_mod._negotiate_encoding("", "/x") is None


def test_negotiate_star_and_unknown(server_mod):
    assert server_mod._negotiate_encoding("*", "/x") == "gzip"
    # unknown encoding outranks nothing supported -> falls through to *
    assert server_mod._negotiate_encoding("br;q=1.0, *;q=0.5", "/x") == "gzip"


def test_negotiate_still_prefers_zstd_by_default(server_mod):
    if server_mod.ZSTD_AVAILABLE:
        assert server_mod._negotiate_encoding("gzip, zstd", "/x") == "zstd"


# ── Dashboard ETag / 304 (T12) ──────────────────────────────────────────
def test_dashboard_etag_roundtrip(client):
    r = client.get("/")
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert etag.startswith('W/"')
    r2 = client.get("/", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers["etag"] == etag


def test_dashboard_etag_mismatch_is_200(client):
    r = client.get("/", headers={"If-None-Match": 'W/"bogus"'})
    assert r.status_code == 200


# ── PWA endpoints (T17) ─────────────────────────────────────────────────
def test_icon_and_manifest_endpoints(client):
    r = client.get("/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text

    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest+json" in r.headers["content-type"]
    import json
    m = json.loads(r.text)
    assert m["name"] == "System Monitor"
    assert any("icon.svg" in i["src"] for i in m["icons"])


def test_dashboard_links_manifest_url_not_data_uri(client):
    html = client.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert "data:application/manifest+json" not in html


# ── History endpoints accept minute/points (T13) ────────────────────────
def test_history_endpoints_accept_params(client, admin):
    for path in ("/api/net-history", "/api/cpu-freq-history",
                 "/api/disk-io-history", "/api/gpu-history"):
        r = client.get(path + "?minute=5&points=100", headers=admin)
        assert r.status_code == 200, path
    # invalid params rejected
    assert client.get("/api/net-history?minute=-1", headers=admin).status_code == 422
    assert client.get("/api/net-history?points=10", headers=admin).status_code == 422
