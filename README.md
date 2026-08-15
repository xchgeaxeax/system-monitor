# System Performance Monitor v4

轻量级 Linux 系统性能监控面板。后台采样线程保持指标热缓存，API 请求毫秒级返回；页面不可见时零请求。

## 特性

- 🔐 **多用户认证**：首次打开面板创建管理员账号；管理员可创建/删除/重置密码/升降级普通用户；每个用户可管理自己的 API Key
- 🔔 **告警面板**：磁盘/内存/温度/SMART/负载/VRAM 阈值告警，持久化历史；可确认（ack）、可删除（保留日志）、可清空
- 🎨 **黑白主题**：跟随系统 `prefers-color-scheme` 自动切换，也可手动 深色/浅色/自动
- 📈 **图表**：网络吞吐、CPU 频率、磁盘 I/O、GPU 利用率/VRAM 曲线（Canvas 自绘，gap 断线 + hover tooltip）
- 🖥️ 覆盖 CPU / GPU(ROCm+Intel+sysfs) / 内存 / 存储+SMART / 网络 / 温度 / 进程 / 系统日志
- ⚡ 性能：采样线程 1.5s 一次，SMART 60s 缓存，工具探测 5min 缓存，历史按时间窗口裁剪
- 🔧 命令行管理：`monitor-cli.py`（用户、密码、API Key、告警）

## 角色与权限

| 能力 | 管理员 (admin) | 普通用户 (user) |
|------|:---:|:---:|
| 查看所有监控数据 | ✅ | ✅ |
| 管理告警（ack/删除/清空） | ✅ | ✅ |
| 创建/删除/重置密码/升降级用户 | ✅ | ❌ |
| 管理自己的 API Key | ✅ | ✅ |
| 查看/删除所有 API Key | ✅ | 仅自己的 |
| 修改自己的密码 | ✅ | ✅ |

## 快速部署

```bash
# Root 模式（完整功能：SMART/日志），推荐
sudo bash deploy.sh --root

# User 模式（基本功能）
bash deploy.sh --user

# 自定义端口
bash deploy.sh --root --port 8080
```

首次访问面板会显示**创建管理员账号**界面。

## 命令行管理（monitor-cli.py）

```bash
# 查看状态（用户/Key/会话）
python3 monitor-cli.py status

# 用户管理（需要 root）
python3 monitor-cli.py user list
python3 monitor-cli.py user create <user> <password> [admin|user]
python3 monitor-cli.py user delete <user>
python3 monitor-cli.py user reset-password <user> [new-password]
python3 monitor-cli.py user role <user> <admin|user>

# 重置第一个管理员密码（需要 root，兼容旧命令）
sudo python3 monitor-cli.py reset-password [new-password]

# API Key 管理
python3 monitor-cli.py key list
python3 monitor-cli.py key create <name> [owner]
python3 monitor-cli.py key delete <name>

# 告警
python3 monitor-cli.py alerts                        # 查看
python3 monitor-cli.py alerts add "维护中" warning    # 手动添加（固定，不自动恢复）
python3 monitor-cli.py alerts ack <rule_id>          # 确认
python3 monitor-cli.py alerts delete <rule_id>       # 删除
python3 monitor-cli.py alerts clear                  # 清空已解决历史
```

> 注意：CLI 与服务器必须指向同一数据目录（默认 `./data/`）。如果服务器设置了
> `AI_MONITOR_DATA_DIR`，CLI 也要 `export AI_MONITOR_DATA_DIR=...`。

## API Key 使用

```bash
curl -H "Authorization: Bearer ***" https://monitor.example.com/api/summary
curl -H "Authorization: Bearer ***" https://monitor.example.com/api/gpu
```

Web 登录方式：`Authorization: Bearer <session token>`（登录后浏览器自动携带）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| AI_MONITOR_PORT | 9527 | 监听端口 |
| AI_MONITOR_HOST | 0.0.0.0 | 监听地址（Caddy 反代时建议 127.0.0.1） |
| AI_MONITOR_DATA_DIR | ./data | 认证/告警数据目录 |
| AI_MONITOR_DEBUG | 0 | 调试模式（开启 /api/all） |
| AI_MONITOR_SAMPLE_INTERVAL | 1.5 | 采样间隔（秒） |
| AI_MONITOR_HISTORY_WINDOW | 300 | 历史曲线保留时长（秒） |
| AI_MONITOR_SMART_TTL | 60 | SMART 采集缓存（秒） |
| AI_MONITOR_SESSION_TTL | 43200 | Web 会话有效期（秒） |
| AI_MONITOR_ROCM_SMI | rocm-smi | ROCm SMI 路径 |
| AI_MONITOR_INTEL_GPU_TOP | intel_gpu_top | Intel GPU Top 路径 |
| AI_MONITOR_SMARTCTL | smartctl | smartctl 路径 |
| AI_MONITOR_NVME | nvme | nvme CLI 路径 |
| AI_MONITOR_JOURNALCTL | journalctl | journalctl 路径 |

## API 端点

| 端点 | 说明 | 认证 |
|------|------|------|
| / | Dashboard | 需要 |
| /api/health | 健康检查（公开，用于探活） | 否 |
| /api/auth/status | 是否已配置认证 | 否 |
| /api/auth/setup | 首次创建管理员 | 否 |
| /api/auth/login / logout | 登录/登出 | 登录需否 |
| /api/summary / quick-stats | 概览 | 需要 |
| /api/cpu / gpu / memory / storage / network / temps | 详情 | 需要 |
| /api/net-history / gpu-history / cpu-freq-history / disk-io-history | 曲线数据 | 需要 |
| /api/processes?sort_by=cpu&search= | 进程 | 需要 |
| /api/logs?lines=&unit=&search=&level=&noisy= | 系统日志 | 需要 |
| /api/alerts | 告警快照 | 需要 |
| /api/alerts/ack?rule_id= (POST) | 确认告警 | 需要 |
| /api/alerts/active?rule_id= (DELETE) | 删除告警 | 需要 |
| /api/alerts/history/{i} (DELETE) | 删除历史条目 | 需要 |
| /api/alerts/clear-resolved (POST) | 清空已解决 | 需要 |
| /api/users (GET) | 列出所有用户 | 仅管理员 |
| /api/users (POST) | 创建用户 | 仅管理员 |
| /api/users/{user} (DELETE) | 删除用户 | 仅管理员 |
| /api/users/{user}/password (POST) | 重置用户密码 | 仅管理员 |
| /api/users/{user}/role (POST) | 升降级用户 | 仅管理员 |
| /api/auth/keys (GET/POST/DELETE) | API Key 管理 | 需要（普通用户仅自己的） |
| /api/auth/password (POST) | 修改自己的密码 | 需要 |
| /api/docs | API 文档 | 需要 |

## 告警规则

| 规则 | 条件 | 级别 |
|------|------|------|
| 磁盘占用 | ≥90%（≥95% 为 danger） | warning/danger |
| 内存占用 | ≥90%（≥95% 为 danger） | warning/danger |
| Swap | ≥80% | warning |
| 负载 | 1min load ≥ 2× 核心数 | warning |
| 温度 | > 90% 临界温度 | danger |
| SMART | critical_warning 或 health=CHECK | danger |
| NVMe 寿命 | percentage_used ≥ 90% | warning |
| GPU VRAM | ≥95% | warning |

告警自动触发/自动恢复（数据源缺失时不会误报恢复）。手动添加的告警（`alerts add`）
为固定告警，只能手动删除。

## 系统要求

- Linux（Ubuntu/Debian/CentOS/Fedora/openSUSE）
- Python 3.8+，fastapi / uvicorn / psutil
- systemd

## 可选工具

| 工具 | 用途 | 安装 |
|------|------|------|
| intel-gpu-tools | Intel GPU 监控 | `apt install intel-gpu-tools`（需 `setcap cap_perfmon+ep`） |
| rocm-smi | AMD GPU 监控 | `apt install rocm-smi-lib` |
| smartmontools | SATA SMART | `apt install smartmontools` |
| nvme-cli | NVMe SMART | `apt install nvme-cli` |

## 卸载

```bash
sudo systemctl stop ai-monitor-root && sudo systemctl disable ai-monitor-root
sudo rm /etc/systemd/system/ai-monitor-root.service
sudo systemctl daemon-reload
rm -rf /opt/system-monitor   # 或你的部署目录（含 data/ 认证数据）
```
