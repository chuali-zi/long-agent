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

import hashlib
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
    """无状态匹配器，可被多个组件共享。

    内置一个按 (cmdline, rules_version) 缓存的小型 LRU。
    rules_version 是规则集的稳定指纹（id + risk + pattern），任何规则集变更都会
    自然失效旧条目；同一个进程内重复出现的同一条 cmdline 直接命中缓存。
    """

    _CACHE_MAX = 1024

    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._version = self._fingerprint(rules)

    @classmethod
    def from_yaml(cls, path: str) -> "RuleEngine":
        return cls(load_rules(path))

    @staticmethod
    def _fingerprint(rules: list[Rule]) -> str:
        h = hashlib.sha256()
        for r in rules:
            h.update(r.id.encode("utf-8"))
            h.update(b"\x1f")
            h.update(r.risk.value.encode("utf-8"))
            h.update(b"\x1f")
            h.update((r.pattern.pattern if r.pattern else "").encode("utf-8"))
            h.update(b"\x1f")
            h.update((r.command or "").encode("utf-8"))
            h.update(b"\x1f")
            h.update(",".join(r.flags_any).encode("utf-8"))
            h.update(b"\x1f")
            h.update(",".join(r.flags_all).encode("utf-8"))
            h.update(b"\x1f")
            h.update(",".join(r.target_in).encode("utf-8"))
            h.update(b"\x1e")
        return h.hexdigest()[:12]

    # ---- 匹配 ----------------------------------------------------------

    def scan_cmdline(self, cmdline: str) -> list[Hit]:
        """对原始 shell 字符串做扫描；先 pattern，再切词后 argv 维度。

        热路径走进程级 LRU；rules_version 进入 key，规则集变化时旧条目失效。
        """
        cached = _scan_cached(cmdline, self._version, id(self))
        if cached is not None:
            return list(cached)

        hits = self._scan_uncached(cmdline)
        _scan_store(cmdline, self._version, id(self), tuple(hits))
        return hits

    def _scan_uncached(self, cmdline: str) -> list[Hit]:
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

    def cache_clear(self) -> None:
        """显式清空缓存。通常仅在测试或热重载时调用。"""
        _scan_cached.cache_clear()

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


# ---- 进程级 LRU 缓存 ------------------------------------------------------
# Key 三元组：(cmdline, rules_version, engine_id)
#   - cmdline      : 直接查表
#   - rules_version: 规则集指纹，规则变更后旧条目自动失效
#   - engine_id    : 多实例不互相串味（测试场景常见）
# 使用插入有序 dict + 手动 pop 实现简单 LRU；写入是 dict[key]=value 单步原子，
# 不需要 GIL 之外的额外同步。

_MANUAL_CACHE: dict[tuple[str, str, int], tuple[Hit, ...]] = {}


def _scan_cached(cmdline: str, version: str, engine_id: int) -> tuple[Hit, ...] | None:
    return _MANUAL_CACHE.get((cmdline, version, engine_id))


def _scan_store(cmdline: str, version: str, engine_id: int, hits: tuple[Hit, ...]) -> None:
    _MANUAL_CACHE[(cmdline, version, engine_id)] = hits
    if len(_MANUAL_CACHE) > RuleEngine._CACHE_MAX:
        # 插入有序 dict：最早进入的就是 LRU 头
        oldest_key = next(iter(_MANUAL_CACHE))
        _MANUAL_CACHE.pop(oldest_key, None)


class _CacheHandle:
    @staticmethod
    def cache_clear() -> None:
        _MANUAL_CACHE.clear()


_scan_cached.cache_clear = _CacheHandle.cache_clear  # type: ignore[attr-defined]
