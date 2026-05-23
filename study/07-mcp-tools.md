# 07 · MCP 工具集与协议层

> 文件：
> - `kyagent/mcp/tools/base.py`（Tool 抽象 + ToolRegistry + ToolError）
> - `kyagent/mcp/tools/{process,network,logs,service,filesystem,package}.py`（6 大工具家族，共 18 个工具）
> - `kyagent/mcp/tools/__init__.py`（`default_registry()`）
> - `kyagent/mcp/server.py`（MCP stdio JSON-RPC 2.0 服务器）
> - `tests/test_mcp.py`

对应赛题 **第 ① 条 "OS 环境深度感知"** + **第 ② 条 "MCP 运维插件化"**。

---

## 1. Tool 抽象（base.py:35）

```python
class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}

    risk_level: RiskLevel = RiskLevel.LOW
    requires_root: bool = False
    read_only: bool = True

    def to_mcp(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    def validate(self, args: dict) -> dict:
        """根据 input_schema 做轻量校验。"""
        props = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])
        cleaned = {}
        for key in required:
            if key not in args:
                raise ToolError(f"参数 {key!r} 必填")
        for k, v in args.items():
            schema = props.get(k)
            if schema is None:
                continue  # 忽略未知字段
            cleaned[k] = self._coerce_type(v, schema, k)
        return cleaned

    @staticmethod
    def _coerce_type(value, schema, key):
        expected = schema.get("type")
        if expected is None:
            return value
        if expected == "string" and not isinstance(value, str):
            return str(value)
        if expected == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ToolError(f"{key} 期望 integer，收到 {value!r}")
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("1","true","yes","y")
            return bool(value)
        if expected == "array" and not isinstance(value, list):
            raise ToolError(f"{key} 期望 array")
        return value

    @abc.abstractmethod
    def build_argv(self, args: dict) -> list[str]:
        """根据已校验过的 args 构造最终 argv。子类必须实现。"""

    def format_result(self, exec_result) -> ToolResult:
        """默认格式化：成功取 stdout，失败带 stderr。子类可定制。"""
```

Tool 的契约：
- **声明式属性**：name / description / input_schema / risk_level / requires_root / read_only
- **`validate(args)`** —— 轻量 JSON Schema 校验（required + 一级 type）
- **`build_argv(cleaned)`** —— 把 cleaned args 翻译成 argv（这是子类的核心逻辑）
- **`format_result(exec_result)`** —— 默认实现够用，特殊工具可重写

### 1.1 validate 的"宽容但严格"

```python
for k, v in args.items():
    schema = props.get(k)
    if schema is None:
        continue
    cleaned[k] = self._coerce_type(v, schema, k)
```

- 未知字段被 **静默丢弃**（不抛错）—— 防止 LLM 偶尔多塞字段
- 已知字段必须能 coerce 到声明类型 —— `port = "80"` 会被 `int("80")` 成 80（test_mcp.py:48）

### 1.2 ToolError

```python
class ToolError(Exception):
    """工具参数非法或语义错误，应作为 ToolResult.error 返回给 LLM。"""
```

工具自己的参数清洗（如 `_safe_path`、`_validate_unit`）抛 ToolError 后被 Agent 主循环捕获。**ToolError 是约定的"软失败"信号**，不会让进程崩。

### 1.3 ToolResult

```python
@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict = field(default_factory=dict)
    error: str | None = None
```

`data` 字段是结构化版（例如 `process_list` 把 `row_count` 放进去），方便后续机器读取；`content` 是给 LLM 看的人话版。

### 1.4 format_result 默认实现（base.py:109）

```python
def format_result(self, exec_result):
    if exec_result.skipped_reason == "windows_mock":
        return ToolResult(ok=True, content=exec_result.stdout, data=exec_result.to_dict())
    if exec_result.skipped_reason:
        return ToolResult(ok=False, content="",
                          error=f"{exec_result.skipped_reason}: {exec_result.stderr}",
                          data=exec_result.to_dict())
    if exec_result.timed_out:
        return ToolResult(ok=False, content=exec_result.stdout,
                          error="execution timed out", data=exec_result.to_dict())
    if exec_result.returncode != 0:
        return ToolResult(ok=False, content=exec_result.stdout,
                          error=exec_result.stderr or f"exit={exec_result.returncode}",
                          data=exec_result.to_dict())
    return ToolResult(ok=True, content=exec_result.stdout, data=exec_result.to_dict())
```

四个分支：windows_mock 按成功；其它 skipped → 失败；timeout → 失败；rc != 0 → 失败；其余成功。

---

## 2. ToolRegistry（base.py:135）

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_mcp_list(self) -> list[dict]:
        return [t.to_mcp() for t in self.all()]

    def to_anthropic_tools(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in self.all()
        ]
```

注意：
- **注册时拒绝重名**：避免后注册的工具覆盖前面的
- **两套序列化**：`to_mcp_list()` 用于 MCP `tools/list` 响应（字段 `inputSchema`），`to_anthropic_tools()` 用于 Anthropic API（字段 `input_schema`）

---

## 3. 六大工具家族

### 3.1 进程（process.py）—— 3 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `process_list` | `ps -eo user,pid,pcpu,pmem,etime,stat,comm,args --sort {-pcpu/-pmem/pid} [-u user]` | low | - |
| `lsof_port` | `lsof -nP -i {TCP/UDP}:{port}` | low | - |
| `lsof_pid` | `lsof -nP -p {pid}` | low | - |

`PsListTool.format_result` 重写：截到前 20 行 + 把 row_count 放进 data（process.py:33）。

### 3.2 网络（network.py）—— 3 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `net_listen` | `ss {-tlnp/-ulnp/-tulnp}` | low | - |
| `net_connections` | `ss -tnp [state {established/time-wait/...}]` | low | - |
| `net_ping` | `ping -c {count} -W 2 {host}` | low | - |

`net_ping` 的 `count` 受限 1-10，避免被当成 DoS 工具用。

### 3.3 日志（logs.py）—— 2 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `log_journal` | `journalctl --no-pager [--since X] [-p X] [-u X] -n N [--grep X]` | low | - |
| `log_dmesg` | `dmesg --human --color=never [-l level]` | low | - |

`log_journal` 的 priority 字段会被检查是否在 `_PRIORITIES = {emerg, alert, crit, err, warning, notice, info, debug}` 集合里，不在就忽略。

`DmesgTool.format_result` 重写：截到最后 200 行（dmesg 通常很长）。

### 3.4 服务（service.py）—— 4 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `svc_status` | `systemctl status --no-pager --lines=50 {unit}` | low | - |
| `svc_list` | `systemctl list-units --no-pager --no-legend --all [--state X]` | low | - |
| `svc_restart` | `systemctl restart {unit}` | **high** | **yes** |
| `svc_reload` | `systemctl reload {unit}` | medium | yes |

**关键安全锚点**：`_validate_unit` 在 `build_argv` 之前必跑。

```python
_FORBIDDEN_UNITS = {
    "systemd-logind", "systemd-journald", "systemd-udevd",
    "dbus", "dbus.service", "polkit", "polkit.service",
}

def _validate_unit(unit: str) -> str:
    if not unit or any(c in unit for c in [" ", ";", "|", "&", "$", "`"]):
        raise ToolError(f"非法 unit 名: {unit!r}")
    if unit.split(".")[0] in _FORBIDDEN_UNITS:
        raise ToolError(f"unit {unit!r} 在工具层禁用名单内（防误关核心服务）")
    return unit
```

两道防御：
1. **shell 元字符黑名单**：`unit = "sshd; rm -rf /"` 直接 ToolError（即便后续 sudoers 不允许这种 unit 也不让它进 argv）
2. **核心服务黑名单**：`systemd-logind` / `dbus` / `polkit` 这种关掉就毁系统的，工具层禁止操作

注意 **没有 `mask` / `disable` / `enable` 工具**——这些操作根本不暴露给 LLM。要做这些必须人工 SSH 上去。

### 3.5 文件系统（filesystem.py）—— 4 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `fs_df` | `df -h -x tmpfs -x devtmpfs [path]` | low | - |
| `fs_du` | `du -h --max-depth={1-5} {path}` | low | - |
| `fs_ls` | `ls -lah --color=never {path}` | low | - |
| `fs_find` | `find {path} -maxdepth N [-name X] [-mtime -N] -type f` | low | - |

**关键安全锚点**：`_safe_path` 在每个工具的 build_argv 都跑。

```python
_PROTECTED_READ = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}

def _safe_path(p: str) -> str:
    if not p:
        raise ToolError("path 不能为空")
    p = posixpath.normpath(p)  # 不是 os.path！见 05-safety-layer.md
    if any(c in p for c in [";", "|", "&", "$", "`", "\n"]):
        raise ToolError(f"非法路径字符: {p!r}")
    if p in _PROTECTED_READ:
        raise ToolError(f"路径 {p} 在工具层禁读名单内")
    return p
```

三道防御：
1. **空路径**：直接拒绝
2. **shell 元字符**：分号、管道、反引号、换行 → 拒绝
3. **敏感文件黑名单**：`/etc/shadow` / `/etc/gshadow` / `/etc/sudoers` 即便 LLM 调 `fs_ls` 读元信息也拒绝

`fs_find` **特别强制不走 `-exec`** —— argv 构造里根本没有 `-exec`，即便 LLM 输入也无法插入。同样禁了 `-delete`。

### 3.6 软件包（package.py）—— 2 个工具

| 工具 | argv 模板 | risk | root |
|---|---|---|---|
| `pkg_info` | `{dnf/yum} info {name}` 或 `apt show / rpm -qi / dpkg -s` | low | - |
| `pkg_installed` | `{dnf/yum} list installed [keyword]` 或 `apt list --installed / rpm -qa / dpkg -l` | low | - |

`_detect_pm()`：优先级 dnf > yum > apt > rpm > dpkg，默认 dnf（麒麟主流）。

注意：**没有 `pkg_install` / `pkg_remove`**——安装卸载是高风险变更，不在 LLM 工具层暴露。

---

## 4. default_registry —— 一行注册（__init__.py）

```python
def register_builtin(registry: ToolRegistry) -> ToolRegistry:
    process.register(registry)
    network.register(registry)
    logs.register(registry)
    service.register(registry)
    filesystem.register(registry)
    package.register(registry)
    return registry

def default_registry() -> ToolRegistry:
    return register_builtin(ToolRegistry())
```

每个 module 暴露一个 `register(registry)` 函数，自己创建工具实例并注册。`default_registry()` 是 18 个工具的一站式装配。

---

## 5. MCP stdio 服务器（mcp/server.py）

### 5.1 协议

MCP（Model Context Protocol）= Anthropic 提出的 LLM 工具接入协议。kyagent 实现了 stdio 传输（JSON-RPC 2.0 over stdin/stdout，newline-delimited）。

支持的方法：
- `initialize` —— 协议握手，返回 protocolVersion + capabilities
- `initialized` —— 客户端确认握手完成（通常是 notification 无 id）
- `ping` —— 心跳
- `tools/list` —— 列出所有工具
- `tools/call` —— 调用一个工具

### 5.2 \_resp / \_err helpers

```python
def _resp(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def _err(req_id, code, message, data=None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}
```

错误码用 JSON-RPC 标准：`-32700` parse error / `-32601` method not found / `-32603` internal error。

### 5.3 McpServer.serve —— stdin/stdout loop

```python
def serve(self) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._write(_err(None, -32700, "parse error"))
            continue
        try:
            response = self._dispatch(msg)
        except Exception as e:
            response = _err(msg.get("id"), -32603, "internal error",
                            {"exc": str(e), "traceback": traceback.format_exc()[-2000:]})
        if response is not None:
            self._write(response)
```

特点：
1. **单线程串行**：stdio 天然不能并发，所有调用顺序执行
2. **解析错误不退出**：单条 line 解析失败只回 -32700，继续读下一条
3. **任何异常被包成 -32603**：进程不会因为单次工具失败而崩
4. **notification（无 id）不回包**：`_dispatch` 返回 None 时不写

### 5.4 \_dispatch 路由

```python
def _dispatch(self, msg) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        return _resp(req_id, self._initialize(params))
    if method == "initialized":
        self._initialized = True
        return None if is_notification else _resp(req_id, {})
    if method == "ping":
        return _resp(req_id, {})
    if method == "tools/list":
        return _resp(req_id, {"tools": self.registry.to_mcp_list()})
    if method == "tools/call":
        return _resp(req_id, self._call_tool(params))
    return _err(req_id, -32601, f"method not found: {method}")
```

### 5.5 \_initialize

```python
def _initialize(self, params):
    return {
        "protocolVersion": self.PROTOCOL_VERSION,   # "2024-11-05"
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": self.cfg.mcp.server_name,
            "version": self.cfg.mcp.server_version,
        },
    }
```

`listChanged: False` 表示我们不会主动通知客户端工具列表变化（kyagent 启动后工具集固定）。

### 5.6 \_call_tool —— 核心方法

`_call_tool` 已经把"真重复"的三段委托给共享流水线
`kyagent/mcp/tools/pipeline.py`，剩下的差异（CONFIRM 通道行为、trace 生命周期、
返回类型包装）才留在这里。

```python
def _call_tool(self, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = self.registry.get(name) if isinstance(name, str) else None

    trace = Trace(user="mcp-client")
    self.audit.open(trace)
    trace.metadata.update({"channel": "mcp", "tool": name})

    if tool is None:
        self.audit.event(trace, EventKind.ERROR, {"reason":"unknown_tool", "name":name})
        self.audit.close(trace)
        return {"content":[{"type":"text","text":f"unknown tool: {name}"}], "isError":True}

    # validate + build_argv + TOOL_REQUEST + 可选 PERCEPTION（共享流水线）
    prep = prepare_call(tool, args, trace=trace, audit=self.audit)
    if isinstance(prep, PipelineError):
        self.audit.close(trace)
        return {"content":[{"type":"text","text":prep.detail}], "isError":True}

    verdict = check_safety(prep, trace=trace, audit=self.audit, guardrail=self.guardrail)

    if verdict.decision is Decision.DENY:
        self.audit.close(trace)
        return {"content":[{"type":"text",
            "text":f"已拦截：{verdict.risk.value}\n" + "\n".join(verdict.rationale)}],
            "isError":True}
    if verdict.decision is Decision.CONFIRM:
        # MCP 通道无交互通道，按拒绝处理
        self.audit.event(trace, EventKind.ERROR, {"reason":"needs_confirm_via_mcp"})
        self.audit.close(trace)
        return {"content":[{"type":"text",
            "text":(f"需用户确认才能执行（risk={verdict.risk.value}）；"
                    "通过 MCP 通道默认不发起确认。请改走 kyagent chat。")}],
            "isError":True}

    # 落地执行 + 格式化（共享流水线，含 stderr 拼接 + OUTPUT_CAP 截断）
    _, formatted, content = execute_and_format(
        prep, trace=trace, audit=self.audit, executor=self.executor,
    )
    self.audit.event(trace, EventKind.AGENT_REPLY,
                     {"ok":formatted.ok, "len":len(formatted.content),
                      "error":formatted.error})
    self.audit.close(trace)

    return {"content":[{"type":"text","text": content or (formatted.error or "")}],
            "isError": not formatted.ok}
```

`prepare_call` / `check_safety` / `execute_and_format` 的内部行为见 03-agent-core
的「8. _handle_tool_use」一节——MCP 与 Agent 共用这三个函数，所以现在两条通道：
- 都会落 `PERCEPTION` 事件（read_only + LOW 时）——以前只在 Agent 通道落
- 都会把 stderr 拼进 content（失败时）
- 都会按 `OUTPUT_CAP = 6000` 截断输出

历史上这三件事曾经只在 Agent 通道做，导致审计 timeline 跨通道漂移。统一到
pipeline 之后，"两条通道行为一致"成为模块约束。

关键差异 vs Agent 主循环（本质差异，刻意不抽进 pipeline）：
1. **不走 LLM**：MCP 调用方就是 LLM 自己，没有"再请 LLM 决策"环节
2. **CONFIRM = DENY**：MCP 没法发起交互式 confirm，pipeline.check_safety
   不处理决策，由 _call_tool 自己 deny
3. **每条 tools/call 起一条 trace**：每个工具调用都是独立审计单元（Agent 是
   per-ask 一条 trace、内含多次工具）

### 5.7 main 入口

```python
def main():
    cfg = load_config()
    rt = build_runtime(cfg)   # 通道无关的基础设施装配（与 Agent.from_config 共享）
    server = McpServer(cfg, rt.registry, rt.guardrail, rt.executor, rt.audit)
    server.serve()
```

`kyagent/runtime.py` 是 composition root，把 SandboxConfig / ExecutionProxy /
Guardrail / AuditStore+AuditLogger / default_registry（含 `enable_tools` 白名单
过滤）装配成一个 `Runtime` dataclass。Agent.from_config 也调它，这样两条通道用
的执行器 / 护栏 / 审计 / 工具注册表是同一组配置出来的，杜绝"字面级一字不差的
复制"漂移点。

被 `kyagent mcp serve` 子命令调用。也可以通过 `python -m kyagent.mcp.server` 直接启动。

### 5.8 挂到 Claude Desktop

```json
// ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "kyagent": {
      "command": "kyagent",
      "args": ["mcp", "serve"]
    }
  }
}
```

Claude Desktop 启动后会 fork 一个 kyagent 进程通过 stdio 与之对话。即便 Claude 跳过自家安全检查，kyagent 的 guardrail + executor + audit 三件套仍然在本地把控。

---

## 6. test_mcp.py 关键用例

### 6.1 注册完整性

```python
def test_default_registry_has_core_tools():
    reg = default_registry()
    names = set(reg.names())
    must_have = {
        "process_list", "lsof_port", "net_listen", "log_journal",
        "svc_status", "svc_restart", "fs_df", "pkg_info",
    }
    assert must_have.issubset(names)
```

### 6.2 协议 shape

```python
def test_to_mcp_list_shape():
    reg = default_registry()
    items = reg.to_mcp_list()
    for it in items:
        assert "name" in it
        assert "description" in it
        assert "inputSchema" in it
        assert it["inputSchema"]["type"] == "object"

def test_anthropic_tools_shape():
    reg = default_registry()
    items = reg.to_anthropic_tools()
    for it in items:
        assert set(it.keys()) == {"name", "description", "input_schema"}
```

### 6.3 参数清洗

```python
def test_validate_required_args_missing():
    tool = default_registry().get("lsof_port")
    with pytest.raises(ToolError):
        tool.validate({})

def test_validate_coerces_string_to_int():
    tool = default_registry().get("lsof_port")
    cleaned = tool.validate({"port": "80"})
    assert cleaned["port"] == 80
    argv = tool.build_argv(cleaned)
    assert argv == ["lsof", "-nP", "-i", "TCP:80"]
```

### 6.4 安全锚点

```python
def test_svc_restart_rejects_shell_metacharacters():
    tool = default_registry().get("svc_restart")
    with pytest.raises(ToolError):
        tool.build_argv({"unit": "sshd; rm -rf /"})

def test_svc_restart_rejects_forbidden_unit():
    tool = default_registry().get("svc_restart")
    with pytest.raises(ToolError):
        tool.build_argv({"unit": "systemd-logind"})

def test_find_rejects_shell_meta_in_name():
    tool = default_registry().get("fs_find")
    with pytest.raises(ToolError):
        tool.build_argv({"path": "/var/log", "name": "*.log; rm -rf /"})

def test_filesystem_blocks_shadow_read():
    tool = default_registry().get("fs_ls")
    with pytest.raises(ToolError):
        tool.build_argv({"path": "/etc/shadow"})
```

每个工具自己的清洗逻辑都有用例。

---

## 7. 三层清洗的协作

工具调用从 LLM 到落地一共要过三层：

```
LLM 给的 args (dict)
        │
        ▼  Tool.validate(args)
   清洗层 1：JSON Schema 类型 + required 校验
   失败 → ToolError → "工具参数非法"
        │
        ▼  Tool.build_argv(cleaned)
   清洗层 2：工具自己的语义清洗（_safe_path / _validate_unit / shell-meta 黑名单）
   失败 → ToolError → "工具参数非法"
        │
        ▼ argv → Guardrail.check_argv(argv)
   清洗层 3：规则引擎 + 策略映射（系统级危险模式）
   决定 ALLOW/CONFIRM/DENY
        │
        ▼ ExecutionProxy.run(argv)
   落地：sudo + sandbox + Popen
```

**层数多是有意的纵深防御**：每一层都是必要的。
- 第 1 层防 LLM 给参数类型不对（轻量）
- 第 2 层防工具自己的语义攻击（细粒度黑名单）
- 第 3 层防"工具组合"在 OS 上的危险后果（cmdline 规则）
- 落地层防执行环境本身（PATH、env、rlimit）
- 最外层还有 sudoers 白名单

---

## 8. 工具列表的运行时白名单

`cfg.mcp.enable_tools` 是部署侧的工具白名单：

```yaml
mcp:
  enable_tools: ["process_list", "lsof_port", "fs_df"]  # 留空表示全部启用
```

`Agent.from_config` 和 `McpServer.main` 都会读它：

```python
if cfg.mcp.enable_tools:
    keep = set(cfg.mcp.enable_tools)
    registry._tools = {n: t for n, t in registry._tools.items() if n in keep}
```

例如生产环境想完全禁掉 svc_restart，配置里加白名单不含 `svc_restart` 即可，工具不会暴露给 LLM 也不会出现在 `tools/list` 里。

---

## 9. 关键不变量

1. **每个工具的 `build_argv` 必返回 `list[str]`**：没有字符串、没有 shell
2. **每个工具的 `read_only` 字段必须如实声明**：影响 `_is_parallel_safe`
3. **每个工具的 `risk_level` 是下限**：永远只能向上抬，不能向下降
4. **没有 raw shell 工具**：所有工具都是受控的命令包装
5. **每个 `Tool` 实例是无状态的**：可以被并发 worker 共享（虽然当前并行 dormant）

---

## 10. 下一步

继续 → [08-audit-chain.md](./08-audit-chain.md) 看推理链审计三件套。
