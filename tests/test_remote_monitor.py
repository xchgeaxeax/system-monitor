"""Tests for the v4.5 remote monitor: strata /metrics parsing, cumulative
counter deltas, SQLite rolling-window store, and the /api/remote endpoints."""
import time


# Sample modeled on a real recorded strata /metrics payload (trimmed).
STRATA_PAYLOAD = {
    "engine": {"model": "test-model", "max_context": 262144},
    "live": {"state": "generating", "queued": 1},
    "hardware": {
        "gpu_util": 99, "gpu_mem_used": 50813530112, "gpu_mem_total": 51527024640,
        "gpu_temp": 60, "gpu_power": 240.39,
        "cpu": 37.1, "ram_used": 74996867072, "ram_total": 102110724096,
        "disk_read_mb": 4.43, "disk_write_mb": 0.0,
        "tok_s": 120.76, "tok_s_mean": 97.99,
    },
    "hardware_static": {"gpu_name": "RTX 4090 D", "cores": 16},
    "totals": {"requests": 33, "prompt_tokens": 2512939, "output_tokens": 11146},
}


def test_remote_parse_maps_fields(server_mod):
    row = server_mod._remote_parse(STRATA_PAYLOAD, {})
    assert row["gpu_util"] == 99
    assert row["gpu_temp"] == 60
    assert row["gpu_power"] == 240.39
    assert row["cpu"] == 37.1
    assert row["ram_pct"] == 73.4  # 74996867072/102110724096*100
    assert row["state"] == "generating"
    assert row["queued"] == 1
    assert row["tok_s"] == 120.76
    # first sample: delta of a cumulative counter = the counter itself
    assert row["req_delta"] == 33
    assert row["tok_out_delta"] == 11146


def test_remote_parse_handles_missing_sections(server_mod):
    row = server_mod._remote_parse({}, {})
    assert row["gpu_util"] is None
    assert row["ram_pct"] is None
    assert row["state"] == "unknown"
    assert row["req_delta"] is None


def test_remote_delta_normal_and_restart(server_mod):
    d = server_mod._remote_delta
    assert d(10, 4) == 6
    assert d(10, None) == 10      # first sample
    assert d(2, 10) == 2          # service restarted: reset baseline, no negative
    assert d(None, 5) is None
    assert d(7, 7) == 0


def test_lt_write_query_rolling_window(server_mod):
    # Direct write/query round-trip on the module's store.
    now = time.time()
    for i in range(5):
        server_mod.lt_write({"ts": now - i * 15, "gpu_util": 50 + i, "cpu": 10 + i})
    rows = server_mod.lt_query(1)  # last hour
    assert len(rows) >= 5
    assert max(r["gpu_util"] for r in rows) >= 54
    # rows older than the retention window must be pruned on next write
    old_ts = now - server_mod.LT_RETENTION_S - 3600
    server_mod.lt_write({"ts": old_ts, "gpu_util": 1})
    rows = server_mod.lt_query(168)  # 7d window: old row must be gone
    assert all(r["ts"] > old_ts for r in rows)


def test_lt_query_downsamples_keeping_spikes(server_mod):
    now = time.time()
    n = server_mod.LT_MAX_POINTS * 3
    saved_cap = server_mod.LT_MAX_POINTS
    try:
        base = now - 300
        for i in range(n):
            server_mod.lt_write({"ts": base + i * 0.01, "gpu_util": 10, "cpu": 10})
        # inject a spike in the middle
        server_mod.lt_write({"ts": base + n * 0.005, "gpu_util": 999, "cpu": 999})
        rows = server_mod.lt_query(1)
        assert len(rows) <= saved_cap
        assert max(r["gpu_util"] for r in rows) == 999  # spike survived
    finally:
        pass


def test_remote_endpoints_shape(client, admin, server_mod):
    r = client.get("/api/remote", headers=admin)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False  # scraping disabled in tests; shape must still hold
    assert "data" in d and "url" in d

    r = client.get("/api/remote/stats", headers=admin)
    assert r.status_code == 200
    s = r.json()
    assert s["retention_s"] == 604800  # 7 days default

    r = client.get("/api/remote/history?hours=24", headers=admin)
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    # parameter validation
    assert client.get("/api/remote/history?hours=0", headers=admin).status_code == 422
    assert client.get("/api/remote/history?hours=169", headers=admin).status_code == 422
    # auth required
    assert client.get("/api/remote").status_code == 401


def test_dashboard_has_remote_tab(client):
    html = client.get("/").text
    assert 'data-tab="remote"' in html
    assert 'id="panel-remote"' in html
    assert 'id="remote-range"' in html
    assert "/api/remote/history" in html
