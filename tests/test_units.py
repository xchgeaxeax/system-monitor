"""Unit tests for pure/low-level helpers (no HTTP, no sampling)."""
import time

import pytest


# ── History ring buffer ─────────────────────────────────────────────────
def test_history_appends_and_trims_by_window(server_mod):
    h = server_mod.History(window_s=1, max_points=100)
    now = time.time()
    h.append((now - 5, 1))   # outside window
    h.append((now - 0.5, 2))
    h.append((now, 3))
    pts = h.last()
    # old point trimmed
    assert all(ts >= now - 1 for ts, *_ in pts)
    assert (now, 3) in pts


def test_history_max_points(server_mod):
    h = server_mod.History(window_s=10000, max_points=10)
    now = time.time()
    for i in range(50):
        h.append((now + i, i))
    assert len(h.last()) == 10
    # keeps the most recent
    assert h.last()[-1] == (now + 49, 49)


def test_history_last_n(server_mod):
    h = server_mod.History(window_s=10000, max_points=100)
    now = time.time()
    for i in range(20):
        h.append((now + i, i))
    assert len(h.last(5)) == 5


# ── Login rate limiter ──────────────────────────────────────────────────
def test_login_limiter_allows_then_blocks(server_mod):
    lim = server_mod.LoginRateLimiter(limit=3, window_s=60)
    key = "1.2.3.4:alice"
    for _ in range(3):
        lim.check(key)
        lim.record(key)
    with pytest.raises(server_mod.HTTPException) as ei:
        lim.check(key)
    assert ei.value.status_code == 429


def test_login_limiter_clear_resets(server_mod):
    lim = server_mod.LoginRateLimiter(limit=2, window_s=60)
    key = "1.2.3.4:bob"
    lim.check(key); lim.record(key)
    lim.check(key); lim.record(key)
    lim.clear(key)
    lim.check(key)  # no raise after clear


def test_login_limiter_disabled_when_zero(server_mod):
    lim = server_mod.LoginRateLimiter(limit=0, window_s=60)
    for _ in range(100):
        lim.check("x:y")
        lim.record("x:y")  # never raises when disabled


# ── Password hashing ────────────────────────────────────────────────────
def test_password_hash_roundtrip(server_mod):
    import secrets as _s
    salt = _s.token_bytes(16)
    user = {"salt": salt.hex(), "hash": server_mod._hash_password("S3cret!", salt)}
    assert server_mod.verify_password("S3cret!", user) is True
    assert server_mod.verify_password("wrong", user) is False


def test_dummy_user_constant(server_mod):
    # The dummy must be a valid dict so verify_password() can run against it
    # (constant-time login for unknown users).
    assert server_mod.verify_password("anything", server_mod._DUMMY_USER) is False


# ── parse_float ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("12.5", 12.5),
    ("(3.2)", 3.2),
    ("  42 ", 42.0),
    ("0x10", 0.0),          # extracts the first numeric run ("0"), not hex
    ("", 7.0),              # no digits -> default
    (None, 7.0),
    ("abc", 7.0),           # no digits -> default
])
def test_parse_float(server_mod, raw, expected):
    assert server_mod.parse_float(raw, 7.0) == expected


# ── Webhook payload builder ─────────────────────────────────────────────
def test_webhook_payload_bark(server_mod):
    ch = {"type": "bark", "device": "dev1"}
    item = {"event": "alert", "severity": "danger", "message": "disk full",
            "rule_id": "r1", "when": "now"}
    headers, body = server_mod.WebhookNotifier._payload(ch, "https://api.day.app", item)
    assert body["title"]
    assert "disk full" in body["body"]
    assert body["group"] == "system-monitor"


def test_webhook_payload_telegram(server_mod):
    ch = {"type": "telegram", "token": "tok"}
    item = {"event": "test", "severity": "warning", "message": "hi",
            "rule_id": "r", "when": "now"}
    headers, body = server_mod.WebhookNotifier._payload(ch, "999", item)
    assert body["chat_id"] == "999"
    assert "hi" in body["text"]


def test_webhook_payload_generic(server_mod):
    ch = {"type": "generic"}
    item = {"event": "alert", "severity": "danger", "message": "m",
            "rule_id": "rid", "when": "w"}
    headers, body = server_mod.WebhookNotifier._payload(ch, "http://x", item)
    assert body["rule_id"] == "rid"
    assert body["event"] == "alert"
    assert "host" in body


def test_webhook_deliver_bark_url(server_mod):
    # bark: url + device joined
    import socket
    ch = {"type": "bark", "device": "dev9"}
    item = {"event": "t", "severity": "warning", "message": "m",
            "rule_id": "r", "when": "w"}
    # _deliver would hit the network; instead assert the URL construction by
    # monkeypatching urlopen.
    calls = []
    orig = server_mod.urllib.request.urlopen
    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=0): return b"ok"
    def fake(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp()
    server_mod.urllib.request.urlopen = fake
    try:
        server_mod.WebhookNotifier._deliver(server_mod.WebhookNotifier(), ch,
                                            "https://api.day.app", item)
    finally:
        server_mod.urllib.request.urlopen = orig
    assert calls and calls[0] == "https://api.day.app/dev9"


# ── build_alert_checks ──────────────────────────────────────────────────
def test_build_alert_checks_disk_and_mem(server_mod):
    snap = {
        "partitions": [
            {"mountpoint": "/", "percent": 96, "used_gb": 100, "total_gb": 104},
            {"mountpoint": "/data", "percent": 50, "used_gb": 10, "total_gb": 20},
        ],
        "mem_percent": 91, "mem_used_gb": 15, "mem_total_gb": 16,
        "swap_percent": 10, "load_ratio": 0.5, "load_avg": [0.1, 0.1, 0.1],
        "temps": [], "gpus": [],
    }
    checks = server_mod.build_alert_checks(snap, {})
    rids = {c["rule_id"] for c in checks}
    assert "disk_full:/" in rids
    assert "mem_high" in rids
    # /data at 50% should not trigger
    assert not any(r.startswith("disk_full:/data") for r in rids)
    # severity: 96% disk -> danger, 91% mem -> warning
    disk = next(c for c in checks if c["rule_id"] == "disk_full:/")
    assert disk["severity"] == "danger"
    mem = next(c for c in checks if c["rule_id"] == "mem_high")
    assert mem["severity"] == "warning"


def test_build_alert_checks_no_false_positives(server_mod):
    snap = {
        "partitions": [{"mountpoint": "/", "percent": 40, "used_gb": 1, "total_gb": 10}],
        "mem_percent": 30, "mem_used_gb": 1, "mem_total_gb": 16,
        "swap_percent": 0, "load_ratio": 0.2, "load_avg": [0.1, 0.1, 0.1],
        "temps": [], "gpus": [],
    }
    assert server_mod.build_alert_checks(snap, {}) == []


def test_build_alert_checks_smart_and_gpu(server_mod):
    snap = {"partitions": [], "mem_percent": 10, "mem_used_gb": 1, "mem_total_gb": 16,
            "swap_percent": 0, "load_ratio": 0.1, "load_avg": [0, 0, 0],
            "temps": [], "gpus": [{"id": "g0", "name": "GPU0", "vram_percent": 96}]}
    smart = {"nvme0": {"health": "WARNING", "model": "M"}}
    checks = server_mod.build_alert_checks(snap, smart)
    rids = {c["rule_id"] for c in checks}
    assert "smart:nvme0" in rids
    assert any("g0" in r for r in rids)  # gpu vram rule
