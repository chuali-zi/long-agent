"""kyagent 命令行入口。

子命令：
  chat               进入交互式对话（默认）
  ask <text>         单轮提问
  tools list         列出所有已注册工具
  safety test <cmd>  在不执行的情况下让安全护栏判定 cmd
  audit list         最近 trace 概览
  audit show <id>    打印某条 trace 的完整推理链
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
from kyagent.audit.store import AuditStore
from kyagent.audit.trace import EventKind
from kyagent.config import load_config
from kyagent.mcp.tools import default_registry
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.policy import Decision

app = typer.Typer(no_args_is_help=False, add_completion=False, help="kyagent — 麒麟安全运维 Agent CLI")
tools_app = typer.Typer(help="工具相关")
safety_app = typer.Typer(help="安全护栏调试")
audit_app = typer.Typer(help="审计日志")
mcp_app = typer.Typer(help="MCP 协议")
app.add_typer(tools_app, name="tools")
app.add_typer(safety_app, name="safety")
app.add_typer(audit_app, name="audit")
app.add_typer(mcp_app, name="mcp")

console = Console()


# ---- 交互回调：在 CLI 里弹出 confirm ---------------------------------------


def _cli_confirm(tool_name: str, argv: list[str], verdict: dict) -> bool:
    console.rule(f"[yellow]需要用户确认 — {tool_name}[/yellow]")
    console.print(Panel.fit(
        f"[bold]风险等级[/]: {verdict['risk']}\n"
        f"[bold]待执行 argv[/]: {' '.join(argv)}\n"
        f"[bold]命中规则[/]:\n" + "\n".join(
            f"  - {h['rule_id']} ({h['risk']}): {h['description']}"
            for h in verdict["hits"]
        ),
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
    console.print(Panel.fit(
        f"[bold cyan]kyagent[/] 已就绪\n"
        f"  LLM 后端     : [bold]{agent.llm.name}[/]\n"
        f"  执行账户     : [bold]{cfg.executor.account}[/]\n"
        f"  已注册工具   : {len(agent.registry.all())}\n"
        f"  审计 DB      : {cfg.resolve(cfg.audit.database)}\n"
        f"  输入 [bold]/exit[/] 退出，[bold]/audit[/] 看本轮 trace",
        title="kyagent", border_style="cyan",
    ))

    last_trace_id: str | None = None
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


# ---- tools ----------------------------------------------------------------


@tools_app.command("list")
def tools_list(config: str | None = typer.Option(None, "--config", "-c")):
    """列出所有已注册工具与风险等级。"""
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
    cmdline: str = typer.Argument(..., help="待检测的 shell 命令字符串"),
    config: str | None = typer.Option(None, "--config", "-c"),
):
    """让安全护栏对 cmdline 出具裁决（不真正执行）。"""
    cfg = load_config(config)
    guardrail = Guardrail.from_config(cfg)
    verdict = guardrail.check_cmdline(cmdline)

    color = {"low": "green", "medium": "yellow", "high": "red", "critical": "bold red"}[verdict.risk.value]
    decision_color = {"allow": "green", "confirm": "yellow", "deny": "bold red"}[verdict.decision.value]

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


# ---- audit ---------------------------------------------------------------


@audit_app.command("list")
def audit_list(
    limit: int = typer.Option(20, "--limit", "-n"),
    config: str | None = typer.Option(None, "--config", "-c"),
):
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


def _print_trace(cfg, trace_id: str) -> None:
    store = AuditStore(cfg.resolve(cfg.audit.database))
    events = store.get_events(trace_id)
    if not events:
        console.print(f"[red]找不到 trace {trace_id}[/]")
        return
    kind_color = {
        EventKind.USER_INPUT.value: "bold green",
        EventKind.PERCEPTION.value: "cyan",
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


# ---- 默认子命令：直接 chat -----------------------------------------------


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        chat()


if __name__ == "__main__":
    app()
