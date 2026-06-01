"""kyagent 命令行入口。

子命令：
  chat               进入交互式对话（默认）
  ask <text>         单轮提问
  tools list         列出所有已注册工具
  safety test <cmd>  在不执行的情况下让安全护栏判定 cmd
  audit list         最近 trace 概览
  audit show <id>    打印某条 trace 的完整推理链
  tui                进入轻量 TUI 壳
  mcp serve          以 stdio 模式启动 MCP 服务器
"""
from __future__ import annotations

import io
import json
import sys

import typer

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免 emoji/中文报错
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, io.UnsupportedOperation):
        pass
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from kyagent.agent.core import Agent
from kyagent.audit.trace import EventKind
from kyagent.config import load_config
from kyagent.mcp.plugins import configured_registry
from kyagent.confirm import ConfirmRequest
from kyagent.runtime import build_audit_store
from kyagent.safety.guardrail import Guardrail

app = typer.Typer(no_args_is_help=False, add_completion=False, help="kyagent — 麒麟安全运维 Agent CLI")
tools_app = typer.Typer(help="工具相关")
safety_app = typer.Typer(help="安全护栏调试")
audit_app = typer.Typer(help="审计日志")
mcp_app = typer.Typer(help="MCP 协议")
web_app = typer.Typer(help="B/S 架构 Web 服务（FastAPI）")
app.add_typer(tools_app, name="tools")
app.add_typer(safety_app, name="safety")
app.add_typer(audit_app, name="audit")
app.add_typer(mcp_app, name="mcp")
app.add_typer(web_app, name="web")

console = Console()


# ---- 交互回调：在 CLI 里弹出 confirm ---------------------------------------


def _cli_confirm(req: ConfirmRequest) -> bool:
    """统一的交互式 confirm 渲染器。

    与具体 verdict 类型解耦：任何 verdict 只要实现 to_confirm_request() 都能复用。
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
    ans = Prompt.ask("[bold]是否放行？[/]", choices=["y", "n"], default="n")
    return ans.lower() == "y"


# ---- chat / ask ----------------------------------------------------------


@app.command()
def chat(
    config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    user: str = typer.Option("interactive", "--user", "-u"),
):
    """启动交互式对话。"""
    cfg = load_config(config)
    agent = Agent.from_config(cfg, confirm=_cli_confirm)

    backend_line = f"  LLM 后端     : [bold]{agent.llm.name}[/]"

    console.print(Panel.fit(
        f"[bold cyan]kyagent[/] 已就绪\n"
        f"{backend_line}\n"
        f"  执行账户     : [bold]{cfg.executor.account}[/]\n"
        f"  已注册工具   : {len(agent.registry.all())}\n"
        f"  审计 DB      : {cfg.resolve(cfg.audit.database)}\n"
        f"  输入 [bold]/exit[/] 退出，[bold]/audit[/] 看本轮 trace",
        title="kyagent", border_style="cyan",
    ))

    last_trace_id: str | None = None
    try:
        while True:
            try:
                text = Prompt.ask("[bold green]你[/]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]再见[/]")
                break
            text = text.strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
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
    finally:
        agent.shutdown()


@app.command()
def ask(
    text: str = typer.Argument(..., help="单轮提问"),
    config: str | None = typer.Option(None, "--config", "-c"),
    user: str = typer.Option("oneshot", "--user", "-u"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出，方便管道"),
):
    """非交互式提问。"""
    cfg = load_config(config)
    agent = Agent.from_config(cfg, confirm=lambda *a, **k: False)  # 单轮模式不允许 confirm
    try:
        result = agent.ask(text, user=user)
        if json_out:
            sys.stdout.write(json.dumps({
                "trace_id": result.trace.trace_id,
                "text": result.final_text,
                "iterations": result.tool_iterations,
                "denied": result.denied,
                "notes": result.notes,
                "backend": agent.llm.name,
            }, ensure_ascii=False, indent=2) + "\n")
            return
        console.print(result.final_text)
        if result.notes:
            console.print("[dim]" + " | ".join(result.notes) + "[/]")
    finally:
        agent.shutdown()


@app.command()
def tui(
    config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    user: str = typer.Option("tui", "--user", "-u"),
):
    """启动轻量 TUI 壳（持续会话、工具视图、确认与 trace 回放）。"""
    from kyagent.tui import run_tui

    run_tui(config=config, user=user)


# ---- tools ----------------------------------------------------------------


@tools_app.command("list")
def tools_list(config: str | None = typer.Option(None, "--config", "-c")):
    """列出所有已注册工具与风险等级。"""
    cfg = load_config(config)
    registry = configured_registry(cfg)
    table = Table(title="kyagent 工具清单", show_lines=False)
    table.add_column("name", style="cyan")
    table.add_column("risk")
    table.add_column("root?")
    table.add_column("read-only?")
    table.add_column("description")
    for tool in registry.all():
        risk_style = {"low": "green", "medium": "yellow", "high": "red", "critical": "bold red"}
        rc = risk_style.get(tool.risk_level.value, "white")
        table.add_row(
            tool.name,
            Text(tool.risk_level.value, style=rc),
            "yes" if tool.requires_root else "no",
            "yes" if tool.read_only else "no",
            tool.description.split("\n")[0],
        )
    console.print(table)


# ---- safety --------------------------------------------------------------


@safety_app.command("test")
def safety_test(
    text: str = typer.Argument(..., help="待检测的命令字符串 / 自然语言意图（自动两层都跑）"),
    config: str | None = typer.Option(None, "--config", "-c"),
    layer: str = typer.Option(
        "both", "--layer", "-l",
        help="检测层：intent（NL 意图层）/ argv（shell 二次过滤）/ both（默认）",
    ),
):
    """让安全护栏对输入文本出具裁决（不真正执行）。

    赛题第 3 条要求两层过滤都做到：
      · intent 层：用户原始自然语言意图（如"请帮我删除 /etc"）+ Prompt Injection
      · argv 层：LLM 已经吐出的 shell 命令（如 rm -rf /etc）
    默认两层都跑；综合取最严的裁决。
    """
    cfg = load_config(config)
    color_map = {"low": "green", "medium": "yellow", "high": "red", "critical": "bold red"}
    decision_color_map = {"allow": "green", "confirm": "yellow", "deny": "bold red"}

    intent_verdict = None
    argv_verdict = None

    if layer in ("intent", "both"):
        from kyagent.safety.intent import IntentGuard
        ig = IntentGuard.from_config(cfg)
        intent_verdict = ig.evaluate(text)
        body = [
            f"[bold]输入[/]: {text}",
            f"[bold]risk[/]:    [{color_map[intent_verdict.risk.value]}]{intent_verdict.risk.value}[/]",
            f"[bold]decision[/]:[{decision_color_map[intent_verdict.decision.value]}]{intent_verdict.decision.value}[/]",
            "",
            "[bold]hits[/]:",
        ]
        if not intent_verdict.hits:
            body.append("  (无)")
        for h in intent_verdict.hits:
            body.append(
                f"  - {h.rule_id} [{h.category}] ({h.risk.value}): "
                f"matched={h.matched!r} via {h.variant}"
            )
        body.append("\n[bold]rationale[/]:")
        for r in intent_verdict.rationale:
            body.append(f"  · {r}")
        console.print(Panel(
            "\n".join(body),
            title="意图层（自然语言 + Prompt Injection）",
            border_style=decision_color_map[intent_verdict.decision.value],
        ))

    if layer in ("argv", "both"):
        guardrail = Guardrail.from_config(cfg)
        argv_verdict = guardrail.check_cmdline(text)
        body = [
            f"[bold]cmdline[/]: {text}",
            f"[bold]risk[/]:    [{color_map[argv_verdict.risk.value]}]{argv_verdict.risk.value}[/]",
            f"[bold]decision[/]:[{decision_color_map[argv_verdict.decision.value]}]{argv_verdict.decision.value}[/]",
            "",
            "[bold]hits[/]:",
        ]
        if not argv_verdict.hits:
            body.append("  (无)")
        for h in argv_verdict.hits:
            body.append(
                f"  - {h.rule_id} ({h.risk.value}): {h.description}"
                f"\n      matched: {h.matched}"
            )
        body.append("\n[bold]rationale[/]:")
        for r in argv_verdict.rationale:
            body.append(f"  · {r}")
        console.print(Panel(
            "\n".join(body),
            title="argv 层（LLM 输出二次过滤）",
            border_style=decision_color_map[argv_verdict.decision.value],
        ))

    # 综合裁决：两层取最严
    if intent_verdict and argv_verdict:
        final_decision = max(
            (intent_verdict.decision, argv_verdict.decision),
            key=lambda d: d.order,
        )
        final_risk = max(
            (intent_verdict.risk, argv_verdict.risk),
            key=lambda r: r.order,
        )
        console.print(
            f"[bold]综合裁决[/]: "
            f"risk=[{color_map[final_risk.value]}]{final_risk.value}[/] "
            f"decision=[{decision_color_map[final_decision.value]}]{final_decision.value}[/]"
        )


# ---- audit ---------------------------------------------------------------


@audit_app.command("list")
def audit_list(
    limit: int = typer.Option(20, "--limit", "-n"),
    config: str | None = typer.Option(None, "--config", "-c"),
):
    cfg = load_config(config)
    store = build_audit_store(cfg)
    try:
        rows = store.list_traces(limit=limit)
    finally:
        store.close()
    table = Table(title="最近 trace")
    table.add_column("trace_id")
    table.add_column("user")
    table.add_column("started_at")
    table.add_column("channel")
    for r in rows:
        meta = r.get("metadata") or {}
        ts = r["started_at"]
        from datetime import datetime
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(r["trace_id"], r["user"], ts_str, str(meta.get("channel", meta.get("backend", "?"))))
    console.print(table)


@audit_app.command("show")
def audit_show(
    trace_id: str = typer.Argument(...),
    config: str | None = typer.Option(None, "--config", "-c"),
):
    cfg = load_config(config)
    _print_trace(cfg, trace_id)


@audit_app.command("verify")
def audit_verify(
    trace_id: str = typer.Argument(...),
    config: str | None = typer.Option(None, "--config", "-c"),
):
    """校验 trace 的哈希链、HMAC 和闭链尾封印。"""
    cfg = load_config(config)
    store = build_audit_store(cfg)
    try:
        result = store.verify_trace(trace_id)
    finally:
        store.close()
    console.print_json(data=result)
    if not result["ok"]:
        raise typer.Exit(code=2)


def _print_trace(cfg, trace_id: str) -> None:
    store = build_audit_store(cfg)
    try:
        events = store.get_events(trace_id)
    finally:
        store.close()
    if not events:
        console.print(f"[red]找不到 trace {trace_id}[/]")
        return
    kind_color = {
        EventKind.USER_INPUT.value: "bold green",
        EventKind.PERCEPTION.value: "cyan",
        EventKind.DIAGNOSIS.value: "bold cyan",
        EventKind.LLM_THOUGHT.value: "magenta",
        EventKind.TOOL_REQUEST.value: "blue",
        EventKind.SAFETY_CHECK.value: "yellow",
        EventKind.EXECUTION.value: "blue",
        EventKind.EXECUTION_RESULT.value: "cyan",
        EventKind.AGENT_REPLY.value: "bold blue",
        EventKind.ERROR.value: "red",
    }
    console.rule(f"trace {trace_id}")
    for ev in events:
        c = kind_color.get(ev["kind"], "white")
        head = f"[{c}]#{ev['seq']:02d}  {ev['kind']}[/]"
        body = json.dumps(ev["payload"], ensure_ascii=False, indent=2, default=str)
        # 控制每条事件最大输出
        if len(body) > 1500:
            body = body[:1500] + "\n...[truncated]"
        console.print(Panel(body, title=head, border_style=c))


# ---- mcp -----------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve(config: str | None = typer.Option(None, "--config", "-c")):
    """以 stdio 模式启动 MCP 服务器。"""
    from kyagent.mcp.server import main as serve_main
    if config:
        import os
        os.environ["KYAGENT_CONFIG"] = config
    serve_main()


# ---- web -----------------------------------------------------------------


@web_app.command("serve")
def web_serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="监听地址"),
    port: int = typer.Option(8000, "--port", "-p", help="监听端口"),
    config: str | None = typer.Option(None, "--config", "-c"),
    reload: bool = typer.Option(False, "--reload", help="开发态自动重载"),
):
    """启动 B/S 架构 Web 服务（FastAPI + uvicorn）。

    需要安装 [web] extra：``pip install -e .[web]``
    """
    try:
        import uvicorn  # type: ignore
    except ImportError:
        console.print("[red]缺少依赖：请先 [bold]pip install -e .[web][/]")
        raise typer.Exit(code=1)
    from kyagent.web.security import UnsafeBindError, ensure_safe_bind
    try:
        ensure_safe_bind(host)
    except UnsafeBindError as exc:
        console.print(f"[red]拒绝启动：{exc}[/]")
        raise typer.Exit(code=2) from exc
    from kyagent.web.server import build_app
    cfg = load_config(config)
    app_obj = build_app(cfg)
    console.print(Panel.fit(
        f"[bold cyan]kyagent web[/] 已就绪\n"
        f"  监听         : http://{host}:{port}\n"
        f"  LLM 后端     : [bold]{cfg.agent.llm_backend}[/]\n"
        f"  执行账户     : [bold]{cfg.executor.account}[/]\n"
        f"  审计 DB      : {cfg.resolve(cfg.audit.database)}\n"
        f"  打开浏览器访问 / 即可看到前端",
        title="kyagent · web", border_style="cyan",
    ))
    uvicorn.run(app_obj, host=host, port=port, reload=reload, log_level="info")


# ---- 默认子命令：直接 chat -----------------------------------------------


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        chat()


if __name__ == "__main__":
    app()
