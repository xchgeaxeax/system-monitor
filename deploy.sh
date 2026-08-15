#!/bin/bash
# System Monitor v4 - Deployment Script
# Usage: ./deploy.sh [--user|--root] [--port PORT]
# Default: user service on port 9527
#
# Env overrides: AI_MONITOR_PORT, AI_MONITOR_HOST (default 0.0.0.0;
# set to 127.0.0.1 when behind a reverse proxy)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
INSTALL_DIR="/opt/system-monitor"
PYTHON_BIN="/usr/bin/python3"
PORT="${AI_MONITOR_PORT:-9527}"
HOST="${AI_MONITOR_HOST:-0.0.0.0}"
MODE="user"  # or "root"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --user) MODE="user"; shift ;;
        --root) MODE="root"; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [--user|--root] [--port PORT]"
            echo ""
            echo "Options:"
            echo "  --user    Install as user service (no root needed)"
            echo "  --root    Install as system service (full features)"
            echo "  --port    Set listen port (default: 9527)"
            echo "  --help    Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1" ;;
    esac
done

# Root mode requires root privileges (check early for a clear error)
if [ "$MODE" = "root" ] && [ "$(id -u)" -ne 0 ]; then
    error "Root mode requires root privileges. Run with sudo."
fi

# Check Python
if ! command -v python3 &>/dev/null; then
    error "Python3 is not installed"
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: $PYTHON_VERSION"

# Check pip
if ! python3 -m pip --version &>/dev/null; then
    warn "pip not found, installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update && sudo apt-get install -y python3-pip
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3-pip
    elif command -v yum &>/dev/null; then
        sudo yum install -y python3-pip
    elif command -v zypper &>/dev/null; then
        sudo zypper install -y python3-pip
    else
        error "Cannot install pip. Please install python3-pip manually."
    fi
fi

# Install Python dependencies
info "Installing Python dependencies..."
python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null || \
python3 -m pip install -r requirements.txt

# Optional tools check
info "Checking optional tools..."
for tool in nvtop intel_gpu_top rocm-smi smartctl; do
    if command -v $tool &>/dev/null; then
        info "  $tool: found"
    else
        info "  $tool: not found (optional)"
    fi
done

# Stop existing services (all possible unit names, for re-deploys)
info "Stopping existing services..."
systemctl --user stop system-monitor system-monitor-user 2>/dev/null || true
systemctl stop system-monitor system-monitor-root system-monitor-user 2>/dev/null || true

# Free the target port: kill anything still bound to it (e.g. a server.py
# started manually from a workspace checkout that outlived the old install).
PORT_PIDS=$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$"' | grep -oP 'pid=\K[0-9]+' | sort -u || true)
if [[ -n "$PORT_PIDS" ]]; then
    for pid in $PORT_PIDS; do
        CMD=$(ps -p "$pid" -o cmd= 2>/dev/null || echo "pid $pid")
        if echo "$CMD" | grep -q "server.py"; then
            warn "Port $PORT is held by: $CMD"
            kill "$pid" 2>/dev/null || true
        fi
    done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        ss -ltn 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$"' | grep -q . || break
        sleep 1
    done
    if ss -ltn 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$"' | grep -q .; then
        error "Port $PORT is still in use. Find and stop the process manually (ss -ltnp | grep $PORT)."
    fi
    info "Port $PORT freed."
fi

# Install files
info "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp server.py dashboard.html monitor-cli.py "$INSTALL_DIR/"
cp -r *.service "$INSTALL_DIR/" 2>/dev/null || true

# Migrate data (users/keys/alerts) from a previous install location
if [ ! -f "$INSTALL_DIR/data/auth.json" ]; then
    for CAND in /opt/system-monitor/data "$SCRIPT_DIR/data" "$HOME/.hermes/workspace/monitor/data"; do
        if [ -f "$CAND/auth.json" ] && [ "$CAND" != "$INSTALL_DIR/data" ]; then
            mkdir -p "$INSTALL_DIR/data"
            cp -r "$CAND/." "$INSTALL_DIR/data/"
            info "Migrated data from $CAND"
            break
        fi
    done
fi

# Setup permissions
chmod +x "$INSTALL_DIR/server.py"
chmod +x "$INSTALL_DIR/monitor-cli.py"

# Configure and enable service (canonical unit name: system-monitor.service)
if [ "$MODE" = "user" ]; then
    info "Installing as user service..."

    # Create user systemd directory
    mkdir -p ~/.config/systemd/user

    # Copy service file under the canonical unit name, apply port + host
    sed -e "s/AI_MONITOR_PORT=.*/AI_MONITOR_PORT=$PORT/" \
        -e "s/AI_MONITOR_HOST=.*/AI_MONITOR_HOST=$HOST/" \
        "$INSTALL_DIR/system-monitor-user.service" > ~/.config/systemd/user/system-monitor.service

    # Reload and enable
    systemctl --user daemon-reload
    systemctl --user enable system-monitor
    systemctl --user start system-monitor

    # Check status
    if systemctl --user is-active system-monitor &>/dev/null; then
        info "User service started successfully!"
    else
        error "Failed to start user service. Check: journalctl --user -u system-monitor -n 20"
    fi

    echo ""
    echo "=== Deployment Complete ==="
    echo "Access: http://localhost:$PORT"
    echo "API Docs: http://localhost:$PORT/api/docs"
    echo ""
    echo "Management commands:"
    echo "  systemctl --user status system-monitor"
    echo "  systemctl --user restart system-monitor"
    echo "  systemctl --user stop system-monitor"
    echo "  journalctl --user -u system-monitor -f"

else
    info "Installing as system service (root)..."

    # Copy service file under the canonical unit name, apply port + host
    sed -e "s/AI_MONITOR_PORT=.*/AI_MONITOR_PORT=$PORT/" \
        -e "s/AI_MONITOR_HOST=.*/AI_MONITOR_HOST=$HOST/" \
        "$INSTALL_DIR/system-monitor-root.service" > /etc/systemd/system/system-monitor.service

    # Set CAP_PERFMON for intel_gpu_top if installed
    if command -v intel_gpu_top &>/dev/null; then
        INTEL_GPU_TOP_PATH=$(which intel_gpu_top)
        setcap cap_perfmon+ep "$INTEL_GPU_TOP_PATH" 2>/dev/null || \
            warn "Could not set CAP_PERFMON for intel_gpu_top"
    fi

    # Clean up legacy unit names from older versions
    for LEGACY in ai-monitor-root ai-monitor system-monitor-root system-monitor-user; do
        if [ -f "/etc/systemd/system/$LEGACY.service" ]; then
            systemctl disable --now "$LEGACY" 2>/dev/null || true
            rm -f "/etc/systemd/system/$LEGACY.service"
            info "Removed legacy unit: $LEGACY.service"
        fi
    done

    # Reload and enable
    systemctl daemon-reload
    systemctl enable system-monitor
    systemctl start system-monitor

    # Check status
    if systemctl is-active system-monitor &>/dev/null; then
        info "System service started successfully!"
    else
        error "Failed to start system service. Check: journalctl -u system-monitor -n 20"
    fi

    echo ""
    echo "=== Deployment Complete ==="
    echo "Access: http://localhost:$PORT"
    echo "API Docs: http://localhost:$PORT/api/docs"
    echo ""
    echo "Management commands:"
    echo "  sudo systemctl status system-monitor"
    echo "  sudo systemctl restart system-monitor"
    echo "  sudo systemctl stop system-monitor"
    echo "  sudo journalctl -u system-monitor -f"
fi
