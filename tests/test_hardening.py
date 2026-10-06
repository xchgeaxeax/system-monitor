"""Tests for the high-priority hardening round:

- T3: _client_ip() honors X-Forwarded-For only when TRUST_PROXY is enabled
- T4: the top-memory process list is served from a TTL cache
- T2: health / webhooks-test are sync endpoints (FastAPI thread-pools them,
      keeping the event loop free during subprocess/urlopen calls)
"""
import time


# ── T3: proxy-aware rate-limit key ──────────────────────────────────────

class _Req:
    """Minimal stand-in for starlette Request (only .client/.headers used)."""
    def __init__(self, host="127.0.0.1", headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def test_client_ip_trusts_xff_only_when_enabled(server_mod):
    req = _Req(headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    old = server_mod.TRUST_PROXY
    try:
        server_mod.TRUST_PROXY = True
        assert server_mod._client_ip(req) == "203.0.113.7"  # first hop
        server_mod.TRUST_PROXY = False
        assert server_mod._client_ip(req) == "127.0.0.1"     # socket peer
    finally:
        server_mod.TRUST_PROXY = old


def test_client_ip_falls_back_without_header(server_mod):
    old = server_mod.TRUST_PROXY
    try:
        server_mod.TRUST_PROXY = True
        assert server_mod._client_ip(_Req(host="10.1.2.3")) == "10.1.2.3"
        # Empty/garbage header must not produce an empty key
        assert server_mod._client_ip(_Req(headers={"x-forwarded-for": " , "})) == "127.0.0.1"
    finally:
        server_mod.TRUST_PROXY = old


# ── T4: top-memory list TTL cache ───────────────────────────────────────

def test_memory_proc_list_is_cached(server_mod):
    # Force a cold cache, take a fresh scan, then a cached one.
    server_mod._proc_mem_cache["time"] = 0.0
    t_before = time.time()
    s1 = server_mod._get_memory_snapshot(proc_list=True)
    # The cache timestamp is stamped at scan start, so it lands in
    # [t_before, now].
    assert t_before <= server_mod._proc_mem_cache["time"] <= time.time()
    # The second call must have been served from cache (timestamp unchanged).
    ts_after_scan = server_mod._proc_mem_cache["time"]
    s2 = server_mod._get_memory_snapshot(proc_list=True)
    assert server_mod._proc_mem_cache["time"] == ts_after_scan
    assert s1["proc_memory"] == s2["proc_memory"]
    assert len(s1["proc_memory"]) <= 30
    # Totals must stay live even when the list is cached: expiring the cache
    # must trigger a rescan (fresh timestamp), not an error.
    server_mod._proc_mem_cache["time"] = 0.0
    server_mod._get_memory_snapshot(proc_list=True)
    assert server_mod._proc_mem_cache["time"] >= t_before


def test_memory_totals_not_cached(server_mod):
    # proc_list=False must never touch the cache (sampler path).
    server_mod._proc_mem_cache["data"] = [{"pid": -1, "name": "sentinel"}]
    s = server_mod._get_memory_snapshot(proc_list=False)
    assert s["proc_memory"] == []


# ── T2: blocking endpoints must be sync defs ────────────────────────────

def test_blocking_endpoints_are_sync(server_mod):
    import inspect
    # FastAPI runs sync (non-async) endpoints in a thread pool. These two do
    # blocking work (subprocess probes / urlopen), so they must NOT be async.
    for fn_name in ("api_health", "webhooks_test"):
        fn = getattr(server_mod, fn_name)
        assert not inspect.iscoroutinefunction(fn), f"{fn_name} must be sync"
