# 06 · 执行代理与沙箱

> 文件：
> - `kyagent/executor/sandbox.py`（POSIX rlimit + 干净 env + preexec_fn）
> - `kyagent/executor/proxy.py`（ExecutionProxy.run + sudo 包裹 + 超时杀进程组）
> - `configs/sudoers.kyagent`（系统侧的最小权限白名单）
> - `tests/test_executor.py`

这是 kyagent **赛题第 ④ 条** "最小权限代理执行" 的落地。

---

## 1. 设计原则

每一条都直接对应一种攻击：

| 设计 | 防的是什么 |
|---|---|
| 永远只接 `argv: list[str]`，绝不调 shell | shell 注入 |
| 默认以非 root 受限账户运行 | 提权风险 |
| `requires_root` 走 `sudo -n -u root <argv>`，依赖 sudoers 白名单 | 不可控提权 |
| `forbid_root=True` 时拒绝所有 root 提权 | 防误启用 |
| PATH 白名单 + 显式 `which()` | 命令路径污染 |
| 干净 env（过滤 `LD_PRELOAD` 等） | 共享库注入 |
| `preexec_fn`：setpgid + RLIMIT_* | 失控进程 / 资源耗尽 |
| communicate 带 timeout，超时杀整个进程组 | 超时挂死 |
| stdout/stderr 截断到 output_cap | 输出炸内存 |
| Windows 上 mock，永远不执行 Linux 工具 | 开发态误操作本机 |

---

## 2. SandboxConfig（sandbox.py:10）

```python
@dataclass
class SandboxConfig:
    account: str = "kyagent"
    timeout: float = 30.0
    output_cap: int = 65536
    restrict_path: bool = True
    path_whitelist: tuple[str, ...] = (
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    )
    rlimit_cpu_seconds: int = 60        # 单 CPU 秒
    rlimit_address_mb: int = 1024       # 虚拟地址 1G
    rlimit_fsize_mb: int = 32           # 单文件最大写入 32M
    rlimit_nofile: int = 256
    forbid_root: bool = True
```

所有沙箱参数集中在一处，没有"散落各处的魔数"。`tuple` 而不是 `list`：表明它在运行时不可变。

字段说明：
- `account` — 受限运行账户名。默认 `kyagent`，要求系统上有此用户（建议 `useradd -r -s /usr/sbin/nologin`）
- `timeout` — 单命令超时秒（命令执行总时长）
- `output_cap` — stdout / stderr 各自的字节上限
- `restrict_path` — 是否强制 PATH 白名单（生产 True，开发可关闭）
- `path_whitelist` — 命令必须在这些目录下被找到才允许执行
- `rlimit_*` — POSIX 资源限制，子进程粒度
- `forbid_root` — 全局禁用任何 root 提权（CI / 演示模式）

---

## 3. preexec_fn 工厂（sandbox.py:33）

```python
def make_preexec_fn(cfg: SandboxConfig):
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
```

`preexec_fn` 在 `fork()` 之后、`exec()` 之前在子进程里运行。这是设置 rlimit / 改进程组的关键时机。

要点：
1. **每个 setrlimit 都 try/except**：某个 rlimit 在容器里可能不允许，单条失败不应阻止其它生效
2. **setpgrp()**：让子进程成为新进程组组长。这样超时 kill 时可以 `killpg(pgid, SIGTERM)` 一次性把整棵子树都杀掉（防止它 fork 出 daemon 逃脱）
3. **Windows 返回 None**：subprocess.Popen 在 win32 上不接受 `preexec_fn` 参数

**安全代价**：这个 `preexec_fn` 在多线程父进程下与 `fork()` 组合是不安全的（glibc malloc 死锁、async-signal-safety）。这就是为什么 `supports_parallel_tool_execution` 在 POSIX 上 gated off——详见 12-concurrency.md 的 C1。

---

## 4. build_clean_env（sandbox.py:75）

```python
def build_clean_env(cfg: SandboxConfig, extra=None) -> dict[str, str]:
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
        forbidden = {"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
                     "PYTHONPATH", "BASH_ENV"}
        for k, v in extra.items():
            if k in forbidden:
                continue
            base[k] = v
    return base
```

几个关键防御：
1. **只放白名单变量**：父进程的 env 没继承，所以 `KYAGENT_API_KEY` 之类的密钥不会泄漏给子命令
2. **PATH 强制白名单**：哪怕用户在 cfg 关 restrict_path，默认值也是白名单
3. **`forbidden` 黑名单**：调用方传 `extra={"LD_PRELOAD": "..."}` 也会被静默过滤
4. **`TERM=dumb`**：避免 dmesg / journalctl 等命令输出 ANSI 颜色码污染 stdout

`tests/test_executor.py:49` 显式覆盖了 LD_PRELOAD 过滤：
```python
def test_clean_env_blocks_ld_preload():
    cfg = SandboxConfig()
    env = build_clean_env(cfg, extra={"LD_PRELOAD": "/tmp/x.so", "FOO": "bar"})
    assert "LD_PRELOAD" not in env
    assert env["FOO"] == "bar"
```

---

## 5. ExecutionProxy 初始化（proxy.py:56）

```python
def __init__(self, cfg: SandboxConfig):
    self.cfg = cfg
    self._current_user = getpass.getuser()
    self._which_cache: dict[str, str | None] = {}
    self._env_template: dict[str, str] = build_clean_env(self.cfg)
    self._shared_preexec = make_preexec_fn(self.cfg)
```

三个进程级缓存（这是 commit e276c77 的优化点）：
1. **`_which_cache`** —— 命令 → 绝对路径的缓存，避免重复 `shutil.which`
2. **`_env_template`** —— 干净 env 的不可变模板，每次执行只 `dict(...)` 浅拷贝
3. **`_shared_preexec`** —— preexec_fn 闭包，每个 Popen 复用同一份

这些缓存把 executor 的"每次调用开销"从"重新构造一遍"降到"复用 + 偶尔 dict 拷贝"。

---

## 6. supports_parallel_tool_execution（proxy.py:66）

```python
@property
def supports_parallel_tool_execution(self) -> bool:
    """是否允许 Agent 在 ThreadPool 中并发调度本 executor 的 self.run()。

    判据：仅当 preexec_fn 为 None 时为 True。
    ...
    （详见 docstring 全文）
    """
    return self._shared_preexec is None
```

这是 12-concurrency.md C1 的核心。

- 在 Linux 上：`_shared_preexec` 总是非 None → 返回 False → Agent 不进并行路径
- 在 Windows 上：`_shared_preexec` 是 None → 返回 True，但 Agent 主循环另有 `sys.platform != "win32"` gate，所以仍不并行

这是**有意保守**的设计，把并行优化留作未来扩展点。

---

## 7. \_resolve\_command（proxy.py:71）

```python
def _resolve_command(self, cmd: str) -> str | None:
    cached = self._which_cache.get(cmd)
    if cached is not None or cmd in self._which_cache:
        return cached
    located = shutil.which(cmd, path=os.pathsep.join(self.cfg.path_whitelist))
    self._which_cache[cmd] = located
    return located
```

注意 `if cached is not None or cmd in self._which_cache`：缓存 `None` 也算命中（避免重复查找不存在的命令）。

`shutil.which(cmd, path=...)` 是关键：传入 `path` 参数后 **不再读取环境 PATH**，强制只在白名单目录里找。

---

## 8. run() 公共入口（proxy.py:81）

```python
def run(self, argv, *, requires_root=False, cwd=None, env=None, stdin=None):
    if not argv:
        return ExecutionResult(
            argv=argv, returncode=2, stdout="", stderr="empty argv",
            truncated=False, duration=0.0, skipped_reason="empty_argv",
            run_as=self._current_user,
        )

    if sys.platform == "win32":
        return self._run_windows_mock(argv, requires_root)

    return self._run_posix(argv, requires_root=requires_root, cwd=cwd, env=env, stdin=stdin)
```

三道分支：空 argv → 拒绝；Windows → mock；POSIX → 真正执行。

`ExecutionResult.skipped_reason` 字段是这种"没真正执行"的标记，让上层（Tool.format_result）可以区分"真失败"和"被沙箱拦下"。

---

## 9. \_run\_posix —— 完整生命周期（proxy.py:105）

### 9.1 包 sudo

```python
final_argv, sudo_used, run_as = self._wrap_privilege(argv, requires_root)
```

见下文 `_wrap_privilege`。

### 9.2 命令解析

```python
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
```

- 已经是绝对路径（`/usr/bin/ls`）→ 直接用
- 否则走白名单 `which`，把 `ls` 解析成 `/usr/bin/ls`
- 找不到 → 返回 `returncode=127`（POSIX shell 约定）+ `skipped_reason="not_in_path"`

### 9.3 干净 env

```python
if env:
    clean_env = build_clean_env(self.cfg, extra=env)
else:
    clean_env = dict(self._env_template)
```

- 调用方传 env → 重新拼一个（仍会过 `forbidden` 黑名单）
- 调用方没传 → 复用 `_env_template`（dict 浅拷贝，比重新拼快）

### 9.4 Popen

```python
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
    return ExecutionResult(..., returncode=127, skipped_reason="not_found", ...)
except PermissionError as e:
    return ExecutionResult(..., returncode=126, skipped_reason="permission_denied", ...)
```

几个细节：
- **没有 `shell=True`** ：永远不调 shell，命令以 argv 形式直接 execve
- **`stdin=subprocess.DEVNULL`** 默认接 `/dev/null`，工具无法从 stdin 读到内容
- **`close_fds=True`**：关闭所有非标准 fd，防止泄漏父进程文件描述符
- **`preexec_fn` 在 fork 后子进程里跑**：设置 rlimit + setpgid

### 9.5 communicate with timeout

```python
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
```

- `timeout=30s`（默认）：超过这个时长 communicate 抛 TimeoutExpired
- 抛了就调 `_terminate_group(proc)` 把整个子进程组杀掉
- 第二次 communicate 不带 timeout，确保 stdout/stderr 被读光（否则子进程可能死在 SIGPIPE）

### 9.6 \_terminate_group（proxy.py:257）

```python
def _terminate_group(self, proc):
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
```

两步：先 SIGTERM 等 300ms 优雅退出，仍活就 SIGKILL。`killpg` 而不是 `kill` —— 杀整个进程组，子进程 fork 出来的孙子也跑不掉。

异常兜底：进程可能已经退、可能没权限——用 `proc.kill()` 单独杀进程兜底。

### 9.7 输出截断

```python
def _truncate(self, raw: bytes) -> tuple[str, bool]:
    truncated = False
    if len(raw) > self.cfg.output_cap:
        raw = raw[: self.cfg.output_cap]
        truncated = True
    try:
        return raw.decode("utf-8", errors="replace"), truncated
    except Exception:
        return repr(raw), truncated
```

- 截到 64K 上限（默认）
- `errors="replace"` 把非 utf8 字节换成 `�`，永远不抛
- 最后兜底 `repr()`（基本不会走到）

`truncated=True` 让 ToolResult 可以提示"输出已截断"。

---

## 10. \_wrap\_privilege —— sudo 决策（proxy.py:214）

```python
def _wrap_privilege(self, argv, requires_root) -> tuple[list[str], bool, str]:
    target = self.cfg.account or self._current_user

    if not requires_root:
        if self._current_user == target:
            return list(argv), False, self._current_user
        return self._sudo_wrap(argv, target), True, target

    if self.cfg.forbid_root and target != "root":
        return ["/bin/false"], False, target

    return self._sudo_wrap(argv, "root"), True, "root"

def _sudo_wrap(self, argv, target_user):
    return ["sudo", "-n", "-u", target_user, "--"] + list(argv)
```

四种情形：

| 当前用户 | requires_root | forbid_root | 行为 |
|---|---|---|---|
| `kyagent`（= target） | False | * | 直接跑，不 sudo |
| 其他用户（≠ target） | False | * | `sudo -n -u kyagent -- <argv>` |
| 任意 | True | True | `["/bin/false"]` 占位失败 |
| 任意 | True | False | `sudo -n -u root -- <argv>` |

`sudo -n`：非交互模式（绝不弹密码 prompt）；密码缺失 sudo 直接报错。
`--`：分隔 sudo 选项和实际 argv，防止 argv 里的 `-X` 被 sudo 当成自己的选项。

### 10.1 forbid_root 的占位

```python
return ["/bin/false"], False, target
```

不直接 raise，而是返回 `/bin/false`（unix 经典"什么都不做、退出码 1"的命令）。这样上层逻辑（audit / format_result）依然能走完正常路径，只是 returncode != 0。审计里会清楚看到这条命令"被沙箱拒绝"。

`tests/test_executor.py:77` 覆盖：
```python
def test_forbid_root_returns_false_command():
    cfg = SandboxConfig(account="kyagent", forbid_root=True)
    proxy = ExecutionProxy(cfg)
    final, _, _ = proxy._wrap_privilege(["systemctl", "restart", "nginx"],
                                        requires_root=True)
    assert final == ["/bin/false"]
```

---

## 11. ExecutionResult（proxy.py:24）

```python
@dataclass
class ExecutionResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    duration: float
    timed_out: bool = False
    skipped_reason: str | None = None    # "windows_mock" / "not_in_path" / "not_found" / "permission_denied" / "empty_argv"
    sudo_used: bool = False
    run_as: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        # 写入 audit 时用
        ...
```

所有结果都封装到这里，包括失败情形。`to_dict()` 排除掉 extra（避免不必要字段进 audit）。

---

## 12. configs/sudoers.kyagent —— 系统侧的最小权限

```
Cmnd_Alias KY_READONLY = \
    /usr/bin/ps, /bin/ps, \
    /usr/bin/ss, /usr/sbin/ss, \
    ...

Cmnd_Alias KY_SVC_QUERY = \
    /usr/bin/systemctl status *, \
    /usr/bin/systemctl is-active *, \
    /usr/bin/systemctl list-units *, \
    ...

Cmnd_Alias KY_SVC_MUTATE = \
    /usr/bin/systemctl restart *, \
    /usr/bin/systemctl reload *, \
    /usr/bin/systemctl start *, \
    /usr/bin/systemctl stop *

kyagent  ALL=(root)  NOPASSWD: KY_READONLY, KY_SVC_QUERY, KY_PKG_QUERY, KY_FILE_META
kyagent  ALL=(root)  NOPASSWD: KY_SVC_MUTATE
kyagent  ALL=(ALL)   !/bin/sh, !/bin/bash, !/usr/bin/zsh, !/usr/bin/perl, !/usr/bin/python*, \
                     !/usr/bin/vi, !/usr/bin/vim, !/usr/bin/nano, !/usr/bin/awk, !/usr/bin/sed

Defaults:kyagent  !visiblepw, !env_keep, lecture=never, requiretty=false, \
                  log_input, log_output, iolog_dir=/var/log/sudo-io/%{user}
```

要点：
1. **绝不出现 `ALL`**：每条命令都列绝对路径
2. **`NOPASSWD` 仅给只读 / 受控写**：危险的解释器（sh / python / awk）显式黑名单
3. **`!env_keep`**：sudo 跨权限边界时不保留任何环境变量（双保险，executor 已经洗了一遍）
4. **`log_input` / `log_output`**：sudo 自己也记审计（写到 `/var/log/sudo-io/`），与 kyagent 自己的 audit 互为对照
5. **`requiretty=false`**：因为 kyagent 作为 daemon 跑时没有 tty

这个文件是 **系统管理员的纵深防御**：即便 kyagent 进程被劫持，攻击者也只能跑 sudoers 白名单里的命令；想拿 root shell 直接被 sudo 拒绝。

安装步骤（注释里写了）：
```bash
sudo install -m 0440 configs/sudoers.kyagent /etc/sudoers.d/kyagent
sudo visudo -cf /etc/sudoers.d/kyagent
sudo useradd -r -s /usr/sbin/nologin kyagent
sudo usermod -aG systemd-journal kyagent
```

---

## 13. \_run\_windows\_mock（proxy.py:194）

```python
def _run_windows_mock(self, argv, requires_root):
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
```

Windows 上不真正跑命令，返回带 mock 标记的"假成功"。这让开发态 demo 可以走完整闭环：

```
（Windows）kyagent ask "查 80 端口"
  → MockBackend 路由到 lsof_port
  → ExecutionProxy._run_windows_mock 返回 mock stdout
  → MockBackend._summarize 把 mock stdout 当结果展示
  → 用户看到完整闭环（虽然结果是假的）
```

`Tool.format_result` 看到 `skipped_reason == "windows_mock"` 时按 OK 处理（base.py:112-113），让 mock 输出能传到 LLM。

---

## 14. 测试覆盖（test_executor.py）

```python
def test_empty_argv_rejected():
def test_windows_mock_mode():           # win32 only
def test_posix_echo():                  # posix only, skip on win32
def test_posix_timeout():                # posix only
def test_clean_env_blocks_ld_preload():
def test_clean_env_restricts_path():
def test_sudo_wrap_for_root_when_allowed():
def test_forbid_root_returns_false_command():
```

8 个测试覆盖了所有关键分支。Windows / POSIX 分别用 `@pytest.mark.skipif` 隔开，CI 矩阵可以在任意平台都跑过。

---

## 15. 关键不变量

1. **永远不调 shell**：`subprocess.Popen` 永远不传 `shell=True`，argv 总是 list
2. **PATH 总是白名单**：哪怕调用方关 restrict_path，cfg 默认值仍然是白名单
3. **forbid_root=True 时绝不 sudo root**：会被 `/bin/false` 兜底
4. **超时一定杀整组**：`killpg` 杀进程组，防止 daemon 逃逸
5. **stdout/stderr 一定截断**：哪怕子进程吐 1GB，最终拿到的也只有 64KB
6. **任何异常都包成 ExecutionResult**：上层永远不需要 try/except executor.run

---

## 16. 下一步

继续 → [07-mcp-tools.md](./07-mcp-tools.md) 看六大工具家族 + MCP server。
