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
            located = shutil.which(cmd, path=os.pathsep.join(self.cfg.path_whitelist))
            if located is None:
                return ExecutionResult(
                    argv=final_argv, returncode=127, stdout="",
                    stderr=f"command not in whitelist PATH: {cmd}",
                    truncated=False, duration=0.0, skipped_reason="not_in_path",
                    sudo_used=sudo_used, run_as=run_as,
                )
            final_argv[0] = located

        clean_env = build_clean_env(self.cfg, extra=env)
        preexec = make_preexec_fn(self.cfg)

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

        返回 (final_argv, sudo_used, run_as)
        """
        target = self.cfg.account or self._current_user

        # 默认按受限账户跑
        if not requires_root:
            # 当前已经是目标账户：直接跑
            if self._current_user == target:
                return list(argv), False, self._current_user
            # 我是别人，需要降权到 target：sudo -n -u target
            return self._sudo_wrap(argv, target), True, target

        # requires_root=True
        if self.cfg.forbid_root and target != "root":
            # 策略禁止 root：直接拒绝
            return (
                ["/bin/false"],  # 占位失败命令
                False,
                target,
            )
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
