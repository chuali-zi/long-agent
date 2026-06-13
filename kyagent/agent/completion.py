"""Completion criteria for remediation turns."""
from __future__ import annotations

from dataclasses import dataclass, field

from kyagent.agent.scope import RemediationScope, normalize_abs_path


@dataclass
class CompletionReport:
    done: bool
    confirmed: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    not_in_scope: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["## 完成状态"]
        lines.append(f"- 总体: {'已完成' if self.done else '未完成'}")
        if self.confirmed:
            lines.append("- 已确认: " + "; ".join(self.confirmed))
        if self.unchecked:
            lines.append("- 未检查: " + "; ".join(self.unchecked))
        if self.uncovered:
            lines.append("- 未覆盖: " + "; ".join(self.uncovered))
        if self.not_in_scope:
            lines.append("- 不在本次范围: " + "; ".join(self.not_in_scope))
        if self.pending_actions:
            lines.append("- 待处理: " + "; ".join(self.pending_actions))
        return "\n".join(lines)


def assess_file_cleanup_completion(
    scope: RemediationScope,
    *,
    required_roots: tuple[str, ...],
    scanned_roots: set[str],
    candidate_paths: set[str],
    executed_paths: list[str],
    verified_paths: set[str],
    protected_paths: set[str] | None = None,
) -> CompletionReport:
    protected = protected_paths or set()
    report = CompletionReport(done=True)

    for root in required_roots:
        if root in scanned_roots:
            report.confirmed.append(f"已枚举目录 {root}")
        else:
            report.unchecked.append(f"目录 {root}")
            report.done = False

    for path in sorted(protected):
        report.confirmed.append(f"已标记保护 {path}")

    pending_verify = [
        path for path in executed_paths
        if normalize_abs_path(path) not in verified_paths
    ]
    if pending_verify:
        report.pending_actions.extend(f"待复核删除/清空 {path}" for path in pending_verify)
        report.done = False

    deleted = [normalize_abs_path(p) for p in executed_paths if normalize_abs_path(p)]
    if deleted:
        report.confirmed.extend(f"已处理 {path}" for path in deleted)
    elif scope.actions & {"cleanup"} and candidate_paths:
        report.pending_actions.append("候选已发现但尚未执行 delete 类目标")
        report.done = False

    if not candidate_paths and required_roots and scanned_roots == set(required_roots):
        report.confirmed.append("范围内未发现额外可清理候选")

    return report
