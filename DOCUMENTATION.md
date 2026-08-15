# System Performance Monitor v3 — Documentation

> Lightweight Linux system performance monitoring panel, on-demand refresh architecture, zero background polling, single-process, no Prometheus/Grafana needed.

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 架构设计](#2-架构设计)
- [3. 技术栈](#3-技术栈)
- [4. 部署与运行](#4-部署与运行)
- [5. API 设计](#5-api-设计)
- [6. 前端设计](#6-前端设计)
- [7. 监控指标详解](#7-监控指标详解)
- [8. GPU 支持](#8-gpu-支持)
- [9. 性能与资源](#9-性能与资源)
- [10. 项目结构](#10-项目结构)
- [11. 配置说明](#11-配置说明)
- [12. 故障排查](#12-故障排查)
- [13. 扩展方向](#13-扩展方向)

---

## 1. 项目概述

### 1.1 项目定位

AI Server Performance Monitor v3 是一个面向 AI 服务器的轻量级性能监控面板，核心设计目标是：

- **极简部署**：单 Python 文件，零外部数据库，无需 Docker/K8s
- **按需刷新**：仅在浏览器可见时请求数据，后台零轮询
- **独立 API**：每个标签页只请求所需数据，避免全量快照
- **多 GPU 支持**：AMD ROCm、Intel Xe、sysfs 回退，自动检测
- **健康检查**：自动检测磁盘、内存、温度、SMART 状态
- **Root 可选**：支持 User 服务（基本功能）和 Root 服务（完整功能）

### 1.2 核心特性

| 特性 | 说明 |
|------|------|
| 按需刷新 | 页面不可见时自动停止请求，标签切换时立即刷新 |
| 独立 API | 每个标签页对应独立 API 端点，只拉取需要的数据 |
| 零后台轮询 | 无后台定时器，无请求时无 CPU 占用 |
| 单进程 | 单个 Python 进程，无额外依赖 |
| GPU 多源 | rocm-smi → intel_gpu_top → sysfs 三级降级 |
| GPU 曲线 | GPU 利用率和 VRAM 使用历史曲线 |
| CPU 频率曲线 | CPU 频率变化历史，检测降频 |
| 磁盘 I/O 速率 | 实时读写速率（MB/s），非累计值 |
| SMART 支持 | NVMe SMART 信息（温度/健康/总读写/通电时间） |
| 网络速率图 | Canvas 实时绘制上下行速率曲线 |
| 系统日志 | journalctl 集成（需要 root） |
| 健康检查 | 自动告警（磁盘/内存/温度/SMART） |
| 环境变量配置 | 所有配置项支持环境变量覆盖 |
| 错误处理 | 全局异常处理器，部分数据返回 |

### 1.3 访问信息

- **Dashboard**: http://localhost:9527
- **API 文档**: http://localhost:9527/api/docs（FastAPI 自动生成）

---

## 2. 架构设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      浏览器 (Dashboard)                      │
│  ┌──────────┬──────────┬──────────┬──────────┬───────────┐  │
│  │Overview  │   CPU    │   GPU    │ Memory   │ Storage   │  │
│  │Network   │Temperat. │Processes │ Logs     │           │  │
│  └──────────┴──────────┴──────────┴──────────┴───────────┘  │
│                      ↓ 按需请求 (仅当前标签页)                 │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP (REST)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  FastAPI Server (server.py)                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Global Exception Handler               │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                    API Layer                        │    │
│  │  /api/health  /api/summary  /api/cpu  /api/gpu      │    │
│  │  /api/memory  /api/storage  /api/network  /api/temps│    │
│  │  /api/processes  /api/logs  /api/all                │    │
│  │  /api/gpu-history  /api/cpu-freq-history            │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  Data Collection Layer              │    │
│  │  psutil │ /proc │ /sys │ rocm-smi │ intel_gpu_top  │    │
│  │  smartctl │ journalctl                               │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  History Buffers                    │    │
│  │  Network speed │ GPU metrics │ CPU freq │ Disk I/O │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼─────────────────────────────┐
              ▼               ▼               ▼             ▼
         ┌─────────┐    ┌─────────┐    ┌─────────┐   ┌────────┐
         │  psutil │    │ /proc   │    │ /sys    │   │ smartctl│
         │         │    │ (CPU/   │    │ (hwmon/ │   │(root)  │
         │ 进程/内存│    │  block) │    │  drm/   │   └────────┘
         │ 网络/磁盘│    │         │    │  nvme)  │
         └─────────┘    └─────────┘    └─────────┘
```

### 2.2 按需刷新架构

传统监控面板通常采用固定频率轮询（如每 2 秒刷新所有数据），无论用户是否在看页面。本项目采用按需刷新：

**工作流程：**

1. 用户打开浏览器 → 开始轮询当前标签页数据（1.5 秒间隔）
2. 用户切换到其他标签页 → 立即请求新标签页数据
3. 用户切换标签页/最小化浏览器 → **完全停止请求**
4. 用户返回浏览器 → 恢复轮询

**实现机制：**

- 使用浏览器 `visibilitychange` API 检测页面可见性
- `document.hidden === true` 时清除 `setInterval`
- 标签切换时触发一次立即刷新 `fetchData()`
- 后端无任何定时任务，完全被动响应请求

**优势：**

- 页面不可见时零网络请求
- 后端无请求时零 CPU 占用
- 每个标签页只请求需要的 API，减少数据传输

### 2.3 独立 API 设计

每个标签页对应独立的 API 端点，避免一次性返回所有数据：

```
标签页        API 端点
------        --------
Overview      /api/summary + /api/quick-stats + /api/network + /api/net-history + /api/health + /api/storage
CPU           /api/cpu + /api/cpu-freq-history
GPU           /api/gpu + /api/gpu-history
Memory        /api/memory
Storage       /api/storage
Network       /api/network + /api/net-history
Temperatures  /api/temps
Processes     /api/processes
Logs          /api/logs
```

**设计理由：**

- GPU 检测（rocm-smi）可能耗时，CPU 标签页不需要 GPU 数据
- SMART 信息读取可能慢，Overview 不需要
- 减少单次请求的响应大小和计算开销

### 2.4 数据源分层

```
优先级 1: psutil (进程/内存/网络/磁盘基础信息)
优先级 2: /proc (CPU 信息、块设备统计)
优先级 3: /sys/class/hwmon (温度/风扇)
优先级 4: /sys/class/drm (GPU 信息)
优先级 5: /sys/class/nvme (NVMe SMART)
优先级 6: rocm-smi (AMD GPU 详细信息)
优先级 7: intel_gpu_top (Intel GPU 详细信息)
优先级 8: smartctl (SATA SMART，需要 root)
优先级 9: journalctl (系统日志，需要 root)
```

### 2.5 历史数据缓冲

服务端维护时间序列历史数据，用于绘制曲线图：

| 历史数据 | 用途 | 最大点数 | 时间跨度 |
|----------|------|----------|----------|
| 网络速率 | 网络速率图 | 120 | ~2 分钟 |
| GPU 指标 | GPU 利用率/VRAM 曲线 | 120 | ~2 分钟 |
| CPU 频率 | CPU 频率曲线 | 120 | ~2 分钟 |
| 磁盘 I/O | 实时速率计算 | 1 个快照 | - |

### 2.6 GPU 拓扑缓存

GPU 拓扑信息（型号、总线 ID、厂商）缓存 5 分钟，避免每次请求都扫描 sysfs：

```python
_gpu_topology: Optional[Dict] = None
_gpu_topology_time: float = 0
_GPU_TOPOLOGY_TTL = 300  # 5 minutes
```

---

## 3. 技术栈

### 3.1 后端

| 组件 | 版本/说明 |
|------|-----------|
| Python | 3.14+ |
| FastAPI | Web 框架，自动生成 OpenAPI 文档 |
| Uvicorn | ASGI 服务器 |
| psutil | 跨平台系统监控库 |
| subprocess | 调用 rocm-smi/intel_gpu_top/smartctl/journalctl |
| logging | 结构化日志输出 |

### 3.2 前端

| 组件 | 说明 |
|------|------|
| 原生 HTML/CSS/JS | 零框架，单文件内嵌 |
| Canvas API | 网络速率图、GPU 曲线、CPU 频率曲线 |
| Fetch API | HTTP 请求 |
| Visibility API | 页面可见性检测 |
| Promise.allSettled | 并行请求，部分失败不影响其他 |

### 3.3 部署

| 组件 | 说明 |
|------|------|
| systemd (user) | 用户级服务管理（推荐） |
| systemd (system) | 系统级服务管理（root，完整功能） |
| Python3 系统包 | 无需虚拟环境 |

---

## 4. 部署与运行

### 4.1 依赖安装

```bash
# 安装 Python 依赖
pip install fastapi uvicorn psutil

# 可选：GPU 工具
sudo apt install rocm-smi-lib    # AMD GPU
sudo apt install intel-gpu-tools # Intel GPU
sudo apt install smartmontools   # SMART 信息
```

### 4.2 方案 A：User 服务（推荐）

适合大多数场景，功能完整，仅缺少 SMART 完整信息和系统日志。

**服务文件：** `ai-monitor.service`

```ini
[Unit]
Description=AI Server Performance Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/xchgeaxeax/.hermes/workspace/monitor/server.py
WorkingDirectory=/home/xchgeaxeax/.hermes/workspace/monitor
Restart=on-failure
RestartSec=5
MemoryMax=128M
LimitNOFILE=1024

[Install]
WantedBy=default.target
```

```bash
# 安装服务文件
cp ai-monitor.service ~/.config/systemd/user/

# 重载配置
systemctl --user daemon-reload

# 启用开机自启并启动
systemctl --user enable --now ai-monitor

# 查看状态
systemctl --user status ai-monitor
```

### 4.3 方案 B：Root 服务（完整功能）

需要 root 权限才能获取：
- SMART 完整信息（smartctl）
- 系统日志（journalctl）
- 某些硬件传感器数据

**服务文件：** `ai-monitor-root.service`

```ini
[Unit]
Description=AI Server Performance Monitor (Root)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/xchgeaxeax/.hermes/workspace/monitor/server.py
WorkingDirectory=/home/xchgeaxeax/.hermes/workspace/monitor
Restart=on-failure
RestartSec=5
User=root
MemoryMax=256M
LimitNOFILE=2048
Environment=AI_MONITOR_PORT=9527
Environment=AI_MONITOR_HOST=0.0.0.0
Environment=AI_MONITOR_DEBUG=0

[Install]
WantedBy=multi-user.target
```

```bash
# 安装服务文件（需要 sudo）
sudo cp ai-monitor-root.service /etc/systemd/system/

# 重载配置
sudo systemctl daemon-reload

# 启用开机自启并启动
sudo systemctl enable --now ai-monitor-root

# 查看状态
sudo systemctl status ai-monitor-root
```

### 4.4 管理命令

**User 服务：**
```bash
systemctl --user start ai-monitor
systemctl --user stop ai-monitor
systemctl --user restart ai-monitor
systemctl --user status ai-monitor
journalctl --user -u ai-monitor -f
```

**Root 服务：**
```bash
sudo systemctl start ai-monitor-root
sudo systemctl stop ai-monitor-root
sudo systemctl restart ai-monitor-root
sudo systemctl status ai-monitor-root
sudo journalctl -u ai-monitor-root -f
```

### 4.5 手动运行

```bash
cd /home/xchgeaxeax/.hermes/workspace/monitor
python3 server.py
# 或
bash start.sh
```

---

## 5. API 设计

### 5.1 端点总览

| 端点 | 方法 | 说明 | 响应大小 |
|------|------|------|----------|
| `/api/health` | GET | 健康检查 | ~200B |
| `/api/summary` | GET | 精简摘要（状态栏） | ~300B |
| `/api/quick-stats` | GET | Quick Stats 数据 | ~200B |
| `/api/cpu` | GET | CPU 详细信息 | ~2KB |
| `/api/gpu` | GET | GPU 详细信息 | ~1-5KB |
| `/api/memory` | GET | 内存 + Top 30 进程 | ~3KB |
| `/api/storage` | GET | 存储 + SMART + NVMe | ~2-5KB |
| `/api/network` | GET | 网络接口统计 | ~500B |
| `/api/net-history` | GET | 网络速率历史 | ~3KB |
| `/api/gpu-history` | GET | GPU 指标历史 | ~3KB |
| `/api/cpu-freq-history` | GET | CPU 频率历史 | ~2KB |
| `/api/temps` | GET | 温度传感器 + 风扇 | ~1KB |
| `/api/processes` | GET | Top 50 进程 | ~5KB |
| `/api/logs` | GET | 系统日志（需要 root） | ~2KB |
| `/api/all` | GET | 完整快照（调试用） | ~20KB |

### 5.2 响应示例

**`/api/health`**
```json
{
  "status": "ok",
  "version": "3.0",
  "uptime_s": 697874,
  "checks": {
    "rocm_smi_available": false,
    "intel_gpu_top_available": false,
    "disk_ok": true,
    "memory_ok": true,
    "nvme_smart_ok": true,
    "temps_ok": true
  }
}
```

**`/api/summary`**
```json
{
  "cpu_percent": 12.5,
  "cpu_load": [1.2, 0.8, 0.6],
  "cpu_temp_max": 45.0,
  "mem_percent": 35.2,
  "mem_used_gb": 14.1,
  "mem_total_gb": 40.0,
  "gpu_count": 1,
  "gpu_temp_max": 45.0,
  "gpu_power_w": 85.0,
  "gpu_vram_used_mb": 4096.0
}
```

**`/api/cpu`**
```json
{
  "model": "AMD Ryzen Threadripper PRO 5955WX",
  "cores": 16,
  "threads": 32,
  "freq_current": 3600.0,
  "freq_max": 4500.0,
  "freq_min": 400.0,
  "usage_percent": 12.5,
  "usage_per_core": [5, 8, 15, ...],
  "load_avg": [1.2, 0.8, 0.6],
  "core_temps": [
    {"id": "coretemp_1", "label": "Core 0", "temp_c": 42.0, "crit_c": 95.0}
  ],
  "core_freqs": [3600, 3600, ...],
  "cache": {
    "l1d cache": "32K",
    "l1i cache": "32K",
    "l2 cache": "512K",
    "l3 cache": "64M"
  }
}
```

**`/api/gpu`**
```json
{
  "rocm": [
    {
      "name": "AMD Radeon PRO V620",
      "temp_edge": 45.0,
      "temp_junction": 62.0,
      "temp_memory": 38.0,
      "power_w": 85.0,
      "vram_used_mb": 4096.0,
      "vram_total_mb": 32768.0,
      "vram_percent": 12.5,
      "utilization": 45.0,
      "sclk": 1800,
      "mclk": 1600,
      "pids": [
        {"pid": 12345, "name": "python", "vram_used_mb": 2048, "is_compute": true}
      ]
    }
  ],
  "intel": [],
  "sysfs": []
}
```

### 5.3 错误处理

全局异常处理器捕获所有 API 错误，返回 JSON 格式：

```json
{
  "error": "详细错误信息",
  "partial": true
}
```

---

## 6. 前端设计

### 6.1 布局结构

```
┌──────────────────────────────────────────────────────────┐
│  Topbar: [● AI Server Monitor v3] [Uptime] [Load] [Live]│
├──────────────────────────────────────────────────────────┤
│  Tabs: Overview | CPU | GPU | Memory | Storage | ...    │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Content Area (当前标签页内容)                            │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Summary Grid / Cards / Tables / Graphs          │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 6.2 刷新策略

```javascript
// 刷新间隔：1.5 秒
const refreshRate = 1500;

// 页面可见性变化
document.addEventListener('visibilitychange', () => {
    isVisible = !document.hidden;
    if (isVisible) {
        startRefresh();    // 恢复轮询
    } else {
        stopRefresh();     // 停止轮询
    }
});

// 标签切换时立即刷新
tab.addEventListener('click', () => {
    currentTab = tab.dataset.tab;
    fetchData();           // 立即请求新标签页数据
});
```

### 6.3 数据请求策略

每个标签页只请求需要的 API，使用 `Promise.allSettled` 并行请求：

```javascript
switch (currentTab) {
    case 'overview':
        endpoints.summary = '/api/summary';
        endpoints.quick = '/api/quick-stats';
        endpoints.net = '/api/network';
        endpoints.netHistory = '/api/net-history';
        endpoints.health = '/api/health';
        endpoints.storage = '/api/storage';
        break;
    case 'cpu':
        endpoints.cpu = '/api/cpu';
        endpoints.cpuFreqHistory = '/api/cpu-freq-history';
        break;
    // ...
}

await Promise.allSettled(
    Object.entries(endpoints).map(async ([key, url]) => {
        results[key] = await fetchJSON(url);
    })
);
```

### 6.4 图表

- **网络速率图**：Canvas 绘制 RX/TX 曲线，自动缩放 Y 轴
- **CPU 频率曲线**：Cyan 色线条，显示频率变化
- **GPU 曲线**：蓝色（利用率）+ 绿色（VRAM）双线

### 6.5 颜色编码

| 场景 | 绿色 | 橙色 | 红色 |
|------|------|------|------|
| CPU/内存使用率 | < 60% | 60-80% | > 80% |
| CPU 温度 | < 72°C | 72-90°C | > 90°C |
| GPU 温度 | < 80°C | 80-95°C | > 95°C |
| 磁盘使用率 | < 70% | 70-90% | > 90% |

---

## 7. 监控指标详解

### 7.1 CPU

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 型号 | `/proc/cpuinfo` | CPU 完整型号名称 |
| 核心/线程数 | psutil | 物理核心数和逻辑线程数 |
| 频率 | psutil + sysfs | 当前/最大/最小频率 |
| 使用率 | psutil | 总使用率 + 每核使用率 |
| 负载平均值 | `os.getloadavg()` | 1/5/15 分钟负载 |
| 核心温度 | `/sys/class/hwmon/` | coretemp 驱动，每核温度 |
| 核心频率 | `/sys/.../scaling_cur_freq` | 每核实时频率 |
| 缓存信息 | `/proc/cpuinfo` | L1/L2/L3 缓存大小 |

### 7.2 GPU

| 指标 | AMD (rocm-smi) | AMD (sysfs) | Intel |
|------|----------------|-------------|-------|
| 温度 | Edge/Junction/Memory | temp1_input | temp1_input |
| 功耗 | Average Graphics Package Power | power1_average | power1_average |
| VRAM | sysfs mem_info_vram_* | sysfs mem_info_vram_* | - |
| 频率 | SCLK/MCLK/SOCCLK | - | freq_current_mhz |
| 使用率 | GPU Activity (%) | gpu_busy_percent | total_usage |
| 进程 | rocm-smi --showpids | - | - |

### 7.3 Memory

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 总量/已用/可用 | psutil | 物理内存 |
| 缓存/缓冲 | psutil | Linux 缓存机制 |
| Swap | psutil | 交换空间 |
| Top 30 进程 | psutil | 按 RSS 排序 |

### 7.4 Storage

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 分区容量 | psutil | 挂载点、文件系统类型 |
| 磁盘 I/O 累计 | psutil | 累计读写量、IOPS |
| 磁盘 I/O 速率 | psutil（差分计算） | 实时 MB/s |
| NVMe 温度 | `/sys/class/hwmon/` | NVMe 控制器温度 |
| SMART | `/sys/class/nvme/` + smartctl | 健康状态、总读写、通电时间 |

### 7.5 Network

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 实时速率 | psutil（差分计算） | 上下行 Mbps，使用实际时间差 |
| 累计流量 | psutil | 总收发字节数 |
| 数据包统计 | psutil | 收发包数、错误、丢包 |
| 链路状态 | `/sys/class/net/` | up/down、速率 |

隐藏接口：lo、docker、veth、br-、virbr、tailscale、wg、tun、tap

### 7.6 Temperatures

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 温度传感器 | `/sys/class/hwmon/` | 所有 hwmon 设备 |
| 风扇转速 | `/sys/class/hwmon/` | RPM |
| 临界温度 | `/sys/class/hwmon/` | temp*_crit |

### 7.7 Processes

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| Top 50 进程 | psutil | CPU 排序 |
| 筛选条件 | - | CPU > 0.1% 或 Mem > 0.3% 或 RSS > 50MB |
| 运行时间 | psutil create_time | 进程存活时间 |

### 7.8 System Logs

| 指标 | 数据来源 | 说明 |
|------|----------|------|
| 最近日志 | journalctl | 默认 50 行，最多 200 行 |
| 权限要求 | root | 非 root 只能看到部分日志 |

---

## 8. GPU 支持

### 8.1 三级检测机制

```
第 1 级: rocm-smi (AMD GPU 详细信息)
    ↓ 不可用
第 2 级: intel_gpu_top (Intel GPU)
    ↓ 不可用
第 3 级: sysfs fallback (AMD/Intel 基础信息)
```

### 8.2 AMD GPU (rocm-smi)

```bash
# 使用的命令
rocm-smi --showproductname --showtemp --showpower --showmemuse --showvoltage --json
rocm-smi -g -c -m -o --json
rocm-smi --showpids verbose --json
```

提供的指标：
- Edge/Junction/Memory 温度
- 功耗、电压
- VRAM 使用率
- SCLK/MCLK/SOCCLK 频率
- GPU 进程列表（PID、名称、VRAM 占用）

### 8.3 Intel GPU (intel_gpu_top)

```bash
intel_gpu_top --json --one-shot
```

提供的指标：
- 总使用率
- Render/Blitter/Video 细分使用率
- GT0/GT1 频率

### 8.4 sysfs Fallback

当 rocm-smi/intel_gpu_top 不可用时，从 `/sys/class/drm/card*/device/` 读取：

- `gpu_busy_percent` / `mem_busy_percent`
- `mem_info_vram_total` / `mem_info_vram_used`
- 温度从 hwmon 读取
- 功耗从 hwmon 读取

### 8.5 GPU 拓扑缓存

GPU 拓扑信息（型号、总线 ID、厂商）缓存 5 分钟：

```python
_gpu_topology: Optional[Dict] = None
_gpu_topology_time: float = 0
_GPU_TOPOLOGY_TTL = 300  # 5 minutes
```

### 8.6 GPU 进程监控

仅在 rocm-smi 可用时提供：

```json
{
  "pid": 12345,
  "name": "python",
  "vram_used_mb": 2048.0,
  "is_compute": true
}
```

---

## 9. 性能与资源

### 9.1 资源占用

| 场景 | 内存 | CPU |
|------|------|-----|
| 无请求时 | ~38MB | ~0.1% |
| 正常请求 | ~42-50MB | 瞬时 < 1% |
| 峰值（/api/all） | ~68MB | 瞬时 < 5% |

### 9.2 API 响应时间

| 端点 | 典型响应时间 |
|------|-------------|
| /api/health | < 5ms |
| /api/summary | < 10ms |
| /api/cpu | < 15ms |
| /api/memory | < 20ms |
| /api/storage | < 50ms |
| /api/network | < 5ms |
| /api/gpu (rocm-smi) | 50-150ms |
| /api/gpu (sysfs) | < 20ms |
| /api/all | 100-300ms |

### 9.3 优化措施

1. **按需请求**：每个标签页只请求需要的 API
2. **Summary 轻量 GPU 检测**：状态栏的 `/api/summary` 只扫描 sysfs，不调用 rocm-smi
3. **GPU 拓扑缓存**：5 分钟 TTL，避免重复扫描
4. **子进程超时**：所有外部命令设置 1-5 秒超时
5. **内存限制**：systemd 配置 `MemoryMax=128M`（User）或 `256M`（Root）
6. **无日志写入**：`access_log=False`，减少 I/O
7. **实际时间差计算**：网络/磁盘速率使用实际时间差，非固定值

---

## 10. 项目结构

```
monitor/
├── server.py                 # 主程序 (~1600 行)
│   ├── 配置区 (环境变量支持)
│   ├── 工具函数
│   ├── CPU 数据采集 (+缓存信息)
│   ├── GPU 拓扑检测 + 缓存
│   ├── GPU 数据采集 (三级降级)
│   ├── Memory 数据采集
│   ├── Storage 数据采集 (+I/O 速率)
│   ├── Network 数据采集 (+实际时间差)
│   ├── Temperature 数据采集
│   ├── Processes 数据采集
│   ├── System Logs 数据采集
│   ├── Health 健康检查
│   ├── FastAPI 应用 + 全局异常处理
│   ├── API 端点 (15 个)
│   ├── Dashboard HTML/CSS/JS
│   └── 启动入口
├── start.sh                  # 启动脚本
├── ai-monitor.service        # systemd user 服务文件
├── ai-monitor-root.service   # systemd root 服务文件
├── README.md                 # 简要说明
└── DOCUMENTATION.md          # 完整文档（本文件）
```

---

## 11. 配置说明

### 11.1 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_MONITOR_PORT` | 9527 | 监听端口 |
| `AI_MONITOR_HOST` | 0.0.0.0 | 监听地址 |
| `AI_MONITOR_DEBUG` | 0 | 调试模式（1/true 开启） |
| `AI_MONITOR_ROCM_SMI` | rocm-smi | ROCm SMI 命令路径 |
| `AI_MONITOR_INTEL_GPU_TOP` | intel_gpu_top | Intel GPU Top 命令路径 |
| `AI_MONITOR_SMARTCTL` | smartctl | smartctl 命令路径 |
| `AI_MONITOR_JOURNALCTL` | journalctl | journalctl 命令路径 |

### 11.2 systemd 配置

**User 服务：**
```ini
MemoryMax=128M      # 内存上限
Restart=on-failure  # 失败自动重启
RestartSec=5        # 重启间隔
LimitNOFILE=1024    # 文件描述符限制
```

**Root 服务：**
```ini
User=root           # 以 root 运行
MemoryMax=256M      # 更大内存上限（smartctl 需要）
LimitNOFILE=2048
```

### 11.3 外部工具依赖

| 工具 | 必需 | 用途 |
|------|------|------|
| rocm-smi | 否 | AMD GPU 详细信息 |
| intel_gpu_top | 否 | Intel GPU 详细信息 |
| smartctl | 否 | SATA SMART 信息（需要 root） |
| journalctl | 否 | 系统日志（需要 root） |

所有外部工具均为可选，缺失时自动降级到 sysfs。

---

## 12. 故障排查

### 12.1 服务无法启动

```bash
# User 服务
journalctl --user -u ai-monitor -f

# Root 服务
sudo journalctl -u ai-monitor-root -f

# 手动运行查看错误
cd /home/xchgeaxeax/.hermes/workspace/monitor
python3 server.py
```

### 12.2 GPU 信息不显示

```bash
# 检查 rocm-smi
rocm-smi

# 检查 sysfs
ls /sys/class/drm/card*/device/

# 检查权限
sudo usermod -aG video $USER
```

### 12.3 SMART 信息不完整

SMART 完整信息需要 root 权限：

```bash
# 以 root 运行 smartctl
sudo smartctl -A -j /dev/nvme0n1

# 或使用 root 服务
sudo systemctl enable --now ai-monitor-root
```

### 12.4 端口被占用

```bash
# 查看占用端口的进程
sudo lsof -i :9527

# 修改环境变量
export AI_MONITOR_PORT=9528
```

### 12.5 健康检查 Warning

```bash
# 查看详细健康状态
curl http://localhost:9527/api/health | python3 -m json.tool

# 常见原因：
# - disk_ok: false → 磁盘使用率 > 95%
# - memory_ok: false → 内存使用率 > 95%
# - nvme_smart_ok: false → NVMe SMART 警告
# - temps_ok: false → 温度超过临界值 90%
```

---

## 13. 扩展方向

### 13.1 可能的功能扩展

- [ ] 告警通知（温度/使用率阈值触发 Webhook）
- [ ] 历史数据存储（SQLite/InfluxDB）
- [ ] 多服务器聚合视图
- [ ] WebSocket 实时推送（替代轮询）
- [ ] 移动端适配优化
- [ ] 自定义刷新间隔
- [ ] GPU 功耗曲线
- [ ] 系统事件日志
- [ ] 认证机制（Basic Auth / JWT）

### 13.2 架构改进方向

- [ ] 分离前后端（独立前端项目）
- [ ] 插件化数据采集（支持自定义监控项）
- [ ] Docker 镜像打包
- [ ] 多语言支持

---

## 附录

### A. 快速参考

```bash
# User 服务
systemctl --user start ai-monitor
systemctl --user status ai-monitor
journalctl --user -u ai-monitor -f

# Root 服务
sudo systemctl start ai-monitor-root
sudo systemctl status ai-monitor-root
sudo journalctl -u ai-monitor-root -f

# 健康检查
curl http://localhost:9527/api/health

# 完整快照
curl http://localhost:9527/api/all | python3 -m json.tool
```

### B. 版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| v2.0 | 2026-07-24 | 按需刷新架构，单文件设计 |
| v3.0 | 2026-08-04 | GPU 曲线、CPU 频率曲线、磁盘 I/O 速率、系统日志、健康检查、环境变量配置、Root 服务支持 |

---

*文档生成时间：2026-08-04*
