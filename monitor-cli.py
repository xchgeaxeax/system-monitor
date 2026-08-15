#!/usr/bin/env python3
"""
monitor-cli.py - Command-line management for System Monitor v4 (multi-user)

Usage:
  monitor-cli.py status                              Show users/keys/sessions
  monitor-cli.py user list                           List users
  monitor-cli.py user create <user> <password> [admin|user]
  monitor-cli.py user delete <user>
  monitor-cli.py user reset-password <user> [new-password]
  monitor-cli.py user role <user> <admin|user>
  monitor-cli.py reset-password [new-password]       (legacy: reset the first admin)
  monitor-cli.py key list                            List API keys
  monitor-cli.py key create <name> [owner]           Create API key (owner defaults to first admin)
  monitor-cli.py key delete <name>
  monitor-cli.py alerts                              Show active alerts
  monitor-cli.py alerts add <msg> [warning|danger]   Add a manual (pinned) alert
  monitor-cli.py alerts ack <rule_id>                Acknowledge alert
  monitor-cli.py alerts delete <rule_id>             Delete alert
  monitor-cli.py alerts clear                        Clear resolved history

Requires: running as root (or a user that can read the data dir).
"""
import getpass
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("AI_MONITOR_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
AUTH_FILE = DATA_DIR / "auth.json"
ALERT_FILE = DATA_DIR / "alerts.json"
PBKDF2_ITER = 390000
ROLES = ("admin", "user")


def _hash_password(password: str, salt: bytes) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER).hex()


def load_auth() -> dict:
    try:
        with open(AUTH_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    # migrate single-admin format
    if data.get("admin") and not data.get("users"):
        old = data.pop("admin")
        uname = old.get("username", "admin")
        old.pop("username", None)
        data["users"] = {uname: {**old, "role": "admin"}}
        for k in data.get("keys", {}).values():
            k.setdefault("owner", uname)
        save_auth(data)
    return data


def save_auth(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def load_alerts() -> dict:
    try:
        with open(ALERT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"active": {}, "history": []}


def save_alerts(data: dict):
    tmp = ALERT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(ALERT_FILE)


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_root():
    if os.geteuid() != 0:
        print("Error: this command requires root (run with sudo).", file=sys.stderr)
        sys.exit(1)


def cmd_status():
    data = load_auth()
    users = data.get("users", {})
    if not users:
        print("Auth NOT configured. First browser visit to the dashboard will show the setup screen.")
        return
    print(f"Users ({len(users)}):")
    for name, u in users.items():
        print(f"  {name:<20} role={u.get('role', 'user'):<6} created {u.get('created_at')}")
    keys = data.get("keys", {})
    print(f"API keys ({len(keys)}):")
    for name, k in keys.items():
        print(f"  {name:<20} owner={k.get('owner', '?'):<15} created {k.get('created_at')}  last used {k.get('last_used')}")
    sessions = {t: s for t, s in data.get("sessions", {}).items() if s.get("expires_at", 0) > time.time()}
    print(f"Active web sessions: {len(sessions)}")
    for t, s in sessions.items():
        print(f"  {s.get('username')}  (since {s.get('created_at')})")


def cmd_user():
    if len(sys.argv) < 3:
        print("Usage: monitor-cli.py user <list|create|delete|reset-password|role> [args]", file=sys.stderr)
        sys.exit(1)
    sub = sys.argv[2]
    if sub == "list":
        users = load_auth().get("users", {})
        if not users:
            print("No users.")
        for name, u in users.items():
            print(f"  {name:<20} role={u.get('role', 'user'):<6} created {u.get('created_at')}")
        return

    check_root()
    if sub == "create":
        if len(sys.argv) < 5:
            print("Usage: monitor-cli.py user create <user> <password> [admin|user]", file=sys.stderr)
            sys.exit(1)
        user, pw = sys.argv[3], sys.argv[4]
        role = sys.argv[5] if len(sys.argv) > 5 else "user"
        if role not in ROLES:
            print("Error: role must be admin or user", file=sys.stderr)
            sys.exit(1)
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,32}", user):
            print("Error: invalid username", file=sys.stderr)
            sys.exit(1)
        if len(pw) < 8:
            print("Error: password must be at least 8 characters", file=sys.stderr)
            sys.exit(1)
        data = load_auth()
        if user in data.get("users", {}):
            print(f"Error: user '{user}' already exists.", file=sys.stderr)
            sys.exit(1)
        salt = secrets.token_bytes(16)
        data.setdefault("users", {})[user] = {
            "username": user, "salt": salt.hex(),
            "hash": _hash_password(pw, salt), "role": role, "created_at": now_iso(),
        }
        save_auth(data)
        print(f"User '{user}' created (role={role}).")
    elif sub == "delete":
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py user delete <user>", file=sys.stderr)
            sys.exit(1)
        user = sys.argv[3]
        data = load_auth()
        u = data.get("users", {}).get(user)
        if not u:
            print(f"Error: user '{user}' not found.", file=sys.stderr)
            sys.exit(1)
        if u.get("role") == "admin" and sum(1 for x in data["users"].values() if x.get("role") == "admin") <= 1:
            print("Error: cannot delete the last admin.", file=sys.stderr)
            sys.exit(1)
        del data["users"][user]
        data["sessions"] = {t: s for t, s in data.get("sessions", {}).items() if s.get("username") != user}
        data["keys"] = {k: v for k, v in data.get("keys", {}).items() if v.get("owner") != user}
        save_auth(data)
        print(f"User '{user}' deleted (sessions and keys removed).")
    elif sub == "reset-password":
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py user reset-password <user> [new-password]", file=sys.stderr)
            sys.exit(1)
        user = sys.argv[3]
        new = sys.argv[4] if len(sys.argv) > 4 else getpass.getpass("New password (min 8 chars): ")
        if len(new) < 8:
            print("Error: password must be at least 8 characters.", file=sys.stderr)
            sys.exit(1)
        data = load_auth()
        u = data.get("users", {}).get(user)
        if not u:
            print(f"Error: user '{user}' not found.", file=sys.stderr)
            sys.exit(1)
        salt = secrets.token_bytes(16)
        u["salt"] = salt.hex()
        u["hash"] = _hash_password(new, salt)
        save_auth(data)
        print(f"Password for '{user}' reset.")
    elif sub == "role":
        if len(sys.argv) < 5:
            print("Usage: monitor-cli.py user role <user> <admin|user>", file=sys.stderr)
            sys.exit(1)
        user, role = sys.argv[3], sys.argv[4]
        if role not in ROLES:
            print("Error: role must be admin or user", file=sys.stderr)
            sys.exit(1)
        data = load_auth()
        u = data.get("users", {}).get(user)
        if not u:
            print(f"Error: user '{user}' not found.", file=sys.stderr)
            sys.exit(1)
        if u.get("role") == "admin" and role != "admin" and sum(1 for x in data["users"].values() if x.get("role") == "admin") <= 1:
            print("Error: cannot demote the last admin.", file=sys.stderr)
            sys.exit(1)
        u["role"] = role
        save_auth(data)
        print(f"User '{user}' role set to {role}.")
    else:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def cmd_reset_password():
    """Legacy: reset password of the first admin user."""
    check_root()
    data = load_auth()
    users = data.get("users", {})
    if not users:
        print("Error: no users configured yet. Set up via the dashboard first.", file=sys.stderr)
        sys.exit(1)
    admin = next((u for u in users.values() if u.get("role") == "admin"), None)
    if not admin:
        print("Error: no admin user found.", file=sys.stderr)
        sys.exit(1)
    name = admin["username"]
    new = sys.argv[2] if len(sys.argv) >= 3 else getpass.getpass(f"New password for '{name}' (min 8 chars): ")
    if len(new) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    salt = secrets.token_bytes(16)
    admin["salt"] = salt.hex()
    admin["hash"] = _hash_password(new, salt)
    data["sessions"] = {}
    save_auth(data)
    print(f"Password for admin '{name}' reset. All web sessions invalidated.")


def cmd_key():
    if len(sys.argv) < 3:
        print("Usage: monitor-cli.py key <list|create|delete> [args]", file=sys.stderr)
        sys.exit(1)
    sub = sys.argv[2]
    data = load_auth()
    keys = data.setdefault("keys", {})
    if sub == "list":
        if not keys:
            print("No API keys.")
        for name, k in keys.items():
            print(f"  {name:<20} owner={k.get('owner', '?'):<15} created {k.get('created_at')}  last used {k.get('last_used')}")
    elif sub == "create":
        check_root()
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py key create <name> [owner]", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[3]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            print("Error: name must be alphanumeric (plus . _ -)", file=sys.stderr)
            sys.exit(1)
        if name in keys:
            print(f"Error: key '{name}' already exists.", file=sys.stderr)
            sys.exit(1)
        owner = sys.argv[4] if len(sys.argv) > 4 else None
        if owner is None:
            admin = next((u for u in data.get("users", {}).values() if u.get("role") == "admin"), None)
            owner = admin["username"] if admin else "admin"
        elif owner not in data.get("users", {}):
            print(f"Error: owner '{owner}' is not a user.", file=sys.stderr)
            sys.exit(1)
        key = "amk_" + secrets.token_urlsafe(24)
        keys[name] = {"name": name, "owner": owner, "key": key, "created_at": now_iso(), "last_used": None}
        save_auth(data)
        print(f"API key created for '{name}' (owner: {owner}):")
        print(f"  {key}")
        print("Use it as: curl -H 'Authorization: Bearer ***' https://host/api/summary")
    elif sub == "delete":
        check_root()
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py key delete <name>", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[3]
        if keys.pop(name, None) is None:
            print(f"Error: key '{name}' not found.", file=sys.stderr)
            sys.exit(1)
        save_auth(data)
        print(f"API key '{name}' deleted.")
    else:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def cmd_alerts():
    data = load_alerts()
    if len(sys.argv) >= 3 and sys.argv[2] == "add":
        if len(sys.argv) < 5:
            print("Usage: monitor-cli.py alerts add <message> [warning|danger]", file=sys.stderr)
            sys.exit(1)
        msg = sys.argv[3]
        sev = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] in ("warning", "danger") else "warning"
        rid = "manual:" + secrets.token_hex(4)
        data["active"][rid] = {
            "rule_id": rid, "family": "manual", "pinned": True,
            "severity": sev, "message": msg, "since": now_iso(), "acknowledged": False,
        }
        save_alerts(data)
        print(f"Alert added ({rid}): {msg}")
        return
    if len(sys.argv) >= 3 and sys.argv[2] == "ack":
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py alerts ack <rule_id>", file=sys.stderr)
            sys.exit(1)
        a = data["active"].get(sys.argv[3])
        if not a:
            print("Error: active alert not found.", file=sys.stderr)
            sys.exit(1)
        a["acknowledged"] = True
        a["acknowledged_at"] = now_iso()
        save_alerts(data)
        print(f"Alert acknowledged: {sys.argv[3]}")
        return
    if len(sys.argv) >= 3 and sys.argv[2] == "delete":
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py alerts delete <rule_id>", file=sys.stderr)
            sys.exit(1)
        rid = sys.argv[3]
        if rid in data["active"]:
            data["active"].pop(rid)
        else:
            found = False
            for h in data["history"]:
                if h.get("rule_id") == rid:
                    data["history"].remove(h)
                    found = True
                    break
            if not found:
                print("Error: alert not found.", file=sys.stderr)
                sys.exit(1)
        save_alerts(data)
        print(f"Alert deleted: {rid}")
        return
    if len(sys.argv) >= 3 and sys.argv[2] == "clear":
        n = len(data["history"])
        data["history"] = []
        save_alerts(data)
        print(f"Cleared {n} resolved history entries.")
        return

    active = data["active"]
    if not active:
        print("No active alerts. All clear.")
    for a in active.values():
        mark = "✓ " if a.get("acknowledged") else "⚠ "
        print(f"  {mark}[{a['severity'].upper():7}] {a['message']}")
        print(f"         since {a['since']}" + (f"  (ack {a.get('acknowledged_at')})" if a.get("acknowledged") else ""))
    hist = data["history"][:10]
    if hist:
        print(f"\nLast {len(hist)} resolved:")
        for h in hist:
            print(f"  [{h['severity'].upper():7}] {h['message']}  (since {h['since']}, resolved {h.get('resolved_at', '?')})")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd == "user":
        cmd_user()
    elif cmd == "reset-password":
        cmd_reset_password()
    elif cmd == "key":
        cmd_key()
    elif cmd == "alerts":
        cmd_alerts()
    else:
        print(f"Unknown command: {cmd}\n{__doc__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
