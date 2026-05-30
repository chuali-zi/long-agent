"""Claude-Code / OpenCode / Codex 风格的流式 TUI。

设计要点
========
* 用户只看见两类东西：自己的输入、agent 的最终回答。
* agent 在跑工具或思考时，底部一条 status 行用 rich.live.Live 实时刷新，
  显示 "🧠 思考中..." / "🔧 调用 lsof_port  argv: lsof -nP -i TCP:80"。
* 不画 trace timeline、不展示 kind/summary 堆栈——那种"调试视图"留给
  `kyagent audit show <id>` CLI 命令。
* 进度事件契约见 kyagent.progress：ProgressEvent(kind, text, tool, argv, meta)。

依赖
====
* `rich`：必备（pyproject 主依赖，纯 Python，LoongArch 友好）。
* `prompt_toolkit`：可选；ImportError 时回退到内置 input()。

并发
====
`TuiApp.handle_progress` 可能从 worker 线程被调用（POSIX 并行执行 allow-only
工具时）。所有内部状态变更 + live.update() 调用都包在模块级 `_LIVE_LOCK` 里。
"""
from __future__ import annotations

import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from kyagent.agent.core import Agent
from kyagent.audit.store import AuditStore
from kyagent.config import Config, load_config
from kyagent.confirm import ConfirmRequest
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.progress import ProgressCallback, ProgressEvent

# prompt_toolkit 是可选依赖。LoongArch 上若安装失败，回退到 input()。
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    _HAS_PROMPT_TOOLKIT = True
except Exception:  # pragma: no cover - 缺包路径
    PromptSession = None  # type: ignore[assignment]
    patch_stdout = None  # type: ignore[assignment]
    _HAS_PROMPT_TOOLKIT = False


# Live.update / 内部状态变更的统一锁。模块级，保证 worker 线程和主线程互斥。
_LIVE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 顶层导出 1/2：供 cli.py 和 tests 复用的纯函数
# ---------------------------------------------------------------------------


def confirm_request_lines(req: ConfirmRequest) -> list[str]:
    """把 ConfirmRequest 摊平成可逐行渲染的 list[str]。

    被 cli._cli_confirm 和 tests/test_tui.py 引用，签名稳定，不要乱动。
    """
    lines = [f"标题: {req.title}", f"风险: {req.risk}"]
    if req.body:
        lines.append(f"详情: {req.body}")
    if req.summary_lines:
        lines.append("命中规则:")
        lines.extend(f"  - {s}" for s in req.summary_lines)
    return lines


def tool_rows(registry: ToolRegistry, cfg: Config) -> list[tuple[str, str, str, str, str]]:
    """生成 /tools 视图的数据行（不含表头）。

    返回 (name, risk, root?, read_only?, description_first_line)。
    """
    enable = set(cfg.mcp.enable_tools or [])
    rows: list[tuple[str, str, str, str, str]] = []
    for tool in registry.all():
        if enable and tool.name not in enable:
            continue
        rows.append(
            (
                tool.name,
                tool.risk_level.value,
                "yes" if tool.requires_root else "no",
                "yes" if tool.read_only else "no",
                (tool.description or "").split("\n", 1)[0],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# TuiSession：把 Agent 包成一个"问一句、得一答"的小会话
# ---------------------------------------------------------------------------


@dataclass
class _AskResult:
    """run_turn 返回值，对 TuiApp 而言只关心 final_text 和 trace_id。"""

    final_text: str
    trace_id: str
    iterations: int = 0
    denied: bool = False
    notes: list[str] = field(default_factory=list)


class TuiSession:
    """对 Agent 的轻封装：保留上下文、给上层一个 ask()/reset() 接口。"""

    def __init__(self, agent: Agent, user: str = "tui") -> None:
        self.agent = agent
        self.user = user
        self.last_trace_id: str | None = None

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        user: str = "tui",
        confirm: Callable[[ConfirmRequest], bool],
        on_progress: ProgressCallback | None = None,
    ) -> "TuiSession":
        # NOTE: Agent.from_config 的 on_progress kwarg 由另一个 agent 接入。
        # 如果当前实现还没接 on_progress，这里会 TypeError——这正是兜底信号。
        agent = Agent.from_config(cfg, confirm=confirm, on_progress=on_progress)
        return cls(agent=agent, user=user)

    def ask(self, text: str) -> _AskResult:
        result = self.agent.ask(text, user=self.user)
        self.last_trace_id = result.trace.trace_id
        return _AskResult(
            final_text=result.final_text or "",
            trace_id=result.trace.trace_id,
            iterations=getattr(result, "tool_iterations", 0),
            denied=getattr(result, "denied", False),
            notes=list(getattr(result, "notes", []) or []),
        )

    def reset(self) -> None:
        try:
            self.agent.messages.clear()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TuiApp：核心 UI 状态机
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    role: str  # "你" / "kyagent"
    text: str


# status 显示的当前阶段。
@dataclass
class _Status:
    label: str = "等待输入"
    spinner: str | None = None  # rich spinner name; None=纯文本
    tone: str = "dim"  # rich style

    def renderable(self) -> RenderableType:
        body = Text(self.label, style=self.tone)
        if self.spinner:
            return Group(Spinner(self.spinner, text=body, style=self.tone))
        return body


_BANNER = (
    "[bold cyan]kyagent[/]  ·  [dim]/tools  /audit  /reset  /exit[/]"
)


class TuiApp:
    """流式 TUI 主体。

    主流程：
        run() -> 循环 -> _collect_input()（无 Live） -> _run_turn()（Live）。
    Live 只在 turn 期间持有；prompt 永远在 Live 之外执行，否则光标会乱跳。
    """

    def __init__(
        self,
        session: TuiSession,
        console: Console | None = None,
        *,
        backend_name: str = "?",
    ) -> None:
        self.session = session
        self.console = console or Console(force_terminal=True, legacy_windows=False)
        self.backend_name = backend_name

        self._history: list[_Turn] = []
        self._status = _Status(label="等待输入", spinner=None, tone="dim")
        self._live = None  # rich.live.Live | None；仅在 turn 期间非空
        self._prompt_session = (
            PromptSession() if _HAS_PROMPT_TOOLKIT else None
        )

    # -- on_progress 适配 ---------------------------------------------------

    @property
    def on_progress(self) -> ProgressCallback:
        """返回可以丢给 Agent 的 callback。"""
        return self.handle_progress

    def handle_progress(self, event: ProgressEvent) -> None:
        """把 ProgressEvent 翻译成底部 status 行。

        线程安全：可能从 worker 线程调用。
        """
        with _LIVE_LOCK:
            self._apply_event(event)
            self._refresh_live()

    def _apply_event(self, event: ProgressEvent) -> None:
        kind = event.kind
        meta = event.meta or {}

        if kind == "agent_start":
            self._status = _Status(label="🧠 思考中...", spinner="dots", tone="cyan")
            return

        if kind == "thinking_start":
            it = meta.get("iteration")
            label = "🧠 思考中..." if it is None else f"🧠 思考中... (round {it})"
            self._status = _Status(label=label, spinner="dots", tone="cyan")
            return

        if kind == "thinking_end":
            # 如果下一步要调工具，下一个 tool_call_start 会覆盖；否则保持思考态。
            preview = (event.text or "").strip().splitlines()[:1]
            preview_text = preview[0] if preview else ""
            if preview_text:
                preview_text = _clip(preview_text, 60)
                self._status = _Status(
                    label=f"💭 {preview_text}", spinner=None, tone="dim"
                )
            else:
                self._status = _Status(
                    label="🧠 整理思路...", spinner="dots", tone="cyan"
                )
            return

        if kind == "tool_call_start":
            tool = event.tool or "?"
            argv = list(event.argv or [])
            shown = argv[:6]
            argv_text = " ".join(shlex.quote(a) for a in shown)
            if len(argv) > 6:
                argv_text += " …"
            label = f"🔧 调用 {tool}"
            if argv_text:
                label += f"  argv: {argv_text}"
            self._status = _Status(label=label, spinner="dots", tone="yellow")
            return

        if kind == "tool_call_end":
            tool = event.tool or "?"
            ok = bool(meta.get("ok", True))
            if ok:
                self._status = _Status(label=f"✅ {tool} 完成", spinner=None, tone="green")
            else:
                self._status = _Status(label=f"⚠️ {tool} 未通过", spinner=None, tone="yellow")
            return

        if kind == "agent_final":
            # 最终答案进对话历史；status 回到 idle。
            self._history.append(_Turn(role="kyagent", text=event.text or ""))
            self._status = _Status(label="等待输入", spinner=None, tone="dim")
            return

        if kind == "error":
            reason = meta.get("reason") or "error"
            detail = _clip(event.text or "", 80)
            self._status = _Status(
                label=f"❌ {reason}: {detail}", spinner=None, tone="red"
            )
            return

        # 未知 kind：保持现状。

    def _refresh_live(self) -> None:
        live = self._live
        if live is None:
            return
        try:
            live.update(self._render())
        except Exception:
            # Live 已被关闭等情况，吞掉避免污染主流程。
            pass

    # -- 渲染 ---------------------------------------------------------------

    def _render(self) -> RenderableType:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="status", size=3),
        )
        layout["header"].update(self._render_header())
        layout["body"].update(self._render_history_panel())
        layout["status"].update(self._render_status_panel())
        return layout

    def _render_header(self) -> RenderableType:
        line = f"{_BANNER}\n[dim]backend:[/] [bold]{self.backend_name}[/]"
        return Panel(line, border_style="cyan", title="kyagent", padding=(0, 1))

    def _render_history_panel(self) -> RenderableType:
        if not self._history:
            body: RenderableType = Text(
                "（输入你的问题开始对话，例如：80 端口被谁占了）", style="dim"
            )
            return Panel(body, title="对话", border_style="blue", padding=(0, 1))

        chunks: list[RenderableType] = []
        for turn in self._history:
            tag_style = "bold green" if turn.role == "你" else "bold blue"
            tag = Text(f"{turn.role}> ", style=tag_style)
            # 用 Text() 包裹正文，避免 LLM 输出里的 [foo] 被 markup 吞掉
            body_text = Text(turn.text or "", style="white")
            chunks.append(Text.assemble(tag, body_text))
            chunks.append(Text(""))  # 空行
        return Panel(Group(*chunks), title="对话", border_style="blue", padding=(0, 1))

    def _render_status_panel(self) -> RenderableType:
        return Panel(
            self._status.renderable(),
            title="当前",
            border_style="magenta",
            padding=(0, 1),
        )

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> None:
        self._print_welcome()
        while True:
            try:
                text = self._collect_input()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]再见[/]")
                return
            if text is None:
                return
            text = text.strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                self.console.print("[dim]再见[/]")
                return
            if self._handle_meta(text):
                continue
            self._append_history("你", text)
            self._run_turn(text)

    def _handle_meta(self, text: str) -> bool:
        if text == "/tools":
            self.print_tools()
            return True
        if text == "/audit":
            self.print_last_audit()
            return True
        if text == "/reset":
            self.session.reset()
            self._history.clear()
            self.console.print("[dim]上下文已清空[/]")
            return True
        return False

    def _run_turn(self, text: str) -> None:
        """在 Live 包裹下执行 session.ask；进度事件由 handle_progress 推 UI。"""
        # 延迟 import 以避免无 Live 时引入开销
        from rich.live import Live

        with _LIVE_LOCK:
            self._status = _Status(label="🧠 思考中...", spinner="dots", tone="cyan")

        with Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        ) as live:
            with _LIVE_LOCK:
                self._live = live
            try:
                result = self.session.ask(text)
            except Exception as e:
                with _LIVE_LOCK:
                    self._status = _Status(
                        label=f"❌ run failed: {_clip(str(e), 80)}",
                        spinner=None,
                        tone="red",
                    )
                    self._refresh_live()
                self._append_history("kyagent", f"(运行失败：{e})")
                with _LIVE_LOCK:
                    self._live = None
                return
            else:
                # 若 agent 实现未触发 agent_final 事件，兜底补一条 history。
                if not self._history or self._history[-1].role != "kyagent":
                    self._append_history("kyagent", result.final_text or "(空)")
                with _LIVE_LOCK:
                    self._status = _Status(label="等待输入", spinner=None, tone="dim")
                    self._refresh_live()
            finally:
                with _LIVE_LOCK:
                    self._live = None

        # Live 外面再静态打印一次最终对话状态，固化在终端历史里。
        self.console.print(self._render_history_panel())

    # -- 输入采集 -----------------------------------------------------------

    def _collect_input(self) -> str | None:
        msg = "你> "
        if self._prompt_session is not None and patch_stdout is not None:
            with patch_stdout(raw=True):
                return self._prompt_session.prompt(msg)
        # 回退路径：纯 input()。
        try:
            return input(msg)
        except EOFError:
            return None

    # -- confirm（被 cli.py 注入到 Agent；当前由 cli 自己处理 prompt）-----

    def confirm(self, req: ConfirmRequest) -> bool:
        """非 Live 阶段使用的 confirm 渲染器。

        Live 在 _run_turn() 内是活动的；Agent.ask() 在 worker 线程调用 confirm
        前会受 _LIVE_LOCK 影响（实际项目里 confirm 走 cli 注入的 _cli_confirm）。
        这里保留一个降级版本，主要给外部测试 / 替代 UI 用。
        """
        self.console.print(Panel(
            Text("\n".join(confirm_request_lines(req))),
            title="需要确认",
            border_style="yellow",
        ))
        ans = input("放行？(y/N) ").strip().lower()
        return ans == "y"

    # -- 元命令实现 ---------------------------------------------------------

    def print_tools(self) -> None:
        cfg = self.session.agent.cfg if hasattr(self.session.agent, "cfg") else None
        registry = getattr(self.session.agent, "registry", None) or default_registry()
        if cfg is None:
            # 没拿到 cfg 也至少能列出 registry 自己
            class _FakeCfg:
                class mcp:  # noqa: D401 - 临时桩
                    enable_tools: list[str] = []

            rows = tool_rows(registry, _FakeCfg)  # type: ignore[arg-type]
        else:
            rows = tool_rows(registry, cfg)
        table = Table(title="kyagent 工具清单")
        table.add_column("name", style="cyan")
        table.add_column("risk")
        table.add_column("root?")
        table.add_column("read-only?")
        table.add_column("description")
        risk_style = {
            "low": "green",
            "medium": "yellow",
            "high": "red",
            "critical": "bold red",
        }
        for name, risk, root, ro, desc in rows:
            table.add_row(
                name,
                Text(risk, style=risk_style.get(risk, "white")),
                root,
                ro,
                Text(desc),
            )
        self.console.print(table)

    def print_last_audit(self) -> None:
        """轻量 audit：列出最近 N 条 trace_id；不再画 timeline。"""
        cfg = getattr(self.session.agent, "cfg", None)
        if cfg is None:
            self.console.print("[dim]无法读取 audit（agent.cfg 未暴露）[/]")
            return
        try:
            store = AuditStore(cfg.resolve(cfg.audit.database))
            rows = store.list_traces(limit=10)
        except Exception as e:
            self.console.print(f"[red]读取 audit 失败：{e}[/]")
            return
        if not rows:
            self.console.print("[dim]还没有 trace[/]")
            return
        table = Table(title="最近 trace（用 `kyagent audit show <id>` 看详情）")
        table.add_column("trace_id", style="cyan")
        table.add_column("user")
        table.add_column("started_at")
        from datetime import datetime
        for r in rows:
            ts = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
            table.add_row(r["trace_id"], r.get("user", "?"), ts)
        self.console.print(table)

    # -- 工具方法 -----------------------------------------------------------

    def _append_history(self, role: str, text: str) -> None:
        with _LIVE_LOCK:
            self._history.append(_Turn(role=role, text=text))
            self._refresh_live()

    def _print_welcome(self) -> None:
        self.console.print(
            Panel(
                f"{_BANNER}\n"
                f"[dim]backend:[/] [bold]{self.backend_name}[/]   "
                f"输入 [bold]/exit[/] 退出。",
                title="kyagent",
                border_style="cyan",
            )
        )


# ---------------------------------------------------------------------------
# 顶层导出 2/2：run_tui 入口（被 cli.py 引用）
# ---------------------------------------------------------------------------


def run_tui(config: str | None = None, user: str = "tui") -> None:
    """CLI 入口。从配置加载 Agent，构造 TuiApp，进入主循环。"""
    cfg = load_config(config)
    console = Console(force_terminal=True, legacy_windows=False)

    # confirm 占位：真正交互在 Live 外完成。先用 auto_deny 等价物，cli 后续替换。
    def _placeholder_confirm(req: ConfirmRequest) -> bool:
        console.print(
            Panel(
                Text("\n".join(confirm_request_lines(req))),
                title="需要确认",
                border_style="yellow",
            )
        )
        try:
            ans = input("放行？(y/N) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans == "y"

    # 先构造 app（拿到 on_progress），再让 session 把 callback 接进 agent。
    # 为此我们需要倒装：先建一个临时 session 占位，构造 app，再 hook on_progress。
    # 但 Agent.from_config 一次性吃 on_progress，所以这里直接用一个 forwarder。
    forwarder: dict[str, ProgressCallback] = {}

    def _forward(event: ProgressEvent) -> None:
        cb = forwarder.get("cb")
        if cb is not None:
            cb(event)

    session = TuiSession.from_config(
        cfg,
        user=user,
        confirm=_placeholder_confirm,
        on_progress=_forward,
    )
    app = TuiApp(
        session=session,
        console=console,
        backend_name=getattr(session.agent.llm, "name", "?"),
    )
    forwarder["cb"] = app.on_progress
    app.run()


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _clip(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


__all__ = [
    "TuiApp",
    "TuiSession",
    "confirm_request_lines",
    "tool_rows",
    "run_tui",
]
