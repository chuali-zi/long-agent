"""沙箱配置 + POSIX 资源限制。"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass
class SandboxConfig:
    """执行沙箱参数。"""

    # 运行账户（POSIX 上若不是当前用户则需要 sudo -u 切换）
    account: str = "kyagent"
    # 命令超时
    timeout: float = 30.0
    # 输出字节上限（stdout 和 stderr 各自）
    output_cap: int = 65536
    # 是否强制 PATH 白名单
    restrict_path: bool = True
    path_whitelist: tuple[str, ...] = (
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    )
    # POSIX rlimit
    rlimit_cpu_seconds: int = 60        # 单 CPU 秒
    rlimit_address_mb: int = 1024       # 虚拟地址 1G
    rlimit_fsize_mb: int = 32           # 单文件最大写入 32M
    rlimit_nofile: int = 256
    # 是否禁止 root 提权（赛题语义："非必要不使用 root"）
    # True  = 通过 sudoers 白名单走 sudo -n -u root（默认）
    # 严格禁止 root 提升请用下面的 forbid_root_strict
    forbid_root: bool = True
    # 严格模式：彻底拒绝任何 root 提升，绕过 sudoers。仅用于演示 / 无 sudoers 部署
    forbid_root_strict: bool = False
    # Allow Agent to run preflighted LOW/read-only tools in parallel worker threads.
    allow_parallel_read_only_tools: bool = True


def make_preexec_fn(cfg: SandboxConfig):
    """生成 POSIX preexec_fn：设置 rlimit、setpgid。

    在 Windows 上返回 None；subprocess 不接受该参数。
    """
    if sys.platform == "win32":
        return None

    import resource

    def _preexec():
        # 独立进程组，便于整组超时 kill
        try:
            os.setpgrp()
        except OSError:
            pass

        # CPU 时间 / 地址空间 / 单文件大小 / 句柄数
        try:
            resource.setrlimit(resource.RLIMIT_CPU,
                               (cfg.rlimit_cpu_seconds, cfg.rlimit_cpu_seconds))
        except (OSError, ValueError):
            pass
        try:
            mem_bytes = cfg.rlimit_address_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (OSError, ValueError):
            pass
        try:
            f_bytes = cfg.rlimit_fsize_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (f_bytes, f_bytes))
        except (OSError, ValueError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE,
                               (cfg.rlimit_nofile, cfg.rlimit_nofile))
        except (OSError, ValueError):
            pass

    return _preexec


def build_clean_env(cfg: SandboxConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造干净的子进程环境，过滤掉 LD_PRELOAD 等高危变量。"""
    base = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "USER": cfg.account,
        "LOGNAME": cfg.account,
        "SHELL": "/bin/sh",
        "TERM": os.environ.get("TERM", "dumb"),
    }
    if cfg.restrict_path:
        base["PATH"] = os.pathsep.join(cfg.path_whitelist)
    else:
        base["PATH"] = os.environ.get("PATH", os.pathsep.join(cfg.path_whitelist))
    if extra:
        # 严禁覆盖以下高风险变量
        forbidden = {
            "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PYTHONPATH", "BASH_ENV",
            "PATH", "HOME", "SHELL", "USER", "LOGNAME",
        }
        for k, v in extra.items():
            if k in forbidden:
                continue
            base[k] = v
    return base
