# kyagent 源码精读 · 从这里开始

> 你正在读的是 D:\race\long 仓库（A2 赛题：面向麒麟操作系统的安全智能运维 Agent）的逐文档源码精读。
> 目标：读完之后，你能闭着眼睛画出每条数据流、解释任何一行代码为什么这样写、以及它在比赛评分维度上对应哪一条要求。

---

## 0. 仓库一句话定位

**kyagent = "自然语言 ↔ 麒麟 OS 实时状态"的可控闭环。**
用户用中文说一句话，Agent 调系统工具拿到真状态，安全护栏二次过滤，最小权限账户落地执行，全过程留审计。

对应赛题 5 条功能要求 → 5 个模块：
| 赛题要求 | 模块 | 单点入口 |
|---|---|---|
| ① OS 环境深度感知 | `kyagent/mcp/tools/` | `default_registry()` |
| ② MCP 运维插件化 | `kyagent/mcp/` | `Tool` + `ToolRegistry` + `McpServer` |
| ③ 安全意图校验器 | `kyagent/safety/` | `Guardrail.check_argv()` |
| ④ 最小权限代理执行 | `kyagent/executor/` | `ExecutionProxy.run()` |
| ⑤ 推理链路溯源 | `kyagent/audit/` | `AuditLogger.event()` |

把这五个模块用 `kyagent/agent/core.py` 的主循环串起来，就是 `Agent.ask()`。

---

## 1. 推荐阅读路线

**新手快速过一遍（约 1 小时）**：
1. 这份 `00-START-HERE.md`
2. `01-architecture-overview.md`  — 看顶层架构图就好
3. `02-data-flow.md`              — 一次 `ask()` 的完整时序，是理解全局的钥匙
4. `03-agent-core.md`             — 主循环细节

**全面理解（约半天）**：按 00 → 13 顺序通读，每读完一份，对应在源码里跳一遍，把 file:line 锚点都点开看一次。

**专项精读**：
- 想理解安全护栏怎么拦危险命令 → `05-safety-layer.md` + `11-security-model.md`
- 想理解为什么这套架构能并发安全 → `12-concurrency.md`
- 想理解评测 baseline 怎么测 → `13-testing-bench.md`

---

## 2. 完整文档清单

| 编号 | 标题 | 主要回答的问题 |
|---|---|---|
| 00 | START-HERE | 路线图（你在这里） |
| 01 | architecture-overview | 这些模块怎么拼在一起？谁依赖谁？ |
| 02 | data-flow | 一句"查 80 端口"从输入到回复经过多少层？ |
| 03 | agent-core | Agent.ask() 主循环的每个分支都干嘛？ |
| 04 | llm-backends | Mock / SDK / `*_httpx` 后端怎么统一？ |
| 05 | safety-layer | 规则引擎 + 策略映射 + 可选 LLM 复审是怎么组装的？ |
| 06 | executor-sandbox | sudo 怎么包？preexec_fn 设了哪些 rlimit？env 怎么洗？ |
| 07 | mcp-tools | 六大工具家族每一个 argv 长什么样？ |
| 08 | audit-chain | Trace / TraceEvent / SQLite / JSONL 四件套如何协作？ |
| 09 | config | YAML + 环境变量 + Pydantic 怎么组合？ |
| 10 | cli-entry | typer 子命令树 + Rich 渲染怎么工作？ |
| 11 | security-model | 全部防御层叠在一起的威胁模型 |
| 12 | concurrency | 并发模型 + 三处 review 修复（H1/C2/C1） |
| 13 | testing-bench | 11 个测试文件 + 冻结基线测什么？ |

---

## 3. 看代码时怎么定位

仓库根目录 `D:\race\long`，所有源码在 `kyagent/`，配置在 `configs/`，测试在 `tests/`。

```
D:\race\long\
├── kyagent/                # 主包
│   ├── __init__.py         # 暴露 __version__
│   ├── __main__.py         # python -m kyagent → cli.app()
│   ├── cli.py              # typer + rich 的 CLI 子命令树
│   ├── config.py           # Pydantic 配置 schema + YAML 加载器
│   ├── agent/
│   │   ├── core.py         # ★ Agent.ask() 主循环（最重要）
│   │   ├── llm.py          # ★ LLM 后端抽象：Mock/SDK/*_httpx
│   │   └── prompt.py       # SYSTEM_PROMPT 文本
│   ├── safety/
│   │   ├── patterns.py     # RiskLevel + Rule dataclass + load_rules()
│   │   ├── rules.py        # ★ RuleEngine 匹配引擎 + 进程级 LRU
│   │   ├── policy.py       # Decision enum + Policy.decide()
│   │   └── guardrail.py    # ★ 多级护栏主流水线
│   ├── executor/
│   │   ├── sandbox.py      # SandboxConfig + preexec_fn + 干净 env
│   │   └── proxy.py        # ★ ExecutionProxy.run() POSIX 落地
│   ├── audit/
│   │   ├── trace.py        # Trace + TraceEvent + EventKind + RLock
│   │   ├── logger.py       # ★ AuditLogger.event() 双通道写入
│   │   └── store.py        # SQLite 持久化（WAL）
│   └── mcp/
│       ├── server.py       # MCP stdio JSON-RPC 服务器
│       └── tools/
│           ├── base.py     # ★ Tool 抽象基类 + ToolRegistry
│           ├── process.py  # process_list / lsof_port / lsof_pid
│           ├── network.py  # net_listen / net_connections / net_ping
│           ├── logs.py     # log_journal / log_dmesg
│           ├── service.py  # svc_status / svc_list / svc_restart / svc_reload
│           ├── filesystem.py  # fs_df / fs_du / fs_ls / fs_find
│           └── package.py  # pkg_info / pkg_installed
├── configs/
│   ├── default.yaml        # 默认配置（deepseek_httpx；缺 key 降级 mock）
│   ├── openai.yaml         # OpenAI 协议兼容（架构示例；当前部署仅推 DeepSeek）
│   ├── safety-rules.yaml   # 27 条危险命令规则
│   └── sudoers.kyagent     # /etc/sudoers.d 白名单（NOPASSWD + 显式黑名单）
├── tests/                  # 244 个测试用例，Windows 下有 2 个 POSIX-only skip
├── benchmarks/
│   ├── bench_ask.py        # 冻结的性能基线脚本
│   └── baseline.json       # 冻结的基线数字
├── README.md / docs/kyagent/README.md
└── pyproject.toml
```

带 ★ 标记的是关键文件，研究路径时优先点开。

---

## 4. 在仓库里跑代码（快速冒烟）

```bash
# 装包
python -m pip install -e .

# 跑全部测试（当前收集 244 个；POSIX-only 用例在 Windows 上 2 个 skip）
python -m pytest -q

# 默认 deepseek_httpx；不设置 DEEPSEEK_API_KEY 时会直接报错
python -m kyagent ask "查下 CPU 占用最高的进程" --json

# 列工具 + 风险等级
python -m kyagent tools list

# 让护栏单独评估一条命令，不真正执行
python -m kyagent safety test "rm -rf /etc"

# 查看上一轮 trace
python -m kyagent audit list -n 5
python -m kyagent audit show <trace-id>

# 作为 MCP server 挂到 Claude Desktop
python -m kyagent mcp serve
```

---

## 5. 接下来读哪一份？

**最关键的一份是 `02-data-flow.md`** —— 它把所有模块用一条时序串起来，看完之后你心里就有"代码运行起来到底在干嘛"的清晰画面，再去读各个模块就不会迷路。

进入 → [01-architecture-overview.md](./01-architecture-overview.md)
