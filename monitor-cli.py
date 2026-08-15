#!/usr/bin/env python3
"""
monitor-cli.py - Command-line management for System Monitor v4

Usage:
  monitor-cli.py status                          Show auth status
  monitor-cli.py reset-password [new-password]   Reset admin password (interactive if omitted)
  monitor-cli.py set-password <user> <new>       Set password for existing admin (no old needed, CLI is local root)
  monitor-cli.py key list                        List API keys
  monitor-cli.py key create <name>               Create API key
  monitor-cli.py key delete <name>               Delete API key
  monitor-cli.py alerts                          Show active alerts
  monitor-cli.py alerts add <msg> [sev]          Add a manual (pinned) alert
  monitor-cli.py alerts ack <rule_id>            Acknowledge alert
  monitor-cli.py alerts delete <rule_id>         Delete alert
  monitor-cli.py alerts clear                    Clear resolved history

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


def _hash_password(password: str, salt: bytes) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER).hex()


def load_auth() -> dict:
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


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


def cmd_status():
    data = load_auth()
    admin = data.get("admin")
    if not admin:
        print("Auth NOT configured. First browser visit to the dashboard will show the setup screen.")
        return
    print(f"Admin user : {admin.get('username')}")
    print(f"Created at : {admin.get('created_at')}")
    keys = data.get("keys", {})
    print(f"API keys   : {len(keys)}")
    for name, k in keys.items():
        print(f"  - {name} ({k.get('key', '')[:12]}…) created {k.get('created_at')}, last used {k.get('last_used')}")
    sessions = {t: s for t, s in data.get("sessions", {}).items() if s.get("expires_at", 0) > time.time()}
    print(f"Sessions   : {len(sessions)} active")


def cmd_reset_password():
    if os.geteuid() != 0:
        print("Error: reset-password requires root (run with sudo).", file=sys.stderr)
        sys.exit(1)
    data = load_auth()
    if not data.get("admin"):
        print("Error: no admin configured yet. Set it up via the dashboard first.", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) >= 3:
        new = sys.argv[2]
    else:
        new = getpass.getpass("New password (min 8 chars): ")
        confirm = getpass.getpass("Confirm password: ")
        if new != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            sys.exit(1)
    if len(new) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    salt = secrets.token_bytes(16)
    data["admin"]["salt"] = salt.hex()
    data["admin"]["hash"] = _hash_password(new, salt)
    # Invalidate all existing sessions
    data["sessions"] = {}
    save_auth(data)
    print(f"Password for '{data['admin']['username']}' reset. All web sessions invalidated.")


def cmd_set_password():
    if os.geteuid() != 0:
        print("Error: requires root.", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 4:
        print("Usage: monitor-cli.py set-password <user> <new-password>", file=sys.stderr)
        sys.exit(1)
    user, new = sys.argv[2], sys.argv[3]
    data = load_auth()
    admin = data.get("admin")
    if not admin or admin.get("username") != user:
        print(f"Error: admin user '{user}' not found.", file=sys.stderr)
        sys.exit(1)
    if len(new) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    salt = secrets.token_bytes(16)
    admin["salt"] = salt.hex()
    admin["hash"] = _hash_password(new, salt)
    save_auth(data)
    print(f"Password for '{user}' updated.")


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
            print(f"  {name:<20} {k.get('key', '')[:16]}…  created {k.get('created_at')}  last used {k.get('last_used')}")
    elif sub == "create":
        if len(sys.argv) < 4:
            print("Usage: monitor-cli.py key create <name>", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[3]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            print("Error: name must be alphanumeric (plus . _ -)", file=sys.stderr)
            sys.exit(1)
        if name in keys:
            print(f"Error: key '{name}' already exists.", file=sys.stderr)
            sys.exit(1)
        key = "amk_" + secrets.token_urlsafe(24)
        keys[name] = {"name": name, "key": key, "created_at": now_iso(), "last_used": None}
        save_auth(data)
        print(f"API key created for '{name}':")
        print(f"  {key}")
        print("Use it as: curl -H 'Authorization: Bearer <key>' https://host:9000/api/summary")
    elif sub == "delete":
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
    elif cmd == "reset-password":
        cmd_reset_password()
    elif cmd == "set-password":
        cmd_set_password()
    elif cmd == "key":
        cmd_key()
    elif cmd == "alerts":
        cmd_alerts()
    else:
        print(f"Unknown command: {cmd}\n{__doc__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
