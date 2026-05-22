# 10 · CLI 入口

> 文件：`kyagent/cli.py`（typer + rich）+ `kyagent/__main__.py`

---

## 1. 子命令树

```
kyagent
├── chat                   # 进入交互式对话（默认子命令）
├── ask <text>             # 单轮提问
├── tools list             # 列出所有工具
├── safety test <cmd>      # 让护栏单独裁决一条命令（不执行）
├── audit list             # 最近 N 条 trace 概览
├── audit show <id>        # 打印某条 trace 的完整事件流
└── mcp serve              # 以 stdio 启动 MCP 服务器
```

入口：`kyagent/__main__.py`

```python
from kyagent.cli import app

if __name__ == "__main__":
    app()
```

`python -m kyagent` = `kyagent/__main__.py` 被执行 → `cli.app()`。

---

## 2. typer 应用树（cli.py:41）

```python
app = typer.Typer(no_args_is_help=False, add_completion=False,
                  help="kyagent — 麒麟安全运维 Agent CLI")
tools_app = typer.Typer(help="工具相关")
safety_app = typer.Typer(help="安全护栏调试")
audit_app = typer.Typer(help="审计日志")
mcp_app = typer.Typer(help="MCP 协议")

app.add_typer(tools_app, name="tools")
app.add_typer(safety_app, name="safety")
app.add_typer(audit_app, name="audit")
app.add_typer(mcp_app, name="mcp")
```

每个 `Typer` 实例对应一组子命令，挂在主 `app` 下成树状结构。

---

## 3. UTF-8 stdout 强制（cli.py:21）

```python
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass
```

Windows 控制台默认 GBK 编码，输出中文 / emoji 会 `UnicodeEncodeError`。这段在 import 时强行切到 utf-8。

---

## 4. CLI confirm 回调

```python
from kyagent.confirm import ConfirmRequest

def _cli_confirm(req: ConfirmRequest) -> bool:
    """统一的交互式 confirm 渲染器。

    与具体 verdict 类型解耦：任何 verdict 只要被 confirm_adapter
    翻译成 ConfirmRequest 都能复用同一份 UI。
    """
    console.rule(f"[yellow]需要用户确认 — {req.title}[/yellow]")
    lines = [f"[bold]风险等级[/]: {req.risk}"]
    if req.body:
        lines.append(f"[bold]详情[/]: {req.body}")
    if req.summary_lines:
        lines.append("[bold]命中规则[/]:")
        lines.extend(f"  - {s}" for s in req.summary_lines)
    console.print(Panel.fit(
        "\n".join(lines),
        title="安全审查", border_style="yellow",
    ))
    ans = Prompt.ask("[bold]是否放行？[/]", choices=["y","n"], default="n")
    return ans.lower() == "y"
```

把 `ConfirmRequest`（一个 UI 数据契约，住在 `kyagent/confirm.py`）用 Rich Panel
渲染（黄框）：title 直接来自 req，body 是 argv 或 rationale 等，summary_lines 是
命中规则的字符串化清单。`Prompt.ask` 限制只能输 y/n，**默认 n**（即用户回车 = 拒绝）。

这是 `ConfirmFn = Callable[[ConfirmRequest], bool]` 的具体实现——签名只看
ConfirmRequest，与具体 Verdict/IntentVerdict 类型解耦。chat 模式注入它，ask 模式
不注入（用 `lambda *a, **k: False` 直接拒绝）。

`Verdict → ConfirmRequest` 的翻译由 `kyagent/agent/confirm_adapter.py` 负责：
`for_tool_call(verdict, tool_name, argv)` 用于 argv 层裁决；
`for_intent(verdict)` 用于意图层裁决。adapter 是 safety domain 与 UI 契约
**唯一的接触面**，CLI 端无需知道 verdict 内部字段。

---

## 5. chat 命令（cli.py:75）

```python
@app.command()
def chat(config=None, user="interactive"):
    cfg = load_config(config)
    agent = Agent.from_config(cfg, confirm=_cli_confirm)
    console.print(Panel.fit(...))  # 欢迎面板

    last_trace_id = None
    while True:
        try:
            text = Prompt.ask("[bold green]你[/]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/]")
            break
        text = text.strip()
        if not text: continue
        if text in ("/exit", "/quit"): break
        if text == "/audit":
            if last_trace_id:
                _print_trace(cfg, last_trace_id)
            else:
                console.print("[dim]还没有 trace[/]")
            continue
        if text == "/reset":
            agent.messages.clear()
            console.print("[dim]上下文已清空[/]")
            continue

        result = agent.ask(text, user=user)
        last_trace_id = result.trace.trace_id
        console.print(Panel.fit(
            result.final_text or "[dim](空)[/]",
            title=f"kyagent · trace={result.trace.trace_id[:12]} · iter={result.tool_iterations}",
            border_style="blue",
        ))
        if result.notes:
            console.print("[dim]" + " | ".join(result.notes) + "[/]")
```

交互特性：
- `/exit` / `/quit` 退出
- `/audit` 打印上一条 trace（无需记 trace_id）
- `/reset` 清空对话上下文（messages list）
- 每次 ask 后渲染一个蓝色 Panel，title 含 trace_id 前 12 字符 + 迭代数
- notes 以暗色拼接在下方（"已拦截 X | 用户拒绝 Y"）

---

## 6. ask 命令（cli.py:127）

```python
@app.command()
def ask(text, config=None, user="oneshot", json_out=False):
    cfg = load_config(config)
    agent = Agent.from_config(cfg, confirm=lambda *a, **k: False)  # 单轮不允许 confirm
    result = agent.ask(text, user=user)
    if json_out:
        sys.stdout.write(json.dumps({
            "trace_id": result.trace.trace_id,
            "text": result.final_text,
            "iterations": result.tool_iterations,
            "denied": result.denied,
            "notes": result.notes,
        }, ensure_ascii=False, indent=2) + "\n")
        return
    console.print(result.final_text)
    if result.notes:
        console.print("[dim]" + " | ".join(result.notes) + "[/]")
```

单轮模式特点：
- **confirm = always False**：单轮没有人交互，凡 CONFIRM 一律 deny
- **`--json` 输出**：machine-readable，方便管道接入其它工具

---

## 7. tools list 命令（cli.py:155）

```python
@tools_app.command("list")
def tools_list(config=None):
    cfg = load_config(config)
    registry = default_registry()
    table = Table(title="kyagent 工具清单", show_lines=False)
    table.add_column("name", style="cyan")
    table.add_column("risk")
    table.add_column("root?")
    table.add_column("read-only?")
    table.add_column("description")
    for tool in registry.all():
        if cfg.mcp.enable_tools and tool.name not in cfg.mcp.enable_tools:
            continue
        risk_style = {"low":"green", "medium":"yellow", "high":"red", "critical":"bold red"}
        rc = risk_style.get(tool.risk_level.value, "white")
        table.add_row(
            tool.name,
            Text(tool.risk_level.value, style=rc),
            "yes" if tool.requires_root else "no",
            "yes" if tool.read_only else "no",
            tool.description.split("\n")[0],
        )
    console.print(table)
```

列出 18 个工具的：名字 / risk / 是否需要 root / 是否只读 / 描述。

会过 `cfg.mcp.enable_tools` 白名单（如果非空只显示白名单内的）。

---

## 8. safety test 命令（cli.py:184）

```python
@safety_app.command("test")
def safety_test(cmdline, config=None):
    cfg = load_config(config)
    guardrail = Guardrail.from_config(cfg)
    verdict = guardrail.check_cmdline(cmdline)

    color = {"low":"green","medium":"yellow","high":"red","critical":"bold red"}[verdict.risk.value]
    decision_color = {"allow":"green","confirm":"yellow","deny":"bold red"}[verdict.decision.value]

    body_lines = [
        f"[bold]cmdline[/]: {cmdline}",
        f"[bold]risk[/]:    [{color}]{verdict.risk.value}[/]",
        f"[bold]decision[/]:[{decision_color}]{verdict.decision.value}[/]",
        "",
        "[bold]hits[/]:",
    ]
    if not verdict.hits:
        body_lines.append("  (无)")
    for h in verdict.hits:
        body_lines.append(
            f"  - {h.rule_id} ({h.risk.value}): {h.description}"
            f"\n      matched: {h.matched}"
        )
    body_lines.append("\n[bold]rationale[/]:")
    for r in verdict.rationale:
        body_lines.append(f"  · {r}")

    console.print(Panel("\n".join(body_lines), title="safety verdict", border_style=decision_color))
```

把 Verdict 用 Rich Panel 渲染：
- 标题：safety verdict
- 边框颜色按 decision（绿/黄/红）
- 内容：cmdline + risk + decision + 命中规则列表 + rationale

**这个命令非常适合演示**：

```
kyagent safety test "rm -rf /etc"
```

会看到一个红边 Panel，明确写着 `decision: deny`、`risk: critical`、命中了哪些规则。完全离线，不会真正执行。

---

## 9. audit list 命令（cli.py:221）

```python
@audit_app.command("list")
def audit_list(limit=20, config=None):
    cfg = load_config(config)
    store = AuditStore(cfg.resolve(cfg.audit.database))
    rows = store.list_traces(limit=limit)
    table = Table(title="最近 trace")
    table.add_column("trace_id")
    table.add_column("user")
    table.add_column("started_at")
    table.add_column("channel")
    for r in rows:
        meta = r.get("metadata") or {}
        ts = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(r["trace_id"], r["user"], ts,
                      str(meta.get("channel", meta.get("backend", "?"))))
    console.print(table)
```

`channel` 字段优先取 metadata 里的 `channel`（mcp 通道写入），没有就取 `backend`（agent 主循环写入），都没有显示 `?`。

---

## 10. audit show 命令（cli.py:243）

```python
@audit_app.command("show")
def audit_show(trace_id, config=None):
    cfg = load_config(config)
    _print_trace(cfg, trace_id)


def _print_trace(cfg, trace_id):
    store = AuditStore(cfg.resolve(cfg.audit.database))
    events = store.get_events(trace_id)
    if not events:
        console.print(f"[red]找不到 trace {trace_id}[/]")
        return
    kind_color = {...}
    console.rule(f"trace {trace_id}")
    for ev in events:
        c = kind_color.get(ev["kind"], "white")
        head = f"[{c}]#{ev['seq']:02d}  {ev['kind']}[/]"
        body = json.dumps(ev["payload"], ensure_ascii=False, indent=2, default=str)
        if len(body) > 1500:
            body = body[:1500] + "\n...[truncated]"
        console.print(Panel(body, title=head, border_style=c))
```

按 seq 顺序打印每个事件，每条事件一个 Panel，颜色按 kind 区分：
- USER_INPUT → bold green
- LLM_THOUGHT → magenta
- TOOL_REQUEST → blue
- SAFETY_CHECK → yellow
- EXECUTION / EXECUTION_RESULT → blue / cyan
- AGENT_REPLY → bold blue
- ERROR → red

Payload 超过 1500 字截断（防止单条事件占满屏幕）。

---

## 11. mcp serve 命令（cli.py:283）

```python
@mcp_app.command("serve")
def mcp_serve(config=None):
    from kyagent.mcp.server import main as serve_main
    if config:
        os.environ["KYAGENT_CONFIG"] = config
    serve_main()
```

把 `--config` 选项通过环境变量传给 MCP server（因为 server.main 也走 `load_config()`）。

---

## 12. 默认子命令（cli.py:296）

```python
@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        chat()
```

`kyagent`（不带任何参数）= `kyagent chat`。这是 typer 的 `invoke_without_command=True` 模式：如果用户没指定子命令，就跑这个回调。

---

## 13. Rich 渲染元素清单

CLI 大量用 Rich 库来美化输出：

| 元素 | 用途 |
|---|---|
| `Console` | 全局打印对象 |
| `Panel.fit` | 自适应宽度的带边框面板 |
| `Table` | 表格（tools list / audit list） |
| `Prompt.ask` | 交互输入 |
| `Text` | 带样式的字符串（risk 颜色） |
| `console.rule` | 水平分隔线 |
| `[color]text[/]` | 行内样式（fg color） |

---

## 14. 端到端命令示例

```bash
# 1. 进入聊天
kyagent chat

# 2. 单轮 + JSON 输出
kyagent ask "查 80 端口" --json

# 3. 查看所有工具
kyagent tools list

# 4. 测试某条命令的护栏裁决
kyagent safety test "rm -rf /etc"

# 5. 查看最近 10 条 trace
kyagent audit list -n 10

# 6. 回看某条 trace
kyagent audit show trace-1f2e3d4567

# 7. 启动 MCP server（被 Claude Desktop 调用）
kyagent mcp serve

# 8. 显式指定配置（当前推荐 DeepSeek）
kyagent ask "查 80 端口" --config configs/deepseek.yaml
```

---

## 15. 关键不变量

1. **`kyagent`（无参数）= `kyagent chat`**：默认子命令
2. **CLI confirm 只在 chat 子命令注入**：ask 子命令一律拒绝
3. **`safety test` 是纯离线的**：不会真的执行任何东西
4. **`audit list/show` 不需要 Agent 实例**：直接读 SQLite

---

## 16. 下一步

继续 → [11-security-model.md](./11-security-model.md) 看全部防御层叠在一起的威胁模型。
