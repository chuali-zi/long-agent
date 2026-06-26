"""Agent 主循环：接收 → 感知 → 推理 → 校验 → 执行 → 审计 闭环。

把所有子系统组合起来：LLM 决策 → Guardrail 二次过滤 → ExecutionProxy 落地 → AuditLogger 全链路。
"""
from __future__ import annotations

import sys
import threading
import json
import os
import posixpath
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from kyagent.agent.llm import (
    AssistantMessage,
    LlmBackend,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    build_backend,
)
from kyagent.agent.prompt import SYSTEM_PROMPT
from kyagent.agent.scope import (
    RemediationScope,
    extract_absolute_paths,
    file_remediation_checklist_applies,
    normalize_abs_path,
    parse_remediation_ports,
    path_is_within,
    process_remediation_checklist_applies,
)
from kyagent.agent.completion import assess_file_cleanup_completion
from kyagent.safety.write_preflight import categorize_cleanup_candidate
from kyagent.audit.logger import AuditLogger
from kyagent.audit.trace import EventKind, Trace
from kyagent.config import Config, load_config
from kyagent.executor.proxy import ExecutionProxy
from kyagent.mcp.tools.base import ToolError, ToolRegistry
from kyagent.progress import ProgressCallback, ProgressEvent, noop_progress
from kyagent.planner import PlanSnapshot, PlanStore, PlanTodoItem
from kyagent.todos import TodoService, TodoSnapshot
from kyagent.mcp.tools.pipeline import (
    PipelineError,
    check_safety,
    execute_and_format,
    prepare_call,
)
from kyagent.agent import confirm_adapter
from kyagent.confirm import ConfirmFn, auto_deny
from kyagent.interactive import (
    UserChoice,
    UserChoiceFn,
    UserChoiceOption,
    auto_cancel_choice,
)
from kyagent.runtime import build_runtime
from kyagent.rca import load_playbooks, validate_report
from kyagent.safety.guardrail import Guardrail
from kyagent.safety.intent import IntentGuard, IntentVerdict
from kyagent.safety.policy import Decision


# Confirm 回调：单参 ConfirmRequest → True 表示用户同意继续。
# 具体的 verdict → ConfirmRequest 翻译由各 Verdict 自己负责（to_confirm_request），
# Agent 与 UI 都不依赖具体 verdict 类型。
_auto_deny = auto_deny  # backward-compat 别名，旧引用不破


_OS_ENGLISH_QUESTION_RE = re.compile(
    r"\b("
    r"os|kylin|linux|systemd|service(?:s)?|daemon(?:s)?|"
    r"process(?:es)?|pid(?:s)?|cpu|memory|mem|disk(?:s)?|"
    r"filesystem(?:s)?|inode(?:s)?|mount(?:s)?|swap|load|uptime|"
    r"kernel(?:s)?|port(?:s)?|listen(?:ing)?|socket(?:s)?|"
    r"network|route(?:s)?|dns|log(?:s)?|journal|audit|selinux|"
    r"firewall|package(?:s)?|rpm|deb|nginx|sshd?|mysql|mariadb|"
    r"postgres|redis|docker"
    r")\b",
    re.IGNORECASE,
)
_OS_CHINESE_QUESTION_RE = re.compile(
    r"("
    r"系统|操作系统|麒麟|服务|进程|端口|监听|网络|磁盘|文件系统|"
    r"内存|负载|日志|防火墙|软件包|内核|挂载|审计|僵尸进程|"
    r"排查|重启|巡检"
    r")"
)
_OS_PERCEPTION_TOOL_PREFIXES = (
    "process_",
    "lsof_",
    "net_",
    "log_",
    "svc_",
    "fs_",
    "pkg_",
    "disk_",
    "sys_",
    "sec_",
    "compliance_",
    "loongarch_",
    "boot_",
)
_NON_PERCEPTION_READ_ONLY_TOOLS = {
    "ask_user_choice",
    "submit_rca_report",
    "todo_read",
}
_TODO_TOOL_NAMES = {"todo_read", "todo_write"}
_TODO_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": 500},
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
        },
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["content", "status", "priority"],
    "additionalProperties": False,
}
_TODO_TOOLS_FOR_LLM = [
    {
        "name": "todo_read",
        "description": "Read the current durable todo list for this agent turn.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "todo_write",
        "description": (
            "Create and maintain the current turn's todo list. Submit the complete updated list every time; "
            "the backend replaces the previous list atomically and keeps the given order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": _TODO_ITEM_SCHEMA,
                    "maxItems": 20,
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
]
_AUTO_APPROVE_FILE_REMEDIATION_TOOLS = {
    "fs_delete_file",
    "fs_truncate",
    "log_delete_file",
}
_AUTO_APPROVE_DEDICATED_REMEDIATION_TOOLS = {
    "lock_remove_stale",
    "unix_socket_remove_stale",
    "cron_d_disable",
    "log_dir_repair_permissions",
}
_AUTO_APPROVE_PROCESS_SIGNALS = {"TERM", "INT", "HUP"}
_DELETED_FILE_EVIDENCE_MARKERS = ("(deleted)", " deleted")
def _configured_auto_approve_runtime_roots() -> tuple[str, ...]:
    raw = os.environ.get("KYAGENT_AUTO_APPROVE_RUNTIME_ROOTS", "")
    if not raw.strip():
        return ()
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        if os.pathsep != ":" and ":" in chunk and not re.match(r"^[A-Za-z]:[\\/]", chunk):
            parts.extend(p.strip() for p in chunk.split(":") if p.strip())
        else:
            parts.append(chunk)
    return tuple(dict.fromkeys(parts))
_ACTION_INTENT_RE = re.compile(
    r"(结束|终止|杀|释放|处理|处置|清理|修复|"
    r"stop|kill|terminate|release|remediate|cleanup|clean up|resolve|"
    r"确认后结束|确认后终止)",
    re.IGNORECASE,
)
_SKIP_USER_TOKENS = frozenset(
    {
        "loadtest",
        "cpu",
        "report",
        "worker",
        "checkout",
        "预发",
        "测试",
        "机器",
        "盒子",
        "确认",
        "后",
        "结束",
        "它",
        "进程",
        "脚本",
        "异常",
        "高",
        "占用",
        "端口",
        "api",
        "http",
        "python",
        "bash",
        "sudo",
        "kyagent",
    }
)
_PROTECTED_PROCESS_MARKERS = (
    "orders-api",
    "billing-api",
    "inventory-api",
    "sshd",
    "systemd",
    "mysql",
    "mariadb",
    "postgres",
    "redis",
)
_PORT_RE = re.compile(r"(?<!\d)([1-9][0-9]{1,4})(?!\d)")
_FILE_REMEDIATION_READ_TOOLS = {
    "fs_find",
    "fs_ls",
    "dir_largest_files",
    "log_files_top",
    "log_rotated_count",
    "file_cleanup_candidates",
}
_FILE_MUTATION_TOOLS = frozenset({
    "fs_delete_file",
    "fs_truncate",
    "log_delete_file",
})


@dataclass
class _FileRemediationChecklist:
    """Internal candidate/execute/verify state for file cleanup turns."""

    scope: RemediationScope
    required_roots: tuple[str, ...] = ()
    scanned_roots: set[str] = field(default_factory=set)
    candidate_paths: set[str] = field(default_factory=set)
    candidate_labels: dict[str, str] = field(default_factory=dict)
    protected_paths: set[str] = field(default_factory=set)
    executed_paths: list[str] = field(default_factory=list)
    verified_paths: set[str] = field(default_factory=set)
    explicit_file_targets: set[str] = field(default_factory=set)

    @classmethod
    def from_user_text(cls, text: str) -> "_FileRemediationChecklist":
        return cls.from_scope(RemediationScope.from_user_text(text))

    @classmethod
    def from_scope(cls, scope: RemediationScope) -> "_FileRemediationChecklist":
        roots = scope.search_roots(round=1)
        return cls(
            scope=scope,
            required_roots=tuple(roots),
            explicit_file_targets=set(scope.explicit_root_storage_files()),
        )

    def inherit_discovery_from(self, previous: "_FileRemediationChecklist") -> None:
        """Carry read-only discovery into a short follow-up cleanup turn.

        Web sessions commonly split cleanup into two turns: first discover and
        classify candidates, then the user replies with a short "clean them".
        The second turn may not repeat service/root names, but the prior
        read-only evidence is still the active safety context for this session.
        """
        if not self.required_roots:
            self.required_roots = tuple(previous.required_roots)
        self.scanned_roots.update(previous.scanned_roots)
        self.candidate_paths.update(previous.candidate_paths)
        self.candidate_labels.update(previous.candidate_labels)
        self.protected_paths.update(previous.protected_paths)
        self.executed_paths.extend(previous.executed_paths)
        self.verified_paths.update(previous.verified_paths)
        self.explicit_file_targets.update(previous.explicit_file_targets)

    def record_read_result(self, tool_name: str, args: dict, content: str) -> None:
        if tool_name not in _FILE_REMEDIATION_READ_TOOLS:
            return
        paths = set(extract_absolute_paths(content))
        path_arg = normalize_abs_path(str(args.get("path") or args.get("root") or ""))
        if tool_name == "fs_ls" and path_arg:
            if path_arg in self.explicit_file_targets:
                paths.add(path_arg)
            paths.update(_extract_ls_children(path_arg, content))
        if tool_name == "file_cleanup_candidates":
            self._record_discovery_candidates(content)
        else:
            for path in paths:
                self._register_candidate(path)

        for root in self.required_roots:
            if self._read_broadly_scans_root(tool_name, args, path_arg, paths, root):
                self.scanned_roots.add(root)

        if not self.executed_paths:
            return
        for target in self.executed_paths:
            if self._read_verifies_target(tool_name, path_arg, paths, target):
                self.verified_paths.add(target)

    def _register_candidate(self, path: str) -> None:
        target = normalize_abs_path(path)
        if not target:
            return
        self.candidate_paths.add(target)
        facts = categorize_cleanup_candidate(target)
        if facts.category_guess in {"audit", "current-log", "db-log"}:
            self._label_candidate(target, "protect")
        elif facts.category_guess in {"rotated-log", "cache", "tmp", "core"}:
            self._label_candidate(target, "delete")
        elif target not in self.candidate_labels:
            self.candidate_labels[target] = "unknown"

    def _record_discovery_candidates(self, content: str) -> None:
        for line in (content or "").splitlines():
            if "  markers=" not in line and "\t" not in line:
                continue
            match = re.search(r"(/[^\s]+)\s+markers=", line)
            if match:
                self._register_candidate(match.group(1))
                continue
            parts = line.split()
            for token in parts:
                if token.startswith("/"):
                    self._register_candidate(token.rstrip(","))
                    break

    def _label_candidate(self, path: str, label: str) -> None:
        target = normalize_abs_path(path)
        if not target:
            return
        self.candidate_paths.add(target)
        self.candidate_labels[target] = label
        if label == "protect":
            self.protected_paths.add(target)

    def pre_write_error(self, path: str, user_text: str) -> str:
        target = normalize_abs_path(path)
        if not target:
            return ""
        if target in self.protected_paths or self.candidate_labels.get(target) == "protect":
            return (
                "file cleanup checklist blocked this write: target is marked protect. "
                f"Do not delete/truncate protected evidence: {target}"
            )
        label = self.candidate_labels.get(target)
        if label == "unknown":
            return (
                "file cleanup checklist blocked this write: candidate disposition is unknown. "
                "Label it delete or protect based on discovery facts before acting: "
                f"{target}"
            )
        relevant_roots = [
            root for root in self.required_roots
            if path_is_within(target, root)
        ]
        missing_roots = [root for root in self.required_roots if root not in self.scanned_roots]
        if relevant_roots and missing_roots:
            return (
                "file cleanup checklist blocked this write: candidate roots are incomplete. "
                "Before deleting/truncating, enumerate these roots with read-only tools: "
                + ", ".join(missing_roots)
            )
        if target in self.candidate_paths and self.candidate_labels.get(target) == "delete":
            return ""
        if target in self.candidate_paths and self.candidate_labels.get(target) != "unknown":
            return ""
        if target in self.explicit_file_targets:
            return (
                "file cleanup checklist blocked this write: explicit root-level target "
                "has not been confirmed by read-only evidence. First enumerate the exact "
                f"file with fs_ls, then retry: {target}"
            )
        if target in extract_absolute_paths(user_text):
            return ""
        if not self.scope.path_in_scope(target):
            return (
                "file cleanup checklist blocked this write: target is not in current scope. "
                f"Report it as 不在本次范围 instead of acting: {target}"
            )
        return (
            "file cleanup checklist blocked this write: target was not present in the "
            "candidate list from prior read-only evidence. First enumerate the exact file "
            f"with file_cleanup_candidates/fs_ls/dir_largest_files/log_files_top, then retry: {target}"
        )

    def record_write_result(self, path: str, ok: bool) -> None:
        target = normalize_abs_path(path)
        if ok and target:
            self.executed_paths.append(target)
            self.candidate_labels[target] = "delete"

    def pre_scan_error(self) -> str:
        if not self.required_roots:
            return ""
        missing = [root for root in self.required_roots if root not in self.scanned_roots]
        if not missing:
            return ""
        return (
            "file cleanup checklist blocked final response: required roots not yet enumerated. "
            "Scan these directories with read-only tools before final response: "
            + ", ".join(missing)
        )

    def final_error(self) -> str:
        pending = [path for path in self.executed_paths if path not in self.verified_paths]
        if not pending:
            return ""
        verify_dirs = sorted({_verify_dir_for_path(path) for path in pending})
        return (
            "file cleanup checklist blocked final response: execution list has unverified "
            "changes. Re-scan these directories with read-only tools and confirm the cleanup "
            "state before final response: "
            + ", ".join(verify_dirs)
        )

    def completion_report(self) -> str:
        report = assess_file_cleanup_completion(
            self.scope,
            required_roots=self.required_roots,
            scanned_roots=self.scanned_roots,
            candidate_paths=self.candidate_paths,
            executed_paths=self.executed_paths,
            verified_paths=self.verified_paths,
            protected_paths=self.protected_paths,
        )
        return report.summary()

    @staticmethod
    def _read_verifies_target(tool_name: str, path_arg: str, paths: set[str], target: str) -> bool:
        parent = posixpath.dirname(target)
        # 一次扫描父目录或任意祖先目录，即可复核该 target 的当前状态（仍在 / 删后已不在）。
        # 删除成功后文件本身不会再出现在结果里，所以"扫到包含它的目录"就是有效复核，
        # 这里的包含关系必须是 target 在 path_arg 之内，而非反向。
        if path_arg and (path_arg == parent or path_is_within(target, path_arg)):
            return True
        if tool_name in {"log_files_top", "log_rotated_count", "file_cleanup_candidates"} and target.startswith("/var/log/"):
            return True
        return any(path_is_within(path, parent) for path in paths)

    @staticmethod
    def _read_broadly_scans_root(
        tool_name: str,
        args: dict,
        path_arg: str,
        paths: set[str],
        root: str,
    ) -> bool:
        if tool_name == "fs_ls":
            return path_arg == root
        if tool_name == "fs_find":
            return path_arg == root and not args.get("name")
        if tool_name == "dir_largest_files":
            return path_arg == root
        if tool_name == "file_cleanup_candidates":
            return normalize_abs_path(str(args.get("root") or "")) == root
        if tool_name in {"log_files_top", "log_rotated_count"}:
            return root.startswith("/var/log/") and any(path_is_within(path, root) for path in paths)
        return False


_SS_PORT_RE = re.compile(r":(\d{2,5})\b")


@dataclass
class _ProcessRemediationChecklist:
    """端口/进程终止类任务的「收尾自检」状态。

    判分点是「目标端口最终被释放」，而非「调用过一次 kill」。本 checklist 在 Agent
    要给最终回答前，要求对每个目标端口做一次 *kill 之后* 的只读复核（lsof_port 报无
    占用，或 net_listen 不再有该端口的 LISTEN），否则拦回继续。这样可挡住两类失败：
      1) 长 prompt 里漏做端口收尾；
      2) 杀掉了上一轮残留进程，却没复核当前这轮端口是否仍被占。
    """

    target_ports: set[int] = field(default_factory=set)
    protected_ports: set[int] = field(default_factory=set)
    kill_issued: bool = False
    # 端口状态：released（已确认释放）/ bound（仍被占）/ needs_reverify（kill 后待复核）
    port_state: dict[int, str] = field(default_factory=dict)
    observation_count: int = 0

    @classmethod
    def from_user_text(cls, text: str) -> "_ProcessRemediationChecklist":
        targets, protected = parse_remediation_ports(text)
        return cls(target_ports=set(targets), protected_ports=set(protected))

    def record_kill(self) -> None:
        self.kill_issued = True
        # 任一次 kill 之后，所有目标端口都必须重新确认是否真的释放。
        for port in self.target_ports:
            self.port_state[port] = "needs_reverify"

    def record_read_result(
        self, tool_name: str, args: dict, content: str, data: dict | None
    ) -> None:
        data = data or {}
        if tool_name == "lsof_port":
            port = _coerce_port(args.get("port"))
            if port is None or port not in self.target_ports:
                return
            self.observation_count += 1
            if data.get("no_match") or "No process is using" in (content or ""):
                self.port_state[port] = "released"
            else:
                self.port_state[port] = "bound"
        elif tool_name == "net_listen":
            # net_listen 只用于确认「仍在监听」（presence ⇒ bound）；不据其「不在列表」
            # 判定释放，避免被 6KB 截断或权限盲区误清。
            listening = {int(m) for m in _SS_PORT_RE.findall(content or "")}
            for port in self.target_ports:
                if port in listening:
                    self.observation_count += 1
                    self.port_state[port] = "bound"

    def progress(self) -> int:
        return self.observation_count

    def final_error(self) -> str:
        if not self.target_ports:
            return ""
        unresolved = sorted(
            port for port in self.target_ports
            if self.port_state.get(port) != "released"
        )
        if not unresolved:
            return ""
        ports = ", ".join(str(p) for p in unresolved)
        return (
            "port remediation checklist blocked final response: target port(s) "
            f"{ports} not yet confirmed released. After terminating the current owner, "
            "re-check each port with lsof_port (cross-check net_listen); only conclude "
            "release when lsof_port reports no process is using the port."
        )


def _coerce_port(value) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _extract_ls_children(path_arg: str, content: str) -> set[str]:
    children: set[str] = set()
    for line in (content or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[-1]
        if name in {".", ".."} or name.startswith("/"):
            continue
        if "->" in parts:
            name = parts[parts.index("->") - 1]
        if "/" in name:
            continue
        children.add(posixpath.join(path_arg, name))
    return children


def _verify_dir_for_path(path: str) -> str:
    path = normalize_abs_path(path)
    if not path:
        return "/"
    return posixpath.dirname(path) or "/"


@dataclass
class AgentRunResult:
    trace: Trace
    final_text: str
    tool_iterations: int = 0
    denied: bool = False
    notes: list[str] = field(default_factory=list)
    plan_id: str | None = None


class Agent:
    """单轮 turn 由 .ask() 完成；维护 messages 上下文。"""

    def __init__(
        self,
        cfg: Config,
        llm: LlmBackend,
        registry: ToolRegistry,
        guardrail: Guardrail,
        executor: ExecutionProxy,
        audit: AuditLogger,
        confirm: ConfirmFn = _auto_deny,
        intent_guard: IntentGuard | None = None,
        on_progress: ProgressCallback | None = None,
        on_user_choice: UserChoiceFn | None = None,
        plan_store: PlanStore | None = None,
        auto_approve_safe_remediation: bool = False,
    ):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.guardrail = guardrail
        self.intent_guard = intent_guard  # 赛题第 3 条：NL 意图层（None 则跳过）
        self.executor = executor
        self.audit = audit
        self.confirm = confirm
        # on_progress 一旦赋值不再变更：worker 线程读到的总是同一个 callable
        self.on_progress: ProgressCallback = on_progress or noop_progress
        # ask_user_choice 工具的回调；UI 未注入时默认拒绝（保守）
        self.on_user_choice: UserChoiceFn = on_user_choice or auto_cancel_choice
        self.plan_store = plan_store
        self.todo_service = TodoService(plan_store) if plan_store is not None else None
        self.auto_approve_safe_remediation = auto_approve_safe_remediation
        self.messages: list[dict] = []
        self._file_remediation_checklist: _FileRemediationChecklist | None = None
        self._process_remediation_checklist: _ProcessRemediationChecklist | None = None
        self._tools_for_llm = self._build_tools_for_llm()
        self.system_prompt = SYSTEM_PROMPT
        if auto_approve_safe_remediation:
            self.system_prompt += (
                "\n\n## 非交互安全修复模式\n"
                "当前 turn 启用了 auto_approve_safe_remediation。对已经通过只读感知确认的"
                "清理、释放端口、释放已删除文件句柄、移除陈旧 lock/socket、禁用可疑 cron "
                "入口、收紧日志目录权限等受控修复目标，不要等待用户确认，"
                "也不要调用 ask_user_choice 做执行确认；应直接调用具体修复工具，"
                "由 Agent 的 safety/write preflight 决定是否放行。"
            )
        self._run_lock = threading.RLock()
        self._active_run_thread_id: int | None = None
        # 持久线程池：避免每多工具回合都付一次 thread spawn 的固定开销，
        # 在 Windows mock 后端这一开销会盖过并行带来的收益。
        self._tool_pool: ThreadPoolExecutor | None = None
        self._shutdown = False

    def _build_tools_for_llm(self) -> list[dict]:
        tools = list(self.registry.to_anthropic_tools())
        if self.plan_store is not None:
            tools.extend(dict(tool) for tool in _TODO_TOOLS_FOR_LLM)
        return tools

    def _ensure_pool(self) -> ThreadPoolExecutor:
        if self._tool_pool is None:
            self._tool_pool = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="ky-tool"
            )
        return self._tool_pool

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._tool_pool is not None:
            self._tool_pool.shutdown(wait=False)
            self._tool_pool = None
        self.audit.close_file()
        self.audit.store.close()
        if self.plan_store is not None:
            self.plan_store.close()

    def _emit(self, event: ProgressEvent) -> None:
        """防御式包装：TUI/外部回调抛异常不应影响 Agent 主循环。

        审计照常走，进度静默丢弃。回调可能从 worker 线程被触发；并发安全由
        UI 端自己负责（progress.py 注释里写了 callback 必须不 raise）。
        """
        try:
            self.on_progress(event)
        except Exception:
            pass

    @classmethod
    def from_config(cls, cfg: Config, confirm: ConfirmFn = _auto_deny,
                    on_progress: ProgressCallback | None = None,
                    on_user_choice: UserChoiceFn | None = None,
                    auto_approve_safe_remediation: bool = False) -> "Agent":
        # 通道无关基础设施统一从 composition root 装配
        rt = build_runtime(cfg)
        # 通道特定（LLM 后端、NL 意图层）在这里组合
        llm = build_backend(cfg)
        intent_guard = IntentGuard.from_config(cfg) if cfg.safety.intent_check else None
        plan_store = None
        if (
            getattr(cfg.planning, "enabled", True)
            and os.environ.get("KYAGENT_BENCH") != "1"
        ):
            plan_store = PlanStore(cfg.resolve(cfg.planning.database))
        return cls(
            cfg, llm, rt.registry, rt.guardrail, rt.executor, rt.audit, confirm,
            intent_guard=intent_guard, on_progress=on_progress,
            on_user_choice=on_user_choice, plan_store=plan_store,
            auto_approve_safe_remediation=auto_approve_safe_remediation,
        )

    # ---- 主入口 --------------------------------------------------------

    def ask(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        with self._run_lock:
            previous_thread_id = self._active_run_thread_id
            self._active_run_thread_id = threading.get_ident()
            try:
                return self._ask_impl(user_input, user=user)
            finally:
                self._active_run_thread_id = previous_thread_id

    def _ask_impl(self, user_input: str, user: str = "anonymous") -> AgentRunResult:
        trace = Trace(user=user)
        self.audit.open(trace)
        trace.metadata.update({"backend": self.llm.name})
        plan: PlanSnapshot | None = None
        self._emit(ProgressEvent(kind="agent_start", text=user_input))

        self.audit.event(trace, EventKind.USER_INPUT, {"text": user_input})
        if self.plan_store is not None:
            plan = self.plan_store.create_run_plan(
                trace_id=trace.trace_id,
                user=user,
                title=user_input,
                metadata={"backend": self.llm.name},
            )
            trace.metadata["plan_id"] = plan.plan_id
            self._emit_plan(trace, plan, kind="plan_start", text=plan.title)
            self._emit_todo_snapshot(trace, TodoSnapshot.from_plan(plan))

        notes: list[str] = []
        iterations = 0
        denied = False
        # 防回环：累计每个"工具+参数"签名的失败次数，达到阈值即中止本次运行。
        repeated_failures: dict[str, int] = {}
        repeated_abort: tuple[str, int] | None = None
        evidence_gate_required = self._is_os_question(user_input)
        evidence_gate_forced_once = False
        evidence_gate_extra_summary = False
        # 防回环：清理 checklist 反复拦截最终回答时的进度守卫。只要每次拦截都伴随
        # 进度（新扫描的 root 或新复核的 path），就清零计数；进度停滞且连续拦截达到
        # 阈值即放行（带未复核告警），避免空转到 max_iterations。
        checklist_block_count = 0
        last_checklist_progress = 0
        # 同型守卫：端口/进程终止 checklist 反复拦截最终回答时的进度守卫。
        proc_checklist_block_count = 0
        last_proc_progress = 0

        # ===== 赛题第 3 条：NL 意图层 + 抗 Prompt Injection =====
        # 这是 LLM 看到 user_input 之前的"一次过滤"。argv 层 Guardrail 是 LLM 输出
        # 之后的"二次过滤"，两者互补缺一不可。
        effective_input = user_input
        if self.intent_guard is not None:
            intent_verdict: IntentVerdict = self.intent_guard.evaluate(
                user_input, context={"user": user}
            )
            self.audit.event(trace, EventKind.INTENT_CHECK, intent_verdict.to_dict())

            if intent_verdict.decision is Decision.DENY:
                denied = True
                notes.append(
                    f"已在意图层拦截（risk={intent_verdict.risk.value}）：" +
                    ",".join(h.rule_id for h in intent_verdict.hits[:3])
                )
                reply = (
                    f"[blocked] 你的请求被自然语言意图风险过滤器拦截。\n"
                    f"风险等级：{intent_verdict.risk.value}\n"
                    + "\n".join(intent_verdict.rationale)
                )
                self.audit.event(trace, EventKind.AGENT_REPLY,
                                 {"text": reply, "blocked_at": "intent"})
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "receive", "failed", "Intent layer denied request",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "failed", current_step="receive")
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=reply,
                                      tool_iterations=0, denied=True, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            if intent_verdict.decision is Decision.CONFIRM:
                approved = False
                try:
                    approved = self.confirm(confirm_adapter.for_intent(intent_verdict))
                except Exception:
                    approved = False
                if not approved:
                    denied = True
                    notes.append(f"用户拒绝意图层 confirm（risk={intent_verdict.risk.value}）")
                    reply = (
                        f"[denied] 用户拒绝高风险意图请求（risk={intent_verdict.risk.value}）"
                    )
                    self.audit.event(trace, EventKind.AGENT_REPLY,
                                     {"text": reply, "blocked_at": "intent_confirm"})
                    if plan is not None:
                        plan = self._set_plan_step(
                            trace, plan, "receive", "failed",
                            "User rejected intent confirmation",
                            event_kind="plan_step_end",
                        )
                        plan = self._set_plan_status(trace, plan, "failed", current_step="receive")
                    self.audit.close(trace)
                    return AgentRunResult(trace=trace, final_text=reply,
                                          tool_iterations=0, denied=True, notes=notes,
                                          plan_id=plan.plan_id if plan else None)

            # 净化 stealth injection（零宽字符）：把净化后的文本送进 LLM，
            # 保留原文在 USER_INPUT 事件里以便审计追溯
            if intent_verdict.sanitized_text is not None:
                effective_input = intent_verdict.sanitized_text
                notes.append("已剥离零宽字符送入 LLM")

        current_scope = RemediationScope.from_user_text(effective_input)
        remediation_scope = current_scope
        checklist_applies = file_remediation_checklist_applies(remediation_scope)
        previous_file_checklist = self._file_remediation_checklist
        if (
            not checklist_applies
            and previous_file_checklist is not None
            and current_scope.actions & {"cleanup"}
            and not current_scope.search_roots(round=1)
        ):
            checklist_applies = True
        if checklist_applies:
            self._file_remediation_checklist = _FileRemediationChecklist.from_scope(
                remediation_scope
            )
            if (
                previous_file_checklist is not None
                and not current_scope.search_roots(round=1)
                and current_scope.actions & {"cleanup"}
            ):
                self._file_remediation_checklist.inherit_discovery_from(
                    previous_file_checklist
                )
        else:
            self._file_remediation_checklist = None

        # 端口/进程终止收尾自检：当用户点名了要释放的端口、且意图是终止类时启用。
        # 以解析到的目标端口为主信号（关键词分类对「被占/占」覆盖不全），辅以 scope 谓词。
        proc_target_ports, _proc_protected = parse_remediation_ports(effective_input)
        process_checklist_applies = bool(proc_target_ports) and (
            "terminate" in current_scope.actions
            or process_remediation_checklist_applies(current_scope)
        )
        if process_checklist_applies:
            self._process_remediation_checklist = (
                _ProcessRemediationChecklist.from_user_text(effective_input)
            )
        else:
            self._process_remediation_checklist = None

        self.messages.append({"role": "user", "content": effective_input})
        turn_system_prompt = self.system_prompt
        if checklist_applies:
            turn_system_prompt += (
                "\n\n## 当前范围模型\n"
                + remediation_scope.summary()
                + "\n\n## 候选清单工作流\n"
                "1. 先用 file_cleanup_candidates / fs_ls / dir_largest_files 生成完整候选清单。\n"
                "2. 为每个候选标注 delete / protect / unknown；unknown 不执行。\n"
                "3. 只对 delete 候选调用 fs_delete_file / fs_truncate / log_delete_file。\n"
                "4. 最终报告必须区分：已确认、未检查、未覆盖、不在本次范围；"
                "未扫描过的目录不要说“没有/cache/不存在”。"
            )
        elif (
            remediation_scope.services
            or remediation_scope.actions
            or remediation_scope.resource_types
        ):
            turn_system_prompt += (
                "\n\n## 当前范围模型\n" + remediation_scope.summary()
            )
        if self._process_remediation_checklist is not None and proc_target_ports:
            ports = ", ".join(str(p) for p in sorted(proc_target_ports))
            turn_system_prompt += (
                "\n\n## 端口收尾自检\n"
                f"本次需要释放的目标端口：{ports}。\n"
                "1. 普通用户的 lsof 返回空、ss 看不到进程名，都不代表端口空闲——"
                "root 起的监听进程对非特权感知是隐形的；用 lsof_port/net_listen/process_list 交叉确认占用者。\n"
                "2. 终止占用者后，必须再次用 lsof_port 复核该端口已无人占用，"
                "确认释放前不要下「已解决」的最终结论。"
            )

        tools_for_llm = self._tools_for_llm
        if plan is not None:
            plan = self._set_plan_step(
                trace, plan, "receive", "complete", "Request accepted",
                event_kind="plan_step_end",
            )
            plan = self._set_plan_step(
                trace, plan, "reason", "running", "Starting reasoning/tool loop",
                event_kind="plan_step_start",
            )

        while (
            iterations < self.cfg.agent.max_iterations
            or evidence_gate_extra_summary
        ):
            extra_summary_iteration = (
                evidence_gate_extra_summary
                and iterations >= self.cfg.agent.max_iterations
            )
            budget_reason = "loop"
            if extra_summary_iteration:
                budget_reason = "evidence_gate_summary"
            if evidence_gate_extra_summary:
                evidence_gate_extra_summary = False
            iterations += 1
            budget_payload = {
                "iteration": iterations,
                "max_iterations": self.cfg.agent.max_iterations,
                "reason": budget_reason,
                "plan_id": plan.plan_id if plan else None,
            }
            self.audit.event(trace, EventKind.BUDGET, budget_payload)
            self._emit(ProgressEvent(kind="budget_update", meta=budget_payload))
            self._emit(ProgressEvent(
                kind="thinking_start",
                meta={"iteration": iterations},
            ))

            def _on_delta(chunk: str) -> None:
                # 闭包捕获 self，给 UI 推 token 级增量（答案/正文）；空块直接跳过
                if chunk:
                    self._emit(ProgressEvent(kind="thinking_delta", delta=chunk))

            def _on_reason(chunk: str) -> None:
                # 推理模型的思维链增量（reasoning_content），独立通道；空块跳过
                if chunk:
                    self._emit(ProgressEvent(kind="reasoning_delta", delta=chunk))

            try:
                # 所有后端要么实现 chat_stream（基类默认调 chat 再发一次完整 delta），
                # 要么暂未升级（并行子代理在 llm.py 加）。getattr 兜底保证两侧都能跑。
                stream_fn = getattr(self.llm, "chat_stream", None)
                if callable(stream_fn):
                    assistant = stream_fn(
                        turn_system_prompt, self.messages, tools_for_llm, _on_delta,
                        on_reasoning=_on_reason,
                    )
                else:
                    assistant = self.llm.chat(
                        turn_system_prompt, self.messages, tools_for_llm
                    )
                    # fallback：一次性把全部文本作为单条 delta 喷出，保持事件契约
                    for _t in assistant.texts():
                        _on_delta(_t)
            except Exception as e:  # noqa: BLE001
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "llm_error", "detail": str(e)})
                self._emit(ProgressEvent(
                    kind="error",
                    text=str(e),
                    meta={"reason": "llm_error"},
                ))
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "failed", str(e),
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "failed", current_step="reason")
                self.audit.close(trace)
                return AgentRunResult(trace=trace, final_text=f"LLM 调用失败：{e}",
                                      tool_iterations=iterations, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            self._emit(ProgressEvent(
                kind="thinking_end",
                text="\n".join(assistant.texts())[:200],
                meta={"tool_calls": [t.name for t in assistant.tool_uses()]},
            ))
            self.audit.event(trace, EventKind.LLM_THOUGHT,
                             {"stop_reason": assistant.stop_reason,
                              "text": "\n".join(assistant.texts())[:4000],
                              "tool_calls": [t.name for t in assistant.tool_uses()]})

            tool_uses = assistant.tool_uses()

            # 没有工具调用：终结
            if not tool_uses:
                final = "\n".join(assistant.texts()).strip()
                if evidence_gate_required and not self._has_perception_evidence(trace):
                    if evidence_gate_forced_once:
                        notes.append("OS 问题强制只读感知后仍未产生 PERCEPTION evidence_id")
                        self.audit.event(trace, EventKind.ERROR, {
                            "reason": "evidence_gate_no_evidence_after_forced_tool",
                        })
                        if plan is not None:
                            plan = self._set_plan_step(
                                trace, plan, "reason", "failed",
                                "Forced read-only perception did not produce evidence",
                                event_kind="plan_step_end",
                            )
                            plan = self._set_plan_status(
                                trace, plan, "failed", current_step="reason"
                            )
                        self.audit.close(trace)
                        self._emit(ProgressEvent(
                            kind="error",
                            text="OS 问题最终回答前缺少 PERCEPTION evidence_id",
                            meta={"reason": "evidence_gate_no_evidence_after_forced_tool"},
                        ))
                        return AgentRunResult(
                            trace=trace,
                            final_text="OS 问题最终回答前缺少系统感知证据，已中止。",
                            tool_iterations=iterations,
                            denied=denied,
                            notes=notes,
                            plan_id=plan.plan_id if plan else None,
                        )
                    forced_tool = self._default_evidence_gate_tool_use()
                    if forced_tool is None:
                        notes.append("OS 问题缺少可用的只读感知工具，证据门无法放行")
                        self.audit.event(trace, EventKind.ERROR, {
                            "reason": "evidence_gate_no_read_only_tool",
                        })
                        if plan is not None:
                            plan = self._set_plan_step(
                                trace, plan, "reason", "failed",
                                "Evidence gate found no read-only perception tool",
                                event_kind="plan_step_end",
                            )
                            plan = self._set_plan_status(
                                trace, plan, "failed", current_step="reason"
                            )
                        self.audit.close(trace)
                        self._emit(ProgressEvent(
                            kind="error",
                            text="OS 问题最终回答前缺少 PERCEPTION evidence_id，且没有可用只读工具",
                            meta={"reason": "evidence_gate_no_read_only_tool"},
                        ))
                        return AgentRunResult(
                            trace=trace,
                            final_text="OS 问题最终回答前缺少系统感知证据，且没有可用只读工具，已中止。",
                            tool_iterations=iterations,
                            denied=denied,
                            notes=notes,
                            plan_id=plan.plan_id if plan else None,
                        )
                    notes.append("OS 问题在最终回答前缺少 PERCEPTION evidence_id，已强制只读感知")
                    evidence_gate_forced_once = True
                    payload = {
                        "event": "evidence_gate_forced_perception",
                        "reason": "os_final_without_perception_evidence",
                        "iteration": iterations,
                        "tool": forced_tool.name,
                        "plan_id": plan.plan_id if plan else None,
                    }
                    self.audit.event(trace, EventKind.PLAN_UPDATE, payload)
                    self._emit(ProgressEvent(
                        kind="plan_step_update",
                        text="OS 问题最终回答前必须先产生 PERCEPTION evidence_id",
                        meta=payload,
                    ))
                    self.messages.append({
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "TODO 1: 调用低风险只读工具感知当前系统状态。\n"
                                    "TODO 2: 将感知结果作为最终回答前的证据。"
                                ),
                            },
                            {
                                "type": "tool_use",
                                "id": forced_tool.id,
                                "name": forced_tool.name,
                                "input": forced_tool.input,
                            },
                        ],
                    })
                    if plan is not None:
                        plan = self._set_plan_step(
                            trace, plan, "reason", "running",
                            "Evidence gate forced read-only perception before final answer",
                            event_kind="plan_step_update",
                        )
                    result_block = self._handle_tool_use(trace, forced_tool, notes, False)
                    if result_block.is_error and result_block.content.startswith("[denied]"):
                        denied = True
                    self.messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": forced_tool.id,
                            "content": result_block.content,
                            "is_error": result_block.is_error,
                        }],
                    })
                    if iterations >= self.cfg.agent.max_iterations:
                        evidence_gate_extra_summary = True
                    continue

                checklist_error = ""
                if self._file_remediation_checklist is not None:
                    checklist_error = self._file_remediation_checklist.pre_scan_error()
                    if not checklist_error:
                        checklist_error = self._file_remediation_checklist.final_error()
                if checklist_error:
                    # 进度守卫：以"已扫描 root 数 + 已复核 path 数"作为进度信号。较上次拦截
                    # 有增长才视为有进展并清零计数；反复拦截却毫无进展达到阈值时，不再
                    # continue 空转，而是放行并附未复核告警。
                    progress_now = (
                        len(self._file_remediation_checklist.scanned_roots)
                        + len(self._file_remediation_checklist.verified_paths)
                        if self._file_remediation_checklist is not None
                        else 0
                    )
                    if progress_now > last_checklist_progress:
                        checklist_block_count = 0
                        last_checklist_progress = progress_now
                    else:
                        checklist_block_count += 1

                    if checklist_block_count < self.cfg.agent.max_repeated_tool_failures:
                        notes.append("file remediation checklist blocked final response")
                        if self._file_remediation_checklist is not None:
                            checklist_error += "\n\n" + self._file_remediation_checklist.completion_report()
                        reason = (
                            "final_without_pre_scan"
                            if "not yet enumerated" in checklist_error
                            else "final_without_post_verify"
                        )
                        payload = {
                            "event": "file_remediation_checklist_required",
                            "reason": reason,
                            "detail": checklist_error,
                            "plan_id": plan.plan_id if plan else None,
                        }
                        self.audit.event(trace, EventKind.PLAN_UPDATE, payload)
                        self._emit(ProgressEvent(
                            kind="plan_required",
                            text=checklist_error,
                            meta=payload,
                        ))
                        if plan is not None:
                            plan = self._set_plan_step(
                                trace, plan, "verify", "running",
                                "File cleanup changes require read-only post-verify",
                                event_kind="plan_step_update",
                            )
                        self.messages.append({
                            "role": "user",
                            "content": "System feedback: " + checklist_error,
                        })
                        continue

                    # 守卫触发：连续 max_repeated_tool_failures 次拦截且无新增复核。
                    # 放行最终回答，但追加未复核告警，并落到下方正常终结分支。
                    notes.append(
                        "file remediation checklist could not verify cleanup; "
                        "returned answer with caveat"
                    )
                    guard_payload = {
                        "event": "file_remediation_unverified",
                        "reason": "file_remediation_unverified",
                        "detail": checklist_error,
                        "block_count": checklist_block_count,
                        "plan_id": plan.plan_id if plan else None,
                    }
                    self.audit.event(trace, EventKind.ERROR, guard_payload)
                    self._emit(ProgressEvent(
                        kind="error",
                        text="部分清理结果未能自动复核，已带告警返回",
                        meta=guard_payload,
                    ))
                    final = (
                        final
                        + "\n\n> 注意：部分清理结果未能自动复核（系统已尽力重扫验证），请人工确认。"
                    ).strip()

                # 端口/进程终止收尾自检：目标端口未确认释放前，拦回最终回答要求复核。
                # 同型进度守卫，防止无法释放（如端口本就空不掉）时空转到 max_iterations。
                proc_error = ""
                if self._process_remediation_checklist is not None:
                    proc_error = self._process_remediation_checklist.final_error()
                if proc_error:
                    proc_progress_now = (
                        self._process_remediation_checklist.progress()
                        if self._process_remediation_checklist is not None
                        else 0
                    )
                    if proc_progress_now > last_proc_progress:
                        proc_checklist_block_count = 0
                        last_proc_progress = proc_progress_now
                    else:
                        proc_checklist_block_count += 1

                    if proc_checklist_block_count < self.cfg.agent.max_repeated_tool_failures:
                        notes.append("port remediation checklist blocked final response")
                        payload = {
                            "event": "port_remediation_checklist_required",
                            "reason": "final_without_port_release_verify",
                            "detail": proc_error,
                            "plan_id": plan.plan_id if plan else None,
                        }
                        self.audit.event(trace, EventKind.PLAN_UPDATE, payload)
                        self._emit(ProgressEvent(
                            kind="plan_required",
                            text=proc_error,
                            meta=payload,
                        ))
                        if plan is not None:
                            plan = self._set_plan_step(
                                trace, plan, "verify", "running",
                                "Port termination requires read-only post-verify",
                                event_kind="plan_step_update",
                            )
                        self.messages.append({
                            "role": "user",
                            "content": "System feedback: " + proc_error,
                        })
                        continue

                    # 守卫触发：连续拦截且无新增复核，放行但附未复核告警。
                    notes.append(
                        "port remediation checklist could not confirm port release; "
                        "returned answer with caveat"
                    )
                    guard_payload = {
                        "event": "port_remediation_unverified",
                        "reason": "port_remediation_unverified",
                        "detail": proc_error,
                        "block_count": proc_checklist_block_count,
                        "plan_id": plan.plan_id if plan else None,
                    }
                    self.audit.event(trace, EventKind.ERROR, guard_payload)
                    self._emit(ProgressEvent(
                        kind="error",
                        text="目标端口是否释放未能自动复核，已带告警返回",
                        meta=guard_payload,
                    ))
                    final = (
                        final
                        + "\n\n> 注意：目标端口是否已释放未能自动复核，请人工确认。"
                    ).strip()

                self.messages.append({"role": "assistant",
                                      "content": [{"type": "text", "text": final}]})
                if plan is not None and self.todo_service is not None:
                    previous_revision = plan.todo_revision
                    plan, final_todos = self.todo_service.finalize(plan.plan_id, success=True)
                    if plan.todo_revision != previous_revision:
                        self._emit_todo_snapshot(trace, final_todos)
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "complete", "No more tool calls",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_step(
                        trace, plan, "verify", "complete", "Final response prepared",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_step(
                        trace, plan, "respond", "complete", "Answer returned",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(trace, plan, "complete", current_step="respond")
                self.audit.event(trace, EventKind.AGENT_REPLY, {"text": final})
                self.audit.close(trace)
                self._emit(ProgressEvent(kind="agent_final", text=final))
                return AgentRunResult(trace=trace, final_text=final,
                                      tool_iterations=iterations, denied=denied, notes=notes,
                                      plan_id=plan.plan_id if plan else None)

            # 把 assistant 消息原样追加（含 tool_use 块）
            self.messages.append({"role": "assistant",
                                  "content": self._blocks_to_dict(assistant)})

            # 选择串行 / 并行：只有执行器明确声明线程安全，且所有工具预检均为
            # allow-only 只读调用时才进入线程池。confirm/deny/未知/参数错误路径
            # 保持串行，避免交互提示与审计流并发交错。
            tool_results: list[dict | None] = [None] * len(tool_uses)
            tool_indices = {id(tu): idx for idx, tu in enumerate(tool_uses)}
            execution_tool_uses = (
                [tu for tu in tool_uses if tu.name == "todo_write"]
                + [tu for tu in tool_uses if tu.name != "todo_write"]
            )
            run_parallel = (
                sys.platform != "win32"
                and len(tool_uses) >= 2
                and not any(tu.name in _TODO_TOOL_NAMES for tu in tool_uses)
                and self._executor_supports_parallel_tools()
                and all(self._is_parallel_safe(tu) for tu in tool_uses)
            )

            if run_parallel:
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "running",
                        f"Running {len(tool_uses)} read-only tools in parallel",
                        event_kind="plan_step_update",
                    )
                pool = self._ensure_pool()
                futures = [
                    pool.submit(self._handle_tool_use, trace, tu, notes, True)
                    for tu in tool_uses
                ]
                for idx, (tu, fut) in enumerate(zip(tool_uses, futures)):
                    result_block = fut.result()
                    if result_block.is_error and result_block.content.startswith("[denied]"):
                        denied = True
                    if repeated_abort is None and result_block.is_error:
                        repeated_abort = self._record_tool_failure(
                            repeated_failures, tu
                        )
                    tool_results[idx] = {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_block.content,
                        "is_error": result_block.is_error,
                    }
            else:
                for tu in execution_tool_uses:
                    idx = tool_indices[id(tu)]
                    result_block = self._handle_tool_use(trace, tu, notes, False)
                    if result_block.is_error and result_block.content.startswith("[denied]"):
                        denied = True
                    if repeated_abort is None and result_block.is_error:
                        repeated_abort = self._record_tool_failure(
                            repeated_failures, tu
                        )
                    tool_results[idx] = {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_block.content,
                        "is_error": result_block.is_error,
                    }

            # 把 tool_result 作为下一轮 user 消息送回（顺序与 tool_uses 一致）
            self.messages.append({"role": "user", "content": tool_results})

            # 防回环：同一工具调用（名称+参数）被反复拒绝/报错时中止，避免空转到
            # max_iterations。破坏性操作被拒后应停下来交人工，而非无限重试。
            if repeated_abort is not None:
                tool_name, count = repeated_abort
                notes.append(
                    f"防回环：{tool_name} 同一调用连续 {count} 次失败/被拒，已中止，需人工介入"
                )
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "repeated_tool_failure",
                    "tool": tool_name,
                    "count": count,
                })
                if plan is not None:
                    plan = self._set_plan_step(
                        trace, plan, "reason", "failed",
                        f"Aborted: {tool_name} repeatedly failed ({count}x)",
                        event_kind="plan_step_end",
                    )
                    plan = self._set_plan_status(
                        trace, plan, "failed", current_step="reason"
                    )
                self.audit.close(trace)
                self._emit(ProgressEvent(
                    kind="error",
                    text=f"{tool_name} 反复被拒绝/报错，已中止，需人工介入",
                    meta={"reason": "repeated_tool_failure", "tool": tool_name},
                ))
                return AgentRunResult(
                    trace=trace,
                    final_text=(
                        f"已中止：{tool_name} 同一调用连续 {count} 次失败或被拒绝且"
                        "未获放行，需要人工介入处理，请勿继续自动重试。"
                    ),
                    tool_iterations=iterations, denied=True, notes=notes,
                    plan_id=plan.plan_id if plan else None,
                )

        # 超出最大迭代
        notes.append(f"达到最大迭代次数 {self.cfg.agent.max_iterations}")
        self.audit.event(trace, EventKind.ERROR, {"reason": "max_iterations"})
        if plan is not None:
            plan = self._set_plan_step(
                trace, plan, "reason", "failed", "Reached max_iterations",
                event_kind="plan_step_end",
            )
            plan = self._set_plan_status(trace, plan, "failed", current_step="reason")
        self.audit.close(trace)
        self._emit(ProgressEvent(
            kind="error",
            text="达到最大工具调用次数",
            meta={"reason": "max_iterations"},
        ))
        return AgentRunResult(trace=trace,
                              final_text="达到最大工具调用次数，已中止。",
                              tool_iterations=iterations, denied=denied, notes=notes,
                              plan_id=plan.plan_id if plan else None)

    # ---- 工具调用处理 --------------------------------------------------

    def _emit_plan(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        *,
        kind: str,
        text: str = "",
        step_id: str = "",
    ) -> None:
        payload = {
            "event": kind,
            "plan_id": plan.plan_id,
            "plan_status": plan.status,
            "current_step": plan.current_step,
        }
        if step_id:
            payload["step_id"] = step_id
            step = next((item for item in plan.steps if item.step_id == step_id), None)
            if step is not None:
                payload["step_status"] = step.status
                payload["detail"] = step.detail
        self.audit.event(trace, EventKind.PLAN_UPDATE, payload)
        self._emit(ProgressEvent(kind=kind, text=text, meta=payload))  # type: ignore[arg-type]
        # Internal run phases remain separate from the user-facing todo stream.

    def _emit_todo_snapshot(self, trace: Trace, snapshot: TodoSnapshot) -> None:
        payload = snapshot.to_dict()
        self.audit.event(trace, EventKind.PLAN_UPDATE, {
            "event": "todo_snapshot",
            **payload,
        })
        self._emit(ProgressEvent(kind="todo_snapshot", meta=payload))  # type: ignore[arg-type]

    def _todo_items_from_tool_input(self, data: dict) -> list[PlanTodoItem]:
        if not isinstance(data, dict):
            raise ToolError("todo_write input must be an object")
        raw = data.get("todos")
        if not isinstance(raw, list):
            raise ToolError("todos must be an array")
        if len(raw) > 20:
            raise ToolError("todos may contain at most 20 items")
        items: list[PlanTodoItem] = []
        for idx, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ToolError(f"todos[{idx - 1}] must be an object")
            content = str(item.get("content", "")).strip()
            if not content:
                raise ToolError(f"todos[{idx - 1}].content is required")
            if len(content) > 500:
                raise ToolError(f"todos[{idx - 1}].content is too long")
            status = str(item.get("status", "pending")).strip()
            if status not in {"pending", "in_progress", "completed", "failed", "cancelled"}:
                raise ToolError(f"todos[{idx - 1}].status is invalid")
            priority = str(item.get("priority", "medium")).strip()
            if priority not in {"high", "medium", "low"}:
                raise ToolError(f"todos[{idx - 1}].priority is invalid")
            items.append(PlanTodoItem(
                todo_id="",
                content=content,
                status=status,  # type: ignore[arg-type]
                priority=priority,  # type: ignore[arg-type]
            ))
        return items

    def _handle_todo_read(self, trace: Trace, tool_use_id: str) -> ToolResultBlock:
        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "todo_read",
            "argv": [],
            "args": {},
            "risk": "low",
            "requires_root": False,
        })
        plan_id = trace.metadata.get("plan_id")
        todos: list[dict] = []
        if isinstance(plan_id, str) and self.plan_store is not None:
            try:
                todos = [todo.to_dict() for todo in self.plan_store.get(plan_id).todos]
            except KeyError:
                todos = []
        content = json.dumps({"todos": todos}, ensure_ascii=False)
        return ToolResultBlock(tool_use_id=tool_use_id, content=content)

    def _handle_todo_write(self, trace: Trace, tu: ToolUseBlock) -> ToolResultBlock:
        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "todo_write",
            "argv": [],
            "args": tu.input or {},
            "risk": "low",
            "requires_root": False,
        })
        if self.plan_store is None:
            return ToolResultBlock(
                tool_use_id=tu.id,
                is_error=True,
                content="todo_write unavailable: planning store is disabled",
            )
        plan_id = trace.metadata.get("plan_id")
        if not isinstance(plan_id, str):
            return ToolResultBlock(
                tool_use_id=tu.id,
                is_error=True,
                content="todo_write unavailable: no active plan",
            )
        try:
            todos = self._todo_items_from_tool_input(tu.input or {})
        except ToolError as exc:
            self.audit.event(trace, EventKind.ERROR, {
                "reason": "invalid_args",
                "tool": "todo_write",
                "detail": str(exc),
            })
            return ToolResultBlock(tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{exc}")
        if self.todo_service is None:
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content="todo_write unavailable: todo service is disabled",
            )
        try:
            plan, snapshot = self.todo_service.replace(plan_id, todos)
        except ValueError as exc:
            self.audit.event(trace, EventKind.ERROR, {
                "reason": "invalid_args",
                "tool": "todo_write",
                "detail": str(exc),
            })
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{exc}"
            )
        self._emit_todo_snapshot(trace, snapshot)
        content = json.dumps({"todos": [todo.to_dict() for todo in plan.todos]}, ensure_ascii=False)
        return ToolResultBlock(tool_use_id=tu.id, content=content)

    def _set_plan_step(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        step_id: str,
        status: str,
        detail: str,
        *,
        event_kind: str,
    ) -> PlanSnapshot:
        if self.plan_store is None:
            return plan
        updated = self.plan_store.set_step(plan.plan_id, step_id, status, detail)
        self._emit_plan(trace, updated, kind=event_kind, text=detail, step_id=step_id)
        return updated

    def _set_plan_status(
        self,
        trace: Trace,
        plan: PlanSnapshot,
        status: str,
        *,
        current_step: str | None = None,
    ) -> PlanSnapshot:
        if self.plan_store is None:
            return plan
        updated = self.plan_store.set_status(
            plan.plan_id, status, current_step=current_step
        )
        if status == "failed" and self.todo_service is not None:
            previous_revision = updated.todo_revision
            updated, snapshot = self.todo_service.finalize(plan.plan_id, success=False)
            if updated.todo_revision != previous_revision:
                self._emit_todo_snapshot(trace, snapshot)
        self._emit_plan(trace, updated, kind="plan_step_update", text=status)
        return updated

    def _executor_supports_parallel_tools(self) -> bool:
        """Whether this executor can safely run tool calls from worker threads."""
        return bool(getattr(self.executor, "supports_parallel_tool_execution", False))

    def _is_parallel_safe(self, tu: ToolUseBlock) -> bool:
        """Return True only for preflighted allow-only read-only tool calls.

        未知工具也按"不可并行"处理；后续 _handle_tool_use 内会以 ERROR 兜底，
        保留原有错误信息。
        """
        if tu.name in {"ask_user_choice", "submit_rca_report"}:
            return False
        tool = self.registry.get(tu.name)
        if tool is None:
            return False
        if not tool.read_only:
            return False
        # 预检的唯一前提：guardrail 必须是确定性的。一旦 llm_reviewer 介入
        # （guardrail.py 明确捕获 reviewer 异常并把结果置 None），主线程预检
        # 拿到的 verdict 与 worker 线程内的 verdict 可能不一致，导致 worker
        # 走到 CONFIRM 分支并触发 self.confirm() 读 stdin。直接拒绝并行。
        if self.guardrail.llm_reviewer is not None:
            return False
        try:
            cleaned = tool.validate(tu.input or {})
            argv = tool.build_argv(cleaned)
        except ToolError:
            return False
        verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
        return verdict.decision is Decision.ALLOW

    def _record_tool_failure(
        self, counters: dict[str, int], tu: ToolUseBlock
    ) -> tuple[str, int] | None:
        """累计同一"工具+参数"调用的失败次数；达到阈值时返回 (工具名, 次数)。

        以工具名 + 规范化后的入参作为签名：模型在被拒后反复"重新枚举→再删"针对
        同一目标的调用会命中同一签名，使空转能被及时识别并中止。
        """
        limit = self.cfg.agent.max_repeated_tool_failures
        if limit <= 0:
            return None
        try:
            args_sig = json.dumps(tu.input or {}, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args_sig = repr(tu.input)
        signature = f"{tu.name}\x00{args_sig}"
        count = counters.get(signature, 0) + 1
        counters[signature] = count
        if count >= limit:
            return (tu.name, count)
        return None

    def _escalate_checklist_block(
        self, trace: Trace, tu: ToolUseBlock, prep, reason: str
    ) -> ToolResultBlock | None:
        """清理核对清单拦截破坏性写操作时，升级为人工审批而非让模型反复重试。

        返回 None 表示人工已批准，调用方应继续执行；返回 ToolResultBlock 表示终止
        （人工拒绝/超时，或当前不在运行线程无法发起审批）。终止文案明确要求模型
        停止重试与重新枚举，改为向用户报告该发现并等待人工处置。
        """
        path = str(prep.cleaned.get("path") or "")
        # confirm() 只能在拥有交互通道的运行线程执行；并行只读池线程不应到这里
        # （破坏性工具串行执行），但仍兜底，避免静默回退成可被模型反复重试的拒绝。
        if threading.get_ident() != self._active_run_thread_id:
            self.audit.event(trace, EventKind.ERROR, {
                "reason": "checklist_escalation_off_thread",
                "tool": tu.name, "path": path,
            })
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content=("[stop] 破坏性操作被清理核对清单拦截、需人工审批，但当前不在"
                         "运行线程，无法发起审批。请停止重试与重新枚举，向用户报告该发现"
                         "并等待人工处置。"),
            )
        self.audit.event(trace, EventKind.PLAN_UPDATE, {
            "event": "checklist_escalated_to_human",
            "tool": tu.name, "path": path, "detail": reason,
        })
        approved = False
        try:
            approved = self.confirm(
                confirm_adapter.for_checklist_block(tu.name, path, reason)
            )
        except Exception:
            approved = False
        if approved:
            self.audit.event(trace, EventKind.SAFETY_CHECK, {
                "user_confirmed": True, "tool": tu.name,
                "reason": "checklist_block_overridden",
            })
            return None
        self.audit.event(trace, EventKind.ERROR, {
            "reason": "checklist_escalation_denied",
            "tool": tu.name, "path": path,
        })
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=True,
            content=("[stop] 该破坏性操作未获人工批准（被清理核对清单拦截后已升级人工"
                     "审批，人工拒绝或超时）。请勿重试或重新枚举，直接向用户报告该发现"
                     f"并等待人工处置。\n原因: {reason}"),
        )

    def _safe_remediation_auto_approval_reason(self, trace: Trace, tu: ToolUseBlock, prep) -> str:
        if not self.auto_approve_safe_remediation:
            return ""
        if tu.name in _AUTO_APPROVE_FILE_REMEDIATION_TOOLS:
            path = str(prep.cleaned.get("path", ""))
            op = "delete" if tu.name in {"fs_delete_file", "log_delete_file"} else "truncate"
            from kyagent.safety.write_preflight import classify_write_preflight

            result = classify_write_preflight(path, operation=op)
            if result.allowed:
                return f"write preflight allowed ({result.rule_id})"
            return ""
        if tu.name in _AUTO_APPROVE_DEDICATED_REMEDIATION_TOOLS:
            try:
                prep.tool.validate(prep.cleaned)
            except ToolError:
                return ""
            return "dedicated remediation tool preflight passed"
        if tu.name != "process_kill":
            return ""
        signal = str(prep.cleaned.get("signal", "TERM")).upper()
        if signal not in _AUTO_APPROVE_PROCESS_SIGNALS:
            return ""
        try:
            pid = int(prep.cleaned.get("pid"))
        except (TypeError, ValueError):
            return ""
        if pid < 2:
            return ""
        for line in self._evidence_lines_for_pid(trace, pid):
            if self._line_confirms_safe_process_target(trace, line):
                return "target PID matched prior read-only evidence and user intent"
        return ""

    @staticmethod
    def _evidence_lines_for_pid(trace: Trace, pid: int) -> list[str]:
        pid_re = re.compile(rf"(?<!\d){pid}(?!\d)")
        read_only_result_seqs = {
            (event.payload or {}).get("execution_result_seq")
            for event in trace.events
            if event.kind is EventKind.PERCEPTION
            and (event.payload or {}).get("execution_result_seq") is not None
        }
        lines: list[str] = []
        for event in trace.events:
            if event.kind is not EventKind.EXECUTION_RESULT:
                continue
            if event.seq not in read_only_result_seqs:
                continue
            payload = event.payload or {}
            text = "\n".join(
                str(payload.get(key) or "")
                for key in ("stdout", "stderr")
                if payload.get(key)
            )
            for line in text.splitlines():
                if pid_re.search(line):
                    lines.append(line)
        return lines

    @staticmethod
    def _user_input_text(trace: Trace) -> str:
        for event in trace.events:
            if event.kind is EventKind.USER_INPUT:
                return str((event.payload or {}).get("text") or "")
        return ""

    def _scope_context_text(self, current_input: str) -> str:
        # Scope for checklist gating is derived from the current turn only so a
        # prior cleanup ticket cannot pollute a port/cron/lock turn in Web chat.
        return current_input

    def _line_confirms_safe_process_target(self, trace: Trace, line: str) -> bool:
        lowered = line.lower()
        user_text = self._user_input_text(trace)

        if any(marker in lowered for marker in _PROTECTED_PROCESS_MARKERS):
            return False
        if self._process_named_as_protected_in_user_text(user_text, line):
            return False
        if self._deleted_open_file_release_confirmed(user_text, line):
            return True
        for port in _PORT_RE.findall(line):
            if self._port_is_requested_target(user_text, port):
                return True
        if self._evidence_matches_user_named_target(user_text, line):
            return True
        if self._evidence_in_configured_runtime_root(user_text, line):
            return True
        return False

    @staticmethod
    def _process_named_as_protected_in_user_text(user_text: str, line: str) -> bool:
        lowered_line = line.lower()
        for marker in _PROTECTED_PROCESS_MARKERS:
            if marker not in lowered_line:
                continue
            protected_near = (
                rf"(不要|别|保留|对照|必须|保持|protected|do not|don't|dont|keep|"
                rf"不要动|别动|不要杀|别杀)[^。；;\n]{{0,80}}{re.escape(marker)}"
                rf"|{re.escape(marker)}[^。；;\n]{{0,80}}"
                rf"(不要|别|保留|对照|必须|保持|protected|do not|don't|dont|keep|"
                rf"不要动|别动|不要杀|别杀)"
            )
            if re.search(protected_near, user_text, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _deleted_open_file_release_confirmed(user_text: str, line: str) -> bool:
        lowered = line.lower()
        if not any(marker in lowered for marker in _DELETED_FILE_EVIDENCE_MARKERS):
            return False
        return bool(
            re.search(
                r"(deleted|unlinked|句柄|df|du|临时|temp|export|释放|reclaim|"
                r"deleted-but-open|已删|unlink)",
                user_text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _evidence_matches_user_named_target(user_text: str, line: str) -> bool:
        if not _ACTION_INTENT_RE.search(user_text):
            return False
        lowered_line = line.lower()
        for token in re.findall(r"[\w][\w.-]{2,}", user_text):
            tl = token.lower()
            if tl in _SKIP_USER_TOKENS or len(tl) < 4:
                continue
            protected_near = (
                rf"(不要|别|保留|对照|必须|保持|protected|do not|don't|dont|keep|"
                rf"不要动|别动|不要杀|别杀)[^。；;\n]{{0,80}}{re.escape(tl)}"
                rf"|{re.escape(tl)}[^。；;\n]{{0,80}}"
                rf"(不要|别|保留|对照|必须|保持|protected|do not|don't|dont|keep|"
                rf"不要动|别动|不要杀|别杀)"
            )
            if re.search(protected_near, user_text, re.IGNORECASE):
                continue
            if tl in lowered_line:
                return True
        return False

    @staticmethod
    def _evidence_in_configured_runtime_root(user_text: str, line: str) -> bool:
        roots = _configured_auto_approve_runtime_roots()
        if not roots:
            return False
        if not _ACTION_INTENT_RE.search(user_text):
            return False
        lowered = line.lower()
        for root in roots:
            normalized = root.strip().rstrip("/\\").lower()
            if not normalized:
                continue
            pattern = rf"(?<![\w.-]){re.escape(normalized)}(?=$|[\s/\\:;,'\")\]])"
            if re.search(pattern, lowered):
                return True
        return False

    @staticmethod
    def _port_is_requested_target(user_text: str, port: str) -> bool:
        protected_near = (
            rf"(不要|别|保留|对照|protected|do not|don't|dont|keep)[^。；;\n]{{0,60}}{port}"
            rf"|{port}[^。；;\n]{{0,60}}(不要|别|保留|对照|protected|do not|don't|dont|keep)"
        )
        if re.search(protected_near, user_text, re.IGNORECASE):
            return False
        target_near = (
            rf"(释放|结束|终止|杀|占用|旧|preview|stale|old|release|terminate|kill)[^。；;\n]{{0,80}}{port}"
            rf"|{port}[^。；;\n]{{0,80}}(释放|结束|终止|杀|占用|旧|preview|stale|old|release|terminate|kill)"
        )
        return bool(re.search(target_near, user_text, re.IGNORECASE))

    @staticmethod
    def _is_os_question(text: str) -> bool:
        text = text or ""
        return bool(
            _OS_ENGLISH_QUESTION_RE.search(text)
            or _OS_CHINESE_QUESTION_RE.search(text)
        )

    @staticmethod
    def _has_perception_evidence(trace: Trace) -> bool:
        return any(
            event.kind is EventKind.PERCEPTION and event.payload.get("evidence_id")
            for event in trace.events
        )

    def _default_evidence_gate_tool_use(self) -> ToolUseBlock | None:
        preferred = [
            "sys_uptime",
            "sys_loadavg",
            "sys_memory",
            "fs_df",
            "process_list",
            "svc_failed",
            "net_listen",
        ]
        for name in preferred:
            tool = self.registry.get(name)
            if tool is None or not tool.read_only:
                continue
            if tool.name in _NON_PERCEPTION_READ_ONLY_TOOLS:
                continue
            if not tool.name.startswith(_OS_PERCEPTION_TOOL_PREFIXES):
                continue
            try:
                cleaned = tool.validate({})
                tool.build_argv(cleaned)
            except ToolError:
                continue
            return ToolUseBlock(
                id=f"evidence-gate-{uuid.uuid4().hex[:8]}",
                name=tool.name,
                input={},
            )
        return None

    # 工具名前缀 → (中文动词, 取目标用的 input 键)。用于把工具调用渲染成
    # 一句人话（"读取 /var/log"），供 web / TUI 直接展示，避免裸露 {iterations}。
    _TOOL_VERB_PREFIX: tuple[tuple[str, str, str], ...] = (
        ("file_cleanup_candidates", "扫描可清理文件", "path"),
        ("fs_delete_file", "删除文件", "path"),
        ("fs_truncate", "清空文件", "path"),
        ("fs_find", "查找文件", "pattern"),
        ("fs_ls", "查看目录", "path"),
        ("fs_du", "统计磁盘占用", "path"),
        ("fs_df", "查看磁盘空间", ""),
        ("fs_", "检查文件系统", "path"),
        ("pkg_install", "安装软件包", "package"),
        ("pkg_update_all", "更新全部软件包", ""),
        ("pkg_security_upgrade", "安装安全更新", ""),
        ("pkg_update", "更新软件包", "package"),
        ("pkg_reinstall", "重装软件包", "package"),
        ("pkg_remove", "卸载软件包", "package"),
        ("pkg_clean_cache", "清理软件包缓存", ""),
        ("pkg_rebuild_db", "重建软件包数据库", ""),
        ("pkg_", "查询软件包", "package"),
        ("git_", "查看 git 信息", ""),
        ("cron_d_disable", "禁用定时任务", ""),
        ("cron_", "检查定时任务", ""),
        ("la_", "检查 LoongArch 兼容性", ""),
        ("verify_", "校验", ""),
        ("plan_", "查看计划", ""),
    )

    @classmethod
    def _tool_action_label(cls, name: str, argv: list[str] | None,
                           tool_input: dict | None) -> str:
        """把一次工具调用渲染成一句中文人话（Cursor 式 "正在做 X"）。

        纯读 name / argv / input，不执行任何东西。优先用前缀动词 + 目标，
        其次回退到真实命令行（argv），最后回退到工具名。
        """
        special = {
            "todo_read": "查看任务清单",
            "todo_write": "更新任务清单",
            "ask_user_choice": "请你做个选择",
            "submit_rca_report": "提交根因分析报告",
        }
        if name in special:
            return special[name]

        def _target() -> str:
            inp = tool_input or {}
            for key in ("path", "package", "pattern", "target", "name", "file", "query"):
                val = inp.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return ""

        for prefix, verb, key in cls._TOOL_VERB_PREFIX:
            if name == prefix or name.startswith(prefix):
                inp = tool_input or {}
                tgt = ""
                if key:
                    val = inp.get(key)
                    if isinstance(val, str) and val.strip():
                        tgt = val.strip()
                if not tgt:
                    tgt = _target()
                return f"{verb} {tgt}".strip() if tgt else verb

        # 未识别工具：有真实命令行就展示命令，否则展示目标 / 工具名
        if argv:
            cmd = " ".join(str(a) for a in argv)
            if len(cmd) > 120:
                cmd = cmd[:117] + "…"
            return f"执行 {cmd}"
        tgt = _target()
        return f"调用 {name} {tgt}".strip() if tgt else f"调用 {name}"

    def _handle_tool_use(self, trace: Trace, tu: ToolUseBlock,
                        notes: list[str], parallel_read_only: bool = False) -> ToolResultBlock:
        """对外入口：包一层 try/finally，确保 tool_call_end 一定发出。

        入口先发一次带人话 action 的 tool_call_start（由 name+input 推导）；
        prepare_call 成功后 inner 会再发一次带 argv 的 tool_call_start 补充命令行。
        """
        self._emit(ProgressEvent(
            kind="tool_call_start",
            tool=tu.name,
            meta={"action": self._tool_action_label(tu.name, None, tu.input)},
        ))
        ok = False
        result_block: ToolResultBlock | None = None
        try:
            result_block = self._handle_tool_use_inner(
                trace, tu, notes, parallel_read_only=parallel_read_only
            )
            ok = (not result_block.is_error)
            return result_block
        finally:
            self._emit(ProgressEvent(
                kind="tool_call_end",
                tool=tu.name,
                text=(result_block.content[:200] if result_block else ""),
                meta={"ok": ok, "action": self._tool_action_label(tu.name, None, tu.input)},
            ))

    def _handle_tool_use_inner(self, trace: Trace, tu: ToolUseBlock,
                               notes: list[str],
                               parallel_read_only: bool = False) -> ToolResultBlock:
        # 特判：ask_user_choice 不走 ExecutionProxy / 安全护栏流水线，
        # 它是纯逻辑工具（UI 交互），单独路由。
        if tu.name == "ask_user_choice":
            tool = self.registry.get(tu.name)
            if tool is None:
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content="未知工具：ask_user_choice"
                )
            try:
                cleaned = tool.validate(tu.input or {})
            except ToolError as e:
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "invalid_args", "tool": tu.name, "detail": str(e),
                })
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{e}"
                )
            tu = ToolUseBlock(id=tu.id, name=tu.name, input=cleaned)
            return self._handle_user_choice(trace, tu)
        if tu.name == "todo_read":
            return self._handle_todo_read(trace, tu.id)
        if tu.name == "todo_write":
            return self._handle_todo_write(trace, tu)
        if tu.name == "submit_rca_report":
            tool = self.registry.get(tu.name)
            if tool is None:
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content="未知工具：submit_rca_report"
                )
            try:
                cleaned = tool.validate(tu.input or {})
            except ToolError as e:
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "invalid_args", "tool": tu.name, "detail": str(e),
                })
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True, content=f"工具参数非法：{e}"
                )
            return self._handle_rca_report(trace, tu.id, cleaned)

        tool = self.registry.get(tu.name)
        if tool is None:
            self.audit.event(trace, EventKind.ERROR,
                             {"reason": "unknown_tool", "tool": tu.name})
            return ToolResultBlock(tool_use_id=tu.id, is_error=True,
                                   content=f"未知工具：{tu.name}")

        # 1. validate + build_argv + TOOL_REQUEST + PERCEPTION（共享流水线）
        prep = prepare_call(tool, tu.input or {}, trace=trace, audit=self.audit)
        if isinstance(prep, PipelineError):
            return ToolResultBlock(tool_use_id=tu.id, is_error=True, content=prep.detail)

        # argv 已就绪：补一次 tool_call_start，让 UI 看到具体命令行 + 精炼人话
        self._emit(ProgressEvent(
            kind="tool_call_start",
            tool=tu.name,
            argv=list(prep.argv),
            meta={"action": self._tool_action_label(
                tu.name, list(prep.argv), prep.cleaned or tu.input)},
        ))

        # 清理核对清单拦截破坏性写操作时，不再把"重新枚举再重试"的提示回灌给模型
        # （那会让模型误以为是拼写/枚举问题而无限重试），而是升级为人工审批。
        human_approved_destructive = False
        if tu.name in _AUTO_APPROVE_FILE_REMEDIATION_TOOLS:
            checklist = self._file_remediation_checklist
            if checklist is not None:
                checklist_error = checklist.pre_write_error(
                    str(prep.cleaned.get("path", "")),
                    self._user_input_text(trace),
                )
                if checklist_error:
                    notes.append(f"file remediation checklist blocked {tu.name}")
                    self.audit.event(trace, EventKind.PLAN_UPDATE, {
                        "event": "file_remediation_checklist_required",
                        "reason": "write_without_complete_candidate_list",
                        "tool": tu.name,
                        "path": prep.cleaned.get("path"),
                        "detail": checklist_error,
                    })
                    decision = self._escalate_checklist_block(
                        trace, tu, prep, checklist_error
                    )
                    if decision is not None:
                        return decision
                    human_approved_destructive = True

        # 2. 安全护栏（即便是 read_only 工具也过一遍，防止参数注入）
        verdict = check_safety(prep, trace=trace, audit=self.audit, guardrail=self.guardrail)

        if verdict.decision is Decision.DENY:
            notes.append(f"已拦截 {tu.name}: {verdict.risk.value}")
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content=("[denied] 工具调用被安全护栏拒绝。\n"
                         f"风险等级: {verdict.risk.value}\n"
                         + "\n".join(verdict.rationale)),
            )

        if verdict.decision is Decision.CONFIRM and human_approved_destructive:
            # 人工已在清理核对清单升级流程中批准这次破坏性写操作，不再重复弹窗。
            pass
        elif verdict.decision is Decision.CONFIRM:
            # 兜底：confirm() 只能在当前 Agent.ask turn 的拥有线程执行。
            # TUI/CLI 通常是 MainThread；Web/FastAPI 会把 ask 放进 worker
            # 线程。并行工具池线程即使意外拿到 CONFIRM 也必须拒绝，
            # 防止多个 tool worker 争抢同一个交互通道。
            if threading.get_ident() != self._active_run_thread_id:
                notes.append(f"非主线程/非运行线程下 CONFIRM 默认拒绝 {tu.name}")
                self.audit.event(trace, EventKind.ERROR,
                                 {"reason": "confirm_in_worker_denied", "tool": tu.name})
                return ToolResultBlock(
                    tool_use_id=tu.id, is_error=True,
                    content=("[denied] 工具需要二次确认，但当前调度在非主线程，"
                             f"已自动拒绝（risk={verdict.risk.value}）"),
                )
            auto_reason = self._safe_remediation_auto_approval_reason(trace, tu, prep)
            if auto_reason:
                approved = True
                notes.append(f"已自动放行受控修复工具 {tu.name}: {auto_reason}")
                self.audit.event(trace, EventKind.SAFETY_CHECK, {
                    "auto_confirmed": True,
                    "tool": tu.name,
                    "reason": auto_reason,
                    "argv": prep.argv,
                })
            else:
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

        if (
            not human_approved_destructive
            and tu.name in _FILE_MUTATION_TOOLS
            and self._file_remediation_checklist is not None
        ):
            write_path = str(prep.cleaned.get("path") or "")
            checklist_err = self._file_remediation_checklist.pre_write_error(
                write_path, self._user_input_text(trace)
            )
            if checklist_err:
                notes.append("file remediation checklist blocked mutation tool")
                self.audit.event(trace, EventKind.ERROR, {
                    "reason": "file_remediation_checklist_blocked_write",
                    "tool": tu.name,
                    "path": write_path,
                    "detail": checklist_err,
                })
                decision = self._escalate_checklist_block(
                    trace, tu, prep, checklist_err
                )
                if decision is not None:
                    return decision
                human_approved_destructive = True

        # 3. 落地执行 + 格式化（共享流水线，含 stderr 拼接 + 长度截断）
        _, formatted, content = execute_and_format(
            prep, trace=trace, audit=self.audit, executor=self.executor,
            parallel_read_only=parallel_read_only,
        )
        if self._file_remediation_checklist is not None:
            if tu.name in _FILE_MUTATION_TOOLS:
                self._file_remediation_checklist.record_write_result(
                    str(prep.cleaned.get("path") or ""),
                    formatted.ok,
                )
            elif tool.read_only:
                self._file_remediation_checklist.record_read_result(
                    tu.name, prep.cleaned, content if formatted.ok else ""
                )
        if self._process_remediation_checklist is not None:
            if tu.name == "process_kill" and formatted.ok:
                self._process_remediation_checklist.record_kill()
            elif tool.read_only and formatted.ok:
                self._process_remediation_checklist.record_read_result(
                    tu.name, prep.cleaned, content, formatted.data
                )
        return ToolResultBlock(tool_use_id=tu.id, is_error=not formatted.ok, content=content)

    def _handle_user_choice(self, trace: Trace, tu: ToolUseBlock) -> ToolResultBlock:
        """处理 ask_user_choice：解析选项 → 审计 → 通过 progress 通知 UI →
        同步等 on_user_choice 回调 → 校验返回值合法 → 转 ToolResultBlock。

        没有 ExecutionProxy 介入：不构 argv、不过 Guardrail。审计照样记，
        以便事后能复盘"agent 问过用户什么 / 用户选了什么"。
        """
        args = tu.input or {}
        question = str(args.get("question", "")).strip()
        raw_options = args.get("options") or []
        options: list[UserChoiceOption] = [
            UserChoiceOption(
                label=str(o.get("label", o.get("value", ""))),
                value=str(o.get("value", "")),
                description=str(o.get("description", "")),
            )
            for o in raw_options
            if isinstance(o, dict) and o.get("value")
        ]

        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "ask_user_choice",
            "question": question,
            "options": [{"value": o.value, "label": o.label} for o in options],
        })

        # 推送给 UI；TUI 可据此渲染选项卡片，但用户作答仍走 on_user_choice 回调
        self._emit(ProgressEvent(
            kind="user_choice",
            text=question,
            meta={"options": [
                {"label": o.label, "value": o.value, "description": o.description}
                for o in options
            ]},
        ))

        if self.auto_approve_safe_remediation:
            self.audit.event(trace, EventKind.EXECUTION_RESULT, {
                "tool": "ask_user_choice",
                "chosen": "",
                "stdout": "",
                "auto_approve_safe_remediation": True,
                "reason": "choice_confirmation_disabled_in_noninteractive_remediation",
            })
            return ToolResultBlock(
                tool_use_id=tu.id,
                is_error=True,
                content=(
                    "[user_choice] 当前为非交互安全修复模式，不能用 ask_user_choice 等待确认。"
                    "请基于已有只读证据直接调用具体受控修复工具；Agent 会通过安全护栏和"
                    "write preflight 自动裁决是否放行。"
                ),
            )

        # 同步阻塞拿用户结果；on_user_choice 可能由 UI 弹窗驱动
        try:
            chosen = self.on_user_choice(
                UserChoice(question=question, options=options)
            )
        except Exception:
            chosen = ""
        chosen = (chosen or "").strip()
        valid_values = {o.value for o in options}
        if chosen and chosen not in valid_values:
            # 拒绝非法值：UI 不应给 LLM 编造选项的机会
            chosen = ""

        self.audit.event(trace, EventKind.EXECUTION_RESULT, {
            "tool": "ask_user_choice",
            "chosen": chosen,
            "stdout": chosen,
        })

        if not chosen:
            return ToolResultBlock(
                tool_use_id=tu.id, is_error=True,
                content="[user_choice] 用户未做出选择或选项无效。",
            )
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=False,
            content=f"用户选择: {chosen}",
        )

    def _handle_rca_report(
        self, trace: Trace, tool_use_id: str, report_args: dict
    ) -> ToolResultBlock:
        """Validate and persist an evidence-backed RCA diagnosis."""
        self.audit.event(trace, EventKind.TOOL_REQUEST, {
            "tool": "submit_rca_report",
            "playbook": report_args.get("playbook"),
            "evidence_ids": report_args.get("evidence_ids", []),
        })
        available = {
            str(event.payload["evidence_id"])
            for event in trace.events
            if event.kind is EventKind.PERCEPTION and event.payload.get("evidence_id")
        }
        try:
            report = validate_report(
                report_args,
                playbooks=load_playbooks(self.cfg.resolve(self.cfg.rca.playbooks_file)),
                available_evidence=available,
            )
        except ValueError as exc:
            self.audit.event(trace, EventKind.ERROR, {
                "reason": "invalid_rca_report", "detail": str(exc),
            })
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content=f"RCA 报告非法：{exc}",
            )
        payload = asdict(report)
        payload["evidence_ids"] = list(report.evidence_ids)
        payload["recommendations"] = list(report.recommendations)
        self.audit.event(trace, EventKind.DIAGNOSIS, payload)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            is_error=False,
            content="RCA 报告已记录：" + json.dumps(payload, ensure_ascii=False),
        )

    @staticmethod
    def _blocks_to_dict(am: AssistantMessage) -> list[dict]:
        out: list[dict] = []
        for b in am.blocks:
            if isinstance(b, TextBlock):
                out.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return out


# ---- 便捷入口 -------------------------------------------------------------


def build_agent(config_path: str | None = None, confirm: ConfirmFn = _auto_deny,
                on_progress: ProgressCallback | None = None,
                on_user_choice: UserChoiceFn | None = None) -> Agent:
    cfg = load_config(config_path)
    return Agent.from_config(cfg, confirm=confirm, on_progress=on_progress,
                             on_user_choice=on_user_choice)
