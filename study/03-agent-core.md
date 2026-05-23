# 03 · Agent 主循环深读

> 这一份对应文件：`kyagent/agent/core.py`
> 配套阅读：`kyagent/agent/prompt.py`（SYSTEM_PROMPT）、`kyagent/agent/confirm_adapter.py`、`kyagent/runtime.py`

---

## 1. 文件结构

```
agent/core.py
├── _auto_deny 别名           # 指向 kyagent.confirm.auto_deny（向后兼容）
├── AgentRunResult dataclass   # ask() 返回值
├── Agent class
│   ├── __init__               # 注入依赖（含 intent_guard）
│   ├── _ensure_pool / shutdown  # ThreadPool 生命周期
│   ├── from_config            # 工厂：调 build_runtime + 装通道特定层
│   ├── ask                    # ★ 主入口（含意图层）
│   ├── _executor_supports_parallel_tools  # 并行 gate-1
│   ├── _is_parallel_safe      # 并行 gate-2
│   ├── _handle_tool_use       # 单个工具调用（走 pipeline）
│   └── _blocks_to_dict        # 序列化 LLM 输出
└── build_agent()              # load_config + Agent.from_config
```

`ConfirmFn` 类型与 `ConfirmRequest` 数据类不住在 `agent/core.py`，它们在顶层
`kyagent/confirm.py`——这是跨层契约（CLI、Agent、未来 web UI 都要用，但谁都不
应该反过来依赖 safety 包）。Verdict → ConfirmRequest 的翻译由
`kyagent/agent/confirm_adapter.py` 负责（`for_tool_call` / `for_intent`）。

---

## 2. ConfirmFn 与依赖注入

```python
# kyagent/confirm.py
@dataclass(frozen=True)
class ConfirmRequest:
    title: str                          # "tool rm" / "自然语言意图审查"
    risk: str                           # "low" / "medium" / "high" / "critical"
    summary_lines: list[str] = ...      # 命中规则列表
    body: str | None = None             # argv / rationale

ConfirmFn = Callable[[ConfirmRequest], bool]

def auto_deny(_req: ConfirmRequest) -> bool:
    return False
```

Agent 不知道 CLI 长什么样，也不知道 stdin 是什么。它只接收一个回调：给一份
`ConfirmRequest`（已经 stringify 好），返回是否放行。具体怎么问用户（弹 Rich
Panel？发邮件？）由调用方自定义。Verdict → ConfirmRequest 的翻译由
`confirm_adapter.for_tool_call(verdict, tool_name, argv)` 与
`confirm_adapter.for_intent(verdict)` 做——这一层是 safety domain 和 UI 契约
**唯一的接触面**，避免 safety 反过来依赖 UI。

CLI 端的实现（`cli._cli_confirm`）：用 Rich Panel 把 `ConfirmRequest` 渲染出来，
Prompt.ask 读 y/n。MCP 端：不存在交互通道，直接当 deny 处理（在
`mcp/server.py:_call_tool` 的 CONFIRM 分支返回 isError）。

这就是经典的 **依赖反转**：核心库（Agent / safety）不依赖具体 IO 形态，由调用
方注入；UI 契约（`ConfirmRequest`）住在顶层，谁都不会被反向拖累。

---

## 3. AgentRunResult

```python
# core.py:42
@dataclass
class AgentRunResult:
    trace: Trace
    final_text: str
    tool_iterations: int = 0
    denied: bool = False
    notes: list[str] = field(default_factory=list)
```

- `trace` — 这一轮的完整审计链。CLI 用它的 `trace_id` 当作返回引用，用户可以用 `kyagent audit show <id>` 再次回看。
- `final_text` — LLM 最终给的纯文本回答。
- `tool_iterations` — 进入 while 主循环的轮次数（计的是与 LLM 的来回，不是工具调用次数）。
- `denied` — 任意一个 tool_result 是 `[denied]` 开头，就置 True。CLI 可以据此提示用户。
- `notes` — 软提示字符串列表（"已拦截 X"、"用户拒绝 Y"），属于辅助说明，不是审计权威源。

---

## 4. Agent.__init__ 与 from_config

```python
def __init__(self, cfg, llm, registry, guardrail, executor, audit,
             confirm=_auto_deny, intent_guard: IntentGuard | None = None):
    self.cfg = cfg
    self.llm = llm
    self.registry = registry
    self.guardrail = guardrail
    self.intent_guard = intent_guard   # 赛题第 3 条：NL 意图层（None 则跳过）
    self.executor = executor
    self.audit = audit
    self.confirm = confirm
    self.messages: list[dict] = []
    self.system_prompt = SYSTEM_PROMPT
    self._tool_pool: ThreadPoolExecutor | None = None
```

所有依赖都是显式参数：
- `cfg` — 配置（让 ask() 知道 max_iterations 等）
- `llm` — 已构造好的 `LlmBackend`
- `registry` — `ToolRegistry`，含工具实例
- `guardrail` — 已构造好的 `Guardrail`（argv 层二次过滤）
- `executor` — `ExecutionProxy`
- `audit` — `AuditLogger`
- `confirm` — `ConfirmFn` 回调（默认 `auto_deny`）
- `intent_guard` — `IntentGuard | None`，意图层一次过滤；None 表示禁用该层

**单元测试友好**：`test_agent_parallel.py` 直接 new 一个 `Agent` 然后替换
`agent.executor = RecordingExecutor(...)` 就能验证调度逻辑。

`from_config` 是工厂方法，把 `Config` 解析成所有依赖。本身已经收敛得很瘦——
通道无关的基础设施（sandbox / executor / guardrail / audit / registry）都由
`kyagent/runtime.py:build_runtime` 统一装配，避免 `McpServer.main` 重复一份：

```python
@classmethod
def from_config(cls, cfg: Config, confirm: ConfirmFn = _auto_deny) -> "Agent":
    # 通道无关基础设施统一从 composition root 装配
    rt = build_runtime(cfg)
    # 通道特定（LLM 后端、NL 意图层）在这里组合
    llm = build_backend(cfg)
    intent_guard = IntentGuard.from_config(cfg) if cfg.safety.intent_check else None
    return cls(cfg, llm, rt.registry, rt.guardrail, rt.executor, rt.audit, confirm,
               intent_guard=intent_guard)
```

`build_runtime` 内部做了：SandboxConfig 装配（含 path_whitelist fallback）、
ExecutionProxy、Guardrail.from_config、AuditStore/AuditLogger（含可选 JSONL）、
default_registry（含 `cfg.mcp.enable_tools` 白名单过滤）。Agent 这边只剩"LLM
后端 + 意图层"两个通道特定品；MCP 服务器入口（`McpServer.main`）同样直接拿
`rt.registry/guardrail/executor/audit`，两条通道行为永远对齐。

注意 `cfg.mcp.enable_tools` 当作白名单：留空时全部启用；非空时过滤 registry。这让运维方能在生产配置里只放白名单工具（例如完全禁掉 `svc_restart` 这类高危）。

---

## 5. ThreadPool 生命周期

```python
# core.py:77
def _ensure_pool(self):
    if self._tool_pool is None:
        self._tool_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="ky-tool"
        )
    return self._tool_pool

def shutdown(self):
    if self._tool_pool is not None:
        self._tool_pool.shutdown(wait=False)
        self._tool_pool = None
```

**懒创建**：只在真正要并行时才 new 一个 ThreadPool。`max_workers=4` 是个保守上限——多了 GIL 抢锁，少了无法吃满 IO。

`shutdown(wait=False)`：调用方主动释放时不等待。这在调用方知道"我不再需要这个 Agent"时才用，否则潜在 worker 还在跑。注意：当前任何 `ask()` 结束都会 `for fut in futures: fut.result()` 等齐了再返回，所以正常路径下不需要 shutdown。

---

## 6. ask() —— 主循环逐行解读

### 6.1 起手三连 + 意图层

```python
def ask(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
    trace = Trace(user=user)
    self.audit.open(trace)
    trace.metadata.update({"backend": self.llm.name})

    self.audit.event(trace, EventKind.USER_INPUT, {"text": user_input})

    # ===== 赛题第 3 条：NL 意图层 + 抗 Prompt Injection =====
    # 这是 LLM 看到 user_input 之前的"一次过滤"。argv 层 Guardrail 是 LLM 输出
    # 之后的"二次过滤"，两者互补缺一不可。
    effective_input = user_input
    if self.intent_guard is not None:
        intent_verdict = self.intent_guard.evaluate(user_input, context={"user": user})
        self.audit.event(trace, EventKind.INTENT_CHECK, intent_verdict.to_dict())
        # DENY → 直接 blocked_at=intent，根本不进 LLM
        # CONFIRM → 调 self.confirm(confirm_adapter.for_intent(verdict))
        # 通过则净化零宽字符后 effective_input 送 LLM
        ...

    self.messages.append({"role": "user", "content": effective_input})
```

- 新建 `Trace`（自动分配 trace_id = `trace-{uuid hex[:12]}`）
- `audit.open()` 在 SQLite 写一条 traces 表记录
- metadata 写后端名（"mock" / "anthropic" / "openai" / "deepseek" / "qwen"），后续 `kyagent audit list` 可以按通道筛选
- 把"用户说的话"写为 USER_INPUT 事件
- **意图层**（赛题第 3 条）：在 user_input 进入 LLM 之前先过 `IntentGuard`，命中
  DENY 直接终止 trace（写 `AGENT_REPLY{blocked_at:"intent"}`）；命中 CONFIRM 通过
  `confirm_adapter.for_intent(verdict)` 翻成 `ConfirmRequest` 交给上层；通过则把
  净化后的 `sanitized_text`（剥零宽字符）送进 LLM，原文保留在 USER_INPUT 事件里
- 加进 `self.messages`，作为后续 LLM 的输入

### 6.2 主循环开始

```python
# core.py:122
notes: list[str] = []
iterations = 0
denied = False

tools_for_llm = self.registry.to_anthropic_tools()

while iterations < self.cfg.agent.max_iterations:
    iterations += 1
```

- `notes` 收集本轮所有的软提示
- `iterations` 计数（默认上限 8，`AgentConfig.max_iterations`）
- `tools_for_llm` 是 Anthropic-style 工具描述（`{name, description, input_schema}` 列表）—— OpenAI 后端内部会再翻译一次

### 6.3 调 LLM

```python
# core.py:131
try:
    assistant = self.llm.chat(self.system_prompt, self.messages, tools_for_llm)
except Exception as e:
    self.audit.event(trace, EventKind.ERROR,
                     {"reason": "llm_error", "detail": str(e)})
    self.audit.close(trace)
    return AgentRunResult(trace=trace, final_text=f"LLM 调用失败：{e}",
                          tool_iterations=iterations, notes=notes)
```

LLM 任何异常都包裹成 ERROR 事件 + 友好提示返回。**不能让 LLM 后端的网络抖动把整条 trace 玩崩**。

### 6.4 记录 LLM 思维链

```python
# core.py:140
self.audit.event(trace, EventKind.LLM_THOUGHT,
                 {"stop_reason": assistant.stop_reason,
                  "text": "\n".join(assistant.texts())[:4000],
                  "tool_calls": [t.name for t in assistant.tool_uses()]})
```

- LLM 思维链（文本 block）落审计，最多截 4000 字
- 同时记录 LLM 提议调用哪些工具（仅名称，不含参数）
- 注：参数后续 TOOL_REQUEST 事件会带

### 6.5 没有工具调用 → 终结

```python
# core.py:147
tool_uses = assistant.tool_uses()
if not tool_uses:
    final = "\n".join(assistant.texts()).strip()
    self.messages.append({"role": "assistant",
                          "content": [{"type": "text", "text": final}]})
    self.audit.event(trace, EventKind.AGENT_REPLY, {"text": final})
    self.audit.close(trace)
    return AgentRunResult(trace=trace, final_text=final,
                          tool_iterations=iterations, denied=denied, notes=notes)
```

LLM 觉得这一轮不需要再调工具了 → 终止。这是 ask() 的正常退出。

### 6.6 把 LLM 这次的输出补进 messages

```python
# core.py:158
self.messages.append({"role": "assistant",
                      "content": self._blocks_to_dict(assistant)})
```

为什么要追加？因为 Anthropic / OpenAI 的多轮 API 要求"完整对话历史"。LLM 看到自己上一轮发起了 `tool_use`，下一轮才能匹配上 `tool_result`。

`_blocks_to_dict`（core.py:301）：把 `AssistantMessage.blocks` 翻译成 API 期待的 dict 列表（text/tool_use 类型）。

### 6.7 选择串行 / 并行

```python
# core.py:161
tool_results: list[dict | None] = [None] * len(tool_uses)
run_parallel = (
    sys.platform != "win32"
    and len(tool_uses) >= 2
    and self._executor_supports_parallel_tools()
    and all(self._is_parallel_safe(tu) for tu in tool_uses)
)
```

四道 AND 条件全过才并行：
1. 不在 Windows（mock 执行器无 I/O，并行无收益）
2. 至少 2 个工具调用（1 个时并行没意义）
3. 执行器声明自己支持并行（详见下节）
4. 所有工具都满足"预检 ALLOW + read_only + reviewer 未启用"

**当前 Linux 生产环境**：第 3 条恒为 False（见 12-concurrency.md），所以并行路径暂时是 dormant 的。这是有意保守。

### 6.8 并行分支

```python
# core.py:172
if run_parallel:
    pool = self._ensure_pool()
    futures = [
        pool.submit(self._handle_tool_use, trace, tu, notes)
        for tu in tool_uses
    ]
    for idx, (tu, fut) in enumerate(zip(tool_uses, futures)):
        result_block = fut.result()
        if result_block.is_error and result_block.content.startswith("[denied]"):
            denied = True
        tool_results[idx] = {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": result_block.content,
            "is_error": result_block.is_error,
        }
```

- 提交所有 worker 后逐个 `fut.result()` 等齐
- **顺序保留**：用 `tool_results[idx] = ...` 而不是 append，所以 messages 里的 tool_result 顺序仍然对齐 `tool_uses`
- 任何一个 worker 的结果是 `[denied]` 开头都把 denied 标记

### 6.9 串行分支

```python
# core.py:188
else:
    for idx, tu in enumerate(tool_uses):
        result_block = self._handle_tool_use(trace, tu, notes)
        if result_block.is_error and result_block.content.startswith("[denied]"):
            denied = True
        tool_results[idx] = {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": result_block.content,
            "is_error": result_block.is_error,
        }
```

完全一样的逻辑，只是不放进 ThreadPool。

### 6.10 把 tool_result 灌回去

```python
# core.py:200
self.messages.append({"role": "user", "content": tool_results})
```

Anthropic 协议要求把 tool_result 作为 **user 角色**送回（OpenAI 协议则是单独的 `role: tool` 消息，OpenAI 后端会内部翻译）。

### 6.11 超出 max_iterations 兜底

```python
# core.py:204
notes.append(f"达到最大迭代次数 {self.cfg.agent.max_iterations}")
self.audit.event(trace, EventKind.ERROR, {"reason": "max_iterations"})
self.audit.close(trace)
return AgentRunResult(trace=trace,
                      final_text="达到最大工具调用次数，已中止。",
                      tool_iterations=iterations, denied=denied, notes=notes)
```

防止 LLM 陷入"循环调工具"的故障模式。8 次往复仍然没产出最终文本就强行收尾。

---

## 7. _executor_supports_parallel_tools 与 _is_parallel_safe

### 7.1 _executor_supports_parallel_tools

```python
# core.py:213
def _executor_supports_parallel_tools(self) -> bool:
    return bool(getattr(self.executor, "supports_parallel_tool_execution", False))
```

用 `getattr` 而不是直接 `self.executor.supports_parallel_tool_execution`：
- 兼容性：未来允许任何"executor-shaped"对象，包括 mock、远程代理
- 对于不声明此属性的执行器，默认 False（最保守）

### 7.2 _is_parallel_safe（C2 修复之后的版本）

```python
# core.py:217
def _is_parallel_safe(self, tu: ToolUseBlock) -> bool:
    tool = self.registry.get(tu.name)
    if tool is None:
        return False
    if not tool.read_only:
        return False
    # C2 第一道防线
    if self.guardrail.llm_reviewer is not None:
        return False
    try:
        cleaned = tool.validate(tu.input or {})
        argv = tool.build_argv(cleaned)
    except ToolError:
        return False
    verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
    return verdict.decision is Decision.ALLOW
```

五重 AND 才允许并行：
1. 工具必须已注册
2. 工具必须是 `read_only = True`（写操作即便预检 ALLOW 也不并行，避免顺序歧义）
3. **没有启用 `llm_reviewer`**（避免预检 vs 内检不一致——见 12-concurrency.md 的 C2）
4. 参数能过 `validate + build_argv`（否则后续 `_handle_tool_use` 会以 ERROR 兜底）
5. Guardrail 预检 verdict 是 ALLOW（CONFIRM/DENY 都走串行）

---

## 8. _handle_tool_use —— 一个工具调用的全生命周期

这是 ask() 主循环里调用最多的方法，也是审计链事件的真正生产者。它的"真重复"
三段（validate+build_argv+request、guardrail、execute+format）已经抽到
`kyagent/mcp/tools/pipeline.py`，Agent 与 MCP 共享同一份流水线，差异点（CONFIRM
处理、trace 生命周期、返回类型）留在调用方。

```python
def _handle_tool_use(self, trace, tu, notes) -> ToolResultBlock:
```

### 8.1 工具不存在 → ERROR 兜底

```python
tool = self.registry.get(tu.name)
if tool is None:
    self.audit.event(trace, EventKind.ERROR,
                     {"reason": "unknown_tool", "tool": tu.name})
    return ToolResultBlock(tool_use_id=tu.id, is_error=True,
                           content=f"未知工具：{tu.name}")
```

LLM 提议调用一个不存在的工具（理论上不该发生，工具表是 LLM 看到的）。审计记错，返回错误，让 LLM 自己改话术。

### 8.2 prepare_call（参数校验 + argv 构造 + TOOL_REQUEST + 可选 PERCEPTION）

```python
prep = prepare_call(tool, tu.input or {}, trace=trace, audit=self.audit)
if isinstance(prep, PipelineError):
    return ToolResultBlock(tool_use_id=tu.id, is_error=True, content=prep.detail)
```

`pipeline.prepare_call` 内部做：
- `tool.validate(args)`：JSON Schema 校验（required/type/enum/min/max/pattern）。失败抛 `ToolError`，pipeline 写 ERROR 事件并返回 `PipelineError("invalid_args", ...)`
- `tool.build_argv(cleaned)`：argv 构造（含 `_safe_path`、`_validate_unit`、shell 元字符黑名单）。失败同上，`PipelineError("build_argv", ...)`
- 落 `TOOL_REQUEST` 事件（含 tool/argv/args/risk/requires_root）
- 若 `tool.read_only and risk_level == LOW`，再落一条 `PERCEPTION` 事件标注"被动信息收集"——MCP 与 Agent 共用这条逻辑后，两条通道的 timeline 不再漂移

调用方拿到 `PipelineError` 就直接打包返回；拿到 `PreparedCall` 就继续走 guardrail。

### 8.3 check_safety（guardrail + SAFETY_CHECK）

```python
verdict = check_safety(prep, trace=trace, audit=self.audit, guardrail=self.guardrail)
```

`pipeline.check_safety` 调 `guardrail.check_argv(argv, declared_risk=tool.risk_level)` 并把 verdict 落 `SAFETY_CHECK` 事件。**它不处理 DENY/CONFIRM**——调用方按通道特性自决。工具声明的 `risk_level` 作为下限传进去：例如 `process_list` 声明 LOW，规则里也没命中，最终 risk=LOW；`svc_restart` 声明 HIGH，规则里命中 0 条，但 declared_risk 抬升 → final risk=HIGH。

### 8.4 DENY 分支

```python
if verdict.decision is Decision.DENY:
    notes.append(f"已拦截 {tu.name}: {verdict.risk.value}")
    return ToolResultBlock(
        tool_use_id=tu.id, is_error=True,
        content=("[denied] 工具调用被安全护栏拒绝。\n"
                 f"风险等级: {verdict.risk.value}\n"
                 + "\n".join(verdict.rationale)),
    )
```

直接返回 `[denied]` 前缀的错误结果，让 LLM 看到并改方案。不写新的 ERROR 事件——SAFETY_CHECK 已经记录了。

### 8.5 CONFIRM 分支（含 C2 第二道防线）

```python
if verdict.decision is Decision.CONFIRM:
    # C2 第二道防线
    if threading.current_thread() is not threading.main_thread():
        notes.append(f"非主线程下 CONFIRM 默认拒绝 {tu.name}")
        self.audit.event(trace, EventKind.ERROR,
                         {"reason": "confirm_in_worker_denied", "tool": tu.name})
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=True,
            content=("[denied] 工具需要二次确认，但当前调度在非主线程，"
                     f"已自动拒绝（risk={verdict.risk.value}）"),
        )
    approved = False
    try:
        approved = self.confirm(
            confirm_adapter.for_tool_call(verdict, tu.name, prep.argv)
        )
    except Exception:
        approved = False
    if not approved:
        notes.append(f"用户拒绝 {tu.name}")
        self.audit.event(trace, EventKind.ERROR,
                         {"reason": "user_denied_confirm", "tool": tu.name})
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=True,
            content=f"[denied] 用户拒绝执行（risk={verdict.risk.value}）",
        )
    self.audit.event(trace, EventKind.SAFETY_CHECK,
                     {"user_confirmed": True, "tool": tu.name})
```

两道防线的协作：
- **第一道**（`_is_parallel_safe`）：理论上 CONFIRM 路径不会进 worker，因为预检就把所有 confirm-required 工具排除了并行
- **第二道**（这里）：兜底——如果第一道因为 `llm_reviewer` 非确定性失守，worker 线程在内检拿到 CONFIRM 也立刻 deny

`self.confirm` 是 `ConfirmFn` 类型——只接收一个 `ConfirmRequest`，调用方通过
`confirm_adapter.for_tool_call(verdict, tu.name, prep.argv)` 把"裁决 + 调用上下文"
翻译成 UI 契约。Agent 自己不感知 ConfirmRequest 内部字段；CLI 也不感知 Verdict。
`try/except` 把 confirm 回调里的任何异常都视为"未批准"，防御性编程。

### 8.6 execute_and_format（落地执行 + 共享格式化）

```python
_, formatted, content = execute_and_format(
    prep, trace=trace, audit=self.audit, executor=self.executor,
)
return ToolResultBlock(tool_use_id=tu.id, is_error=not formatted.ok, content=content)
```

`pipeline.execute_and_format` 内部做：
- 落 `EXECUTION` 事件（含 argv/requires_root）
- 调 `executor.run(argv, requires_root=tool.requires_root)`；POSIX 上会 fork + sudo + preexec_fn + Popen + communicate
- 落 `EXECUTION_RESULT` 事件（含 stdout/stderr/rc/duration/timed_out）
- `tool.format_result(exec_result)` 默认实现把 ExecutionResult 转成 ToolResult（成功取 stdout，失败带 stderr）
- 失败时把 stderr 拼进 content（共享逻辑，与 MCP 通道一致）
- 统一截到 `OUTPUT_CAP = 6000`（pipeline 模块顶级常量），防止单条 tool_result 撑爆 LLM 输入或 MCP 响应

---

## 9. _blocks_to_dict 辅助

```python
# core.py:301
@staticmethod
def _blocks_to_dict(am: AssistantMessage) -> list[dict]:
    out = []
    for b in am.blocks:
        if isinstance(b, TextBlock):
            out.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out
```

把 dataclass 形态的 blocks 转成 API 协议的 dict。这是为了下一轮 messages 里能直接送回去。

---

## 10. SYSTEM_PROMPT 解读

文件：`kyagent/agent/prompt.py`

主要约束写给 LLM 的：
1. **"禁止在文本里假装已执行命令"** —— 所有动作必须 tool_use
2. **"小步推进，每次只调 1-3 个工具"** —— 鼓励链式而不是 fanout
3. **"变更前先查状态"** —— 调 svc_restart 前先 svc_status
4. **"回复先结论后证据"** —— 给运维人看的，不要客套
5. **明确禁止**：拼 shell 字符串、幻觉命令输出、建议绕过安全的命令

这些 prompt 是项目对 LLM 的"行为规范"，但它**不是安全机制**——LLM 偏离指令时仍然受 Guardrail / sandbox 兜底。

---

## 11. build_agent 便捷入口

```python
# core.py:315
def build_agent(config_path=None, confirm=_auto_deny):
    cfg = load_config(config_path)
    return Agent.from_config(cfg, confirm=confirm)
```

外部脚本想直接用 Agent 的话，一行就行：
```python
from kyagent.agent.core import build_agent
agent = build_agent()
print(agent.ask("查 80 端口").final_text)
```

---

## 12. 关键不变量

读完这一份后，请把下面这几条当作"代码的承诺"记牢：

1. **每一次 ask() 都开一条 Trace 并 close**（无论中途 LLM 报错还是超 max_iterations）
2. **每一次 tool_use 都至少有 4 条审计事件**：TOOL_REQUEST → SAFETY_CHECK → EXECUTION → EXECUTION_RESULT（不能跳过 SAFETY_CHECK 直接 EXECUTION）
3. **`self.confirm()` 只在主线程调用**（C2 第二道防线兜底）
4. **`tool_results[idx]` 与 `tool_uses[idx]` 顺序对齐**（messages 里发给 LLM 时不能乱）
5. **DENY 路径不会创建子进程**（短路返回，executor.run 没被调用）
6. **CONFIRM 路径在 ask 单次模式下等价于 DENY**（因为默认 confirm 是 _auto_deny）

---

## 13. 下一步

继续 → [04-llm-backends.md](./04-llm-backends.md) 看三种 LLM 后端如何统一接口。
