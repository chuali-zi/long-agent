"""规则匹配引擎。

匹配维度：
  - pattern  : 正则匹配完整 cmdline
  - command  : argv[0] basename 精确比较
  - flags    : 任一/全部 flag 出现
  - target   : 任一非 flag 参数是否落在受保护路径下

支持两种输入：
  - shell 字符串（cmdline）：内部用 shlex 切词
  - 结构化 argv (list[str])：直接使用

返回命中（Hit）列表，由 Guardrail 进一步合成 Verdict。
"""
from __future__ import annotations

import os
import posixpath
import shlex
from dataclasses import dataclass

from kyagent.safety.patterns import RiskLevel, Rule, load_rules


@dataclass
class Hit:
    rule_id: str
    risk: RiskLevel
    description: str
    matched: str

    def __str__(self) -> str:  # 便于审计读取
        return f"[{self.risk.value}] {self.rule_id}: {self.description} (matched: {self.matched!r})"


def _basename(s: str) -> str:
    return os.path.basename(s) or s


def _is_flag(token: str) -> bool:
    return token.startswith("-") and len(token) > 1


def _path_under(path: str, prefixes: list[str]) -> bool:
    """判断 path 是否落在任一 prefix 下。

    用 posixpath 归一化（目标是 Linux/麒麟），保证 Windows 开发态也能正确判断 '/etc' 这类绝对路径。
    """
    norm = posixpath.normpath(path)
    for prefix in prefixes:
        if prefix == "/" and norm.startswith("/"):
            return True
        prefix = posixpath.normpath(prefix)
        if norm == prefix or norm.startswith(prefix + "/"):
            return True
    return False


class RuleEngine:
    """无状态匹配器，可被多个组件共享。"""

    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: str) -> "RuleEngine":
        return cls(load_rules(path))

    # ---- 匹配 ----------------------------------------------------------

    def scan_cmdline(self, cmdline: str) -> list[Hit]:
        """对原始 shell 字符串做扫描；先 pattern，再切词后 argv 维度。"""
        hits: list[Hit] = []
        try:
            argv = shlex.split(cmdline, posix=True)
        except ValueError:
            argv = cmdline.split()

        for rule in self.rules:
            hit = self._match(rule, cmdline, argv)
            if hit:
                hits.append(hit)
        return hits

    def scan_argv(self, argv: list[str]) -> list[Hit]:
        cmdline = " ".join(shlex.quote(a) for a in argv)
        return self.scan_cmdline(cmdline) if cmdline else []

    # ---- 单条规则匹配 --------------------------------------------------

    def _match(self, rule: Rule, cmdline: str, argv: list[str]) -> Hit | None:
        matched_repr: str | None = None

        # 1. 正则
        if rule.pattern is not None:
            m = rule.pattern.search(cmdline)
            if not m:
                return None
            matched_repr = m.group(0)

        # 2. argv 维度（command + flags + target）
        if rule.command or rule.flags_any or rule.flags_all or rule.target_in:
            if not argv:
                return None

            if rule.command:
                if _basename(argv[0]) != rule.command:
                    return None

            flags_in_argv = {tok for tok in argv[1:] if _is_flag(tok)}

            # 容错：把 "-rf" 拆成 {-r, -f}，便于 flags_all 命中
            expanded_flags = set(flags_in_argv)
            for f in list(flags_in_argv):
                if f.startswith("-") and not f.startswith("--") and len(f) > 2:
                    expanded_flags.update(f"-{c}" for c in f[1:])

            if rule.flags_any:
                if not (set(rule.flags_any) & (flags_in_argv | expanded_flags)):
                    return None

            if rule.flags_all:
                if not set(rule.flags_all).issubset(flags_in_argv | expanded_flags):
                    return None

            if rule.target_in:
                positional = [t for t in argv[1:] if not _is_flag(t)]
                # 也考虑等号形式 of=/dev/sda 这种
                hit_any = False
                for tok in positional:
                    candidate = tok.split("=", 1)[1] if "=" in tok and tok.startswith("/") is False else tok
                    if _path_under(candidate, rule.target_in):
                        hit_any = True
                        matched_repr = matched_repr or f"target={candidate}"
                        break
                if not hit_any:
                    return None

            matched_repr = matched_repr or " ".join(argv[:4])

        if matched_repr is None:
            # 既无 pattern 也无 argv 条件，跳过
            return None

        return Hit(
            rule_id=rule.id,
            risk=rule.risk,
            description=rule.description,
            matched=matched_repr,
        )
