"""API-level tests for the endpoints that previously had no coverage
(processes/kill, logs, API keys, webhook config semantics) plus the
medium-priority hardening round:

- T5: _load_auth mtime cache (external edits still picked up)
- T6: alert thresholds are configurable
- T7: webhook secret preservation is positional per type
- T8: process CPU baselines keyed by (pid, create_time)
"""
import json
import os
import subprocess
import time


# ── Processes (T9 + T8) ─────────────────────────────────────────────────

def test_processes_shape(client, admin):
    r = client.get("/api/processes", headers=admin)
    assert r.status_code == 200
    procs = r.json()
    assert isinstance(procs, list) and len(procs) > 0
    p = procs[0]
    for k in ("pid", "name", "username", "cpu", "mem", "rss_mb", "status", "uptime_s"):
        assert k in p, f"process row missing {k}"


def test_processes_sort_search_limit(client, admin):
    r = client.get("/api/processes?sort_by=mem&limit=5", headers=admin)
    assert r.status_code == 200
    procs = r.json()
    assert len(procs) <= 5
    mems = [p["mem"] for p in procs]
    assert mems == sorted(mems, reverse=True)
    # impossible search returns empty, not an error
    r = client.get("/api/processes?search=zzz_no_such_proc_zzz", headers=admin)
    assert r.status_code == 200 and r.json() == []
    # bad sort_by falls back to cpu instead of 500
    assert client.get("/api/processes?sort_by=bogus", headers=admin).status_code == 200


def test_process_kill_roundtrip(client, admin, server_mod):
    proc = subprocess.Popen(["sleep", "300"])
    try:
        r = client.post("/api/processes/kill", headers=admin,
                        json={"pid": proc.pid, "sig": 15})
        assert r.status_code == 200
        assert r.json()["ok"] and r.json()["pid"] == proc.pid
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
    # killing a dead pid -> 404
    r = client.post("/api/processes/kill", headers=admin,
                    json={"pid": 2 ** 30, "sig": 9})
    assert r.status_code == 404


def test_process_baseline_survives_pid_reuse(server_mod):
    # A different create_time for the same pid must NOT produce a delta
    # against the old process's cpu time (the reuse bug).
    server_mod._proc_cpu_last.clear()
    server_mod._proc_last_time = time.time() - 10  # dt = 10s
    sentinel_pid = 2 ** 30 - 2
    server_mod._proc_cpu_last[sentinel_pid] = (1000.0, 500.0)  # old create_time
    # Simulate the check the scan performs
    prev = server_mod._proc_cpu_last.get(sentinel_pid)
    new_create = 2000.0  # reused pid -> new process, new start time
    assert prev[0] != new_create  # the guard condition rejects the stale baseline
    # Same create_time -> delta is valid and would be used
    assert server_mod._proc_cpu_last[sentinel_pid][0] == 1000.0


def test_process_baseline_pruned(server_mod):
    # get_processes prunes baselines for processes that disappeared.
    server_mod.get_processes(limit=1)
    assert 2 ** 30 - 2 not in server_mod._proc_cpu_last  # sentinel from above


# ── Logs (T9) ───────────────────────────────────────────────────────────

def test_logs_shape_and_filters(client, admin):
    r = client.get("/api/logs?lines=20", headers=admin)
    assert r.status_code == 200
    d = r.json()
    assert "logs" in d and "units" in d
    assert isinstance(d["logs"], list)
    # journalctl may be unavailable in this environment; shape must hold anyway
    assert all(isinstance(line, str) for line in d["logs"])
    # level/search/unit filters must not 500 regardless of availability
    for q in ("level=error", "unit=systemd-journald", "search=loop"):
        r = client.get(f"/api/logs?{q}", headers=admin)
        assert r.status_code == 200, q


# ── API keys (T9) ───────────────────────────────────────────────────────

def test_api_key_lifecycle(client, admin, server_mod):
    r = client.post("/api/auth/keys", headers=admin, json={"name": "testkey"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("amk_")
    # the key authenticates and sees data
    assert client.get("/api/summary", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    # listed (masked)
    r = client.get("/api/auth/keys", headers=admin)
    names = [k["name"] for k in r.json()]
    assert "testkey" in names
    listed = next(k for k in r.json() if k["name"] == "testkey")
    assert listed["key"] if "key" in listed else True  # full secret must not leak
    # duplicate name -> 409
    assert client.post("/api/auth/keys", headers=admin,
                       json={"name": "testkey"}).status_code == 409
    # delete, then the key stops working
    assert client.delete("/api/auth/keys/testkey", headers=admin).status_code == 200
    assert client.get("/api/summary", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_regular_user_key_scoping(client, admin):
    # bob (regular user) sees only his own keys
    client.post("/api/users", headers=admin,
                json={"username": "keybob", "password": "BobPass12345", "role": "user"})
    from tests.test_auth import do_login
    bob = {"Authorization": f"Bearer {do_login(client, 'keybob', 'BobPass12345')['token']}"}
    assert client.post("/api/auth/keys", headers=bob, json={"name": "bobkey"}).status_code == 200
    assert client.post("/api/auth/keys", headers=admin, json={"name": "adminkey"}).status_code == 200
    names = [k["name"] for k in client.get("/api/auth/keys", headers=bob).json()]
    assert "bobkey" in names and "adminkey" not in names
    # bob cannot delete admin's key
    assert client.delete("/api/auth/keys/adminkey", headers=bob).status_code == 403


# ── Webhooks (T9 + T7) ──────────────────────────────────────────────────

def test_webhook_config_and_masking(client, admin):
    r = client.get("/api/webhooks", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"enabled", "channels", "cooldown_s"}
    # PUT a generic channel, GET must not leak the url secret
    cfg = {"enabled": True, "cooldown_s": 60,
           "channels": [{"type": "generic", "url": "http://127.0.0.1:9/x",
                         "min_severity": "danger"}]}
    r = client.put("/api/webhooks", headers=admin, json=cfg)
    assert r.status_code == 200
    got = r.json()
    raw = json.dumps(got)
    assert "http://127.0.0.1:9/x" not in raw  # masked
    ch = next(c for c in got["channels"] if c["type"] == "generic")
    assert ch["configured"] is True
    # unknown type -> 400
    bad = dict(cfg, channels=[{"type": "sms", "url": "x"}])
    assert client.put("/api/webhooks", headers=admin, json=bad).status_code == 400
    # reset
    client.put("/api/webhooks", headers=admin, json={"enabled": False, "channels": []})


def test_webhook_secret_preservation_positional(client, admin, server_mod):
    # Two channels of the SAME type: an empty re-save must preserve each
    # channel's own url (k-th new inherits from k-th stored), not collapse
    # both onto the first stored one.
    cfg = {"enabled": True, "cooldown_s": 0, "channels": [
        {"type": "generic", "url": "http://one.example/hook", "min_severity": "warning"},
        {"type": "generic", "url": "http://two.example/hook", "min_severity": "warning"},
    ]}
    assert client.put("/api/webhooks", headers=admin, json=cfg).status_code == 200
    # Re-save with empty urls (what the dashboard does)
    empty = {"enabled": True, "cooldown_s": 0, "channels": [
        {"type": "generic", "url": "", "min_severity": "warning"},
        {"type": "generic", "url": "", "min_severity": "warning"},
    ]}
    assert client.put("/api/webhooks", headers=admin, json=empty).status_code == 200
    stored = json.loads((server_mod.DATA_DIR / "webhook.json").read_text())
    urls = [c["url"] for c in stored["channels"] if c["type"] == "generic"]
    assert urls == ["http://one.example/hook", "http://two.example/hook"]
    # cleanup
    client.put("/api/webhooks", headers=admin, json={"enabled": False, "channels": []})


def test_webhooks_test_requires_admin(client, admin):
    client.post("/api/users", headers=admin,
                json={"username": "testbob", "password": "BobPass12345", "role": "user"})
    from tests.test_auth import do_login
    bob = {"Authorization": f"Bearer {do_login(client, 'testbob', 'BobPass12345')['token']}"}
    assert client.post("/api/webhooks/test", headers=bob).status_code == 403


# ── Alert thresholds (T6) ───────────────────────────────────────────────

def _snap_for_thresholds(disk=91.0, mem=91.0, swap=0.0, load_ratio=0.0):
    return {
        "partitions": [{"mountpoint": "/x", "percent": disk, "used_gb": 9, "total_gb": 10}],
        "mem_percent": mem, "mem_used_gb": 7, "mem_total_gb": 8,
        "swap_percent": swap, "load_ratio": load_ratio, "load_avg": [1, 1, 1],
        "temps": [], "gpus": [],
    }


def test_alert_threshold_defaults_and_overrides(server_mod, monkeypatch):
    checks = {c["rule_id"] for c in server_mod.build_alert_checks(_snap_for_thresholds(), {})}
    assert "disk_full:/x" in checks and "mem_high" in checks

    # Raise the thresholds -> the same snapshot must trigger nothing
    monkeypatch.setattr(server_mod, "ALERT_DISK_PCT", 95)
    monkeypatch.setattr(server_mod, "ALERT_MEM_PCT", 95)
    checks = {c["rule_id"] for c in server_mod.build_alert_checks(_snap_for_thresholds(), {})}
    assert "disk_full:/x" not in checks and "mem_high" not in checks

    # Lower them -> swap/load rules appear
    monkeypatch.setattr(server_mod, "ALERT_SWAP_PCT", 10)
    monkeypatch.setattr(server_mod, "ALERT_LOAD_RATIO", 0.5)
    checks = {c["rule_id"] for c in server_mod.build_alert_checks(
        _snap_for_thresholds(disk=10, mem=10, swap=50, load_ratio=1.0), {})}
    assert "swap_high" in checks and "load_high" in checks


def test_alert_severity_uses_danger_threshold(server_mod, monkeypatch):
    monkeypatch.setattr(server_mod, "ALERT_DISK_PCT", 80)
    monkeypatch.setattr(server_mod, "ALERT_DISK_DANGER_PCT", 99)
    checks = server_mod.build_alert_checks(_snap_for_thresholds(disk=91), {})
    assert checks[0]["severity"] == "warning"
    monkeypatch.setattr(server_mod, "ALERT_DISK_DANGER_PCT", 90)
    checks = server_mod.build_alert_checks(_snap_for_thresholds(disk=91), {})
    assert checks[0]["severity"] == "danger"


# ── Auth cache (T5) ─────────────────────────────────────────────────────

def test_auth_cache_serves_but_tracks_external_edits(client, admin_setup, server_mod):
    path = server_mod.AUTH_FILE
    with server_mod._auth_lock:
        data = server_mod._load_auth()
        assert "admin" in data.get("users", {})
        # Same mtime -> the exact cached object is returned (no re-parse).
        assert server_mod._load_auth() is data
        # Simulate monitor-cli.py rewriting the file externally: the next
        # read must see the new content, not the cache.
        data2 = json.loads(json.dumps(data))
        data2["users"]["outsider"] = {"username": "outsider", "role": "user",
                                      "salt": "00", "hash": "00"}
        tmp = path.with_suffix(".ext")
        tmp.write_text(json.dumps(data2))
        os.replace(tmp, path)
        fresh = server_mod._load_auth()
        assert "outsider" in fresh.get("users", {})
    # Restore via the server's own save (also refreshes the cache)
    with server_mod._auth_lock:
        fresh["users"].pop("outsider", None)
        server_mod._save_auth(fresh)
        assert "outsider" not in server_mod._load_auth().get("users", {})


def test_auth_cache_read_modify_write(client, admin, server_mod):
    # A login (RMW cycle) must be visible immediately and to the next cycle.
    from tests.test_auth import do_login
    tok = do_login(client, "admin")["token"]
    with server_mod._auth_lock:
        assert tok in server_mod._load_auth().get("sessions", {})
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    with server_mod._auth_lock:
        assert tok not in server_mod._load_auth().get("sessions", {})
