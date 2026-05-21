"""ExecutionProxy：把工具调用真正落地到 OS。

策略：
  - 始终以非 root 受限账户执行（默认 kyagent）
  - 需要提权的工具走 `sudo -n -u root <cmd>`，依赖 sudoers 白名单（见 configs/sudoers.kyagent）
  - 永远只接收 argv 列表（不走 shell），杜绝 shell 注入
  - timeout + output_cap + 干净 env
  - Windows 上以 mock 模式执行（仅用于本机开发），返回明确的 mock 标记
"""
from __future__ import annotations

import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from kyagent.executor.sandbox import SandboxConfig, build_clean_env, make_preexec_fn


@dataclass
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    duration: float
    timed_out: bool = False
    skipped_reason: str | None = None
    sudo_used: bool = False
    run_as: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "skipped_reason": self.skipped_reason,
            "sudo_used": self.sudo_used,
            "run_as": self.run_as,
        }


class ExecutionProxy:
    """所有 shell 落地都经过这里。"""

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._current_user = getpass.getuser()
        # 工具命令 → 绝对路径，进程级稳定（PATH 白名单不变）。
        self._which_cache: dict[str, str | None] = {}
        # 干净 env 的不可变模板：每次执行 dict-copy 而不是重新构造。
        self._env_template: dict[str, str] = build_clean_env(self.cfg)
        # preexec_fn 闭包工厂：闭包体本身无状态，可在所有 Popen 之间共享。
        self._shared_preexec = make_preexec_fn(self.cfg)

    @property
    def supports_parallel_tool_execution(self) -> bool:
        """是否允许 Agent 在 ThreadPool 中并发调度本 executor 的 self.run()。

        判据：仅当 preexec_fn 为 None 时为 True。

        背景：标准 ExecutionProxy 在 POSIX 上始终通过 preexec_fn 设置 setpgid
        与 RLIMIT。多线程父进程 fork() 后在子进程里执行任意 Python 回调
        存在 glibc malloc 死锁、信号语义丢失等已知风险（async-signal-safety）。
        在切换到 posix_spawn 或确认所有 preexec 操作 fork-safe 之前，本属性
        在 Linux 上**永远返回 False** —— 即 Agent 的并行工具调度链路在生产
        环境是 dormant 的（所有多工具回合都走串行）。

        Windows mock 模式下没有 preexec_fn，属性为 True；但 Agent 主循环
        另有 `sys.platform != "win32"` 一层 gate，所以 Windows 也不会走
        并行（mock 执行器无真实 I/O，并发也无收益）。

        这是 *未来扩展点*：换用 fork-safe 的子进程启动方式后，覆盖或撤掉
        此判据即可激活并行路径，届时 _is_parallel_safe + audit per-trace
        锁会真正发挥作用。
        """
        return self._shared_preexec is None

    def _resolve_command(self, cmd: str) -> str | None:
        cached = self._which_cache.get(cmd)
        if cached is not None or cmd in self._which_cache:
            return cached
        located = shutil.which(cmd, path=os.pathsep.join(self.cfg.path_whitelist))
        self._which_cache[cmd] = located
        return located

    # ---- 公共入口 ------------------------------------------------------

    def run(
        self,
        argv: list[str],
        *,
        requires_root: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecutionResult:
        if not argv:
            return ExecutionResult(
                argv=argv, returncode=2, stdout="", stderr="empty argv",
                truncated=False, duration=0.0, skipped_reason="empty_argv",
                run_as=self._current_user,
            )

        # Windows: 没有 POSIX 权限模型，进入 mock 模式
        if sys.platform == "win32":
            return self._run_windows_mock(argv, requires_root)

        return self._run_posix(argv, requires_root=requires_root, cwd=cwd, env=env, stdin=stdin)

    # ---- POSIX 真正落地 ------------------------------------------------

    def _run_posix(
        self,
        argv: list[str],
        *,
        requires_root: bool,
        cwd: str | None,
        env: dict[str, str] | None,
        stdin: str | None,
    ) -> ExecutionResult:
        # 1. 解析最终 argv（按需 sudo 包裹）
        final_argv, sudo_used, run_as = self._wrap_privilege(argv, requires_root)

        # 2. 命令必须能在 PATH 中找到（防注入）
        cmd = final_argv[0]
        if not os.path.isabs(cmd):
            located = self._resolve_command(cmd)
            if located is None:
                return ExecutionResult(
                    argv=final_argv, returncode=127, stdout="",
                    stderr=f"command not in whitelist PATH: {cmd}",
                    truncated=False, duration=0.0, skipped_reason="not_in_path",
                    sudo_used=sudo_used, run_as=run_as,
                )
            final_argv[0] = located

        # 干净 env：模板 + 调用方覆盖；高危变量在合并阶段被过滤。
        if env:
            clean_env = build_clean_env(self.cfg, extra=env)
        else:
            clean_env = dict(self._env_template)
        # preexec_fn 是无状态闭包，复用同一份。
        preexec = self._shared_preexec

        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                final_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                env=clean_env,
                cwd=cwd,
                preexec_fn=preexec,
                close_fds=True,
            )
        except FileNotFoundError as e:
            return ExecutionResult(
                argv=final_argv, returncode=127, stdout="", stderr=str(e),
                truncated=False, duration=0.0, skipped_reason="not_found",
                sudo_used=sudo_used, run_as=run_as,
            )
        except PermissionError as e:
            return ExecutionResult(
                argv=final_argv, returncode=126, stdout="", stderr=str(e),
                truncated=False, duration=0.0, skipped_reason="permission_denied",
                sudo_used=sudo_used, run_as=run_as,
            )

        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(
                input=stdin.encode() if stdin else None,
                timeout=self.cfg.timeout,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_group(proc)
            stdout_b, stderr_b = proc.communicate()

        duration = time.monotonic() - start

        # 输出截断
        stdout, stdout_truncated = self._truncate(stdout_b)
        stderr, stderr_truncated = self._truncate(stderr_b)

        return ExecutionResult(
            argv=final_argv,
            returncode=proc.returncode if not timed_out else -signal.SIGTERM,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
            duration=duration,
            timed_out=timed_out,
            sudo_used=sudo_used,
            run_as=run_as,
        )

    # ---- Windows mock 模式 ---------------------------------------------

    def _run_windows_mock(self, argv: list[str], requires_root: bool) -> ExecutionResult:
        """Windows 上不真正执行 Linux 工具；返回 mock 结果给开发态用。"""
        return ExecutionResult(
            argv=argv,
            returncode=0,
            stdout=(
                f"[mock][win32] would execute: {' '.join(argv)}\n"
                f"[mock][win32] requires_root={requires_root}\n"
                f"[mock][win32] real execution requires Kylin / Linux host.\n"
            ),
            stderr="",
            truncated=False,
            duration=0.0,
            skipped_reason="windows_mock",
            sudo_used=False,
            run_as="mock",
        )

    # ---- 工具函数 ------------------------------------------------------

    def _wrap_privilege(
        self,
        argv: list[str],
        requires_root: bool,
    ) -> tuple[list[str], bool, str]:
        """根据 requires_root 与当前用户决定是否包裹 sudo。

        语义（贴合赛题"最小权限代理执行：非必要不使用 root"）：
          - 不需要 root → 始终在受限账户下跑（必要时 sudo -n -u kyagent 降权）
          - 需要 root  → 通过 ``sudo -n -u root`` 走 sudoers 白名单
              · 是否真的能跑由 ``configs/sudoers.kyagent`` 中的 NOPASSWD 决定，
                不在白名单的命令会被 sudo 自己拒绝（非 0 退出，stderr 有解释），
                这就是"最小权限"的边界。
              · 仅当配置项 ``forbid_root_strict=true`` 时彻底拒绝所有 root 提升
                （绕过 sudoers），用于"演示模式 / 无 sudoers 的高合规场景"。

        修正了原 bug（codex 指控 #1）：
            原代码 ``if forbid_root and target != "root": return ["/bin/false"]``
            把"非必要不用 root"误读成"任何 requires_root 一律拒绝"，让 sudoers
            白名单成了死代码。新代码保留通过 sudoers 走 root 的合法路径。

        返回 (final_argv, sudo_used, run_as)
        """
        target = self.cfg.account or self._current_user

        # 不需要提权：默认按受限账户跑
        if not requires_root:
            if self._current_user == target:
                return list(argv), False, self._current_user
            # 我是别人，需要降权到 target：sudo -n -u target
            return self._sudo_wrap(argv, target), True, target

        # 需要 root
        if self.cfg.forbid_root_strict:
            # 严格模式：彻底禁止 root，绕过 sudoers。占位失败命令明确告诉调用方
            return (
                ["/bin/false"],
                False,
                target,
            )
        # 正常模式（赛题语义"非必要不 root"）：走 sudoers 让其自决
        return self._sudo_wrap(argv, "root"), True, "root"

    def _sudo_wrap(self, argv: list[str], target_user: str) -> list[str]:
        # -n: 非交互式；-u: 指定运行用户；-E 不带，因为我们自建 env
        return ["sudo", "-n", "-u", target_user, "--"] + list(argv)

    def _truncate(self, raw: bytes) -> tuple[str, bool]:
        truncated = False
        if len(raw) > self.cfg.output_cap:
            raw = raw[: self.cfg.output_cap]
            truncated = True
        try:
            return raw.decode("utf-8", errors="replace"), truncated
        except Exception:
            return repr(raw), truncated

    def _terminate_group(self, proc: subprocess.Popen) -> None:
        if sys.platform == "win32":
            proc.terminate()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(0.3)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass
