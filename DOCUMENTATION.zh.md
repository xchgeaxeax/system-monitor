# System Performance Monitor — 完整文档

> 轻量级 Linux 系统性能监控面板：多用户认证、告警面板、黑白主题、按需刷新、单文件部署。

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构设计](#2-架构设计)
3. [快速开始](#3-快速开始)
4. [认证与多用户](#4-认证与多用户)
5. [告警系统](#5-告警系统)
6. [API 参考](#6-api-参考)
7. [命令行工具](#7-命令行工具)
8. [监控指标详解](#8-监控指标详解)
9. [前端设计](#9-前端设计)
10. [性能设计](#10-性能设计)
11. [部署指南](#11-部署指南)
12. [故障排查](#12-故障排查)
13. [项目结构](#13-项目结构)
14. [安全说明](#14-安全说明)

---

## 1. 项目概述

### 1.1 定位

System Performance Monitor 是一个面向 AI 服务器 / 家用服务器的轻量级性能监控面板，设计目标：

- **极简部署**：`server.py` + `dashboard.html` 两个文件即可运行，无数据库、无 Docker
- **高性能**：后台采样线程保持指标热缓存，API 毫秒级返回，20 并发无阻塞
- **按需刷新**：浏览器标签页不可见时自动停止请求，页面关闭时服务器零负载
- **多用户**：管理员 / 普通用户两种角色，管理员管理所有用户
- **可告警**：阈值告警 + 持久化历史，随时可回看、可确认、可删除
- **多 GPU**：AMD ROCm、Intel Xe/iGPU、sysfs 三级降级，自动检测

### 1.2 核心特性

| 特性 | 说明 |
|------|------|
| 多用户认证 | 首次运行创建管理员；PBKDF2-SHA256（39 万轮）密码哈希；会话 + API Key 双凭证 |
| 角色权限 | 管理员管理用户/全部 Key；普通用户仅管理自己的 Key |
| 告警面板 | 8 类阈值规则，自动触发/恢复，持久化历史，ack/删除/清空 |
| 黑白主题 | 跟随系统 `prefers-color-scheme`，手动 深色/浅色/自动 |
| 实时图表 | 网络吞吐 / CPU 频率 / 磁盘 I/O / GPU 利用率+VRAM，Canvas 自绘 |
| 图表交互 | hover 十字线 + tooltip，时间空洞自动断线（避免假曲线） |
| 全覆盖指标 | CPU（每核使用率/频率/温度/功耗）/ GPU / 内存 / 存储+SMART / 网络 / 温度 / 进程 / 日志 |
| 命令行管理 | 用户、密码、API Key、告警全部可 CLI 操作 |
| 环境变量配置 | 所有可调项均支持环境变量覆盖 |

### 1.3 访问信息

- **Dashboard**：`http://<host>:9527`（默认端口，可配置）
- **API 文档**：`http://<host>:9527/api/docs`（FastAPI 自动生成，需登录）
- **健康检查**：`http://<host>:9527/api/health`（公开，可用于探活/告警）

---

## 2. 架构设计

### 2.1 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                     浏览器 (dashboard.html)                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 认证层: 创建账号 / 登录 / 角色判断 (JS)                  │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ 标签页: Overview | Alerts | CPU | GPU | Memory |        │  │
│  │         Storage | Network | Temps | Processes | Logs |  │  │
│  │         Settings                                              │  │
│  └────────────────────────────────────────────────────────┘  │
│   页面可见时每 1.5s 请求当前标签页所需 API（标签隐藏时停止）      │
└──────────────────────────────────────────────────────────────┘
                              │ HTTP + Bearer token
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    FastAPI Server (server.py)                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  认证中间件 require_auth / require_admin                 │  │
│  │  (会话 token / API key → 用户身份 + 角色)                │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  API 层（全部读缓存，毫秒级返回）                          │  │
│  │  /api/summary /api/cpu /api/gpu /api/memory ...         │  │
│  │  /api/users /api/alerts /api/auth/*                     │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  后台采样线程（daemon）                                   │  │
│  │  sampler(1.5s)  → _cache{cpu,mem,net,storage,temps}    │  │
│  │  gpu-sampler(2s) → _gpu_cache (rocm-smi/intel_gpu_top) │  │
│  │  smart-loop(60s) → _smart_cache (nvme/smartctl)        │  │
│  │  每次采样同时评估告警规则                                  │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  数据源: psutil | /proc | /sys | rocm-smi | intel_gpu_top│  │
│  │          nvme | smartctl | journalctl                    │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  持久化: data/auth.json (用户/Key/会话)                   │  │
│  │          data/alerts.json (告警 active + history)        │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 关键设计决策

**为什么用后台采样线程而不是"纯按需"？**

v3 是纯按需架构（无请求不采集），但导致两个问题：
1. 每次请求都现场采集，`/api/gpu` 要跑 `intel_gpu_top -n 2`（约 1.2s），`/api/storage` 要跑 `nvme smart-log` + `smartctl`（约 0.8s），且阻塞事件循环，并发请求互相排队
2. 速率类指标（网络/磁盘 I/O）需要两次采样才能算出速率，按需架构下页面关闭再打开时速率归零

v4 改为**后台采样线程 + 读缓存**：
- 采样线程每 1.5s 采集一次 CPU/内存/网络/存储/温度，写入内存缓存
- GPU 单独 2s 一次（外部命令较慢），SMART 单独 60s 一次（数据变化极慢）
- API 端点只读缓存，全部 < 5ms 返回
- 速率指标由采样线程持续计算，页面打开即有数据
- 代价：服务器常驻约 60-80MB 内存、空闲时约 1-2% CPU（可接受）

**为什么历史按时间窗口而不是按条数裁剪？**

页面关闭期间不采集，按条数裁剪会留下大时间空洞（实测出现 3.5 小时 gap）。前端画图时若把空洞两端用直线连接，曲线严重失真。v4 按时间窗口（默认 5 分钟）裁剪，前端遇到 >8s 的时间间隔自动断线。

### 2.3 线程模型

| 线程 | 周期 | 职责 |
|------|------|------|
| sampler | 1.5s | CPU/内存/网络/存储/温度采样 + 告警评估 |
| gpu-sampler | 2s | rocm-smi / intel_gpu_top / sysfs GPU 采集 |
| smart-loop | 60s | nvme smart-log / smartctl 采集 |
| uvicorn 事件循环 | — | 处理 HTTP 请求（全部读缓存，不阻塞） |
| 线程池（FastAPI 自动） | — | 同步端点（/api/logs、/api/memory 等） |

所有缓存读写用 `threading.Lock` 保护；`auth.json` 的读-改-写用全局 `_auth_lock` 保护（避免并发写丢失会话）。

---

## 3. 快速开始

### 3.1 一键部署

```bash
git clone https://github.com/your-username/system-monitor.git
cd system-monitor

# Root 模式（推荐：SMART/日志完整功能）
sudo bash deploy.sh --root

# User 模式（无需 root，基本功能）
bash deploy.sh --user

# 自定义端口
bash deploy.sh --root --port 8080
```

`deploy.sh` 会：安装 Python 依赖 → 复制文件到 `/opt/system-monitor` → 安装并启动 systemd 服务。

### 3.2 首次使用

1. 打开 `http://<host>:9527`
2. 看到**创建管理员账号**界面，输入用户名 + 密码（≥8 位）
3. 进入面板，右上角 ⚙️ → **Users** 卡片创建普通用户

### 3.3 手动部署

```bash
# 1. 安装依赖
python3 -m pip install fastapi uvicorn psutil

# 2. Root 模式
sudo cp system-monitor-root.service /etc/systemd/system/system-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now system-monitor

# 3. User 模式
cp system-monitor-user.service ~/.config/systemd/user/system-monitor.service
systemctl --user daemon-reload
systemctl --user enable --now system-monitor
```

### 3.4 反向代理（Caddy 示例）

```caddy
monitor.example.com:9000 {
    tls { dns cloudflare {env.CF_API_TOKEN} }
    encode gzip zstd
    reverse_proxy localhost:9527 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

> 反代场景下把 `AI_MONITOR_HOST` 设为 `127.0.0.1`，面板不直接暴露公网。

---

## 4. 认证与多用户

### 4.1 认证流程

```
浏览器 ──GET /──▶ server 返回 dashboard.html（公开壳，无敏感数据）
   │
   ▼ JS 执行
GET /api/auth/status
   ├── configured=false → 显示"创建管理员"界面
   │     └── POST /api/auth/setup {username, password} → 返回 session token
   └── configured=true → 检查 localStorage 里的 token
         ├── 有 token → GET /api/quick-stats 验证
         │     ├── 200 → 进入面板
         │     └── 401 → 显示登录界面
         └── 无 token → 显示登录界面
               └── POST /api/auth/login {username, password} → session token
```

之后所有数据 API 都带 `Authorization: Bearer <token>`。

### 4.2 两种凭证

| 凭证 | 获取方式 | 能力 |
|------|----------|------|
| 会话 token | Web 登录 | 完整（按角色），12h 过期，可登出 |
| API Key | Web Settings 或 CLI 创建 | 只读所有监控数据 + 管理自己的 Key，不过期 |

API Key 格式：`amk_` + 32 字符随机串。**创建时只显示一次**，请妥善保存。

### 4.3 用户管理

管理员可以（Web Settings → Users 卡片，或 CLI）：

- **创建用户**：`user create <用户名> <密码> [admin|user]`
- **删除用户**：连带删除其所有会话和 API Key，旧 token 立即失效
- **重置密码**：`user reset-password <用户> <新密码>`
- **升降级**：`user role <用户> <admin|user>`

**保护规则**：
- 不能删除最后一个管理员
- 不能降级最后一个管理员
- 不能删除自己（需先降级或让其他管理员操作）
- 用户名：2-32 位字母/数字/`.`/`_`/`-`
- 密码：≥8 位，PBKDF2-SHA256 39 万轮 + 16 字节随机盐

### 4.4 数据格式（data/auth.json）

```json
{
  "users": {
    "admin": {
      "username": "admin",
      "salt": "<32 hex>",
      "hash": "<64 hex, PBKDF2-SHA256>",
      "role": "admin",
      "created_at": "2026-08-15T00:00:00Z"
    }
  },
  "keys": {
    "grafana": {
      "name": "grafana",
      "owner": "admin",
      "key": "amk_...",
      "created_at": "...",
      "last_used": null
    }
  },
  "sessions": {
    "<64 hex token>": {
      "username": "admin",
      "kind": "web",
      "created_at": "...",
      "expires_at": 1755200000.0
    }
  }
}
```

> 旧的单管理员格式（`"admin": {...}` 顶层字段）在服务器启动时**自动迁移**为
> `users` 字典，用户名、密码、API Key 全部保留。

---

## 5. 告警系统

### 5.1 规则

| 规则 ID | 条件 | 级别 |
|------|------|------|
| `disk_full:<挂载点>` | 分区使用率 ≥90%（≥95% 升 danger） | warning/danger |
| `mem_high` | 内存 ≥90%（≥95% 升 danger） | warning/danger |
| `swap_high` | Swap ≥80% | warning |
| `load_high` | 1min load ≥ 2× 核心数 | warning |
| `temp_high:<芯片>:<传感器>` | 温度 > 90% 临界温度 | danger |
| `smart:<设备>` | SMART critical_warning ≠ 0 或 health=CHECK | danger |
| `smart_life:<设备>` | NVMe percentage_used ≥ 90% | warning |
| `vram_high:<GPU>` | GPU VRAM ≥95% | warning |

### 5.2 生命周期

```
触发 ──▶ active（页面红点 + 告警面板）
  │         │
  │         ├── 条件消失 ──▶ 自动恢复 ──▶ history（保留日志）
  │         ├── 用户 ack ──▶ active(acknowledged)（变灰，仍显示）
  │         └── 用户删除 ──▶ 从 active 移除（彻底排除问题后）
  │
  └── history 条目可单独删除，或"清空已解决"一键清空
```

**防误报设计**：
- 数据源缺失时（如 GPU 采样失败）**不会**自动恢复对应告警（按 family 区分：core/smart/gpu）
- 手动添加的告警（`alerts add`）是 pinned 的，只会被手动删除，不会自动恢复

### 5.3 数据格式（data/alerts.json）

```json
{
  "active": {
    "disk_full:/": {
      "rule_id": "disk_full:/",
      "family": "core",
      "severity": "warning",
      "message": "Disk / at 92% (430/467 GB)",
      "since": "2026-08-15T06:00:00Z",
      "acknowledged": false
    }
  },
  "history": [
    {
      "rule_id": "mem_high",
      "family": "core",
      "severity": "warning",
      "message": "Memory at 91% ...",
      "since": "...",
      "resolved_at": "...",
      "auto_resolved": true
    }
  ]
}
```

历史最多保留 500 条，API 返回最近 200 条。

---

## 6. API 参考

所有端点基础路径 `/api`。认证端点见 [4.1](#41-认证流程)。

### 6.1 公开端点（无需认证）

#### `GET /api/health`

健康检查，可用于探活。

```json
{
  "status": "ok",
  "failed_checks": [],
  "version": "4.0",
  "auth_configured": true,
  "uptime_s": 622770,
  "sample_age_s": 1.4,
  "active_alerts": 0,
  "checks": {
    "rocm_smi_available": false,
    "intel_gpu_top_available": true,
    "disk_ok": true,
    "memory_ok": true,
    "nvme_smart_ok": true,
    "temps_ok": true
  }
}
```

`status` 为 `ok` / `warning`（有 failed_checks）。`sample_age_s` 是采样线程距离上次采样的秒数（>5s 说明采样线程异常）。

#### `GET /api/auth/status`

```json
{ "configured": true }
```

#### `POST /api/auth/setup`

首次创建管理员（已配置时返回 409）。

请求：`{"username": "admin", "password": "至少8位"}`
响应：`{"token": "...", "username": "admin", "role": "admin", "expires_in": 43200}`

#### `POST /api/auth/login`

请求：`{"username": "...", "password": "..."}`
响应：`{"token": "...", "username": "...", "role": "admin|user", "expires_in": 43200}`
错误：401 `Invalid username or password`

#### `POST /api/auth/logout`

登出当前会话（需要 Bearer token）。响应：`{"ok": true}`

### 6.2 用户管理（仅管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/users` | 列出所有用户 |
| POST | `/api/users` | 创建用户 `{"username","password","role"}` |
| DELETE | `/api/users/{username}` | 删除用户（连带会话+Key） |
| POST | `/api/users/{username}/password` | 重置密码 `{"new_password"}` |
| POST | `/api/users/{username}/role` | 改角色 `{"role": "admin"|"user"}` |

`GET /api/users` 响应：

```json
[
  {"username": "admin", "role": "admin", "created_at": "..."},
  {"username": "alice", "role": "user", "created_at": "..."}
]
```

### 6.3 监控数据（需要认证）

| 端点 | 说明 | 数据来源 |
|------|------|----------|
| `GET /api/summary` | 概览（CPU%/温度/内存/GPU/网络/磁盘/负载） | 缓存 |
| `GET /api/quick-stats` | 精简（uptime/load/cores） | 缓存 |
| `GET /api/cpu` | CPU 详情（每核使用率/频率/温度/功耗/缓存） | 缓存 |
| `GET /api/gpu` | GPU 详情（rocm/intel/sysfs 三源） | 缓存（2s） |
| `GET /api/memory` | 内存 + Top 30 进程（按 RSS） | 缓存 + 现场进程扫描 |
| `GET /api/storage` | 分区 + 磁盘 I/O + NVMe 温度 + SMART | 缓存 + SMART 缓存(60s) |
| `GET /api/network` | 网卡列表（速率/累计/错误/虚拟标记） | 缓存 |
| `GET /api/temps` | 温度传感器 + 风扇 | 缓存 |
| `GET /api/processes?sort_by=cpu&search=&limit=50` | 进程列表（sort_by: cpu/mem/rss_mb） | 现场扫描 |
| `GET /api/logs?lines=100&unit=&search=&level=all&noisy=false` | 系统日志 | journalctl |

**历史曲线端点**（返回 `[[ts, v1, v2, ...], ...]`，时间窗口内）：

| 端点 | 数据 |
|------|------|
| `GET /api/net-history` | `[ts, rx_mbps, tx_mbps]` |
| `GET /api/gpu-history` | `{gpu_id: [[ts, util%, vram%, temp], ...]}` |
| `GET /api/cpu-freq-history` | `[ts, mhz]` |
| `GET /api/disk-io-history` | `[ts, {disk: [read_mbps, write_mbps]}]` |

### 6.4 告警（需要认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/alerts` | `{active: [...], history: [...]}` |
| POST | `/api/alerts/ack?rule_id=...` | 确认告警 |
| DELETE | `/api/alerts/active?rule_id=...` | 删除告警 |
| DELETE | `/api/alerts/history/{index}` | 删除历史条目 |
| POST | `/api/alerts/clear-resolved` | 清空已解决历史 |

### 6.5 API Key 与密码

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/auth/keys` | 认证 | 列表（普通用户只见自己的，含 owner 字段） |
| POST | `/api/auth/keys` | 认证 | 创建 `{"name"}`，响应含完整 key（仅一次） |
| DELETE | `/api/auth/keys/{name}` | 认证 | 删除（只能删自己的，管理员可删全部） |
| POST | `/api/auth/password` | 认证 | 改自己密码 `{"old_password","new_password"}` |

### 6.6 错误格式

- 401 `{"detail": "Unauthorized"}` / `{"detail": "Setup required"}`
- 403 `{"detail": "Admin role required"}` 等
- 404 `{"detail": "..."}`
- 500 `{"error": "Internal server error"}`（细节只进服务器日志，不泄露到客户端）

---

## 7. 命令行工具

`monitor-cli.py` 直接操作 `data/` 目录，与运行中的服务器**实时同步**（服务器按 mtime 自动重载）。

```bash
# 状态总览
python3 monitor-cli.py status

# 用户管理（写操作需要 root）
python3 monitor-cli.py user list
python3 monitor-cli.py user create alice '至少8位密码' user
python3 monitor-cli.py user create bob '至少8位密码' admin
python3 monitor-cli.py user delete alice
python3 monitor-cli.py user reset-password alice '新密码'
python3 monitor-cli.py user role alice admin

# 重置第一个管理员密码（兼容旧命令）
sudo python3 monitor-cli.py reset-password '新密码'

# API Key
python3 monitor-cli.py key list
python3 monitor-cli.py key create grafana admin
python3 monitor-cli.py key delete grafana

# 告警
python3 monitor-cli.py alerts
python3 monitor-cli.py alerts add "维护窗口" warning
python3 monitor-cli.py alerts ack <rule_id>
python3 monitor-cli.py alerts delete <rule_id>
python3 monitor-cli.py alerts clear
```

> **数据目录一致性**：CLI 默认用脚本所在目录的 `data/`。如果服务器设置了
> `AI_MONITOR_DATA_DIR`，CLI 必须 `export AI_MONITOR_DATA_DIR=<同一路径>`。

---

## 8. 监控指标详解

### 8.1 CPU

| 指标 | 来源 | 说明 |
|------|------|------|
| 使用率（总/每核） | `psutil.cpu_percent` | 采样线程每 1.5s 更新 |
| 频率（当前/最大/最小） | `psutil.cpu_freq` | — |
| 每核频率 | `/sys/devices/system/cpu/cpuN/cpufreq/scaling_cur_freq` | — |
| 每核温度 | `/sys/class/hwmon/*/tempN_input`（coretemp） | 含临界温度 |
| 包功耗 | Intel RAPL `energy_uj` 差分 | 非 Intel 平台为 0 |
| 缓存 | `/proc/cpuinfo` | L1d/L1i/L2/L3 |
| 负载 | `os.getloadavg()` | 1/5/15 min |

### 8.2 GPU（三级降级）

1. **AMD ROCm**（`rocm-smi --json`）：温度（edge/junction/memory）、功耗、VRAM 使用/总量、GPU 活动%、显存读写%、S/M/SOC 时钟、占用进程列表（PID/名称/VRAM）
2. **Intel**（`intel_gpu_top -J -n 2`）：总占用、Render/Blitter/Video/Compute 分引擎占用、实际频率、RC6、功耗；频率为 0 时回退 `nvtop -s`
3. **sysfs 兜底**（`/sys/class/drm/cardN/device/`）：`gpu_busy_percent`、`mem_info_vram_*`、hwmon 温度/功耗

拓扑（card → 厂商 → 产品名）通过 PCI vendor/device id + `/proc/pci/devices` 解析，缓存 5 分钟。

### 8.3 内存

`psutil.virtual_memory` / `swap_memory`，附 Top 30 进程（按 RSS，>5MB 才显示）。

### 8.4 存储

- **分区**：`psutil.disk_partitions`（过滤 squashfs/tmpfs/devtmpfs/overlay 和 /snap /dev /run 等），含使用率
- **磁盘 I/O**：`psutil.disk_io_counters(perdisk)`，只统计整盘（sd*/nvme*/virtio*/xvd*，排除分区），速率由采样线程差分
- **NVMe 温度**：`/sys/class/hwmon/*`（nvme 芯片）
- **SMART**（60s 缓存）：
  - NVMe：`nvme smart-log`（root）或 sysfs `smart_information_log` 兜底 —— 温度/健康/可用备用/寿命百分比/累计读写 TB/通电小时
  - SATA：`smartctl -a -j`（依次尝试 `-d sat` / `-d ata` / 无参数）

### 8.5 网络

`psutil.net_io_counters(pernic)`：每网卡速率（Mbps）、累计流量、错误/丢包、链路速度（sysfs `speed`）、operstate；虚拟网卡（docker/veth/br-/tailscale/wg/tun/tap）默认隐藏，可勾选显示。

### 8.6 温度与风扇

`/sys/class/hwmon/*` 全量扫描：温度传感器（含 max/crit 阈值）+ 风扇 RPM。NVMe 芯片按设备去重命名（nvme0/nvme1）。

### 8.7 进程

现场扫描 `psutil.process_iter`（CPU% 由两次扫描的 cpu_times 差分计算）：
- 过滤：mem < 0.3% 且 RSS < 50MB 的进程
- 解析：python/node/java 等显示完整命令行
- 排序：cpu / mem / rss_mb
- 搜索：名称 / 用户 / PID

### 8.8 系统日志

`journalctl -n 500` 采样：
- 按 unit 过滤（从日志自动枚举 unit 列表）
- 按级别（all/err/warning/notice）
- 关键字 grep
- 默认过滤噪音 unit（tailscaled/avahi/systemd-resolved/NetworkManager/fwupd/snapd），可关闭

---

## 9. 前端设计

### 9.1 布局

```
┌──────────────────────────────────────────────────────────┐
│ Topbar: ● System Monitor v4 | Uptime | Load | 🔔 | 🌗 | ⚙️ | Live │
├──────────────────────────────────────────────────────────┤
│ Tabs: Overview | Alerts | CPU | GPU | Memory | Storage |  │
│       Network | Temperatures | Processes | Logs | (Settings) │
├──────────────────────────────────────────────────────────┤
│ 当前标签页内容（卡片式，可折叠）                                │
└──────────────────────────────────────────────────────────┘
```

### 9.2 刷新策略

- 页面**可见**时每 1.5s 请求一次**当前标签页**所需 API（只拉需要的数据）
- 页面**隐藏**（切标签/最小化）时完全停止请求
- 切换标签立即刷新
- 请求失败显示顶部错误横幅（6s 自动消失），不阻塞其他数据

### 9.3 主题

CSS 变量双主题（`:root[data-theme="dark|light"]`）：
- **Auto**（默认）：跟随 `prefers-color-scheme`，系统切换时实时跟随
- **Dark / Light**：手动固定
- 选择持久化在 `localStorage`

### 9.4 图表

纯 Canvas 自绘（无第三方库）：
- 支持多系列、面积填充、自适应 y 轴（nice max）
- **gap 断线**：相邻点时间差 >8s 时断开（避免页面关闭期间产生假直线）
- **hover 交互**：十字线 + 最近点 tooltip（时间 + 各系列值）
- 高 DPI 适配（devicePixelRatio）
- ResizeObserver 自适应容器

### 9.5 认证状态机（JS）

```
load → GET /api/auth/status
  configured=false → 创建账号界面
  configured=true + 无 token → 登录界面
  configured=true + 有 token → GET /api/quick-stats
      200 → 主界面（记住角色，Settings 显示对应权限）
      401 → 登录界面
```

---

## 10. 性能设计

### 10.1 实测数据（16 核 / 32GB / 双 NVMe + 3 SATA）

| 端点 | v3（按需现采） | v4（读缓存） |
|------|---------------|-------------|
| /api/summary | ~50ms | **2.5ms** |
| /api/cpu | ~30ms | **2ms** |
| /api/gpu | **1157ms** | **2ms** |
| /api/storage | **831ms** | **2.5ms** |
| /api/memory | ~40ms | 24ms（含进程扫描） |
| /api/processes | ~30ms | 33ms（现场扫描） |
| /api/logs | ~200ms | 12ms（journalctl 快路径） |

20 并发混合请求（含慢的 /api/logs）：全部 200，总耗时 < 1s，事件循环不阻塞。

### 10.2 缓存层级

| 缓存 | TTL | 内容 |
|------|-----|------|
| `_cache` | 1.5s（采样线程刷新） | cpu/mem/net/storage/temps/summary/quick |
| `_gpu_cache` | 2s | GPU 三源数据 |
| `_smart_cache` | 60s | SMART 全量 |
| `_tool_cache` | 5min | rocm-smi / intel_gpu_top 可用性探测 |
| `_gpu_topology` | 5min | GPU 拓扑（card/厂商/产品名） |

### 10.3 资源占用

- 内存：约 60-80MB（systemd `MemoryMax=256M` 兜底）
- CPU：空闲约 1-2%（采样线程），页面打开时略增
- 磁盘：无写入（除 auth.json/alerts.json 变更时）

---

## 11. 部署指南

### 11.1 服务模式

| 模式 | 服务文件 | 功能 | 适用 |
|------|----------|------|------|
| Root | `system-monitor-root.service` | 完整（SMART/日志/所有温度） | 推荐 |
| User | `system-monitor-user.service` | 基本（无 SMART/日志） | 无 root 权限时 |

### 11.2 关键配置

```ini
[Service]
Environment=AI_MONITOR_PORT=9527
Environment=AI_MONITOR_HOST=127.0.0.1      # 反代场景
Environment=AI_MONITOR_DATA_DIR=/opt/system-monitor/data
```

### 11.3 可选工具安装

```bash
# Intel GPU（需要 CAP_PERFMON）
sudo apt install intel-gpu-tools
sudo setcap cap_perfmon+ep $(which intel_gpu_top)

# AMD GPU
sudo apt install rocm-smi-lib

# 存储 SMART
sudo apt install smartmontools nvme-cli
```

### 11.4 备份

只需备份 `data/` 目录（auth.json 含所有用户密码哈希和 API Key，alerts.json 含告警历史）。

### 11.5 升级

```bash
cd /opt/system-monitor   # 或你的部署目录
git pull                 # 或手动复制新文件
sudo systemctl restart system-monitor
```

> 数据格式变更会自动迁移（如单管理员 → 多用户），无需手动操作。

---

## 12. 故障排查

| 症状 | 排查 |
|------|------|
| 页面显示 `{"detail":"Setup required"}` | 访问的是 API 而不是页面；确认访问 `http://host:port/`（带斜杠的根路径） |
| 登录 401 | 确认用户名密码；CLI `status` 查看用户是否存在 |
| 登录后立刻又跳登录 | token 过期（默认 12h）或被管理员重置；重新登录 |
| GPU 无数据 | 检查 `rocm-smi` / `intel_gpu_top` 是否安装；`/api/health` 的 checks 字段 |
| SMART 显示 N/A | 非 root 运行；或 `nvme-cli` / `smartmontools` 未安装 |
| 日志为空 | 需要 root；或 journalctl 无权限 |
| 图表有断层 | 正常现象：页面关闭期间不采集，>8s 间隔自动断线 |
| 告警不恢复 | 数据源缺失时不会误恢复（设计如此）；或手动删除 |
| 端口被占用 | 改 `AI_MONITOR_PORT` 或 `ss -tlnp \| grep 9527` 查占用 |
| 服务没起来 | `journalctl -u system-monitor -n 50 --no-pager` |
| CLI 和 Web 数据不一致 | CLI 的 `AI_MONITOR_DATA_DIR` 与服务器不一致 |

---

## 13. 项目结构

```
system-monitor/
├── server.py              # 后端（FastAPI + 采样线程 + 认证 + 告警引擎）
├── dashboard.html         # 前端（单文件：HTML + CSS + JS，无第三方依赖）
├── monitor-cli.py         # 命令行管理工具
├── deploy.sh              # 一键部署脚本
├── system-monitor-root.service   # Root 模式 systemd 单元
├── system-monitor-user.service   # User 模式 systemd 单元
├── requirements.txt       # Python 依赖（fastapi/uvicorn/psutil）
├── README.md              # 双语 README
├── DOCUMENTATION.zh.md    # 本文档
├── DOCUMENTATION.en.md    # English documentation
└── data/                  # 运行时生成（不入库）
    ├── auth.json          # 用户/API Key/会话（0600）
    └── alerts.json        # 告警 active + history
```

---

## 14. 安全说明

**已实现**：
- 密码 PBKDF2-SHA256 39 万轮 + 每用户随机盐，文件 0600 权限
- 会话 token 64 字节随机，12h 过期，登出即失效
- API Key 与用户绑定，普通用户只能管理自己的
- 删除用户连带失效其所有会话和 Key
- 角色校验：API Key 永远不能管理用户（只能读数据）
- 异常信息不泄露到客户端（500 只返回通用消息）
- 所有用户输入做校验（用户名正则、密码长度、Key 名称正则）
- 并发安全：auth.json 全局锁，alerts.json 文件锁 + mtime 重载
- 反代场景建议 `AI_MONITOR_HOST=127.0.0.1`

**建议**：
- 公网部署务必走 HTTPS（Caddy/nginx 反代）
- 不要直接暴露 9527 端口到公网
- 定期轮换 API Key
- 备份 `data/` 目录（含密码哈希，丢失意味着所有用户无法登录）

**已知限制**：
- 无暴力破解限速（依赖 HTTPS + 强密码 + 反代；如需可加 fail2ban）
- 无审计日志（登录/管理操作未记录到独立日志）
- 单节点设计（无多副本/分布式）
