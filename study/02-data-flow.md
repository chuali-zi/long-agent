# 02 · 数据流：一次 ask() 的完整时序

> 这一份是理解整个项目的"钥匙"。看明白这条时序后，每个模块在做什么就一清二楚了。

---

## 1. 场景设定

用户在 CLI 输入：

```
kyagent ask "查 80 端口" --json
```

后端默认是 `deepseek_httpx`；如果没有 `DEEPSEEK_API_KEY`，会按配置降级到 `mock`（规则路由，离线可跑）。我们逐步看每一行代码做什么。

---

## 2. 全景时序图

```
用户 ─┐
      │ "查 80 端口"
      ▼
┌─────────────────────────────────────────────────────────────┐
│ kyagent.cli.ask()  (cli.py:128)                              │
│ ─ load_config(None) → Config 实例                            │
│ ─ Agent.from_config(cfg, confirm=lambda*a:False)             │
│ ─ result = agent.ask("查 80 端口", user="oneshot")           │
└────────────────────┬────────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Agent.ask()  (agent/core.py:114)                             │
│ ─ trace = Trace(user="oneshot")                              │
│ ─ self.audit.open(trace)        【event #0：trace 元数据】   │
│ ─ self.audit.event(USER_INPUT, {"text":"查 80 端口"})        │
│ ─ self.messages.append({"role":"user", "content":"..."})    │
│                                                              │
│ ┌─ while iterations < max_iterations:                        │
│ │   iteration 1                                              │
│ │   ▼                                                        │
│ │ ──────── 调 LLM ────────                                   │
│ │ assistant = self.llm.chat(SYSTEM_PROMPT, messages, tools)  │
│ │                                                            │
│ │   MockBackend.chat()  (agent/llm.py:364)                   │
│ │   ─ 最近一条 user 不是 tool_result → 走"路由阶段"          │
│ │   ─ "80 端口" 匹配 _route() 的 "端口" 关键字              │
│ │   ─ _PORT_RE 抽出 80                                       │
│ │   ─ 返回 AssistantMessage(                                 │
│ │       blocks=[TextBlock("我先通过工具 lsof_port..."),     │
│ │              ToolUseBlock(name="lsof_port",                │
│ │                           input={"port":80})],             │
│ │       stop_reason="tool_use")                              │
│ │                                                            │
│ │ audit.event(LLM_THOUGHT, {"text":..., "tool_calls":[...]})│
│ │                                                            │
│ │ tool_uses = [ToolUseBlock(lsof_port, {"port":80})]         │
│ │                                                            │
│ │ ──────── 决定串行/并行 ────────                            │
│ │ run_parallel = (                                           │
│ │   sys.platform != "win32"                                  │
│ │   and len(tool_uses) >= 2          ← 这里 = 1，False     │
│ │   and ...                                                  │
│ │ )                                                          │
│ │ → 走串行分支                                               │
│ │                                                            │
│ │ self.messages.append({"role":"assistant", "content":...}) │
│ │                                                            │
│ │ ──────── 处理工具调用 ────────                             │
│ │ for tu in tool_uses:                                       │
│ │   result_block = self._handle_tool_use(trace, tu, notes) │
│ │   见下方放大                                               │
│ │                                                            │
│ │ self.messages.append({"role":"user",                      │
│ │                       "content":[tool_result_block]})     │
│ │   iteration 2                                              │
│ │   ▼                                                        │
│ │ ──────── 再调 LLM（让它总结） ────────                     │
│ │ assistant = self.llm.chat(...)                             │
│ │   MockBackend.chat() 看到最近一条 user 含 tool_result：    │
│ │   返回 _summarize(...) → "下面是工具返回的关键内容..."    │
│ │   stop_reason = "end_turn"                                 │
│ │                                                            │
│ │ tool_uses = []  ← 没工具调用了                            │
│ │ final = "\n".join(assistant.texts()).strip()              │
│ │ self.messages.append({"role":"assistant", "content":...}) │
│ │ audit.event(AGENT_REPLY, {"text": final})                  │
│ │ audit.close(trace)                                         │
│ └─ break                                                     │
│                                                              │
│ return AgentRunResult(trace, final_text, ...)                │
└─────────────────────────────────────────────────────────────┘

────────── _handle_tool_use 放大（共享流水线 pipeline.py） ──────────

  tool = self.registry.get("lsof_port")
  ┌─ pipeline.prepare_call(tool, {"port":80}, trace, audit)
  │   ├─ tool.validate({"port":80})            ── JSON Schema 校验
  │   │   → cleaned = {"port": 80}
  │   ├─ tool.build_argv(cleaned)              ── 受控 argv
  │   │   → ["lsof", "-nP", "-i", "TCP:80"]
  │   ├─ audit.event(TOOL_REQUEST,
  │   │              {"tool":"lsof_port",
  │   │               "argv":["lsof","-nP","-i","TCP:80"],
  │   │               "args":{"port":80},
  │   │               "risk":"low",
  │   │               "requires_root": False})
  │   ├─ (read_only + LOW) → audit.event(PERCEPTION, {...})
  │   │     ── MCP / Agent 两条通道现在都会落这条
  │   └─ return PreparedCall(tool, cleaned, argv)
  │
  ├─ pipeline.check_safety(prep, trace, audit, guardrail)
  │   ─ guardrail.check_argv(argv, declared_risk=LOW)
  │     ─ engine.scan_cmdline("lsof -nP -i TCP:80")
  │       ─ 命中 0 条规则
  │       ─ risk = LOW
  │     ─ declared_risk = LOW 不抬升
  │     ─ policy.decide(LOW) → Decision.ALLOW
  │   ─ audit.event(SAFETY_CHECK, verdict.to_dict())
  │   → Verdict(decision=ALLOW, risk=LOW, hits=[])
  │
  ├─ decision != DENY/CONFIRM → 直接落地
  │
  ├─ pipeline.execute_and_format(prep, trace, audit, executor)
  │   ├─ audit.event(EXECUTION, {"argv":[...], "requires_root":False})
  │   ├─ exec_result = executor.run(argv, requires_root=False)
  │   │   ┌── ExecutionProxy.run()
  │   │   │   sys.platform == "win32" → _run_windows_mock()
  │   │   │   返回 ExecutionResult(returncode=0, stdout="[mock]...",
  │   │   │                       skipped_reason="windows_mock")
  │   │   │
  │   │   │   (在 Linux 上则进入 _run_posix：
  │   │   │    1. _wrap_privilege 决定是否 sudo 包裹
  │   │   │    2. _resolve_command 走 PATH 白名单
  │   │   │    3. build_clean_env 干净 env
  │   │   │    4. Popen with preexec_fn(setpgid, RLIMIT_*)
  │   │   │    5. communicate(timeout=30s)
  │   │   │    6. 输出截断 + 解码 + 返回)
  │   │   └──
  │   ├─ audit.event(EXECUTION_RESULT, exec_result.to_dict())
  │   ├─ formatted = tool.format_result(exec_result)   ── 默认实现
  │   │   → ToolResult(ok=True, content="[mock][win32] would execute: lsof ...")
  │   └─ content = formatted.content (失败时拼 stderr)
  │              ，统一截到 OUTPUT_CAP = 6000
  │
  └─ return ToolResultBlock(tool_use_id=tu.id,
                            is_error=not formatted.ok,
                            content=content)
```

---

## 3. Trace 时间轴（这一次 ask 产生的 7 段事件）

```
seq=1  USER_INPUT       {"text": "查 80 端口"}                        ← Agent.ask 起手
seq=2  LLM_THOUGHT      {"stop_reason":"tool_use",
                         "text":"我先通过工具 lsof_port 感知...",
                         "tool_calls":["lsof_port"]}
seq=3  TOOL_REQUEST     {"tool":"lsof_port",
                         "argv":["lsof","-nP","-i","TCP:80"],
                         "args":{"port":80},
                         "risk":"low",
                         "requires_root": false}
seq=4  SAFETY_CHECK     {"decision":"allow", "risk":"low",
                         "cmdline":"lsof -nP -i TCP:80",
                         "hits":[], "rationale":["未命中任何危险模式",
                                                "策略映射: low -> allow"]}
seq=5  EXECUTION        {"argv":[...], "requires_root": false}
seq=6  EXECUTION_RESULT {"returncode":0, "stdout":"[mock]...",
                         "stderr":"", "duration":0.0,
                         "skipped_reason":"windows_mock"}
seq=7  AGENT_REPLY      {"text":"下面是工具返回的关键内容..."}
```

每条事件都同步 append 到：
- `Trace.events: list[TraceEvent]`（in-memory）
- SQLite `events` 表
- `var/audit.jsonl`（每行一个事件）

**全部在同一把 `trace._lock`（RLock）保护下完成**，所以 seq 顺序 = 落盘顺序。

---

## 4. 危险命令场景：用户问 "rm -rf /etc"

mock 后端的 `_route()` 没有匹配规则，会走 `_fallback_reply` 直接给一段提示文本，根本不会发起 tool_use。所以这条命令不会经过 executor。

那如果是 Anthropic / OpenAI 后端被诱导发起一个 tool_use 试图删除 /etc？两种情形：

### 4.1 LLM 通过现有工具的参数注入

LLM 调 `fs_ls` 试图传 `path = "/etc/shadow"`：
- `Tool.validate({"path":"/etc/shadow"})` → 通过（type=string）
- `Tool.build_argv(cleaned)`：`filesystem.py:14 _safe_path()`
  - `"/etc/shadow" in _PROTECTED_READ` → 抛 `ToolError`
- Agent 捕获 `ToolError`，写 EventKind.ERROR + 返回 "工具参数非法"
- **executor 根本没启动**

LLM 调 `svc_restart` 传 `unit = "sshd; rm -rf /"`：
- `Tool.build_argv` → `service.py:22 _validate_unit()` 检测分号 → ToolError
- 同上，executor 没启动

### 4.2 LLM 给出一个语义合法但策略禁止的 argv

LLM 调 `svc_restart` 传 `unit = "nginx"`：
- `Tool.validate` + `Tool.build_argv` 通过 → `argv = ["systemctl", "restart", "nginx"]`
- `audit.event(TOOL_REQUEST)`
- `Guardrail.check_argv(argv, declared_risk=HIGH)`
  - `engine.scan_cmdline("systemctl restart nginx")`：命中 0 条规则
  - risk = LOW（来自规则）
  - declared_risk = HIGH（来自 `SvcRestartTool.risk_level`）
  - declared_risk.order > risk.order → risk = HIGH
  - `policy.decide(HIGH)` → `Decision.CONFIRM`（按 default policy）
- `audit.event(SAFETY_CHECK, {decision:"confirm", risk:"high", ...})`
- 主循环看到 CONFIRM：
  - 检查当前线程是不是 main_thread（C2 修复）
  - 是 → 调 `self.confirm(confirm_adapter.for_tool_call(verdict, tu.name, prep.argv))`
    （`ConfirmFn` 只收一个 `ConfirmRequest`，Verdict→UI 契约的翻译由 adapter 做）
  - 在 `kyagent ask` 模式下 confirm 是 `lambda *a, **k: False` → 返回 False
  - 写 `EventKind.ERROR{"reason":"user_denied_confirm"}`
  - 返回 ToolResultBlock(is_error=True, content="[denied] 用户拒绝执行")
- LLM 拿到 [denied] 结果，会改话术（"你拒绝了我，那我只能告诉你..."）
- iteration 继续 / 终止

### 4.3 用户直接喊 "rm -rf /"

这种情形 LLM 不会主动发起危险 tool_use，但如果手贱用户在自定义后端里硬塞一个 raw shell tool（kyagent 默认没有），那么：
- `Guardrail.check_argv(["rm","-rf","/"])` 命中 `dangerous-rm-pattern` (CRITICAL) + `rm-recursive-system`（如果 target_in 命中）
- risk = CRITICAL → policy.decide → `Decision.DENY`
- 主循环写 `EventKind.SAFETY_CHECK` + 返回 ToolResultBlock(is_error=True, content="[denied]...")
- **executor 没启动**

---

## 5. MCP 通道：被外部 LLM host 当工具集挂载

```
Claude Desktop                      kyagent mcp serve
─────────────────                  ─────────────────────────────
  {"jsonrpc":"2.0",
   "id":1, "method":"initialize"} ──▶ McpServer.serve()
                                       _dispatch → _initialize
                                ◀──── {"jsonrpc":"2.0","id":1,
                                       "result":{"protocolVersion":...,
                                                 "capabilities":{...}}}

  {"id":2, "method":"tools/list"} ──▶ _dispatch → registry.to_mcp_list()
                                ◀──── {"id":2,"result":{"tools":[...]}}

  {"id":3, "method":"tools/call",
   "params":{"name":"lsof_port",
             "arguments":{"port":80}}} ─▶ McpServer._call_tool()
                                            trace = Trace(user="mcp-client")
                                            audit.open(trace)
                                            ┌─ 与 Agent 共享同一流水线（pipeline.py）：
                                            │   pipeline.prepare_call(...)
                                            │     → TOOL_REQUEST + 可选 PERCEPTION
                                            │   pipeline.check_safety(...)
                                            │     → SAFETY_CHECK
                                            │   if DENY → 返回 isError
                                            │   if CONFIRM → MCP 通道默认 deny
                                            │   pipeline.execute_and_format(...)
                                            │     → EXECUTION / EXECUTION_RESULT
                                            │     + stderr 拼接 + OUTPUT_CAP 6KB
                                            │   audit.event(AGENT_REPLY)
                                            │   audit.close(trace)
                                            └─
                                ◀──── {"id":3,"result":{"content":[{...}],
                                                        "isError":false}}
```

**关键差异**：MCP 通道没有 LLM 推理（推理在 Claude Desktop 那边），所以没有 LLM_THOUGHT 事件，也没有 USER_INPUT（请求本身就是 tool_call）；`_call_tool` 直接从 `pipeline.prepare_call` 开始落 TOOL_REQUEST（read_only+LOW 时同样会落 PERCEPTION，与 Agent 通道一致）。CONFIRM 在 MCP 通道下没有交互通道，按 deny 处理并返回 `isError=True`。

---

## 6. 数据格式对照表

### 6.1 自然语言 → LLM 输入
```python
messages = [
    {"role": "user", "content": "查 80 端口"},
]
tools = [
    {"name": "lsof_port",
     "description": "查看占用某 TCP/UDP 端口的进程...",
     "input_schema": {"type":"object", "required":["port"], "properties":{...}}},
    # ... 共 18 个工具
]
```

### 6.2 LLM 输出（Anthropic 风格 → 项目内部统一）
```python
AssistantMessage(
    blocks=[
        TextBlock(text="我先通过工具 lsof_port 感知一下系统再回答。"),
        ToolUseBlock(id="toolu_abc", name="lsof_port", input={"port": 80}),
    ],
    stop_reason="tool_use",
)
```

OpenAI 后端内部把 `tool_calls + function.arguments(json str)` 翻译成上面的形态（`OpenAIBackend._from_openai_choice` 在 llm.py:320）。

### 6.3 Tool 返回给 LLM 的 tool_result
```python
self.messages.append({
    "role": "user",
    "content": [
        {"type": "tool_result",
         "tool_use_id": "toolu_abc",
         "content": "USER  PID  ... \nnginx 1234 ...",
         "is_error": False},
    ],
})
```

OpenAI 后端把这个翻译成 `{"role":"tool", "tool_call_id": "toolu_abc", "content": "..."}`（llm.py:253-258）。

### 6.4 ExecutionResult 字段（executor/proxy.py:24）
```python
ExecutionResult(
    argv=["lsof", "-nP", "-i", "TCP:80"],
    returncode=0,
    stdout="...",
    stderr="",
    truncated=False,
    duration=0.123,
    timed_out=False,
    skipped_reason=None,    # 或 "windows_mock" / "not_in_path" / ...
    sudo_used=False,
    run_as="kyagent",
)
```

### 6.5 Verdict（safety/guardrail.py:18）
```python
Verdict(
    cmdline="lsof -nP -i TCP:80",
    decision=Decision.ALLOW,
    risk=RiskLevel.LOW,
    hits=[],                # List[Hit]
    rationale=["未命中任何危险模式", "策略映射: low -> allow"],
)
```

### 6.6 TraceEvent（audit/trace.py:25）
```python
TraceEvent(
    seq=4,
    kind=EventKind.SAFETY_CHECK,
    ts=1747500000.123,
    payload={"decision":"allow", "risk":"low", "hits":[], ...},
)
```

---

## 7. 一句话总结这一份

**用户的话经过 LLM 变成 `tool_use`，工具的 `validate + build_argv` 把它变成受控的 `argv`，护栏出 verdict，执行代理落地，结果回灌给 LLM 总结。中间每一步都同步入审计链。**

---

## 8. 下一步

继续 → [03-agent-core.md](./03-agent-core.md) 把 `Agent.ask()` 的每一行代码、每一个分支都讲清楚。
