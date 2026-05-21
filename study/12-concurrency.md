# 12 · 并发模型与 review 修复

> 这一份串起：并行多工具调度 + per-trace 审计锁 + 三处安全 review 修复（H1/C2/C1）。
> 配套文件：`kyagent/agent/core.log`、`kyagent/audit/logger.log`、`kyagent/executor/proxy.log`

---

## 1. 为什么需要并发

LLM 一轮可能同时发起多个 tool_use（"先查进程，再看端口，再看磁盘"），如果完全串行：

```
总耗时 = ps_time + ss_time + df_time
```

如果各工具间无依赖（都是只读感知）：

```
总耗时 ≈ max(ps_time, ss_time, df_time)
```

理论上能省 60-70% 的尾延迟。**前提是确保并发不引入安全或正确性问题**。

---

## 2. 并发架构总览

```
Agent.ask()
   │
   │  iteration N：assistant 发起多个 tool_use
   ▼
   ┌─────────────────────────────────────────────┐
   │ 四道 gate 判断是否进并行                     │
   │  ① sys.platform != "win32"                  │
   │  ② len(tool_uses) >= 2                      │
   │  ③ executor.supports_parallel_tool_execution│
   │  ④ all(_is_parallel_safe(tu) for tu)        │
   └─────────────────────────────────────────────┘
   │
   ├─ 任一不过 → 串行                          ┐
   │                                            │
   ▼                                            ▼
   ┌──────────────────────┐         ┌──────────────────────┐
   │ ThreadPoolExecutor    │         │ for tu in tool_uses: │
   │  max_workers=4        │         │     _handle_tool_use │
   │  thread="ky-tool"     │         │ （单线程顺序）        │
   │                       │         └──────────────────────┘
   │  for tu in tool_uses:│
   │    pool.submit(...)   │
   │                       │
   │  for fut: fut.result()│  ◀── 等齐之后顺序填 tool_results[idx]
   └──────────────────────┘
   │
   ▼
   self.messages.append({"role":"user","content":tool_results})
```

---

## 3. 四道 gate（agent/core.py:165）

```python
run_parallel = (
    sys.platform != "win32"
    and len(tool_uses) >= 2
    and self._executor_supports_parallel_tools()
    and all(self._is_parallel_safe(tu) for tu in tool_uses)
)
```

### Gate 1：sys.platform != "win32"

Windows 上 ExecutionProxy 走 mock 模式，没有真实 I/O 可以重叠，并行只增加 GIL 竞争。

### Gate 2：len(tool_uses) >= 2

1 个工具时并行没意义。

### Gate 3：executor.supports_parallel_tool_execution

执行器自己声明是否可以被并发调度。`ExecutionProxy` 的实现见下面 C1 部分。

### Gate 4：所有 tool_uses 都 _is_parallel_safe

任何一个不安全（高风险 / 需 confirm / reviewer 启用）就全部串行。**整组共进退**，不会拆成一部分并行一部分串行——避免审计顺序歧义。

---

## 4. _is_parallel_safe 的五层判定（C2 修复后）

```python
def _is_parallel_safe(self, tu):
    tool = self.registry.get(tu.name)
    if tool is None:                                # 1. 工具必须存在
        return False
    if not tool.read_only:                          # 2. 必须只读
        return False
    if self.guardrail.llm_reviewer is not None:     # 3. ★ C2 第一道防线
        return False
    try:
        cleaned = tool.validate(tu.input or {})
        argv = tool.build_argv(cleaned)
    except ToolError:                               # 4. 参数必须能过
        return False
    verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
    return verdict.decision is Decision.ALLOW       # 5. 预检必须 ALLOW
```

五层条件全 AND，任何一层不过 → 串行。

**第 3 层是 C2 修复的关键**：见下方 C2 章节。

---

## 5. 审计层的并发安全（Trace 的 RLock）

### 5.1 Trace._lock = threading.RLock

```python
# audit/trace.py:51
_lock: threading.RLock = field(default_factory=threading.RLock,
                               init=False, repr=False)
```

为什么是 RLock 不是 Lock？因为：

```python
# audit/logger.py:60
def event(self, trace, kind, payload=None):
    with trace._lock:                       # 第一次 acquire
        ev = trace.add(kind, payload)       # add() 内部又 with self._lock → 重入
        ...
```

如果 `_lock` 是普通 Lock，这里就死锁了。RLock 允许同一线程重入。

### 5.2 add / duration / summary 都加锁

```python
def add(self, kind, payload=None):
    with self._lock:
        self._seq += 1
        ev = TraceEvent(seq=self._seq, kind=kind, ts=time.time(), payload=payload or {})
        self.events.append(ev)
        return ev

def duration(self):
    with self._lock:
        if not self.events: return 0.0
        return self.events[-1].ts - self.started_at

def summary(self):
    with self._lock:
        counts = {}
        for ev in self.events:
            counts[ev.kind.value] = counts.get(ev.kind.value, 0) + 1
        ...
```

- 写：`add` 必须锁，否则 `_seq += 1` 和 `events.append` 之间可能被其它 worker 插入
- 读：`duration` / `summary` 必须锁，否则读到半致 events list

### 5.3 logger.event 覆盖整个 I/O 临界区

```python
def event(self, trace, kind, payload=None):
    with trace._lock:                       # ★ 整个事件写入都在锁内
        ev = trace.add(kind, payload)       # 1. 分配 seq
        self.store.append_event(...)        # 2. 写 SQLite
        if self._jsonl_fp is not None:
            line = json.dumps(...)
            with self._jsonl_lock:          # 3. 嵌套锁：JSONL 互斥
                fp = self._jsonl_fp
                if fp is not None:
                    fp.write(line + "\n")
```

**设计权衡**：锁范围大→并行度低；但保证"seq 顺序 = SQLite 落盘顺序 = JSONL 行顺序"。对一个安全审计系统，**顺序一致性比吞吐量更重要**。

### 5.4 序列化测试（test_audit.py:111）

```python
def test_audit_event_serializes_shared_trace_updates(tmp_path):
    store = _BlockingStore()
    logger = AuditLogger(store, jsonl_file=tmp_path/"audit.jsonl")
    trace = Trace(user="tester")

    def add_first():
        logger.event(trace, EventKind.USER_INPUT, {"label":"first"})
    def add_second():
        assert store.first_entered.wait(timeout=1)
        logger.event(trace, EventKind.LLM_THOUGHT, {"label":"second"})

    first = threading.Thread(target=add_first)
    second = threading.Thread(target=add_second)
    first.start(); second.start()
    interleaved_before_first_completed = store.second_appended.wait(timeout=0.2)
    store.release_first.set()
    first.join(); second.join()
    logger.close_file()

    assert not interleaved_before_first_completed   # 没有交错
    assert store.seqs == [1, 2]
    lines = (tmp_path/"audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
```

`_BlockingStore` 是一个测试桩：第一次 `append_event(seq=1)` 时会 `release_first.wait(timeout=2)` 阻塞。如果第二个 event 在第一个还没释放前能进 store，就证明 trace lock 失效了。

测试断言：
- `interleaved_before_first_completed = False` —— 第二个 event 必须等第一个 release 后才能进
- `store.seqs == [1, 2]` —— SQLite 顺序正确
- JSONL 行序号是 [1, 2] —— 文件顺序正确

---

## 6. AuditStore 的并发安全

```python
# audit/store.py:46
def __init__(self, db_path):
    ...
    self._lock = threading.Lock()
    self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                 isolation_level=None)
    self._conn.execute("PRAGMA journal_mode=WAL")

def append_event(self, trace_id, event):
    with self._lock:
        self._conn.execute("INSERT INTO events(...) VALUES(...)", (...))
```

- `check_same_thread=False`：允许多线程共用同一 connection（默认 sqlite3 要求同一线程）
- `_lock`：所有 execute 过这把锁，串行化所有 SQL
- WAL 模式：读不阻塞写

锁顺序：`trace._lock` → `store._lock`（一致方向）。所有 audit.event 调用方都遵守这个顺序，所以不会 AB-BA 死锁。

---

## 7. C1：supports_parallel_tool_execution 在 POSIX 永为 False

### 7.1 现象

```python
# executor/proxy.py:66
@property
def supports_parallel_tool_execution(self) -> bool:
    return self._shared_preexec is None
```

```python
# executor/sandbox.py:33
def make_preexec_fn(cfg):
    if sys.platform == "win32":
        return None              # Windows 才返回 None
    # ... Linux 上必返回非 None 闭包
```

真值表：

| 平台 | `_shared_preexec` | property 值 | Agent gate `!= "win32"` | 实际并行？ |
|---|---|---|---|---|
| Linux 生产 | 非 None 闭包 | **False** | True | **否** |
| Windows 开发 | None | True | False | **否** |

→ 生产环境的并行路径完全 dormant。

### 7.2 为什么这样保守

`preexec_fn` 在 fork() 后子进程里执行任意 Python 回调。在多线程父进程里 fork：
- glibc malloc 的内部锁可能被其它线程持有，子进程拿不到 → 死锁
- 信号处理器状态、TLS 状态可能不一致
- `resource.setrlimit` 不是 async-signal-safe，理论上违规

这是 CPython 已知雷区（[bpo-34037](https://bugs.python.org/issue34037)）。Python 3.12 起 subprocess 模块自身已优先用 `posix_spawn` 替代 fork（如果不需要 preexec_fn）；但只要传 `preexec_fn`，仍然走 fork。

### 7.3 设计意图

- 保留并行路径的代码 + 测试，把 gate 留在 `supports_parallel_tool_execution` 上
- 未来某个版本把 `executor.proxy` 切换到 posix_spawn（或确认 preexec 中所有操作都是 fork-safe），把 property 改成 True 就能开启
- 当前 baseline 的 -34% p50 优化**不依赖并行**，所以暂时关掉并行不影响性能基线

详见 `kyagent/executor/proxy.log`。

---

## 8. C2：预检 ≠ 内检导致 worker 线程读 stdin

### 8.1 风险

`_is_parallel_safe` 在主线程跑一次 `guardrail.check_argv`：

```python
verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
return verdict.decision is Decision.ALLOW
```

`_handle_tool_use` 在 worker 线程**再跑一次**同样的 check：

```python
verdict = self.guardrail.check_argv(argv, declared_risk=tool.risk_level)
# ...
if verdict.decision is Decision.CONFIRM:
    approved = self.confirm(tu.name, argv, verdict.to_dict())
```

**guardrail.check_argv 是确定性的吗？** 取决于 `llm_reviewer`：

```python
# safety/guardrail.py:117
if self.llm_reviewer is not None:
    try:
        reviewed = self.llm_reviewer(cmdline)
    except Exception as e:
        reviewed = None
        rationale.append(f"LLM 复审异常: {e!r}")
```

LLM 复审：
- 调外部 LLM，**非确定性**（temperature、网络抖动）
- 异常被吞并置 None（自承认非确定性）

→ 主线程预检 ALLOW，worker 线程内检可能 CONFIRM → worker 调 `self.confirm(...)` → CLI 的 stdin 被多 worker 抢 → **"谁授权了什么"在审计链上错位**。

### 8.2 两道防线

#### 第一道：_is_parallel_safe 直接拒绝 reviewer 启用时的并行

```python
# core.py:223-228
if self.guardrail.llm_reviewer is not None:
    return False
```

只要 reviewer 启用，**所有工具都不进并行路径**。这把"主线程预检 vs worker 内检"的不一致风险从源头切断。

#### 第二道：_handle_tool_use 在 worker 拿到 CONFIRM 立即 deny

```python
# core.py:273
if verdict.decision is Decision.CONFIRM:
    if threading.current_thread() is not threading.main_thread():
        notes.append(f"非主线程下 CONFIRM 默认拒绝 {tu.name}")
        self.audit.event(trace, EventKind.ERROR,
                         {"reason": "confirm_in_worker_denied", "tool": tu.name})
        return ToolResultBlock(
            tool_use_id=tu.id, is_error=True,
            content=("[denied] 工具需要二次确认，但当前调度在非主线程，"
                     f"已自动拒绝（risk={verdict.risk.value}）"),
        )
    # ... 正常 confirm 路径
```

兜底：即便第一道因为某种原因失守（reviewer 后来才注入？），worker 拿到 CONFIRM 时检查 `threading.current_thread() is not threading.main_thread()`，立即 deny + 写 ERROR 审计事件。

### 8.3 测试覆盖

`tests/test_agent_parallel.py`：

```python
def test_is_parallel_safe_rejects_when_llm_reviewer_enabled(tmp_path):
    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    agent = _agent(tmp_path, ScriptedBackend([("process_list",{...})]), executor)
    tool_use = ToolUseBlock(id="tool-0", name="process_list", input={...})

    assert agent._is_parallel_safe(tool_use) is True
    agent.guardrail.llm_reviewer = lambda cmdline: None
    assert agent._is_parallel_safe(tool_use) is False    # ← 启用 reviewer 后变 False
```

```python
def test_handle_tool_use_denies_confirm_off_main_thread(tmp_path):
    executor = RecordingExecutor(supports_parallel_tool_execution=True)
    confirm_called = []
    def confirm_should_never_run(name, argv, verdict):
        confirm_called.append("RAN")
        return True
    agent = _agent(tmp_path, ScriptedBackend([("svc_reload",{"unit":"nginx"})]),
                   executor, confirm=confirm_should_never_run)

    trace = Trace(user="tester")
    agent.audit.open(trace)
    tool_use = ToolUseBlock(id="tool-0", name="svc_reload", input={"unit":"nginx"})
    notes = []

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ky-test") as pool:
        result_block = pool.submit(agent._handle_tool_use, trace, tool_use, notes).result()

    assert confirm_called == []                                          # ← confirm 没被调用
    assert result_block.is_error
    assert result_block.content.startswith("[denied]")
    assert any("非主线程" in n for n in notes)
    error_events = [e for e in trace.events if e.kind is EventKind.ERROR]
    assert any(e.payload.get("reason") == "confirm_in_worker_denied" for e in error_events)
```

第二个测试故意从 worker 线程调 `_handle_tool_use`（绕过 _is_parallel_safe 的预检），验证 worker 内检拿到 CONFIRM 时绝不调 confirm 函数，而是 deny + 写 ERROR。

详见 `kyagent/agent/core.log`。

---

## 9. H1：_jsonl_fp 的 check-then-use TOCTOU

### 9.1 风险（修复前的代码模式）

```python
# 错误模式
if self._jsonl_fp is not None:            # 第一次解引用
    line = json.dumps(...)
    with self._jsonl_lock:
        self._jsonl_fp.write(...)         # 第二次解引用
```

`close_file()` 同时持 `_jsonl_lock` 把 `_jsonl_fp` 置 None：

```python
def close_file(self):
    with self._jsonl_lock:
        fp = self._jsonl_fp
        self._jsonl_fp = None
    if fp is not None:
        fp.flush(); fp.close()
```

**TOCTOU 窗口**：第一次解引用 → 第二次解引用之间，`close_file` 在另一个线程跑：
1. event() 拿到 `_jsonl_lock`（即将写）
2. close_file 等 `_jsonl_lock`
3. event() 写完释放锁
4. close_file 拿锁，置 None，释放锁，flush+close
5. event() 第二次进锁里 `self._jsonl_fp.write(...)` → **NoneType.write 报错**

### 9.2 当前是否会触发

不会。原因：
- `agent.shutdown()` 不连带调 `audit.close_file()`
- ThreadPoolExecutor 注册的 `_python_exit` 优先于 user atexit，atexit 跑 `_atexit_close` 时 worker 早已 join
- 现有测试都先 join 再 close_file

**但代码模式本身错误，是 footgun**。任何后续添加的 graceful shutdown / `__exit__` 把 close_file 挪到 worker 仍存活的窗口就会立刻活。

### 9.3 修复

```python
# 正确模式
if self._jsonl_fp is not None:            # 仅作 fast-path 短路
    line = json.dumps(...)
    with self._jsonl_lock:
        fp = self._jsonl_fp                # 锁内 capture 到 local
        if fp is not None:                  # 锁内二次判空
            fp.write(line + "\n")           # 用 local 引用写
```

要点：
1. 外层 `if` 仅作 **fast-path 短路**：JSONL 关闭时跳过 `json.dumps` 这种比较贵的操作
2. **真正解引用 + 写入移进 `_jsonl_lock` 内**，先 capture 到 local `fp` 再判空
3. JSONL 关闭后再来的 event **静默丢弃该行**，不抛异常
4. SQLite 仍是权威审计源，丢失的 JSONL 行是冗余通道，不影响回溯

详见 `kyagent/audit/logger.log`。

---

## 10. 锁的层级图

整个项目的锁有这些：

```
┌──────────────────────────┐
│ trace._lock (RLock)       │  → 单 trace 内事件序列化
│  per-Trace instance       │     (audit.logger.event 持有)
└────────┬─────────────────┘
         │ 嵌套
         ▼
┌──────────────────────────┐
│ store._lock (Lock)        │  → SQLite connection 互斥
│  per-AuditStore instance  │     (audit.store.* 持有)
└──────────────────────────┘
         │ 嵌套
         ▼
┌──────────────────────────┐
│ _jsonl_lock (Lock)        │  → JSONL 文件互斥
│  per-AuditLogger instance │     (audit.logger 持有)
└──────────────────────────┘

锁顺序（严格遵守）：trace._lock → store._lock → _jsonl_lock
（store._lock 和 _jsonl_lock 实际上是兄弟，audit.logger.event 顺序拿）

无 AB-BA 风险，因为：
- trace._lock 只在 audit.logger.event 内 acquire
- store._lock 只在 audit.store.* 方法内 acquire
- _jsonl_lock 只在 audit.logger.{event,close_file} 内 acquire
- 谁也不会反向调用
```

---

## 11. ThreadPool 生命周期

```python
# agent/core.py:77
def _ensure_pool(self):
    if self._tool_pool is None:
        self._tool_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ky-tool")
    return self._tool_pool

def shutdown(self):
    if self._tool_pool is not None:
        self._tool_pool.shutdown(wait=False)
        self._tool_pool = None
```

- **懒创建**：只在第一次走并行路径时才 new 一个 pool
- **max_workers=4**：保守上限
- **thread_name_prefix**：方便 trace 里看 "ky-tool-0" / "ky-tool-1" 之类
- **shutdown(wait=False)**：不等 worker 完成（调用方知道自己不需要再用 Agent 才该调）

注意：在每次 `ask()` 主循环里：

```python
futures = [pool.submit(self._handle_tool_use, ...) for tu in tool_uses]
for tu, fut in zip(tool_uses, futures):
    result_block = fut.result()       # ★ 同步等齐
```

`fut.result()` 会阻塞到 worker 完成。所以正常路径下，**ask() 返回时所有 worker 都已经收工**。`shutdown()` 是给"应用层主动释放 Agent"用的。

---

## 12. 当前并发路径的实际行为

由于 C1 的 gate，在生产 Linux 上：

```
ask("查 CPU 和磁盘")
  ↓
LLM 发起 2 个 tool_use: process_list + fs_df
  ↓
run_parallel = (
  True (Linux)
  and True (len >= 2)
  and False ← supports_parallel_tool_execution
  ...
) = False
  ↓
走串行 for 循环：
  _handle_tool_use(process_list) → done
  _handle_tool_use(fs_df) → done
  ↓
tool_results 顺序填好 → 灌回 LLM
```

所有并发安全机制（RLock、SQLite WAL 锁、JSONL 互斥、_is_parallel_safe、worker 兜底）**当前都是为未来准备的**。

测试用例通过 `RecordingExecutor(supports_parallel_tool_execution=True)` 绕过 C1 来覆盖并行路径——保证当未来切换到 posix_spawn 把 gate 打开时，并发不变量已经过测试。

---

## 13. 三个 .log 文件

每个修改的模块下都留了一个 `.log`：

- `kyagent/audit/logger.log` — H1 修复细节
- `kyagent/agent/core.log` — C2 两道防线
- `kyagent/executor/proxy.log` — C1 docstring 澄清

`.gitignore` 加了 `!kyagent/**/*.log` 白名单让这些日志能入库。

---

## 14. 不变量

1. **`self.confirm()` 永远只在主线程调用**：C2 两道防线 + tests/test_agent_parallel.py 验证
2. **同一 trace 内事件落盘顺序 = seq 顺序**：trace._lock + tests/test_audit.py 验证
3. **不同 trace 之间事件可以并行写**：每条 trace 独立锁
4. **JSONL 关闭后 event() 不抛异常**：H1 修复（fast-path + 锁内二次判空）
5. **并行路径整体共进退**：4 道 gate AND 关系，绝不"半并行"
6. **生产环境当前不走并行**：C1 gate 死，留作未来扩展
7. **`tool_results[idx]` 顺序 = `tool_uses[idx]` 顺序**：哪怕走并行也按 submit 顺序填

---

## 15. 下一步

继续 → [13-testing-bench.md](./13-testing-bench.md) 看测试套件 + 冻结性能基线。
