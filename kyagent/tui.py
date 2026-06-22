"""kyagent TUI v2 —— Claude-Code 风格的 per-message Panel + 流式思考 token。

设计要点
========
* **每条发言一个独立 Panel**：用户发言 border=green，agent 最终回答 border=blue。
  立即 ``console.print`` 进 scrollback，**不再放在 Live 区**。
* **流式思考**：``thinking_delta`` 事件按 token 追加 ``self._thinking_buffer``，
  Live 区里以 ``dim italic grey50`` 样式实时刷新（与白色最终回答形成显著对比）。
* **Live 只显示"还在变化的部分"**：当前 turn 的思考流 + 一行 status。
  turn 结束后 Live 退出 / transient 抹掉。
* **Ctrl+L 清屏**：prompt_toolkit KeyBindings。
* **ask_user_choice**：进入 ``prompt_user_choice`` 时 stop Live → 打印选项 Panel →
  prompt → restart Live。
* **LoongArch 兼容**：仅依赖 rich + prompt_toolkit（可选）。

并发
====
``handle_progress`` 可能从 worker 线程被调；所有内部状态变更 + Live.update()
统一锁在模块级 ``_LIVE_LOCK`` 下。
"""
from __future__ import annotations

import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from kyagent.agent.core import Agent
from kyagent.config import Config, load_config
from kyagent.confirm import ConfirmRequest
from kyagent.interactive import UserChoice, UserChoiceFn
from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import ToolRegistry
from kyagent.progress import ProgressCallback, ProgressEvent

# prompt_toolkit 是可选依赖。LoongArch 上若安装失败，回退到 input()，
# 同时 Ctrl+L 清屏快捷键也只能跳过。
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    _HAS_PROMPT_TOOLKIT = True
except Exception:  # pragma: no cover - 缺包路径
    PromptSession = None  # type: ignore[assignment]
    KeyBindings = None  # type: ignore[assignment]
    patch_stdout = None  # type: ignore[assignment]
    _HAS_PROMPT_TOOLKIT = False


# Live.update / 内部状态变更的统一锁。
_LIVE_LOCK = threading.Lock()

# 思考流在 Live 区里最多显示这么多字符（防止铺满屏幕）。
_THINKING_MAX_CHARS = 2000
_THINKING_TAIL_CHARS = 1500


# ---------------------------------------------------------------------------
# 顶层导出：供 cli.py 和 tests 复用的纯函数
# ---------------------------------------------------------------------------


def confirm_request_lines(req: ConfirmRequest) -> list[str]:
    """把 ConfirmRequest 摊平成可逐行渲染的 list[str]。"""
    lines = [f"标题: {req.title}", f"风险: {req.risk}"]
    if req.body:
        lines.append(f"详情: {req.body}")
    if req.summary_lines:
        lines.append("命中规则:")
        lines.extend(f"  - {s}" for s in req.summary_lines)
    return lines


def tool_rows(registry: ToolRegistry, cfg: Config) -> list[tuple[str, str, str, str, str]]:
    """生成 /tools 视图的数据行（不含表头）。"""
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
        on_user_choice: UserChoiceFn | None = None,
    ) -> "TuiSession":
        """构造 Agent + Session。

        ``on_user_choice`` 在 Agent.from_config 已支持时透传；若 Agent 还未
        接入这个 kwarg（兼容期），自动降级为不传，保持 TUI 主体可用。
        """
        try:
            agent = Agent.from_config(
                cfg,
                confirm=confirm,
                on_progress=on_progress,
                on_user_choice=on_user_choice,
            )
        except TypeError:
            # 兼容：Agent.from_config 尚未支持 on_user_choice。
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
# TuiApp：核心 UI
# ---------------------------------------------------------------------------


@dataclass
class _Status:
    label: str = "等待输入"
    spinner: str | None = None  # rich spinner name; None=纯文本
    tone: str = "dim"  # rich style

    def renderable(self) -> RenderableType:
        body = Text(self.label, style=self.tone)
        if self.spinner:
            return Spinner(self.spinner, text=body, style=self.tone)
        return body


_BANNER_HINT = "/tools  /audit  /reset  /exit   ·   Ctrl+L 清屏"


class TuiApp:
    """流式 TUI 主体。

    主流程：
        run() -> 循环：_collect_input()（Live 外） -> _run_turn()（Live 内）。

    Live 只在 turn 期间持有；prompt / choice 永远在 Live 之外执行。
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

        # turn 内变化的状态
        self._status = _Status(label="等待输入", spinner=None, tone="dim")
        self._thinking_buffer: list[str] = []  # 用 list 拼接比 str+= 快
        self._todo_items: list[dict[str, Any]] = []
        self._todo_plan_id = ""
        self._todo_revision = -1
        self._live: Any = None  # rich.live.Live | None；仅在 turn 期间非空
        self._pending_final: str | None = None  # 由 agent_final 事件递交给 _run_turn

        self._prompt_session = self._build_prompt_session()

    # -- prompt_toolkit 构造 -----------------------------------------------

    def _build_prompt_session(self) -> Any:
        if not _HAS_PROMPT_TOOLKIT:
            return None
        kb = KeyBindings()

        @kb.add("c-l")
        def _clear(event: Any) -> None:  # pragma: no cover - 交互快捷键
            try:
                self.console.clear()
            except Exception:
                pass
            try:
                event.app.invalidate()
            except Exception:
                pass

        return PromptSession(key_bindings=kb)

    # -- on_progress 适配 ---------------------------------------------------

    @property
    def on_progress(self) -> ProgressCallback:
        return self.handle_progress

    def on_user_choice_fn(self) -> UserChoiceFn:
        """注入到 Agent 的 user-choice 回调。"""
        return self.prompt_user_choice

    def handle_progress(self, event: ProgressEvent) -> None:
        """线程安全：可能从 worker 线程调用。"""
        with _LIVE_LOCK:
            self._apply_event(event)
            self._refresh_live()

    def _apply_event(self, event: ProgressEvent) -> None:
        kind = event.kind
        meta = event.meta or {}

        if kind == "agent_start":
            # 新 turn：清空思考 buffer。
            self._thinking_buffer.clear()
            self._todo_items.clear()
            self._todo_plan_id = ""
            self._todo_revision = -1
            self._status = _Status(label="🧠 思考中...", spinner="dots", tone="cyan")
            return

        if kind == "thinking_start":
            it = meta.get("iteration")
            label = "🧠 思考中..." if it is None else f"🧠 思考中... (round {it})"
            self._status = _Status(label=label, spinner="dots", tone="cyan")
            return

        if kind in {"thinking_delta", "reasoning_delta"}:
            if event.delta:
                self._thinking_buffer.append(event.delta)
            return

        if kind == "thinking_end":
            # 不清空 buffer —— 多轮工具调用之间思考会接续。
            self._status = _Status(label="🧠 整理思路...", spinner="dots", tone="cyan")
            return

        if kind == "plan_start":
            plan_id = str(meta.get("plan_id") or "")
            if plan_id and plan_id != self._todo_plan_id:
                self._todo_plan_id = plan_id
                self._todo_revision = -1
                self._todo_items.clear()
            return

        if kind == "todo_snapshot":
            plan_id = str(meta.get("plan_id") or "")
            try:
                revision = int(meta.get("revision", -1))
            except (TypeError, ValueError):
                return
            if not plan_id or revision < 0:
                return
            if self._todo_plan_id and plan_id != self._todo_plan_id:
                return
            if not self._todo_plan_id:
                self._todo_plan_id = plan_id
            if revision <= self._todo_revision:
                return
            items = meta.get("items")
            if isinstance(items, list):
                self._todo_revision = revision
                self._todo_items = [item for item in items if isinstance(item, dict)]
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
                self._status = _Status(
                    label=f"⚠️ {tool} 未通过", spinner=None, tone="yellow"
                )
            return

        if kind == "user_choice":
            # 真正的交互由 prompt_user_choice 同步处理；这里只更新 status。
            self._status = _Status(label="🤔 等待你的选择...", spinner=None, tone="yellow")
            return

        if kind == "agent_final":
            # 把最终回答暂存，由 _run_turn 主线程在 Live 外打印（保证顺序）。
            self._pending_final = event.text or ""
            self._status = _Status(label="✅ 已完成", spinner=None, tone="green")
            return

        if kind == "error":
            reason = meta.get("reason") or "error"
            detail = _clip(event.text or "", 80)
            self._status = _Status(
                label=f"❌ {reason}: {detail}", spinner=None, tone="red"
            )
            return

        # 未知 kind：保持现状。

    # -- Live 渲染 ---------------------------------------------------------

    def _refresh_live(self) -> None:
        live = self._live
        if live is None:
            return
        try:
            live.update(self._render_live())
        except Exception:
            pass

    def _render_live(self) -> RenderableType:
        """Live 区只显示"还在变化的部分"：思考流 + status。"""
        chunks: list[RenderableType] = []

        thinking_text = "".join(self._thinking_buffer)
        if thinking_text:
            shown = thinking_text
            if len(shown) > _THINKING_MAX_CHARS:
                shown = "…" + shown[-_THINKING_TAIL_CHARS:]
            chunks.append(
                Panel(
                    Text(shown, style="dim italic grey50"),
                    title="思考",
                    border_style="grey30",
                    padding=(0, 1),
                )
            )

        if self._todo_items:
            rows = Table.grid(padding=(0, 1))
            rows.add_column(width=2)
            rows.add_column(overflow="fold")
            icons = {
                "completed": "✓", "in_progress": "●", "cancelled": "–",
                "failed": "!", "pending": "○",
            }
            for item in self._todo_items:
                status = str(item.get("status") or "pending")
                rows.add_row(icons.get(status, "○"), str(item.get("content") or ""))
            chunks.append(Panel(rows, title="本轮待办", border_style="grey30", padding=(0, 1)))

        chunks.append(self._status.renderable())
        return Group(*chunks)

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
            # 立即把用户发言打成 Panel 进 scrollback
            self._print_user_panel(text)
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
            self.console.print("[dim]上下文已清空[/]")
            return True
        if text == "/clear":
            try:
                self.console.clear()
            except Exception:
                pass
            return True
        return False

    def _run_turn(self, text: str) -> None:
        """Live 包裹下执行 session.ask；思考流在 Live 区滚动。"""
        from rich.live import Live

        with _LIVE_LOCK:
            self._status = _Status(label="🧠 思考中...", spinner="dots", tone="cyan")
            self._thinking_buffer.clear()
            self._pending_final = None

        # transient=True：Live 退出时把思考区从屏幕擦除，只把固化 Panel
        # （用户 / kyagent 发言）留在终端历史。
        with Live(
            self._render_live(),
            console=self.console,
            refresh_per_second=12,
            transient=True,
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
                    self._live = None
                # Live 退出后再打印错误 Panel
                final_text = f"(运行失败：{e})"
                self._print_agent_panel(final_text)
                return
            finally:
                with _LIVE_LOCK:
                    self._live = None

        # Live 已退出 —— 打印最终回答 Panel。
        # 优先用 agent_final 事件递交的文本；否则用 ask() 返回值兜底。
        final_text = self._pending_final
        if final_text is None:
            final_text = result.final_text or "(空)"
        self._print_agent_panel(final_text)

    # -- 输入采集 -----------------------------------------------------------

    def _collect_input(self) -> str | None:
        msg = "你> "
        if self._prompt_session is not None and patch_stdout is not None:
            with patch_stdout(raw=True):
                return self._prompt_session.prompt(msg)
        try:
            return input(msg)
        except EOFError:
            return None

    def _raw_prompt(self, msg: str) -> str | None:
        """无 Live 的同步 prompt（供 prompt_user_choice 用）。"""
        if self._prompt_session is not None and patch_stdout is not None:
            try:
                with patch_stdout(raw=True):
                    return self._prompt_session.prompt(msg)
            except (EOFError, KeyboardInterrupt):
                return None
        try:
            return input(msg)
        except (EOFError, KeyboardInterrupt):
            return None

    # -- 用户选择 -----------------------------------------------------------

    def prompt_user_choice(self, choice: UserChoice) -> str:
        """Agent 主动让用户从一组选项中挑一个。

        Live 在 _run_turn 内可能是活动的；进入这里前先 stop，结束后 start。
        """
        live = self._live
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass
        try:
            options = list(choice.options or [])
            option_lines: list[RenderableType] = []
            for i, o in enumerate(options):
                line = Text.assemble(
                    Text(f"  {i + 1}. ", style="bold cyan"),
                    Text(o.label, style="white"),
                )
                if o.value and o.value != o.label:
                    line.append("  ")
                    line.append(Text(f"({o.value})", style="dim"))
                option_lines.append(line)
                if o.description:
                    option_lines.append(Text(f"     {o.description}", style="dim italic"))

            body = Group(
                Text(choice.question or "请选择", style="bold yellow"),
                Text(""),
                *option_lines,
            )
            self.console.print(
                Panel(
                    body,
                    title="🤔 请选择",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            ans_raw = self._raw_prompt("你的选择（输入序号或 value，回车取消）> ")
            ans = (ans_raw or "").strip()
            if not ans:
                return ""
            if ans.isdigit():
                idx = int(ans) - 1
                if 0 <= idx < len(options):
                    return options[idx].value
                return ""
            for o in options:
                if ans == o.value or ans.lower() == (o.label or "").lower():
                    return o.value
            return ""
        finally:
            if live is not None:
                try:
                    live.start(refresh=True)
                except Exception:
                    pass

    # -- confirm（被 cli.py 注入）------------------------------------------

    def confirm(self, req: ConfirmRequest) -> bool:
        """非 Live 阶段使用的 confirm 渲染器。"""
        live = self._live
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass
        try:
            self.console.print(
                Panel(
                    Text("\n".join(confirm_request_lines(req))),
                    title="需要确认",
                    border_style="yellow",
                )
            )
            ans = self._raw_prompt("放行？(y/N) ")
            return (ans or "").strip().lower() == "y"
        finally:
            if live is not None:
                try:
                    live.start(refresh=True)
                except Exception:
                    pass

    # -- 元命令 -------------------------------------------------------------

    def print_tools(self) -> None:
        cfg = self.session.agent.cfg if hasattr(self.session.agent, "cfg") else None
        registry = getattr(self.session.agent, "registry", None) or default_registry()
        if cfg is None:
            class _FakeCfg:
                class mcp:  # noqa: D401 - 占位
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
        cfg = getattr(self.session.agent, "cfg", None)
        if cfg is None:
            self.console.print("[dim]无法读取 audit（agent.cfg 未暴露）[/]")
            return
        try:
            from kyagent.runtime import build_audit_store
            store = build_audit_store(cfg)
            try:
                rows = store.list_traces(limit=10)
            finally:
                store.close()
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

    # -- 每条发言独立 Panel ------------------------------------------------

    def _print_user_panel(self, text: str) -> None:
        self.console.print(
            Panel(
                Text(text, style="white"),
                title="你",
                border_style="green",
                padding=(0, 1),
            )
        )

    def _print_agent_panel(self, text: str) -> None:
        self.console.print(
            Panel(
                Text(text or "(空)", style="white"),
                title=f"kyagent  [dim]({self.backend_name})[/]",
                border_style="blue",
                padding=(0, 1),
            )
        )

    def _print_welcome(self) -> None:
        body = Text.assemble(
            Text("kyagent", style="bold cyan"),
            Text("  ·  ", style="dim"),
            Text(_BANNER_HINT, style="dim"),
            Text("\n"),
            Text("backend: ", style="dim"),
            Text(self.backend_name, style="bold"),
        )
        self.console.print(
            Panel(body, title="欢迎", border_style="cyan", padding=(0, 1))
        )


# ---------------------------------------------------------------------------
# run_tui 入口（被 cli.py 引用）
# ---------------------------------------------------------------------------


def run_tui(config: str | None = None, user: str = "tui") -> None:
    cfg = load_config(config)
    console = Console(force_terminal=True, legacy_windows=False)

    # 占位 confirm —— cli 会用 _cli_confirm 覆盖；这里只是兜底。
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

    # forwarder：先构造 session（需要 callback），再回填 app.on_progress。
    progress_forwarder: dict[str, ProgressCallback] = {}
    choice_forwarder: dict[str, UserChoiceFn] = {}

    def _forward_progress(event: ProgressEvent) -> None:
        cb = progress_forwarder.get("cb")
        if cb is not None:
            cb(event)

    def _forward_choice(choice: UserChoice) -> str:
        cb = choice_forwarder.get("cb")
        if cb is None:
            return ""
        return cb(choice)

    session = TuiSession.from_config(
        cfg,
        user=user,
        confirm=_placeholder_confirm,
        on_progress=_forward_progress,
        on_user_choice=_forward_choice,
    )
    app = TuiApp(
        session=session,
        console=console,
        backend_name=getattr(session.agent.llm, "name", "?"),
    )
    progress_forwarder["cb"] = app.on_progress
    choice_forwarder["cb"] = app.on_user_choice_fn()
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
