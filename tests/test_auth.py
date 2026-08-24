"""Auth flow, RBAC, API keys, and login security (rate limit, session cap)."""
import time

import pytest

from conftest import do_login


# ── First-run setup ─────────────────────────────────────────────────────
def test_setup_creates_admin(client, admin_setup):
    assert admin_setup["role"] == "admin"
    assert admin_setup["username"] == "admin"
    assert len(admin_setup["token"]) == 64


def test_setup_rejected_when_configured(client, admin_setup):
    r = client.post("/api/auth/setup", json={"username": "other", "password": "OtherPass123"})
    assert r.status_code == 409


def test_auth_status_reports_configured(client, admin_setup):
    assert client.get("/api/auth/status").json() == {"configured": True}


def test_setup_validation(client, admin_setup):
    # (already configured, but the endpoint validates inputs before the guard
    # in create_admin only after the limiter; invalid username -> 400)
    r = client.post("/api/auth/setup", json={"username": "x", "password": "short"})
    assert r.status_code in (400, 409)


# ── Login ───────────────────────────────────────────────────────────────
def test_login_wrong_password_401(client, admin_setup):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401


def test_login_unknown_user_401(client, admin_setup):
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever123"})
    assert r.status_code == 401


def test_login_success_returns_token_and_role(client, admin_setup):
    body = do_login(client)
    assert body["role"] == "admin"
    assert body["token"]


def test_me_reflects_identity(client, admin):
    r = client.get("/api/auth/me", headers=admin)
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["role"] == "admin"


def test_logout_invalidates_token(client, admin_setup, server_mod):
    tok = do_login(client)["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/summary", headers=h).status_code == 200
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    # token is removed from the session store and no longer authenticates
    import server
    with server._auth_lock:
        data = server._load_auth()
    assert tok not in data.get("sessions", {})
    assert client.get("/api/summary", headers=h).status_code == 401


# ── RBAC ────────────────────────────────────────────────────────────────
def test_unauthenticated_api_is_401(client, admin_setup):
    for path in ("/api/summary", "/api/cpu", "/api/processes", "/api/alerts"):
        assert client.get(path).status_code == 401, path


def test_regular_user_cannot_manage_users(client, admin):
    # create a regular user
    r = client.post("/api/users", headers=admin,
                    json={"username": "bob", "password": "BobPass12345", "role": "user"})
    assert r.status_code == 200
    bob = {"Authorization": f"Bearer {do_login(client, 'bob', 'BobPass12345')['token']}"}
    # bob cannot list/create users
    assert client.get("/api/users", headers=bob).status_code == 403
    assert client.post("/api/users", headers=bob,
                       json={"username": "eve", "password": "EvePass12345", "role": "user"}).status_code == 403


def test_regular_user_cannot_kill_processes(client, admin):
    r = client.post("/api/users", headers=admin,
                    json={"username": "carol", "password": "CarolPass123", "role": "user"})
    assert r.status_code == 200
    carol = {"Authorization": f"Bearer {do_login(client, 'carol', 'CarolPass123')['token']}"}
    assert client.post("/api/processes/kill", headers=carol,
                       json={"pid": 1, "sig": 15}).status_code == 403


def test_regular_user_cannot_save_webhooks(client, admin):
    r = client.post("/api/users", headers=admin,
                    json={"username": "dave", "password": "DavePass1234", "role": "user"})
    assert r.status_code == 200
    dave = {"Authorization": f"Bearer {do_login(client, 'dave', 'DavePass1234')['token']}"}
    r = client.put("/api/webhooks", headers=dave,
                   json={"enabled": False, "channels": [], "cooldown_s": 3600})
    assert r.status_code == 403


def test_admin_can_delete_user(client, admin):
    r = client.post("/api/users", headers=admin,
                    json={"username": "tempuser", "password": "TempPass1234", "role": "user"})
    assert r.status_code == 200
    assert client.delete("/api/users/tempuser", headers=admin).status_code == 200
    # gone
    users = client.get("/api/users", headers=admin).json()
    assert "tempuser" not in [u["username"] for u in users]


# ── API keys ────────────────────────────────────────────────────────────
def test_api_key_full_flow(client, admin):
    r = client.post("/api/auth/keys", headers=admin, json={"name": "cikey"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("amk_")
    # key can read data endpoints
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/summary", headers=h).status_code == 200
    # but cannot manage users (web-admin only)
    assert client.get("/api/users", headers=h).status_code == 403
    # list shows prefix, not full key
    keys = client.get("/api/auth/keys", headers=admin).json()
    entry = next(k for k in keys if k["name"] == "cikey")
    assert entry["prefix"].startswith("amk_")
    assert key not in str(entry)
    # delete
    assert client.delete("/api/auth/keys/cikey", headers=admin).status_code == 200
    # key dead now
    assert client.get("/api/summary", headers=h).status_code == 401


# ── Login rate limiting ─────────────────────────────────────────────────
def test_login_rate_limit_429(client, admin_setup):
    # Dedicated username so we don't exhaust the admin window.
    client.post("/api/users", headers={"Authorization": f"Bearer {admin_setup['token']}"},
                json={"username": "ratelimit", "password": "RatePass1234", "role": "user"})
    # 5 wrong attempts allowed, 6th is 429
    codes = []
    for _ in range(6):
        r = client.post("/api/auth/login",
                        json={"username": "ratelimit", "password": "nope"})
        codes.append(r.status_code)
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    # even the RIGHT password is blocked while the window is hot
    r = client.post("/api/auth/login",
                    json={"username": "ratelimit", "password": "RatePass1234"})
    assert r.status_code == 429


# ── Session cap ─────────────────────────────────────────────────────────
def test_session_cap_evicts_oldest(client, admin_setup, server_mod):
    import server
    tokens = []
    for i in range(5):
        tok = do_login(client, "admin", "AdminPass123")["token"]
        tokens.append(tok)
        time.sleep(0.02)
    with server._auth_lock:
        data = server._load_auth()
    n = len(data.get("sessions", {}))
    assert n <= server.MAX_SESSIONS, f"expected <= {server.MAX_SESSIONS} sessions, got {n}"
    # oldest token evicted, newest kept
    assert tokens[0] not in data["sessions"]
    assert tokens[-1] in data["sessions"]
