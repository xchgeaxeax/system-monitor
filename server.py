#!/usr/bin/env python3
"""
System Performance Monitor v4
- On-demand refresh architecture: only responds to requests when page is visible
- Background sampler thread (1.5s) keeps metrics hot; API endpoints are non-blocking reads
- Auth: username/password (first-run setup) + API keys for tool access
- Alert engine: persistent alert history with acknowledge/delete
- Covers: CPU/GPU/Memory/Storage/Network/Temperature/Processes/System Logs
"""
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from uvicorn import Config, Server

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if os.getenv("AI_MONITOR_DEBUG", "").lower() in ("1", "true") else logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ai-monitor")

# ── Config (env overrides) ──────────────────────────────────────────────
VERSION = "4.0"
PORT = int(os.getenv("AI_MONITOR_PORT", "9527"))
HOST = os.getenv("AI_MONITOR_HOST", "0.0.0.0")
DEBUG = os.getenv("AI_MONITOR_DEBUG", "").lower() in ("1", "true")
PROC = Path("/proc")
SYSFS = Path("/sys")
ROCM_SMI = os.getenv("AI_MONITOR_ROCM_SMI", "rocm-smi")
INTEL_GPU_TOP = os.getenv("AI_MONITOR_INTEL_GPU_TOP", "intel_gpu_top")
SMARTCTL = os.getenv("AI_MONITOR_SMARTCTL", "smartctl")
JOURNALCTL = os.getenv("AI_MONITOR_JOURNALCTL", "journalctl")
NVME_CLI = os.getenv("AI_MONITOR_NVME", "nvme")

DATA_DIR = Path(os.getenv("AI_MONITOR_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUTH_FILE = DATA_DIR / "auth.json"
ALERT_FILE = DATA_DIR / "alerts.json"

SAMPLE_INTERVAL = float(os.getenv("AI_MONITOR_SAMPLE_INTERVAL", "1.5"))
HISTORY_WINDOW_S = int(os.getenv("AI_MONITOR_HISTORY_WINDOW", "300"))  # 5 min
HISTORY_MAX_POINTS = int(os.getenv("AI_MONITOR_HISTORY_POINTS", "240"))
SMART_TTL = int(os.getenv("AI_MONITOR_SMART_TTL", "60"))
TOOL_CHECK_TTL = 300
SESSION_TTL_S = int(os.getenv("AI_MONITOR_SESSION_TTL", str(12 * 3600)))
GPU_SAMPLE_INTERVAL = 2.0

# ── Small helpers ───────────────────────────────────────────────────────
def parse_float(s: Any, default: float = 0.0) -> float:
    if s is None:
        return default
    s = str(s).strip().strip("()")
    m = re.search(r"(\d+\.?\d*)", s)
    return float(m.group(1)) if m else default


def read_file_int(path: Path, default: int = 0) -> int:
    try:
        v = path.read_text().strip()
        if v.startswith("0x") or v.startswith("0X"):
            return int(v, 16)
        return int(v)
    except Exception:
        return default


def read_file_float(path: Path, default: float = 0.0) -> float:
    try:
        return float(path.read_text().strip())
    except Exception:
        return default


def read_file_str(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return default


def run_cmd(args: List[str], timeout: int = 3, default: Any = None) -> Any:
    try:
        out = subprocess.check_output(args, timeout=timeout, stderr=subprocess.DEVNULL).decode().strip()
        return out if out else default
    except subprocess.TimeoutExpired:
        logger.warning(f"Command timed out ({timeout}s): {' '.join(args)}")
        return default
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.debug(f"Command failed: {' '.join(args)}: {e}")
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── History (time-window ring buffer) ───────────────────────────────────
class History:
    """Time-windowed ring buffer. Points: (ts, *values)."""

    def __init__(self, window_s: int = HISTORY_WINDOW_S, max_points: int = HISTORY_MAX_POINTS):
        self.window_s = window_s
        self.max_points = max_points
        self.points: List[tuple] = []
        self.lock = threading.Lock()

    def append(self, item: tuple):
        with self.lock:
            self.points.append(item)
            self._trim()

    def _trim(self):
        cutoff = time.time() - self.window_s
        while self.points and self.points[0][0] < cutoff:
            self.points.pop(0)
        if len(self.points) > self.max_points:
            del self.points[: len(self.points) - self.max_points]

    def last(self, n: int = 0) -> List[tuple]:
        with self.lock:
            return self.points[-n:] if n else list(self.points)


_net_history = History()          # (ts, rx_mbps, tx_mbps)
_cpu_freq_history = History()     # (ts, mhz)
_disk_io_history = History()      # (ts, {disk: (r_mbps, w_mbps)})
_gpu_history: Dict[str, History] = {}  # gpu_id -> (ts, util, vram_pct, temp)
_gpu_history_lock = threading.Lock()

# ── GPU topology cache ──────────────────────────────────────────────────
_gpu_topology: Optional[List[Dict]] = None
_gpu_topology_time: float = 0


def detect_gpu_topology() -> List[Dict]:
    global _gpu_topology, _gpu_topology_time
    now = time.time()
    if _gpu_topology is not None and (now - _gpu_topology_time) < TOOL_CHECK_TTL:
        return _gpu_topology

    topology = []
    for card in sorted(SYSFS.glob("class/drm/card*")):
        if any(x in card.name for x in ("-DP", "-HDMI", "-Writeback")):
            continue
        dev = card / "device"
        vendor_id = read_file_int(dev / "vendor", 0)
        device_id = read_file_int(dev / "device", 0)
        if vendor_id == 0:
            continue

        vendor_name = "AMD" if vendor_id == 0x1002 else ("Intel" if vendor_id == 0x8086 else "Unknown")
        bus_id_str = ""
        uevent = read_file_str(dev / "uevent", "")
        for line in uevent.split("\n"):
            if line.startswith("PCI_SLOT_NAME="):
                bus_id_str = line.split("=", 1)[1]
                break

        product_name = f"{vendor_name} GPU"
        try:
            with open(PROC / "pci/devices") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0] == hex(device_id):
                        product_name = " ".join(parts[1:])
                        break
        except Exception:
            pass

        topology.append({
            "id": card.name,
            "vendor": vendor_name.lower(),
            "vendor_id": vendor_id,
            "device_id": device_id,
            "bus_id": bus_id_str,
            "name": product_name,
        })

    _gpu_topology = topology
    _gpu_topology_time = now
    return topology


def _ensure_gpu_history(gid: str) -> History:
    with _gpu_history_lock:
        if gid not in _gpu_history:
            _gpu_history[gid] = History()
        return _gpu_history[gid]


# ── GPU detail (called by GPU sampler thread) ───────────────────────────
def collect_gpu_detail() -> Dict:
    info: Dict[str, List] = {"rocm": [], "intel": [], "sysfs": []}
    topology = detect_gpu_topology()

    # ── AMD via rocm-smi ──
    amd_topo = [t for t in topology if t["vendor"] == "amd"]
    out1 = run_cmd([ROCM_SMI, "--showproductname", "--showtemp", "--showpower", "--showmemuse", "--showvoltage", "--json"], timeout=2)
    if out1:
        out2 = run_cmd([ROCM_SMI, "-g", "-c", "-m", "-o", "--json"], timeout=2)
        out3 = run_cmd([ROCM_SMI, "--showpids", "verbose", "--json"], timeout=2)
        try:
            data1 = json.loads(out1)
            data2 = json.loads(out2) if out2 else {}
            data3 = json.loads(out3) if out3 else {}

            # VRAM per AMD card from sysfs, in sorted card order
            amd_cards = sorted(
                c for c in SYSFS.glob("class/drm/card*")
                if read_file_int(c / "device" / "vendor", 0) == 0x1002
            )
            amd_vram = []
            for card in amd_cards:
                dev = card / "device"
                vt = read_file_int(dev / "mem_info_vram_total", 0)
                vu = read_file_int(dev / "mem_info_vram_used", 0)
                amd_vram.append((round(vt / 1024**2, 1), round(vu / 1024**2, 1)))

            # Map rocm-smi card keys to topology order
            card_keys = [k for k in data1 if k != "rocm_smi"]
            for idx, card_key in enumerate(card_keys):
                if idx >= len(amd_topo):
                    break
                g = data1[card_key]
                g2 = data2.get(card_key, {})
                g3 = data3.get(card_key, {})
                t = amd_topo[idx]

                name = (g.get("Card Series") or g.get("Card Model") or t["name"]).strip()
                vram_total_mb, vram_used_mb = (amd_vram[idx] if idx < len(amd_vram) else (0, 0))

                gpu_pids = []
                if isinstance(g3, dict):
                    for pid_key in g3:
                        if pid_key.startswith("PID"):
                            pid_info = g3[pid_key]
                            gpu_pids.append({
                                "pid": pid_info.get("PID", 0),
                                "name": str(pid_info.get("Process Name", "Unknown"))[:40],
                                "vram_used_mb": parse_float(pid_info.get("VRAM Used", 0)),
                                "is_compute": "Yes" in str(pid_info.get("Is Compute Process", "")),
                            })

                vram_pct = round(parse_float(g.get("GPU Memory Allocated (VRAM%)", 0)), 1)
                gpu_util = round(parse_float(g.get("GPU Activity (%)", 0)), 1)
                temp_edge = round(parse_float(g.get("Temperature (Sensor edge) (C)", 0)), 1)

                info["rocm"].append({
                    "id": t["id"],
                    "name": name,
                    "bus_id": t["bus_id"],
                    "temp_edge": temp_edge,
                    "temp_junction": round(parse_float(g.get("Temperature (Sensor junction) (C)", 0)), 1),
                    "temp_memory": round(parse_float(g.get("Temperature (Sensor memory) (C)", 0)), 1),
                    "power_w": round(parse_float(g.get("Average Graphics Package Power (W)", 0)), 1),
                    "vram_used_mb": vram_used_mb,
                    "vram_total_mb": vram_total_mb,
                    "vram_percent": vram_pct,
                    "utilization": gpu_util,
                    "mem_activity": round(parse_float(g.get("GPU Memory Read/Write Activity (%)", 0)), 1),
                    "sclk": round(parse_float(g2.get("sclk clock speed:", 0)), 0),
                    "mclk": round(parse_float(g2.get("mclk clock speed:", 0)), 0),
                    "socclk": round(parse_float(g2.get("socclk clock speed:", 0)), 0),
                    "pids": gpu_pids,
                })
                _ensure_gpu_history(t["id"]).append((time.time(), gpu_util, vram_pct, temp_edge))
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse rocm-smi output: {e}")

    # ── Intel via intel_gpu_top ──
    intel_topo = [t for t in topology if t["vendor"] == "intel"]
    if intel_topo:
        out = run_cmd([INTEL_GPU_TOP, "-J", "-n", "2"], timeout=5)
        if out:
            try:
                samples = []
                stripped = out.strip()
                decoder = json.JSONDecoder()
                idx = 0
                while idx < len(stripped):
                    start = stripped.find("{", idx)
                    if start == -1:
                        break
                    try:
                        obj, end = decoder.raw_decode(stripped[start:])
                        samples.append(obj)
                        idx = start + end
                    except json.JSONDecodeError:
                        idx = start + 1
                if samples:
                    data = max(samples, key=lambda s: s.get("period", {}).get("duration", 0))
                    engines = data.get("engines", {})
                    power = data.get("power", {})
                    freq = data.get("frequency", {})
                    rc6 = data.get("rc6", {})
                    total_busy = sum(
                        parse_float(engines.get(e, {}).get("busy", 0))
                        for e in ("Render/3D", "Blitter", "Video", "VideoEnhance", "Compute")
                    )
                    total_busy = round(total_busy / len(engines) if engines else 0, 1)
                    gpu_power = parse_float(power.get("GPU", 0))
                    freq_actual_mhz = parse_float(freq.get("actual", 0))
                    if freq_actual_mhz == 0:
                        nvtop_out = run_cmd(["nvtop", "-s"], timeout=2)
                        if nvtop_out:
                            try:
                                nvtop_data = json.loads(nvtop_out)
                                if isinstance(nvtop_data, list) and nvtop_data:
                                    m = re.search(r"(\d+)", str(nvtop_data[0].get("gpu_clock", "")))
                                    if m:
                                        freq_actual_mhz = int(m.group(1))
                            except (json.JSONDecodeError, IndexError):
                                pass
                    for t in intel_topo:
                        info["intel"].append({
                            "id": t["id"],
                            "name": t["name"],
                            "bus_id": t["bus_id"],
                            "total_usage": total_busy,
                            "render_usage": parse_float(engines.get("Render/3D", {}).get("busy", 0)),
                            "blitter_usage": parse_float(engines.get("Blitter", {}).get("busy", 0)),
                            "video_usage": parse_float(engines.get("Video", {}).get("busy", 0)),
                            "compute_usage": parse_float(engines.get("Compute", {}).get("busy", 0)),
                            "freq_actual_mhz": freq_actual_mhz,
                            "rc6": parse_float(rc6.get("value", 0)),
                            "power_w": gpu_power,
                        })
                        _ensure_gpu_history(t["id"]).append((time.time(), total_busy, 0, 0))
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse intel_gpu_top output: {e}")

    # ── Sysfs fallback for uncovered GPUs ──
    covered_ids = {g["id"] for g in info["rocm"] + info["intel"]}
    for t in topology:
        if t["id"] in covered_ids:
            continue
        dev = SYSFS / f"class/drm/{t['id']}/device"
        gpu_busy = read_file_float(dev / "gpu_busy_percent", 0)
        mem_busy = read_file_float(dev / "mem_busy_percent", 0)
        vram_total = read_file_int(dev / "mem_info_vram_total", 0)
        vram_used = read_file_int(dev / "mem_info_vram_used", 0)

        temp_c = 0
        for hwmon_dir in sorted(SYSFS.glob(f"class/drm/{t['id']}/device/hwmon*")):
            if hwmon_dir.is_dir():
                t_val = read_file_int(hwmon_dir / "temp1_input", 0)
                if 0 < t_val < 100000:
                    temp_c = round(t_val / 1000, 1)
                break

        power_w = 0
        for hwmon_dir in sorted(SYSFS.glob(f"class/drm/{t['id']}/device/hwmon*")):
            if hwmon_dir.is_dir():
                p_val = read_file_int(hwmon_dir / "power1_average", 0)
                if p_val > 0:
                    power_w = round(p_val / 1000000, 1)
                break

        info["sysfs"].append({
            "id": t["id"],
            "name": t["name"],
            "bus_id": t["bus_id"],
            "vendor": t["vendor"],
            "utilization": round(gpu_busy, 1),
            "mem_utilization": round(mem_busy, 1),
            "vram_used_mb": round(vram_used / 1024**2, 1) if vram_used > 0 else 0,
            "vram_total_mb": round(vram_total / 1024**2, 1) if vram_total > 0 else 0,
            "temperature": temp_c,
            "power_w": power_w,
        })
        _ensure_gpu_history(t["id"]).append((time.time(), gpu_busy, 0, temp_c))

    return info


# ── Auth (multi-user: admin / regular) ─────────────────────────────────
PBKDF2_ITER = 390000
_auth_lock = threading.Lock()  # guards auth.json read-modify-write
ROLES = ("admin", "user")


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER).hex()


def _load_auth() -> Dict:
    try:
        with open(AUTH_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    # Migrate v4.0 single-admin format -> multi-user
    if data.get("admin") and not data.get("users"):
        old = data.pop("admin")
        uname = old.get("username", "admin")
        old.pop("username", None)
        data["users"] = {uname: {**old, "role": "admin"}}
        # legacy keys belong to the first admin
        for k in data.get("keys", {}).values():
            k.setdefault("owner", uname)
        _save_auth(data)
    return data


def _save_auth(data: Dict):
    tmp = AUTH_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def auth_configured() -> bool:
    with _auth_lock:
        return bool(_load_auth().get("users"))


def verify_password(password: str, user: Dict) -> bool:
    salt = bytes.fromhex(user.get("salt", ""))
    return secrets.compare_digest(_hash_password(password, salt), user.get("hash", ""))


def _public_user(u: Dict) -> Dict:
    return {
        "username": u.get("username"),
        "role": u.get("role", "user"),
        "created_at": u.get("created_at"),
    }


def _count_admins(data: Dict) -> int:
    return sum(1 for u in data.get("users", {}).values() if u.get("role") == "admin")


def create_admin(username: str, password: str) -> Dict:
    """First-run setup: create the initial admin account."""
    salt = secrets.token_bytes(16)
    with _auth_lock:
        data = _load_auth()
        if data.get("users"):
            raise HTTPException(409, "Auth already configured. Use monitor-cli.py to manage users.")
        data["users"] = {username: {
            "username": username,
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "role": "admin",
            "created_at": now_iso(),
        }}
        data.setdefault("keys", {})
        data.setdefault("sessions", {})
        token = secrets.token_hex(32)
        data["sessions"][token] = {
            "username": username, "kind": "web",
            "created_at": now_iso(), "expires_at": time.time() + SESSION_TTL_S,
        }
        _save_auth(data)
    return {"token": token, "username": username, "role": "admin", "expires_in": SESSION_TTL_S}


def login(username: str, password: str) -> Dict:
    # Verify outside the lock (PBKDF2 takes ~100ms)
    with _auth_lock:
        user = _load_auth().get("users", {}).get(username)
    if not user or not verify_password(password, user):
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_hex(32)
    with _auth_lock:
        data = _load_auth()
        data.setdefault("sessions", {})[token] = {
            "username": username, "kind": "web",
            "created_at": now_iso(), "expires_at": time.time() + SESSION_TTL_S,
        }
        _save_auth(data)
    return {"token": token, "username": username, "role": user.get("role", "user"),
            "expires_in": SESSION_TTL_S}


def _resolve_token(token: str) -> Optional[Dict]:
    if not token:
        return None
    with _auth_lock:
        data = _load_auth()
        # API key?
        for key in data.get("keys", {}).values():
            if secrets.compare_digest(key.get("key", ""), token):
                return {
                    "username": key.get("owner", "admin"),
                    "kind": "api_key",
                    "key_name": key.get("name"),
                    "role": "admin" if key.get("owner", "admin") in data.get("users", {})
                            and data["users"][key.get("owner", "admin")].get("role") == "admin" else "user",
                }
        # Session?
        sess = data.get("sessions", {}).get(token)
        if not sess:
            return None
        if sess.get("expires_at", 0) < time.time():
            del data["sessions"][token]
            _save_auth(data)
            return None
        uname = sess.get("username", "?")
        user = data.get("users", {}).get(uname)
        if not user:  # user was deleted after login
            del data["sessions"][token]
            _save_auth(data)
            return None
        return {"username": uname, "kind": "web", "role": user.get("role", "user")}


def _extract_token(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.query_params.get("token", "")


def require_auth(request: Request) -> Dict:
    token = _extract_token(request)
    if not auth_configured():
        # First-run: only setup/status/health allowed before first admin exists
        if request.url.path in ("/api/auth/setup", "/api/auth/status", "/api/health"):
            return {"username": "setup", "kind": "setup", "role": "admin"}
        raise HTTPException(401, "Setup required")
    ident = _resolve_token(token)
    if not ident:
        raise HTTPException(401, "Unauthorized")
    return ident


def require_admin(request: Request) -> Dict:
    """Web admin only (API keys can never manage users/auth)."""
    ident = require_auth(request)
    if ident["kind"] != "web":
        raise HTTPException(403, "Web admin login required (API keys cannot manage users)")
    if ident.get("role") != "admin":
        raise HTTPException(403, "Admin role required")
    return ident


def require_self_or_admin(ident: Dict, target: str) -> None:
    if ident.get("role") != "admin" and target != ident.get("username"):
        raise HTTPException(403, "You can only manage your own resources")


# ── User management (admin) ─────────────────────────────────────────────
def list_users() -> List[Dict]:
    with _auth_lock:
        data = _load_auth()
        return [_public_user(u) for u in data.get("users", {}).values()]


def create_user(username: str, password: str, role: str) -> Dict:
    if role not in ROLES:
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,32}", username):
        raise HTTPException(400, "Username must be 2-32 chars: letters, digits, . _ -")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    with _auth_lock:
        data = _load_auth()
        if username in data.get("users", {}):
            raise HTTPException(409, "Username already exists")
        data["users"][username] = {
            "username": username,
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "role": role,
            "created_at": now_iso(),
        }
        _save_auth(data)
    return _public_user(data["users"][username])


def delete_user(username: str, actor: str) -> None:
    with _auth_lock:
        data = _load_auth()
        user = data.get("users", {}).get(username)
        if not user:
            raise HTTPException(404, "User not found")
        if user.get("role") == "admin" and _count_admins(data) <= 1:
            raise HTTPException(400, "Cannot delete the last admin")
        if username == actor:
            raise HTTPException(400, "Cannot delete your own account (demote or ask another admin)")
        del data["users"][username]
        # remove their sessions and keys
        data["sessions"] = {t: s for t, s in data.get("sessions", {}).items()
                            if s.get("username") != username}
        data["keys"] = {k: v for k, v in data.get("keys", {}).items()
                        if v.get("owner") != username}
        _save_auth(data)


def reset_user_password(username: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    with _auth_lock:
        data = _load_auth()
        user = data.get("users", {}).get(username)
        if not user:
            raise HTTPException(404, "User not found")
        user["salt"] = salt.hex()
        user["hash"] = _hash_password(new_password, salt)
        _save_auth(data)


def change_user_role(username: str, role: str) -> None:
    if role not in ROLES:
        raise HTTPException(400, "Role must be 'admin' or 'user'")
    with _auth_lock:
        data = _load_auth()
        user = data.get("users", {}).get(username)
        if not user:
            raise HTTPException(404, "User not found")
        if user.get("role") == "admin" and role != "admin" and _count_admins(data) <= 1:
            raise HTTPException(400, "Cannot demote the last admin")
        user["role"] = role
        _save_auth(data)


# ── Alert engine ────────────────────────────────────────────────────────
class AlertManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.active: Dict[str, Dict] = {}   # rule_id -> alert
        self.history: List[Dict] = []       # resolved/deleted alerts, newest first
        self._file_mtime: float = 0
        self._load()

    def _load(self):
        try:
            with open(ALERT_FILE) as f:
                data = json.load(f)
            self.active = data.get("active", {})
            self.history = data.get("history", [])
        except Exception:
            self.active, self.history = {}, []
        try:
            self._file_mtime = ALERT_FILE.stat().st_mtime
        except Exception:
            self._file_mtime = 0

    def _reload_if_changed(self):
        """Pick up external edits (e.g. monitor-cli.py)."""
        try:
            mtime = ALERT_FILE.stat().st_mtime
        except Exception:
            return
        if mtime != self._file_mtime:
            self._load()

    def _save(self):
        tmp = ALERT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"active": self.active, "history": self.history[-500:]}, f, indent=2)
        tmp.replace(ALERT_FILE)
        try:
            self._file_mtime = ALERT_FILE.stat().st_mtime
        except Exception:
            pass

    def evaluate(self, checks: List[Dict], available: Optional[set] = None):
        """checks: [{rule_id, severity, message, active: bool, family: str}]
        available: set of rule families whose data source is currently present.
        Only rules from available families can auto-resolve (prevents
        spurious resolution when a data source is temporarily missing)."""
        with self.lock:
            self._reload_if_changed()
            changed = False
            for c in checks:
                rid = c["rule_id"]
                if c["active"] and rid not in self.active:
                    self.active[rid] = {
                        "rule_id": rid,
                        "family": c.get("family", "core"),
                        "severity": c["severity"],
                        "message": c["message"],
                        "since": now_iso(),
                        "acknowledged": False,
                    }
                    changed = True
            # auto-resolve rules that cleared (only for families with live data)
            for rid in list(self.active.keys()):
                if any(c["rule_id"] == rid and c["active"] for c in checks):
                    continue
                a = self.active[rid]
                if a.get("pinned"):
                    continue  # manually added alert; only removable by delete
                fam = a.get("family", "core")
                if available is not None and fam not in available:
                    continue  # data source unavailable; keep alert
                a = self.active.pop(rid)
                a["resolved_at"] = now_iso()
                a["auto_resolved"] = True
                self.history.insert(0, a)
                changed = True
            if changed:
                self._save()

    def acknowledge(self, rule_id: str) -> bool:
        with self.lock:
            self._reload_if_changed()
            a = self.active.get(rule_id)
            if not a:
                return False
            a["acknowledged"] = True
            a["acknowledged_at"] = now_iso()
            self._save()
            return True

    def delete(self, rule_id: str) -> bool:
        """Delete an active alert (suppresses it until it re-triggers)."""
        with self.lock:
            self._reload_if_changed()
            a = self.active.pop(rule_id, None)
            if not a:
                a = next((h for h in self.history if h.get("rule_id") == rule_id), None)
                if not a:
                    return False
                self.history.remove(a)
            a["deleted_at"] = now_iso()
            self._save()
            return True

    def delete_history(self, index: int) -> bool:
        with self.lock:
            self._reload_if_changed()
            if 0 <= index < len(self.history):
                self.history.pop(index)
                self._save()
                return True
            return False

    def clear_resolved(self) -> int:
        with self.lock:
            self._reload_if_changed()
            n = len(self.history)
            self.history = []
            self._save()
            return n

    def snapshot(self) -> Dict:
        with self.lock:
            self._reload_if_changed()
            return {
                "active": list(self.active.values()),
                "history": self.history[:200],
            }


alerts = AlertManager()


def build_alert_checks(snap: Dict, smart: Dict) -> List[Dict]:
    checks = []
    # Disk usage
    for p in snap.get("partitions", []):
        if p["percent"] >= 90:
            checks.append({
                "rule_id": f"disk_full:{p['mountpoint']}",
                "family": "core",
                "severity": "danger" if p["percent"] >= 95 else "warning",
                "message": f"Disk {p['mountpoint']} at {p['percent']}% ({p['used_gb']}/{p['total_gb']} GB)",
                "active": True,
            })
    # Memory
    if snap.get("mem_percent", 0) >= 90:
        checks.append({
            "rule_id": "mem_high",
            "family": "core",
            "severity": "danger" if snap["mem_percent"] >= 95 else "warning",
            "message": f"Memory at {snap['mem_percent']}% ({snap.get('mem_used_gb', 0)}/{snap.get('mem_total_gb', 0)} GB)",
            "active": True,
        })
    # Swap
    if snap.get("swap_percent", 0) >= 80:
        checks.append({
            "rule_id": "swap_high",
            "family": "core",
            "severity": "warning",
            "message": f"Swap at {snap['swap_percent']}%",
            "active": True,
        })
    # Load
    if snap.get("load_ratio", 0) >= 2:
        checks.append({
            "rule_id": "load_high",
            "family": "core",
            "severity": "warning",
            "message": f"Load average {snap.get('load_avg', [])} is {snap['load_ratio']}x core count",
            "active": True,
        })
    # Temperatures
    for s in snap.get("temps", []):
        crit = s.get("crit_c", 0)
        if crit > 0 and s["temp_c"] > crit * 0.9:
            checks.append({
                "rule_id": f"temp_high:{s['chip']}:{s['label']}",
                "family": "core",
                "severity": "danger",
                "message": f"Temperature {s['chip']} {s['label']} at {s['temp_c']}°C (crit {crit}°C)",
                "active": True,
            })
    # SMART
    for dev, d in smart.items():
        if d.get("health") in ("WARNING", "CHECK"):
            checks.append({
                "rule_id": f"smart:{dev}",
                "family": "smart",
                "severity": "danger",
                "message": f"SMART health issue on {dev} ({d.get('model', '')}): {d['health']}",
                "active": True,
            })
        elif d.get("percentage_used") is not None and d["percentage_used"] >= 90:
            checks.append({
                "rule_id": f"smart_life:{dev}",
                "family": "smart",
                "severity": "warning",
                "message": f"NVMe {dev} ({d.get('model', '')}) at {d['percentage_used']}% of rated life",
                "active": True,
            })
    # GPU VRAM
    for g in snap.get("gpus", []):
        if g.get("vram_percent", 0) >= 95:
            checks.append({
                "rule_id": f"vram_high:{g.get('id', g.get('name', ''))}",
                "family": "gpu",
                "severity": "warning",
                "message": f"GPU {g.get('name', '')} VRAM at {g['vram_percent']}%",
                "active": True,
            })
    return checks


# ── SAMPLER: background thread keeps cache warm ─────────────────────────
_cache: Dict[str, Any] = {
    "cpu": None, "memory": None, "network": None, "storage": None,
    "temps": None, "summary": None, "quick": None, "sample_time": 0,
}
_cache_lock = threading.Lock()

_gpu_cache: Dict[str, Any] = {"data": None, "time": 0}
_gpu_cache_lock = threading.Lock()

_smart_cache: Dict[str, Any] = {"data": None, "time": 0}
_smart_cache_lock = threading.Lock()


def _smart_attr_raw(attrs: Dict[str, Any], attr_id: int) -> Optional[int]:
    """Raw value of a SMART attribute by numeric id (new 7.5 schema: table list)."""
    for a in (attrs or {}).get("table", []):
        if a.get("id") == attr_id:
            rv = a.get("raw", {}).get("value")
            return int(rv) if isinstance(rv, (int, float)) else None
    return None


def _smart_temperature(data: Dict[str, Any], attrs: Dict[str, Any]) -> Optional[float]:
    """Current temperature in °C. Prefers the top-level field (7.5), then the
    temperature attribute (194/190) whose raw string starts with the value."""
    t = data.get("temperature")
    if isinstance(t, dict) and t.get("current") is not None:
        try:
            return float(t["current"])
        except (TypeError, ValueError):
            pass
    for a in (attrs or {}).get("table", []):
        if a.get("id") in (194, 190) or "Temperature" in str(a.get("name", "")):
            m = re.match(r"\s*(\d+)", str(a.get("raw", {}).get("string", "")))
            if m:
                return float(m.group(1))
    return None


def _diskstats_since_boot(dev_name: str):
    """(read_bytes, written_bytes) since boot from /proc/diskstats.

    Returns (None, None) when unavailable. Sector size in diskstats is
    always 512 bytes.
    """
    if not dev_name:
        return None, None
    try:
        with open("/proc/diskstats") as fh:
            for line in fh:
                f = line.split()
                if len(f) > 9 and f[2] == dev_name:
                    return int(f[5]) * 512, int(f[9]) * 512
    except (OSError, ValueError):
        pass
    return None, None


def _parse_smartctl_json(out: str, disk_name: str = "") -> Optional[Dict]:
    """Parse smartctl -j output. Returns a normalized dict or None.

    Handles both the legacy (<=7.1) schema (smart_attributes /
    overall_health_self_assessment, attrs as a dict) and the 7.2+ schema
    (ata_smart_attributes.table list / smart_status, model at top level).
    Without this, smartctl 7.5 reports every SATA drive as N/A.
    """
    try:
        d = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None

    # New (7.2+) schema
    if isinstance(d.get("ata_smart_attributes"), dict):
        attrs = d["ata_smart_attributes"]
        status = d.get("smart_status") or {}
        model = str(d.get("model_name") or d.get("device", {}).get("model_name") or "")[:40]
        serial = str(d.get("serial_number") or d.get("device", {}).get("serial_number") or "")[:20]
        poh = (d.get("power_on_time") or {}).get("hours")
        try:
            poh = int(poh) if poh is not None else 0
        except (TypeError, ValueError):
            poh = 0
        lbs = d.get("logical_block_size") or 512
        try:
            lbs = int(lbs)
        except (TypeError, ValueError):
            lbs = 512
        raw_read = _smart_attr_raw(attrs, 242)
        raw_write = _smart_attr_raw(attrs, 241)
        # Most drives report Total_LBAs_Read/Written in LBA units, but some
        # vendors (notably SanDisk/WD) count in 1000-sector (512 KB) units.
        # Pick the multiplier by vendor, then sanity-check against /proc/
        # diskstats (since-boot counters can never exceed lifetime totals).
        model_l = (model or "").lower()
        unit = 1000 * lbs if any(v in model_l for v in ("sandisk", "wd ", "wd-", "wd_", "western digital")) else lbs
        boot_read, boot_written = _diskstats_since_boot(disk_name)
        if raw_read is not None and raw_write is not None:
            for cand in (unit, lbs, 1000 * lbs):
                ok = True
                if boot_read is not None and cand * raw_read < boot_read:
                    ok = False
                if boot_written is not None and cand * raw_write < boot_written:
                    ok = False
                if ok:
                    unit = cand
                    break
        read_tb = round(raw_read * unit / 1024**4, 3) if raw_read is not None else None
        write_tb = round(raw_write * unit / 1024**4, 3) if raw_write is not None else None
        return {
            "health_ok": bool(status.get("passed")),
            "model": model,
            "serial": serial,
            "temperature": _smart_temperature(d, attrs),
            "power_on_hours": poh,
            "read_tb": read_tb,
            "write_tb": write_tb,
        }

    # Legacy (<=7.1) schema
    if isinstance(d.get("smart_attributes"), dict) and d.get("smart_attributes"):
        attrs = d["smart_attributes"]
        status = d.get("overall_health_self_assessment") or {}
        model = str(d.get("device", {}).get("model_name") or d.get("model_name") or "")[:40]
        serial = str(d.get("device", {}).get("serial_number") or d.get("serial_number") or "")[:20]
        temp_val = next(
            (a.get("value") for a in attrs.values()
             if a.get("name") in ("Temperature_Celsius", "Current Temperature")), None)
        poh_val = next(
            (a.get("raw") for a in attrs.values() if a.get("name") == "Power_On_Hours"), None)
        try:
            poh = int(str(poh_val).split()[0]) if poh_val is not None else 0
        except (ValueError, IndexError):
            poh = 0
        return {
            "health_ok": bool(status.get("passed")),
            "model": model,
            "serial": serial,
            "temperature": temp_val,
            "power_on_hours": poh,
            "read_tb": None,
            "write_tb": None,
        }
    return None

_tool_cache: Dict[str, Any] = {"rocm": None, "intel": None, "time": 0}
_tool_cache_lock = threading.Lock()

_rapl_last_energy = 0
_rapl_last_time: Optional[float] = None
_net_io_snapshot: Optional[Dict] = None
_net_io_time: Optional[float] = None
_disk_io_snapshot: Optional[Dict] = None
_disk_io_time: Optional[float] = None


def _tool_available() -> Dict:
    global _tool_cache
    with _tool_cache_lock:
        if _tool_cache["time"] and time.time() - _tool_cache["time"] < TOOL_CHECK_TTL:
            return _tool_cache
    rocm = run_cmd([ROCM_SMI, "--showproductname", "--json"], timeout=1) is not None
    intel = run_cmd([INTEL_GPU_TOP, "-J", "-n", "1"], timeout=1) is not None
    with _tool_cache_lock:
        _tool_cache = {"rocm": rocm, "intel": intel, "time": time.time()}
    return _tool_cache


def _get_cpu_snapshot() -> Dict:
    global _rapl_last_energy, _rapl_last_time
    cpu_logical = psutil.cpu_count(logical=True) or 1

    core_temps = []
    for hwmon in sorted(SYSFS.glob("class/hwmon/hwmon*")):
        name = read_file_str(hwmon / "name", "")
        if "coretemp" in name.lower():
            for i in range(1, 64):
                v = read_file_int(hwmon / f"temp{i}_input", 0)
                label = read_file_str(hwmon / f"temp{i}_label", "").strip()
                crit = read_file_int(hwmon / f"temp{i}_crit", 0)
                if 0 < v < 150000:
                    # "Package id 0" (coretemp package temp) is too long for the grid
                    lbl = label or f"Core {i - 1}"
                    if lbl.startswith("Package"):
                        lbl = "Package"
                    core_temps.append({
                        "id": f"{name}_{i}",
                        "label": lbl,
                        "temp_c": round(v / 1000, 1),
                        "crit_c": round(crit / 1000, 1) if crit > 0 else 0,
                    })

    core_freqs = []
    for i in range(cpu_logical):
        f = read_file_int(SYSFS / f"devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", 0)
        core_freqs.append(f / 1000 if f > 0 else None)

    freq = psutil.cpu_freq()
    avg_freq = None
    if freq and freq.current > 0:
        avg_freq = freq.current
    else:
        valid = [f for f in core_freqs if f]
        if valid:
            avg_freq = sum(valid) / len(valid)
    if avg_freq is not None:
        _cpu_freq_history.append((time.time(), round(avg_freq, 0)))

    cpu_power_w = None  # None = no RAPL, or RAPL not yet readable
    power_limit_w = 0
    rapl_path = SYSFS / "class/powercap/intel-rapl/intel-rapl:0"
    if rapl_path.exists():
        now = time.time()
        energy_uj = read_file_int(rapl_path / "energy_uj", -1)
        if energy_uj >= 0:
            if _rapl_last_energy > 0 and _rapl_last_time:
                dt = now - _rapl_last_time
                if dt > 0.1:
                    max_energy = read_file_int(rapl_path / "max_energy_range_uj", 0)
                    if energy_uj < _rapl_last_energy and max_energy > 0:
                        diff = (max_energy - _rapl_last_energy) + energy_uj
                    else:
                        diff = energy_uj - _rapl_last_energy
                    # Valid baseline -> report the reading (may legitimately be 0.0)
                    cpu_power_w = round(max(diff, 0) / dt / 1000000, 1)
            _rapl_last_energy = energy_uj
            _rapl_last_time = now
        limit_uw = read_file_int(rapl_path / "constraint_0_power_limit_uw", 0)
        if limit_uw > 0:
            power_limit_w = round(limit_uw / 1000000, 0)

    return {
        "model": _cpu_model,
        "cores": psutil.cpu_count(logical=False) or 1,
        "threads": cpu_logical,
        "freq_current": round(freq.current, 1) if freq and freq.current else 0,
        "freq_max": round(freq.max, 1) if freq and freq.max else 0,
        "freq_min": round(freq.min, 1) if freq and freq.min else 0,
        "usage_percent": psutil.cpu_percent(interval=None),
        "usage_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "load_avg": [round(x, 2) for x in os.getloadavg()],
        "core_temps": core_temps,
        "core_freqs": [round(f, 0) if f else 0 for f in core_freqs],
        "cache": _cpu_cache_info,
        "power_w": cpu_power_w,
        "power_limit_w": power_limit_w,
    }


def _get_memory_snapshot(proc_list: bool = True) -> Dict:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    proc_mem = []
    if proc_list:
        try:
            for p in psutil.process_iter(["pid", "name", "memory_info", "memory_percent"]):
                try:
                    info = p.info
                    rss = info["memory_info"].rss if info["memory_info"] else 0
                    if rss > 5 * 1024 * 1024:
                        proc_mem.append({
                            "pid": info["pid"],
                            "name": (info["name"] or "")[:40],
                            "rss_mb": round(rss / 1024 / 1024, 1),
                            "percent": round(info["memory_percent"] or 0, 1),
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            logger.warning(f"Memory process scan failed: {e}")
        proc_mem.sort(key=lambda x: x["rss_mb"], reverse=True)

    return {
        "total_gb": round(mem.total / 2**30, 2),
        "used_gb": round(mem.used / 2**30, 2),
        "available_gb": round(mem.available / 2**30, 2),
        "percent": mem.percent,
        "cached_gb": round(mem.cached / 2**30, 2) if hasattr(mem, "cached") else 0,
        "buffers_gb": round(mem.buffers / 2**30, 2) if hasattr(mem, "buffers") else 0,
        "swap_total_gb": round(swap.total / 2**30, 2),
        "swap_used_gb": round(swap.used / 2**30, 2),
        "swap_percent": swap.percent,
        "proc_memory": proc_mem[:30],
    }


def _get_network_snapshot() -> Dict:
    global _net_io_snapshot, _net_io_time
    now = time.time()
    net_io = psutil.net_io_counters(pernic=True) or {}

    rates = {}
    if _net_io_snapshot and _net_io_time:
        dt = now - _net_io_time
        if dt > 0:
            for iface, c in net_io.items():
                prev = _net_io_snapshot.get(iface)
                if prev:
                    rx_rate = (c.bytes_recv - prev.bytes_recv) / dt * 8 / 1000000
                    tx_rate = (c.bytes_sent - prev.bytes_sent) / dt * 8 / 1000000
                    rates[iface] = {"rx_mbps": round(rx_rate, 1), "tx_mbps": round(tx_rate, 1)}
    _net_io_snapshot = net_io
    _net_io_time = now

    total_rx = sum(r["rx_mbps"] for r in rates.values())
    total_tx = sum(r["tx_mbps"] for r in rates.values())
    _net_history.append((now, round(total_rx, 1), round(total_tx, 1)))

    interfaces = []
    for iface, c in net_io.items():
        if iface == "lo":
            continue
        speed_mbps = read_file_int(SYSFS / f"class/net/{iface}/speed", 0)
        operstate = read_file_str(SYSFS / f"class/net/{iface}/operstate", "unknown")
        interfaces.append({
            "name": iface,
            "virtual": iface.startswith(("docker", "veth", "br-", "virbr", "tailscale", "wg", "tun", "tap")),
            "speed_mbps": speed_mbps,
            "operstate": operstate,
            "rx_mb": round(c.bytes_recv / 1024**2, 1),
            "tx_mb": round(c.bytes_sent / 1024**2, 1),
            "rx_errors": c.errin,
            "tx_errors": c.errout,
            "rx_drop": c.dropin,
            "tx_drop": c.dropout,
            "rx_mbps": rates.get(iface, {}).get("rx_mbps", 0),
            "tx_mbps": rates.get(iface, {}).get("tx_mbps", 0),
        })
    interfaces.sort(key=lambda x: (x["operstate"] != "up", -(x["rx_mb"] + x["tx_mb"])))

    return {"interfaces": interfaces, "rates": rates}


def _get_storage_snapshot() -> Dict:
    global _disk_io_snapshot, _disk_io_time
    now = time.time()
    info: Dict = {"partitions": [], "disks": {}}

    hidden_fstypes = ("squashfs", "tmpfs", "devtmpfs", "overlay")
    hidden_mounts = ("/snap/", "/dev", "/run", "/proc", "/sys")
    for p in psutil.disk_partitions(all=False):
        if p.fstype in hidden_fstypes or p.mountpoint.startswith(hidden_mounts):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            info["partitions"].append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "fstype": p.fstype,
                "total_gb": round(u.total / 2**30, 1),
                "used_gb": round(u.used / 2**30, 1),
                "free_gb": round(u.free / 2**30, 1),
                "percent": u.percent,
            })
        except (PermissionError, OSError):
            pass

    io = psutil.disk_io_counters(perdisk=True) or {}
    current_io: Dict = {}
    for name, c in io.items():
        if not any(name.startswith(p) for p in ("sd", "nvme", "virtio", "xvd")):
            continue
        if re.match(r"^(sd[a-z]+)\d+$", name) or re.match(r"^nvme\d+n\d+p\d+$", name):
            continue
        current_io[name] = {
            "read_bytes": c.read_bytes,
            "write_bytes": c.write_bytes,
            "read_count": c.read_count,
            "write_count": c.write_count,
            "read_time_ms": c.read_time,
            "write_time_ms": c.write_time,
        }

    disk_rates = {}
    if _disk_io_snapshot and _disk_io_time:
        dt = now - _disk_io_time
        if dt > 0:
            for name in current_io:
                if name in _disk_io_snapshot:
                    prev = _disk_io_snapshot[name]
                    r = (current_io[name]["read_bytes"] - prev["read_bytes"]) / dt / 1024 / 1024
                    w = (current_io[name]["write_bytes"] - prev["write_bytes"]) / dt / 1024 / 1024
                    disk_rates[name] = {"read_mbps": round(r, 1), "write_mbps": round(w, 1)}
    _disk_io_snapshot = current_io
    _disk_io_time = now

    for name, c in current_io.items():
        rate = disk_rates.get(name, {})
        info["disks"][name] = {
            "read_mb": round(c["read_bytes"] / 1024**2, 1),
            "write_mb": round(c["write_bytes"] / 1024**2, 1),
            "read_count": c["read_count"],
            "write_count": c["write_count"],
            "read_rate_mbps": rate.get("read_mbps", 0),
            "write_rate_mbps": rate.get("write_mbps", 0),
        }
    _disk_io_history.append((now, {k: (v["read_mbps"], v["write_mbps"]) for k, v in disk_rates.items()}))

    # NVMe temps (cheap)
    nvme_temps = {}
    for hwmon in sorted(SYSFS.glob("class/hwmon/hwmon*")):
        name = read_file_str(hwmon / "name", "").lower()
        if "nvme" in name:
            t = read_file_int(hwmon / "temp1_input", 0)
            if 0 < t < 150000:
                nvme_temps[name] = round(t / 1000, 1)
    info["nvme_temps"] = nvme_temps

    return info


def _collect_smart() -> Dict:
    """Full SMART collection (expensive). Cached with TTL."""
    with _smart_cache_lock:
        if _smart_cache["data"] is not None and time.time() - _smart_cache["time"] < SMART_TTL:
            return _smart_cache["data"]

    smart: Dict = {}
    is_root = os.geteuid() == 0

    for nvme in sorted(SYSFS.glob("class/nvme/nvme*")):
        ctrl_name = nvme.name
        dev = nvme / "device"
        model = (read_file_str(dev / "model", "") or read_file_str(SYSFS / f"block/{ctrl_name}n1/device/model", "")).strip()[:40]
        serial = (read_file_str(dev / "serial", "") or read_file_str(SYSFS / f"block/{ctrl_name}n1/device/serial", "")).strip()[:20]
        fw_rev = read_file_str(dev / "firmware_rev", "").strip()[:20]

        temp_c = 0
        for hwmon_dir in sorted(SYSFS.glob(f"class/nvme/{ctrl_name}/device/hwmon*")):
            if hwmon_dir.is_dir():
                t = read_file_int(hwmon_dir / "temp1_input", 0)
                if 0 < t < 100000:
                    temp_c = round(t / 1000, 1)
                    break
        if temp_c == 0:
            for hwmon_dir in sorted(SYSFS.glob(f"block/{ctrl_name}n1/device/hwmon*")):
                if hwmon_dir.is_dir():
                    t = read_file_int(hwmon_dir / "temp1_input", 0)
                    if 0 < t < 100000:
                        temp_c = round(t / 1000, 1)
                        break

        smart_data: Dict = {}
        if is_root:
            out = run_cmd([NVME_CLI, "smart-log", f"/dev/{ctrl_name}n1"], timeout=3)
            if out:
                for line in out.split("\n"):
                    line = line.strip()
                    if ":" not in line:
                        continue
                    key, _, val = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    # Some vendors append a unit suffix to the label,
                    # e.g. "Data Units Read (1000 bytes)" -> drop it so the
                    # key still matches "data_units_read".
                    key = re.sub(r"\(.*\)$", "", key).rstrip("_")
                    val = val.strip()
                    num_match = re.match(r"(\d+)", val)
                    num_val = int(num_match.group(1)) if num_match else None
                    if key == "critical_warning":
                        smart_data["critical_warning"] = num_val or 0
                    elif key == "temperature" and num_val:
                        temp_c = num_val
                    elif key in ("available_spare", "percentage_used", "data_units_read", "data_units_written",
                                 "host_read_commands", "host_write_commands", "power_on_hours", "power_cycles",
                                 "unsafe_shutdowns", "media_errors"):
                        smart_data[key] = num_val
                        # Many vendors (e.g. SM2269XT, Hynix) print the
                        # already-converted value in parentheses:
                        #   Data Units Read : 34239390 (17.53 TB)
                        # The raw count's unit is vendor-defined (here
                        # 512000 bytes), so a fixed multiplier is wrong;
                        # prefer the vendor's own TB figure.
                        if key in ("data_units_read", "data_units_written"):
                            tb_match = re.search(r"\(\s*([\d.]+)\s*TB\s*\)", val, re.I)
                            if tb_match:
                                smart_data[key + "_tb"] = float(tb_match.group(1))

        if not smart_data:
            smart_log = dev / "smart_information_log"
            if smart_log.exists():
                for field in ("critical_warning", "available_spare", "percentage_used",
                              "data_units_read", "data_units_written",
                              "media_and_data_integrity_errors", "unsafe_shutdowns"):
                    val = read_file_int(smart_log / field, -1)
                    if val >= 0:
                        smart_data[field] = val

        dur = smart_data.get("data_units_read", 0) or 0
        duw = smart_data.get("data_units_written", 0) or 0
        # Prefer the vendor's own TB figure (see parser above); otherwise fall
        # back to the NVMe spec unit size of 1000 bytes for standard drives.
        read_tb = smart_data.get("data_units_read_tb")
        if read_tb is None:
            read_tb = dur * 1000 / 1024**4
        write_tb = smart_data.get("data_units_written_tb")
        if write_tb is None:
            write_tb = duw * 1000 / 1024**4
        smart[ctrl_name] = {
            "model": model,
            "serial": serial,
            "firmware": fw_rev,
            "temperature": temp_c,
            "read_tb": round(read_tb, 2),
            "write_tb": round(write_tb, 2),
            "power_on_hours": smart_data.get("power_on_hours", 0),
            "power_cycles": smart_data.get("power_cycles", 0),
            "health": "OK" if smart_data.get("critical_warning", 0) == 0 else "WARNING",
            "critical_warning": smart_data.get("critical_warning"),
            "available_spare": smart_data.get("available_spare"),
            "percentage_used": smart_data.get("percentage_used"),
            "media_errors": smart_data.get("media_errors"),
            "unsafe_shutdowns": smart_data.get("unsafe_shutdowns"),
        }

    # SATA/ATA drives (via smartctl). Probe regardless of euid: root always
    # works; non-root works when the user has CAP_SYS_RAWIO (e.g. in the
    # `disk` group with a permissive kernel) and degrades to N/A otherwise.
    disks_out = run_cmd(["lsblk", "-dn", "-o", "NAME,TYPE,MODEL,SERIAL"], timeout=2)
    if disks_out:
        for line in disks_out.strip().split("\n"):
            parts = line.split()
            if len(parts) < 2 or parts[1] != "disk":
                continue
            disk_name = parts[0]
            if disk_name.startswith(("nvme", "nbd", "loop", "zram", "ram")):
                continue
            model_name = " ".join(parts[2:]) if len(parts) > 2 else disk_name
            serial_num = parts[-1] if len(parts) > 3 else ""
            model_name = model_name[:40] if model_name != disk_name else ""
            serial_num = serial_num[:20]

            parsed = None
            for args_suffix in (["-d", "sat", "-a", "-j"], ["-d", "ata", "-a", "-j"], ["-a", "-j"]):
                out = run_cmd([SMARTCTL] + args_suffix + [f"/dev/{disk_name}"], timeout=5)
                if out:
                    parsed = _parse_smartctl_json(out, disk_name)
                    if parsed:
                        break
            if parsed:
                smart[disk_name] = {
                    "model": parsed["model"] or model_name,
                    "serial": parsed["serial"] or serial_num,
                    "health": "OK" if parsed["health_ok"] else "CHECK",
                    "temperature": parsed["temperature"],
                    "power_on_hours": parsed["power_on_hours"],
                    "read_tb": parsed["read_tb"],
                    "write_tb": parsed["write_tb"],
                    "smart_available": True,
                }
            else:
                smart[disk_name] = {
                    "model": model_name,
                    "serial": serial_num,
                    "health": "N/A",
                    "temperature": None,
                    "smart_available": False,
                }

    with _smart_cache_lock:
        _smart_cache.clear()
        _smart_cache.update(data=smart, time=time.time())
    return smart


def _get_temps_snapshot() -> Dict:
    sensors = []
    fans = []
    seen_nvmes = set()
    for hwmon in sorted(SYSFS.glob("class/hwmon/hwmon*")):
        chip = read_file_str(hwmon / "name", "").strip() or read_file_str(hwmon / "device", "").strip()[:20] or hwmon.name
        if chip.startswith(("enp", "eth")):
            continue
        if chip.lower() == "nvme":
            try:
                dev_link = (hwmon / "device").resolve()
                m = re.search(r"nvme(\d+)", str(dev_link))
                if m:
                    chip = f"nvme{m.group(1)}"
            except Exception:
                pass
            if chip in seen_nvmes:
                continue
            seen_nvmes.add(chip)

        for i in range(1, 32):
            input_path = hwmon / f"temp{i}_input"
            if not input_path.exists():
                continue
            v = read_file_int(input_path, 0)
            if v <= 0 or v >= 150000:
                continue
            label = read_file_str(hwmon / f"temp{i}_label", "").strip()
            max_raw = read_file_int(hwmon / f"temp{i}_max", 0)
            crit_raw = read_file_int(hwmon / f"temp{i}_crit", 0)
            sensors.append({
                "chip": chip,
                "label": label or f"Temp {i}",
                "temp_c": round(v / 1000, 1),
                "max_c": round(max_raw / 1000, 1) if 0 < max_raw < 150000 else 0,
                "crit_c": round(crit_raw / 1000, 1) if 0 < crit_raw < 150000 else 0,
            })
        for i in range(1, 16):
            rpm_path = hwmon / f"fan{i}_input"
            if not rpm_path.exists():
                continue
            rpm = read_file_int(rpm_path, 0)
            if rpm <= 0:
                continue
            label = read_file_str(hwmon / f"fan{i}_label", "").strip()
            max_rpm = read_file_int(hwmon / f"fan{i}_max", 0)
            fans.append({
                "chip": chip,
                "label": label or f"Fan {i}",
                "rpm": rpm,
                "max_rpm": max_rpm if 0 < max_rpm < 100000 else 0,
            })
    return {"sensors": sensors, "fans": fans}


def _get_summary_snapshot(cpu: Dict, mem: Dict, net: Dict, storage: Dict, temps: Dict, quick: Dict) -> Dict:
    gpu_count = len(detect_gpu_topology())
    gpu_temp_max = 0
    gpu_power_w = 0
    gpu_vram_used_mb = 0
    for card in sorted(SYSFS.glob("class/drm/card*")):
        if any(x in card.name for x in ("-DP", "-HDMI", "-Writeback")):
            continue
        dev = card / "device"
        if read_file_int(dev / "vendor", 0) not in (0x1002, 0x8086):
            continue
        for hwmon_dir in sorted(SYSFS.glob(f"class/drm/{card.name}/device/hwmon*")):
            if hwmon_dir.is_dir():
                t = read_file_int(hwmon_dir / "temp1_input", 0)
                if 0 < t < 100000:
                    gpu_temp_max = max(gpu_temp_max, round(t / 1000, 1))
                p = read_file_int(hwmon_dir / "power1_average", 0)
                if p > 0:
                    gpu_power_w += round(p / 1000000, 1)
                break
        vu = read_file_int(dev / "mem_info_vram_used", 0)
        if vu > 0:
            gpu_vram_used_mb += round(vu / 1024**2, 1)

    total_rx = sum(i["rx_mbps"] for i in net["interfaces"])
    total_tx = sum(i["tx_mbps"] for i in net["interfaces"])

    return {
        "cpu_percent": cpu["usage_percent"],
        "cpu_temp_max": max((c["temp_c"] for c in cpu["core_temps"]), default=0),
        "cpu_power_w": cpu["power_w"],
        "mem_percent": mem["percent"],
        "mem_used_gb": mem["used_gb"],
        "mem_total_gb": mem["total_gb"],
        "gpu_count": gpu_count,
        "gpu_temp_max": gpu_temp_max,
        "gpu_power_w": gpu_power_w,
        "gpu_vram_used_mb": gpu_vram_used_mb,
        "net_rx_mbps": round(total_rx, 1),
        "net_tx_mbps": round(total_tx, 1),
        "disk_max_percent": max((p["percent"] for p in storage["partitions"]), default=0),
        "disk_io_mbps": round(sum(d["read_rate_mbps"] + d["write_rate_mbps"] for d in storage["disks"].values()), 1),
        "uptime_str": quick["uptime_str"],
        "load_avg": quick["load_avg"],
        "load_ratio": quick["load_ratio"],
        "cores": quick["cores"],
    }


def _get_quick_snapshot() -> Dict:
    uptime_s = int(time.time() - psutil.boot_time())
    days = uptime_s // 86400
    hours = (uptime_s % 86400) // 3600
    load_avg = os.getloadavg()
    cores = psutil.cpu_count(logical=True) or 1
    return {
        "uptime_s": uptime_s,
        "uptime_str": f"{days}d {hours}h" if days > 0 else f"{hours}h",
        "load_avg": [round(x, 2) for x in load_avg],
        "load_ratio": round(load_avg[0] / cores, 2),
        "cores": cores,
    }


# Static CPU info (read once)
_cpu_model = "Unknown"
_cpu_cache_info: Dict = {}
try:
    with open(PROC / "cpuinfo") as f:
        content = f.read()
    for line in content.split("\n"):
        if line.startswith("model name"):
            _cpu_model = line.split(":", 1)[1].strip()
            break
    for key in ("l1d cache", "l1i cache", "l2 cache", "l3 cache"):
        m = re.search(rf"{key}:\s*(.+)", content)
        if m:
            _cpu_cache_info[key] = m.group(1).strip()
except Exception:
    pass


def _sample_once():
    global _cache
    try:
        cpu = _get_cpu_snapshot()
        mem = _get_memory_snapshot(proc_list=False)
        net = _get_network_snapshot()
        storage = _get_storage_snapshot()
        temps = _get_temps_snapshot()
        quick = _get_quick_snapshot()
        summary = _get_summary_snapshot(cpu, mem, net, storage, temps, quick)

        with _cache_lock:
            _cache.update({
                "cpu": cpu, "memory": mem, "network": net, "storage": storage,
                "temps": temps, "summary": summary, "quick": quick,
                "sample_time": time.time(),
            })

        # Alert evaluation (uses cached SMART to stay cheap)
        smart = _smart_cache["data"] or {}
        snap_for_alerts = {
            "partitions": storage["partitions"],
            "mem_percent": mem["percent"],
            "mem_used_gb": mem["used_gb"],
            "mem_total_gb": mem["total_gb"],
            "swap_percent": mem["swap_percent"],
            "load_ratio": quick["load_ratio"],
            "load_avg": quick["load_avg"],
            "temps": temps["sensors"],
            "gpus": [],
        }
        gpu_data = _gpu_cache["data"]
        if gpu_data:
            for g in gpu_data.get("rocm", []):
                snap_for_alerts["gpus"].append({"id": g.get("id"), "name": g["name"], "vram_percent": g["vram_percent"]})
            for g in gpu_data.get("sysfs", []):
                vt, vu = g.get("vram_total_mb", 0), g.get("vram_used_mb", 0)
                if vt:
                    snap_for_alerts["gpus"].append({"id": g.get("id"), "name": g["name"], "vram_percent": round(vu / vt * 100, 1)})
        available = {"core"}
        if smart:
            available.add("smart")
        if gpu_data:
            available.add("gpu")
        alerts.evaluate(build_alert_checks(snap_for_alerts, smart), available=available)
    except Exception as e:
        logger.warning(f"Sample failed: {e}", exc_info=DEBUG)


def _sampler_loop():
    psutil.cpu_percent(interval=None)  # prime
    # prime rate snapshots
    _get_network_snapshot()
    _get_storage_snapshot()
    time.sleep(SAMPLE_INTERVAL)
    while True:
        _sample_once()
        time.sleep(SAMPLE_INTERVAL)


def _gpu_sampler_loop():
    while True:
        try:
            data = collect_gpu_detail()
            with _gpu_cache_lock:
                _gpu_cache["data"] = data
                _gpu_cache["time"] = time.time()
        except Exception as e:
            logger.warning(f"GPU sample failed: {e}", exc_info=DEBUG)
        time.sleep(GPU_SAMPLE_INTERVAL)


def _smart_loop():
    while True:
        try:
            _collect_smart()
        except Exception as e:
            logger.warning(f"SMART sample failed: {e}", exc_info=DEBUG)
        time.sleep(SMART_TTL)


# ── Processes (on-demand, CPU% via /proc time deltas) ───────────────────
_proc_cpu_last: Dict[int, float] = {}  # pid -> (user + system) seconds
_proc_last_time: float = 0.0


def get_processes(sort_by: str = "cpu", search: str = "", limit: int = 50) -> List[Dict]:
    global _proc_last_time
    procs = []
    now = time.time()
    dt = now - _proc_last_time if _proc_last_time else 0
    _proc_last_time = now
    try:
        for p in psutil.process_iter(
                ["pid", "name", "cmdline", "memory_percent", "memory_info", "status",
                 "create_time", "username", "cpu_times"]):
            try:
                info = p.info
                mem = info["memory_percent"] or 0
                rss = (info["memory_info"].rss if info["memory_info"] else 0)
                if mem < 0.3 and rss < 50 * 1024 * 1024:
                    continue

                # CPU% from time deltas between successive scans
                cpu = 0.0
                ct = info["cpu_times"]
                if ct:
                    cur = ct.user + ct.system
                    prev = _proc_cpu_last.get(info["pid"])
                    if prev is not None and dt > 0.2 and cur >= prev:
                        cpu = min((cur - prev) / dt * 100, 100.0)
                    _proc_cpu_last[info["pid"]] = cur
                if len(_proc_cpu_last) > 5000:
                    _proc_cpu_last.clear()

                name = (info["name"] or "")[:50]
                cmdline = info.get("cmdline") or []
                if cmdline and name in ("MainThread", "python", "python3", "node", "java", "go", "deno", "bun"):
                    cmd_str = " ".join(cmdline)[:80]
                    if cmd_str and cmd_str != name:
                        name = cmd_str

                procs.append({
                    "pid": info["pid"],
                    "name": name,
                    "username": info["username"] or "-",
                    "cpu": round(cpu, 1),
                    "mem": round(mem, 1),
                    "rss_mb": round(rss / 1024 / 1024, 1),
                    "status": info["status"] or "unknown",
                    "uptime_s": int(now - (info["create_time"] or now)),
                    "cpu_time_s": round((info["cpu_times"].user + info["cpu_times"].system) if info["cpu_times"] else 0, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        logger.warning(f"Process scan failed: {e}")

    if search:
        q = search.lower()
        procs = [p for p in procs if q in p["name"].lower() or q in p["username"].lower() or str(p["pid"]) == q]
    procs.sort(key=lambda x: x[sort_by] if sort_by in ("cpu", "mem", "rss_mb") else x["cpu"], reverse=True)
    return procs[:min(limit, 200)]


# ── System logs ─────────────────────────────────────────────────────────
NOISY_UNITS = ("tailscaled", "avahi-daemon", "systemd-resolved", "NetworkManager", "fwupd", "snapd")
_UNIT_RE = re.compile(r"\s(\S+?)\[\d+\]:")


def get_system_logs(lines: int = 50, unit: str = "", search: str = "",
                    level: str = "", exclude_noisy: bool = True) -> Dict:
    lines = min(max(lines, 10), 200)
    cmd = [JOURNALCTL, "--no-pager", "-q", "-n", "500"]
    if unit:
        cmd += ["-u", unit]
    if level and level != "all":
        cmd += [f"-p", level]
    if search:
        cmd += ["--grep", search]
    out = run_cmd(cmd, timeout=3)
    logs = []
    if out:
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            if exclude_noisy and not unit:
                m = _UNIT_RE.search(line)
                if m and m.group(1) in NOISY_UNITS:
                    continue
            logs.append(line)
        logs = logs[-lines:]

    # Units from a separate sample (only when no unit filter)
    units = []
    if not unit:
        sample_out = run_cmd([JOURNALCTL, "-n", "500", "--no-pager", "-q"], timeout=3)
        if sample_out:
            seen = set()
            for line in sample_out.split("\n"):
                m = _UNIT_RE.search(line)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    units.append(m.group(1))
            units = sorted(units)[:80]

    return {"logs": logs, "units": units}


# ── Health ──────────────────────────────────────────────────────────────
def get_health_detail() -> Dict:
    tools = _tool_available()
    with _cache_lock:
        summary = _cache["summary"]
    checks = {
        "rocm_smi_available": tools["rocm"],
        "intel_gpu_top_available": tools["intel"],
    }
    if summary:
        checks["disk_ok"] = summary["disk_max_percent"] < 95
        checks["memory_ok"] = summary["mem_percent"] < 95
    with _smart_cache_lock:
        smart = _smart_cache["data"] or {}
    checks["nvme_smart_ok"] = not any(d.get("health") == "WARNING" for d in smart.values())
    with _cache_lock:
        temps = _cache["temps"]
    high_temp = False
    if temps:
        for s in temps["sensors"]:
            if s["crit_c"] > 0 and s["temp_c"] > s["crit_c"] * 0.9:
                high_temp = True
                break
    checks["temps_ok"] = not high_temp

    status = "ok"
    failed = [k for k in ("disk_ok", "memory_ok", "nvme_smart_ok", "temps_ok") if checks.get(k) is False]
    if failed:
        status = "warning"

    return {
        "status": status,
        "failed_checks": failed,
        "version": VERSION,
        "auth_configured": auth_configured(),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "sample_age_s": round(time.time() - (_cache.get("sample_time") or 0), 1),
        "active_alerts": len(alerts.snapshot()["active"]),
        "checks": checks,
    }


# ── FastAPI App ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"System Monitor v4 starting on {HOST}:{PORT} (auth configured: {auth_configured()})")
    t1 = threading.Thread(target=_sampler_loop, daemon=True, name="sampler")
    t2 = threading.Thread(target=_gpu_sampler_loop, daemon=True, name="gpu-sampler")
    t3 = threading.Thread(target=_smart_loop, daemon=True, name="smart-sampler")
    t1.start(); t2.start(); t3.start()
    get_processes(limit=1)  # prime process CPU% baseline
    yield
    logger.info("System Monitor v4 stopped")


app = FastAPI(title="System Performance Monitor", version=VERSION, lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"API error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(content={"error": "Internal server error"}, status_code=500)


def _snap(key: str) -> Any:
    with _cache_lock:
        v = _cache.get(key)
    if v is None:
        raise HTTPException(503, "Sampler warming up, retry in a moment")
    return v


# ── Auth endpoints (no auth required) ───────────────────────────────────
@app.get("/api/auth/status")
async def auth_status():
    return {"configured": auth_configured()}


class Credentials(BaseModel):
    username: str = ""
    password: str = ""


class KeyCreate(BaseModel):
    name: str = "api"


class PasswordChange(BaseModel):
    old_password: str = ""
    new_password: str = ""


class UserCreate(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "user"


class UserPasswordReset(BaseModel):
    new_password: str = ""


class UserRoleChange(BaseModel):
    role: str = "user"


@app.post("/api/auth/setup")
def auth_setup(body: Credentials):
    username = body.username.strip()
    password = body.password
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,32}", username):
        raise HTTPException(400, "Username must be 2-32 chars: letters, digits, . _ -")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    return create_admin(username, password)


@app.post("/api/auth/login")
def auth_login(body: Credentials):
    return login(body.username, body.password)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = _extract_token(request)
    with _auth_lock:
        data = _load_auth()
        if data.get("sessions", {}).pop(token, None):
            _save_auth(data)
    return {"ok": True}


# ── Authenticated: auth management ──────────────────────────────────────
@app.get("/api/auth/me", dependencies=[Depends(require_auth)])
async def auth_me(request: Request):
    return _resolve_token(_extract_token(request)) or {}


# ── User management (admin only) ────────────────────────────────────────
@app.get("/api/users", dependencies=[Depends(require_admin)])
async def users_list():
    return list_users()


@app.post("/api/users", dependencies=[Depends(require_admin)])
def users_create(body: UserCreate):
    return create_user(body.username.strip(), body.password, body.role)


@app.delete("/api/users/{username}", dependencies=[Depends(require_admin)])
def users_delete(username: str, request: Request):
    ident = require_admin(request)
    delete_user(username, ident["username"])
    return {"ok": True}


@app.post("/api/users/{username}/password", dependencies=[Depends(require_admin)])
def users_reset_password(username: str, body: UserPasswordReset):
    reset_user_password(username, body.new_password)
    return {"ok": True}


@app.post("/api/users/{username}/role", dependencies=[Depends(require_admin)])
def users_change_role(username: str, body: UserRoleChange):
    change_user_role(username, body.role)
    return {"ok": True}


# ── API keys (self or admin) ────────────────────────────────────────────
@app.get("/api/auth/keys", dependencies=[Depends(require_auth)])
async def keys_list(request: Request):
    ident = require_auth(request)
    with _auth_lock:
        data = _load_auth()
    keys = data.get("keys", {})
    if ident.get("role") != "admin":
        keys = {k: v for k, v in keys.items() if v.get("owner") == ident["username"]}
    return [
        {"name": k.get("name"), "owner": k.get("owner"),
         "prefix": k.get("key", "")[:12] + "…",
         "created_at": k.get("created_at"), "last_used": k.get("last_used")}
        for k in sorted(keys)
        for k in [keys[k]]
    ]


@app.post("/api/auth/keys", dependencies=[Depends(require_auth)])
def keys_create(body: KeyCreate, request: Request):
    ident = require_auth(request)
    name = body.name.strip()[:32] or "api"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise HTTPException(400, "Invalid key name")
    owner = ident["username"]
    key = "amk_" + secrets.token_urlsafe(24)
    with _auth_lock:
        data = _load_auth()
        if name in data.get("keys", {}):
            raise HTTPException(409, "Key name already exists")
        data.setdefault("keys", {})[name] = {
            "name": name, "owner": owner, "key": key,
            "created_at": now_iso(), "last_used": None,
        }
        _save_auth(data)
    return {"name": name, "key": key, "owner": owner,
            "created_at": data["keys"][name]["created_at"]}


@app.delete("/api/auth/keys/{name}", dependencies=[Depends(require_auth)])
def keys_delete(name: str, request: Request):
    ident = require_auth(request)
    with _auth_lock:
        data = _load_auth()
        key = data.get("keys", {}).get(name)
        if key is None:
            raise HTTPException(404, "Key not found")
        # only owner or admin can delete
        if ident.get("role") != "admin" and key.get("owner") != ident["username"]:
            raise HTTPException(403, "You can only delete your own keys")
        del data["keys"][name]
        _save_auth(data)
    return {"ok": True}


# ── Own password (any authenticated web user) ───────────────────────────
@app.post("/api/auth/password", dependencies=[Depends(require_auth)])
def change_password(body: PasswordChange, request: Request):
    ident = require_auth(request)
    if ident["kind"] != "web":
        raise HTTPException(403, "Use the web UI to change passwords")
    old, new = body.old_password, body.new_password
    if len(new) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    with _auth_lock:
        data = _load_auth()
        user = data.get("users", {}).get(ident["username"], {})
        if not verify_password(old, user):
            raise HTTPException(401, "Current password is incorrect")
        salt = secrets.token_bytes(16)
        user["salt"] = salt.hex()
        user["hash"] = _hash_password(new, salt)
        _save_auth(data)
    return {"ok": True}


# ── Authenticated: metrics ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    # Public shell: contains no sensitive data. The frontend JS checks
    # /api/auth/status and shows the setup/login screen as needed; all
    # real data lives behind require_auth.
    return DASHBOARD_HTML


@app.get("/api/health")
async def api_health():
    return get_health_detail()


@app.get("/api/summary", dependencies=[Depends(require_auth)])
async def api_summary():
    return _snap("summary")


@app.get("/api/quick-stats", dependencies=[Depends(require_auth)])
async def api_quick_stats():
    return _snap("quick")


@app.get("/api/cpu", dependencies=[Depends(require_auth)])
async def api_cpu():
    return _snap("cpu")


@app.get("/api/gpu", dependencies=[Depends(require_auth)])
def api_gpu():
    with _gpu_cache_lock:
        data = _gpu_cache["data"]
        age = round(time.time() - (_gpu_cache["time"] or 0), 1)
    if data is None:
        return {"rocm": [], "intel": [], "sysfs": [], "stale": True, "age_s": age}
    return {**data, "stale": False, "age_s": age}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
def api_memory():
    # Fresh process list on demand (cheap, ~10ms); totals from cache
    return _get_memory_snapshot(proc_list=True)


@app.get("/api/storage", dependencies=[Depends(require_auth)])
def api_storage():
    base = dict(_snap("storage"))
    smart = _collect_smart()  # cached 60s
    base["smart"] = smart
    return base


@app.get("/api/network", dependencies=[Depends(require_auth)])
async def api_network():
    return _snap("network")


@app.get("/api/net-history", dependencies=[Depends(require_auth)])
async def api_net_history():
    return _net_history.last()


@app.get("/api/gpu-history", dependencies=[Depends(require_auth)])
async def api_gpu_history():
    with _gpu_history_lock:
        return {gid: h.last() for gid, h in _gpu_history.items()}


@app.get("/api/cpu-freq-history", dependencies=[Depends(require_auth)])
async def api_cpu_freq_history():
    return _cpu_freq_history.last()


@app.get("/api/disk-io-history", dependencies=[Depends(require_auth)])
async def api_disk_io_history():
    return _disk_io_history.last()


@app.get("/api/temps", dependencies=[Depends(require_auth)])
async def api_temps():
    return _snap("temps")


@app.get("/api/processes", dependencies=[Depends(require_auth)])
def api_processes(sort_by: str = "cpu", search: str = "", limit: int = 50):
    if sort_by not in ("cpu", "mem", "rss_mb"):
        sort_by = "cpu"
    return get_processes(sort_by=sort_by, search=search, limit=limit)


@app.get("/api/logs", dependencies=[Depends(require_auth)])
def api_logs(lines: int = 50, unit: str = "", search: str = "",
             level: str = "all", noisy: bool = False):
    return get_system_logs(lines, unit, search, level, exclude_noisy=not noisy)


# ── Authenticated: alerts ───────────────────────────────────────────────
@app.get("/api/alerts", dependencies=[Depends(require_auth)])
async def api_alerts():
    return alerts.snapshot()


@app.post("/api/alerts/ack", dependencies=[Depends(require_auth)])
async def api_alert_ack(rule_id: str = Query(...)):
    if not alerts.acknowledge(rule_id):
        raise HTTPException(404, "Active alert not found")
    return {"ok": True}


@app.delete("/api/alerts/active", dependencies=[Depends(require_auth)])
async def api_alert_delete(rule_id: str = Query(...)):
    if not alerts.delete(rule_id):
        raise HTTPException(404, "Alert not found")
    return {"ok": True}


@app.delete("/api/alerts/history/{index}", dependencies=[Depends(require_auth)])
async def api_alert_history_delete(index: int):
    if not alerts.delete_history(index):
        raise HTTPException(404, "History entry not found")
    return {"ok": True}


@app.post("/api/alerts/clear-resolved", dependencies=[Depends(require_auth)])
async def api_alert_clear_resolved():
    return {"cleared": alerts.clear_resolved()}


# ── Debug ───────────────────────────────────────────────────────────────
if DEBUG:
    @app.get("/api/all", dependencies=[Depends(require_auth)])
    def api_all():
        return {
            "health": get_health_detail(),
            "summary": _snap("summary"),
            "cpu": _snap("cpu"),
            "gpu": api_gpu(),
            "memory": _snap("memory"),
            "storage": api_storage(),
            "network": _snap("network"),
            "temps": _snap("temps"),
            "processes": get_processes(limit=20),
        }


# ── Dashboard HTML (loaded from file) ───────────────────────────────────
def _load_dashboard() -> str:
    p = Path(__file__).resolve().parent / "dashboard.html"
    try:
        return p.read_text()
    except Exception as e:
        logger.error(f"Failed to load dashboard.html: {e}")
        return "<h1>dashboard.html missing</h1>"


DASHBOARD_HTML = _load_dashboard()


if __name__ == "__main__":
    logger.info(f"Starting System Monitor v4 on http://{HOST}:{PORT}")
    config = Config(app, host=HOST, port=PORT, log_level="info" if DEBUG else "warning", access_log=False)
    server = Server(config)
    server.run()
