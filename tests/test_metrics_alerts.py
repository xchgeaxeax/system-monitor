"""Metric endpoints, alert engine (trigger/resolve/ack/delete), and webhook
config (secret preservation + masking)."""
import pytest


# ── Metric endpoints ────────────────────────────────────────────────────
def test_health(client, admin_setup):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "warning")
    assert body["version"]
    assert "checks" in body


def test_summary_shape(client, admin):
    r = client.get("/api/summary", headers=admin)
    assert r.status_code == 200
    s = r.json()
    for k in ("cpu_percent", "mem_percent", "mem_total_gb", "disk_max_percent",
              "load_avg", "uptime_str", "cores"):
        assert k in s, f"summary missing {k}"
    assert s["mem_total_gb"] > 0
    assert 0 <= s["cpu_percent"] <= 100


def test_cpu_endpoint(client, admin):
    r = client.get("/api/cpu", headers=admin)
    assert r.status_code == 200
    cpu = r.json()
    assert "usage_percent" in cpu


def test_memory_endpoint(client, admin):
    r = client.get("/api/memory", headers=admin)
    assert r.status_code == 200
    m = r.json()
    assert m["total_gb"] > 0
    assert 0 <= m["percent"] <= 100
    assert isinstance(m["proc_memory"], list)


def test_storage_endpoint(client, admin):
    r = client.get("/api/storage", headers=admin)
    assert r.status_code == 200
    st = r.json()
    assert isinstance(st["partitions"], list)
    assert "smart" in st


def test_network_endpoint(client, admin):
    r = client.get("/api/network", headers=admin)
    assert r.status_code == 200
    net = r.json()
    assert isinstance(net["interfaces"], list)
    assert "rates" in net


def test_temps_endpoint(client, admin):
    r = client.get("/api/temps", headers=admin)
    assert r.status_code == 200
    t = r.json()
    assert isinstance(t["sensors"], list)
    assert isinstance(t["fans"], list)


def test_gpu_endpoint_shape(client, admin):
    r = client.get("/api/gpu", headers=admin)
    assert r.status_code == 200
    g = r.json()
    for k in ("rocm", "intel", "sysfs", "stale"):
        assert k in g, f"gpu missing {k}"


def test_history_endpoints(client, admin):
    for path in ("/api/net-history", "/api/cpu-freq-history", "/api/disk-io-history",
                 "/api/gpu-history"):
        r = client.get(path, headers=admin)
        assert r.status_code == 200, path


def test_processes_endpoint(client, admin):
    r = client.get("/api/processes", headers=admin)
    assert r.status_code == 200
    procs = r.json()
    assert isinstance(procs, list) and len(procs) > 0
    p0 = procs[0]
    for k in ("pid", "name", "cpu", "mem", "rss_mb", "status"):
        assert k in p0
    # search filters by pid
    pid = procs[0]["pid"]
    r2 = client.get(f"/api/processes?search={pid}", headers=admin)
    assert any(p["pid"] == pid for p in r2.json())


def test_monitor_self_stats(client, admin):
    r = client.get("/api/monitor", headers=admin)
    assert r.status_code == 200
    m = r.json()
    assert m["version"]
    assert m["rss_mb"] is None or m["rss_mb"] > 0


def test_logs_endpoint(client, admin):
    r = client.get("/api/logs?lines=20", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert "logs" in body


# ── Alert engine ────────────────────────────────────────────────────────
def _fake_checks(active_rid, severity="warning", family="core"):
    return [{
        "rule_id": active_rid,
        "family": family,
        "severity": severity,
        "message": f"test alert {active_rid}",
        "active": True,
    }]


def test_alert_trigger_and_ack(client, admin, server_mod):
    rid = "test:trigger"
    server_mod.alerts.evaluate(_fake_checks(rid), available={"core"})
    snap = client.get("/api/alerts", headers=admin).json()
    assert any(a["rule_id"] == rid for a in snap["active"])
    # ack
    r = client.post(f"/api/alerts/ack?rule_id={rid}", headers=admin)
    assert r.status_code == 200
    snap = client.get("/api/alerts", headers=admin).json()
    a = next(x for x in snap["active"] if x["rule_id"] == rid)
    assert a["acknowledged"] is True
    # delete to clean up
    assert client.delete(f"/api/alerts/active?rule_id={rid}", headers=admin).status_code == 200


def test_alert_auto_resolves_when_cleared(client, admin, server_mod):
    rid = "test:resolve"
    server_mod.alerts.evaluate(_fake_checks(rid), available={"core"})
    assert any(a["rule_id"] == rid for a in
               client.get("/api/alerts", headers=admin).json()["active"])
    # next evaluation with the rule inactive -> auto-resolve into history
    server_mod.alerts.evaluate([], available={"core"})
    snap = client.get("/api/alerts", headers=admin).json()
    assert not any(a["rule_id"] == rid for a in snap["active"])
    assert any(a["rule_id"] == rid for a in snap["history"])


def test_alert_not_resolved_when_source_unavailable(client, admin, server_mod):
    rid = "test:unavail"
    server_mod.alerts.evaluate(_fake_checks(rid, family="smart"), available={"smart"})
    # clear the check, but smart source now unavailable -> must stay active
    server_mod.alerts.evaluate([], available=set())
    snap = client.get("/api/alerts", headers=admin).json()
    assert any(a["rule_id"] == rid for a in snap["active"])
    # clean up by force-delete
    client.delete(f"/api/alerts/active?rule_id={rid}", headers=admin)


def test_alert_ack_unknown_404(client, admin):
    assert client.post("/api/alerts/ack?rule_id=nope", headers=admin).status_code == 404


def test_alert_delete_history_and_clear_resolved(client, admin, server_mod):
    rid = "test:hist"
    server_mod.alerts.evaluate(_fake_checks(rid), available={"core"})
    server_mod.alerts.evaluate([], available={"core"})  # resolve -> history
    snap = client.get("/api/alerts", headers=admin).json()
    idx = next(i for i, h in enumerate(snap["history"]) if h["rule_id"] == rid)
    assert client.delete(f"/api/alerts/history/{idx}", headers=admin).status_code == 200
    # clear all resolved
    r = client.post("/api/alerts/clear-resolved", headers=admin)
    assert r.status_code == 200
    assert client.get("/api/alerts", headers=admin).json()["history"] == []


# ── Webhook config ──────────────────────────────────────────────────────
def test_webhook_get_masked(client, admin):
    r = client.put("/api/webhooks", headers=admin, json={
        "enabled": True, "cooldown_s": 300,
        "channels": [{"type": "bark", "url": "https://api.day.app",
                      "token": "", "device": "SECRETKEY", "min_severity": "warning"}],
    })
    assert r.status_code == 200
    got = client.get("/api/webhooks", headers=admin).json()
    assert got["enabled"] is True
    ch = got["channels"][0]
    assert ch["type"] == "bark"
    assert ch["configured"] is True
    # secrets must NOT be echoed back
    assert "url" not in ch
    assert "device" not in ch
    assert "SECRETKEY" not in str(got)


def test_webhook_preserves_secrets_on_empty_resave(client, admin):
    # configure
    client.put("/api/webhooks", headers=admin, json={
        "enabled": True, "cooldown_s": 300,
        "channels": [{"type": "telegram", "url": "12345", "token": "BOTTOKEN",
                      "device": "", "min_severity": "info"}],
    })
    # dashboard re-sends masked (empty url/token) config -> must keep stored
    client.put("/api/webhooks", headers=admin, json={
        "enabled": True, "cooldown_s": 300,
        "channels": [{"type": "telegram", "url": "", "token": "",
                      "device": "", "min_severity": "info"}],
    })
    import server
    cfg = server._load_webhook_cfg()
    ch = next(c for c in cfg["channels"] if c["type"] == "telegram")
    assert ch["url"] == "12345", "url was wiped by empty re-save"
    assert ch["token"] == "BOTTOKEN", "token was wiped by empty re-save"


def test_webhook_invalid_type_400(client, admin):
    r = client.put("/api/webhooks", headers=admin, json={
        "enabled": True, "cooldown_s": 300,
        "channels": [{"type": "carrier-pigeon", "url": "x", "token": "",
                      "device": "", "min_severity": "warning"}],
    })
    assert r.status_code == 400


def test_webhook_test_endpoint(client, admin):
    # no channels configured with a url -> results report no url
    client.put("/api/webhooks", headers=admin, json={
        "enabled": True, "cooldown_s": 300, "channels": [],
    })
    r = client.post("/api/webhooks/test", headers=admin)
    assert r.status_code == 200
    assert r.json() == {"results": []}
