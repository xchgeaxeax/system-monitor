"""Shared fixtures for the System Monitor test suite.

Design notes
------------
- The server module is imported ONCE (session scope) with a throwaway
  AI_MONITOR_DATA_DIR so no real auth.json/alerts.json is ever touched.
- Sampler/GPU/SMART intervals are pushed to 1 hour: the background sampler
  primes its snapshots at startup and then sleeps, so tests call
  server._sample_once() manually. This makes alert tests race-free (the
  sampler can't auto-resolve an alert we injected mid-test).
- MAX_SESSIONS is set low (3) so the session-cap test is meaningful.
- Login rate limit stays at the production default (5 / 60s); the
  rate-limit test uses its own username so it can't starve other tests.
"""
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def server_mod(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("mondata")
    os.environ["AI_MONITOR_DATA_DIR"] = str(data_dir)
    os.environ["AI_MONITOR_SAMPLE_INTERVAL"] = "3600"
    os.environ["AI_MONITOR_GPU_SAMPLE_INTERVAL"] = "3600"
    os.environ["AI_MONITOR_SMART_TTL"] = "3600"
    os.environ["AI_MONITOR_MAX_SESSIONS"] = "3"
    os.environ["AI_MONITOR_LOGIN_RATE_LIMIT"] = "5"
    os.environ["AI_MONITOR_LOGIN_RATE_WINDOW"] = "60"
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import server  # noqa: E402  (env must be set before import)
    return server


@pytest.fixture(scope="session")
def client(server_mod):
    from starlette.testclient import TestClient

    with TestClient(server_mod.app) as c:
        # Prime the sample cache so /api/* endpoints don't 503.
        server_mod._sample_once()
        time.sleep(0.05)
        yield c


@pytest.fixture(scope="session")
def admin_setup(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "AdminPass123"})
    assert r.status_code == 200, r.text
    return r.json()


def do_login(client, username="admin", password="AdminPass123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture()
def admin(client, admin_setup):
    """Fresh admin session token per test (re-login is cheap and keeps the
    session-cap test from invalidating other tests' tokens)."""
    return {"Authorization": f"Bearer {do_login(client)['token']}"}
