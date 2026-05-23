# 08 · 推理链审计

> 文件：
> - `kyagent/audit/trace.py`（Trace + TraceEvent + EventKind）
> - `kyagent/audit/logger.py`（AuditLogger 双通道写入）
> - `kyagent/audit/store.py`（SQLite WAL 持久化）
> - `tests/test_audit.py`

对应赛题 **第 ⑤ 条 "推理链路溯源"** 的落地。

---

## 1. 设计目标

完整记录："接收指令 → 感知环境 → 推理决策 → 安全校验 → 执行结果"的闭环。出现问题时能完整复盘：
- LLM 说了什么？ → `LLM_THOUGHT` 事件
- 提议调什么工具？参数是什么？ → `TOOL_REQUEST`
- 安全护栏给的 verdict 是什么？rationale 是什么？ → `SAFETY_CHECK`
- 真的执行了吗？参数变成什么 argv？以谁身份跑？ → `EXECUTION`
- 执行结果（stdout/stderr/rc/duration）？ → `EXECUTION_RESULT`
- Agent 最终给用户回什么？ → `AGENT_REPLY`

**双通道持久化**：
- **SQLite WAL**：权威源。两张表 traces + events，支持按 trace_id / kind / ts 索引查询
- **JSONL line-buffered**：流式审计源。一行一事件，可被 SIEM / ELK / journalctl 直接 tail

---

## 2. EventKind

```python
class EventKind(str, Enum):
    USER_INPUT = "user_input"           # 1. 接收指令
    INTENT_CHECK = "intent_check"       # 1b. NL 意图层裁决（赛题第 3 条）
    PERCEPTION = "perception"           # 2. 感知环境（read_only + LOW 工具落此事件）
    LLM_THOUGHT = "llm_thought"         # 3. 推理决策（LLM 文本输出）
    TOOL_REQUEST = "tool_request"       # 3b. LLM 提议调工具
    SAFETY_CHECK = "safety_check"       # 4. 安全校验 + verdict
    EXECUTION = "execution"             # 5. 命令实际执行
    EXECUTION_RESULT = "execution_result"  # 5b. 执行结果
    AGENT_REPLY = "agent_reply"         # 6. Agent 最终回复
    ERROR = "error"
```

正常一次带工具调用的 ask() 闭环事件依次是：USER_INPUT → (INTENT_CHECK 若启用) →
LLM_THOUGHT → TOOL_REQUEST → (PERCEPTION 若工具 read_only+LOW) → SAFETY_CHECK →
EXECUTION → EXECUTION_RESULT → AGENT_REPLY。

PERCEPTION 不再是保留位：`kyagent/mcp/tools/pipeline.py:prepare_call` 对所有
`read_only and risk_level == LOW` 的工具落一条 PERCEPTION 事件，标注"被动信息
收集"——Agent 与 MCP 共用该流水线，两条通道现在都会落这条事件，对齐审计 timeline。

INTENT_CHECK 由 `Agent.ask()` 在 LLM 调用之前写入（前提是 `cfg.safety.intent_check=true`
且 `IntentGuard` 已注入）；payload 含 risk / decision / hits / rationale / sanitized_text 等。

ERROR 是兜底（LLM 报错 / 工具参数错 / max_iterations / user_denied_confirm /
confirm_in_worker_denied / needs_confirm_via_mcp 等等）。

---

## 3. TraceEvent（trace.py:24）

```python
@dataclass
class TraceEvent:
    seq: int                # 单 trace 内严格递增
    kind: EventKind
    ts: float              # time.time() unix 秒
    payload: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "ts": self.ts,
            "payload": self.payload,
        }
```

四个字段：
- `seq` —— 单 trace 内严格递增（1, 2, 3, ...）；不同 trace 之间不可比较
- `kind` —— EventKind 枚举
- `ts` —— `time.time()` 取的 unix 时间戳（float 秒）
- `payload` —— 任意 JSON 可序列化字典

---

## 4. Trace（trace.py:41）

```python
@dataclass
class Trace:
    trace_id: str = field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:12]}")
    user: str = "anonymous"
    started_at: float = field(default_factory=time.time)
    events: list[TraceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
```

字段：
- `trace_id` —— 自动生成 `trace-{12 hex}`，唯一
- `user` —— 由 Agent / McpServer 传入（CLI 用 `interactive` / `oneshot`，MCP 用 `mcp-client`）
- `started_at` —— 创建时刻 unix 秒
- `events` —— 内存中的事件列表
- `metadata` —— 任意附加信息（如 `{"backend":"mock"}`）
- `_seq` —— 内部计数器，下一个事件的 seq
- `_lock` —— **RLock**（不是 Lock！因为 `logger.event` 持有 lock 时调 `trace.add`，后者又 with lock，所以必须可重入）

### 4.1 add（trace.py:53）

```python
def add(self, kind: EventKind, payload: dict = None) -> TraceEvent:
    with self._lock:
        self._seq += 1
        ev = TraceEvent(
            seq=self._seq,
            kind=kind,
            ts=time.time(),
            payload=payload or {},
        )
        self.events.append(ev)
        return ev
```

- 锁内做 `_seq += 1` + 构造 + append → 同一 trace 多 worker 写也不会撕裂
- `payload or {}` 兜底空字典，避免 None

### 4.2 duration & summary

```python
def duration(self) -> float:
    with self._lock:
        if not self.events:
            return 0.0
        return self.events[-1].ts - self.started_at

def summary(self) -> dict:
    with self._lock:
        counts = {}
        for ev in self.events:
            counts[ev.kind.value] = counts.get(ev.kind.value, 0) + 1
        duration = 0.0 if not self.events else self.events[-1].ts - self.started_at
        return {
            "trace_id": self.trace_id,
            "user": self.user,
            "started_at": self.started_at,
            "duration": round(duration, 3),
            "event_count": len(self.events),
            "by_kind": counts,
        }
```

两个查询方法都锁，因为它们要读 events list（worker 可能正在 append）。

---

## 5. AuditLogger（logger.py）

这是审计链的"门面"。所有组件都通过 `audit.event(trace, kind, payload)` 落事件。

### 5.1 初始化（logger.py:29）

```python
def __init__(self, store, jsonl_file=None, verbose=False):
    self.store = store
    self.verbose = verbose
    self._jsonl_path = Path(jsonl_file) if jsonl_file else None
    self._jsonl_fp = None
    self._jsonl_lock = threading.Lock()
    if self._jsonl_path is not None:
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        # line-buffered append handle，'\n' 触发 flush 到 OS
        self._jsonl_fp = self._jsonl_path.open(
            "a", encoding="utf-8", buffering=1
        )
        atexit.register(_atexit_close, weakref.ref(self))
```

要点：
1. **JSONL 句柄常驻**：`open("a", buffering=1)` 一次打开后一直用。`buffering=1` = line-buffered = 每写一行就 flush 到 OS buffer
2. **atexit 兜底关闭**：进程退出时 _atexit_close 会 close fp
3. **weakref**：避免 atexit 持有强引用阻止 GC
4. **`_jsonl_lock` 是 Lock 不是 RLock**：JSONL 写入不应该重入

### 5.2 open（logger.py:49）

```python
def open(self, trace: Trace) -> None:
    self.store.open_trace(trace)
    if self.verbose:
        _logger.info("trace opened: %s by %s", trace.trace_id, trace.user)
```

`store.open_trace` 在 SQLite 写一条 traces 表（含 user/started_at/metadata）。

### 5.3 event —— 核心方法（logger.py:54）

```python
def event(self, trace: Trace, kind: EventKind, payload: dict = None) -> None:
    with trace._lock:
        ev = trace.add(kind, payload)
        self.store.append_event(trace.trace_id, ev)
        if self._jsonl_fp is not None:
            # fast-path 短路
            line = json.dumps(
                {"trace_id": trace.trace_id, **ev.to_dict()},
                ensure_ascii=False, default=str,
            )
            with self._jsonl_lock:
                fp = self._jsonl_fp
                if fp is not None:
                    fp.write(line + "\n")
        if self.verbose:
            _logger.info("[%s] %s payload=%s", trace.trace_id[:8], kind.value, payload)
```

锁的层次（从外到内）：
1. **`trace._lock`（RLock）**：覆盖整个 event 写入 —— 保证同一 trace 的 seq 顺序 = 落盘顺序
2. **`_jsonl_lock`（Lock）**：覆盖 JSONL 文件写 —— 保证不同 trace 间 JSONL 行不撕裂

**H1 修复的关键点**：`if self._jsonl_fp is not None:` 是 fast-path 短路，避免 JSONL 关闭时仍构造 `json.dumps`。**真正的判空 + 写入在 `_jsonl_lock` 内的 `fp = self._jsonl_fp; if fp is not None: fp.write(...)`** 完成，把变量先存到本地再判空，规避 close_file() 中途置 None 后第二次解引用炸 NoneType 的 TOCTOU。详细分析见 12-concurrency.md。

`default=str`：JSON 序列化遇到 datetime / Enum 等不支持类型时回退 str()。

### 5.4 close & close_file

```python
def close(self, trace: Trace) -> None:
    self.store.close_trace(trace)
    if self.verbose:
        _logger.info("trace closed: %s", trace.summary())

def close_file(self) -> None:
    """关闭 JSONL 句柄（atexit / 测试 teardown 使用）。"""
    with self._jsonl_lock:
        fp = self._jsonl_fp
        self._jsonl_fp = None
    if fp is not None:
        try:
            fp.flush()
        finally:
            fp.close()
```

`close`：trace 级别的关闭，只更新 traces 表里的 metadata（store.close_trace）。事件已经流式写入了。
`close_file`：进程级别的关闭 JSONL 句柄，由 atexit 调用。

`close_file` 的实现要点：
- 锁内先把 fp 取出到 local，再把 `_jsonl_fp` 置 None，**然后释放锁再 flush+close**
- 这样其他线程 event() 拿锁后看到 `_jsonl_fp is None` 直接跳过写
- flush/close 在锁外做，避免阻塞其它 logger 操作

---

## 6. AuditStore（store.py）

### 6.1 schema

```sql
CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    user        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    metadata    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL,
    payload     TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);

CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);
```

三个索引：
- `(trace_id, seq)` —— 回放某条 trace 时按 seq 排序
- `(kind, ts)` —— `find_events_by_kind` 用，比如查最近的 SAFETY_CHECK
- `started_at DESC` —— `list_traces` 按时间倒序

### 6.2 初始化（store.py:46）

```python
def __init__(self, db_path):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._lock = threading.Lock()
    self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                 isolation_level=None)
    self._conn.execute("PRAGMA journal_mode=WAL")
    self._conn.executescript(_SCHEMA)
```

要点：
- **`check_same_thread=False`**：允许多线程共用同一 connection
- **`isolation_level=None`**：自动提交模式（每条 INSERT 立即落盘，不需要显式 commit）
- **WAL 模式**：写不阻塞读，适合"频繁追加事件 + 偶尔回看"模式
- **`_lock`**：所有 execute 都过这个 Lock —— SQLite 的 connection 同一时刻只能跑一条语句

### 6.3 三个写入方法

```python
def open_trace(self, trace):
    with self._lock:
        self._conn.execute(
            "INSERT OR REPLACE INTO traces(trace_id,user,started_at,metadata) VALUES(?,?,?,?)",
            (trace.trace_id, trace.user, trace.started_at, json.dumps(trace.metadata)),
        )

def append_event(self, trace_id, event):
    with self._lock:
        self._conn.execute(
            "INSERT INTO events(trace_id,seq,kind,ts,payload) VALUES(?,?,?,?,?)",
            (trace_id, event.seq, event.kind.value, event.ts,
             json.dumps(event.payload, ensure_ascii=False, default=str)),
        )

def close_trace(self, trace):
    with self._lock:
        self._conn.execute(
            "UPDATE traces SET metadata=? WHERE trace_id=?",
            (json.dumps(trace.metadata, ensure_ascii=False, default=str), trace.trace_id),
        )
```

- 全部用 **参数化查询**：SQL 注入免疫
- 锁覆盖单条 execute：SQLite 内部已经做了序列化，外层 lock 防止 connection 上的并发使用

### 6.4 三个查询方法

```python
def list_traces(self, limit=50) -> list[dict]:
    cur = self._conn.execute(
        "SELECT trace_id,user,started_at,metadata FROM traces ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for trace_id, user, started_at, metadata in cur.fetchall():
        out.append({
            "trace_id": trace_id, "user": user, "started_at": started_at,
            "metadata": json.loads(metadata) if metadata else {},
        })
    return out

def get_events(self, trace_id) -> list[dict]:
    cur = self._conn.execute(
        "SELECT seq,kind,ts,payload FROM events WHERE trace_id=? ORDER BY seq",
        (trace_id,),
    )
    return [
        {"seq":s, "kind":k, "ts":t, "payload":json.loads(p)}
        for s, k, t, p in cur.fetchall()
    ]

def find_events_by_kind(self, kind, limit=100):
    cur = self._conn.execute(
        "SELECT trace_id,seq,ts,payload FROM events WHERE kind=? ORDER BY ts DESC LIMIT ?",
        (kind.value, limit),
    )
    for trace_id, seq, ts, payload in cur.fetchall():
        yield {"trace_id":trace_id, "seq":seq, "ts":ts, "payload":json.loads(payload)}
```

- `list_traces` / `find_events_by_kind` 是 CLI `kyagent audit list/show` 用的
- `find_events_by_kind` 是生成器，避免一次性 load 大表

注意：查询方法 **没有过锁**。SQLite WAL 模式下读不阻塞写。但严格来说，cur.fetchall 与外部写入并行时可能拿到部分数据——对审计回看场景这不是问题。

---

## 7. CLI 集成（cli.py）

### 7.1 `kyagent audit list`

```python
@audit_app.command("list")
def audit_list(limit=20, config=None):
    cfg = load_config(config)
    store = AuditStore(cfg.resolve(cfg.audit.database))
    rows = store.list_traces(limit=limit)
    table = Table(title="最近 trace")
    table.add_column("trace_id")
    table.add_column("user")
    table.add_column("started_at")
    table.add_column("channel")
    for r in rows:
        meta = r.get("metadata") or {}
        ts = r["started_at"]
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(r["trace_id"], r["user"], ts_str,
                      str(meta.get("channel", meta.get("backend", "?"))))
    console.print(table)
```

列出最近 20 条 trace，含 trace_id / user / 时间 / channel（mcp 或 LLM 后端名）。

### 7.2 `kyagent audit show <trace-id>`

```python
def _print_trace(cfg, trace_id):
    store = AuditStore(cfg.resolve(cfg.audit.database))
    events = store.get_events(trace_id)
    if not events:
        console.print(f"[red]找不到 trace {trace_id}[/]")
        return
    kind_color = {
        EventKind.USER_INPUT.value: "bold green",
        EventKind.LLM_THOUGHT.value: "magenta",
        EventKind.TOOL_REQUEST.value: "blue",
        EventKind.SAFETY_CHECK.value: "yellow",
        EventKind.EXECUTION.value: "blue",
        EventKind.EXECUTION_RESULT.value: "cyan",
        EventKind.AGENT_REPLY.value: "bold blue",
        EventKind.ERROR.value: "red",
    }
    console.rule(f"trace {trace_id}")
    for ev in events:
        c = kind_color.get(ev["kind"], "white")
        head = f"[{c}]#{ev['seq']:02d}  {ev['kind']}[/]"
        body = json.dumps(ev["payload"], ensure_ascii=False, indent=2, default=str)
        if len(body) > 1500:
            body = body[:1500] + "\n...[truncated]"
        console.print(Panel(body, title=head, border_style=c))
```

按 seq 顺序打印每个事件，按 kind 着色。每条 payload 用 JSON 缩进，截断到 1500 字。

这就是赛题"异常回溯"的物理实现：出问题时，跑 `kyagent audit show trace-xyz`，就能看到完整推理链。

---

## 8. JSONL 用法

`var/audit.jsonl` 每行长这样：

```json
{"trace_id":"trace-1f2e3d","seq":1,"kind":"user_input","ts":1747500000.123,"payload":{"text":"查 80 端口"}}
{"trace_id":"trace-1f2e3d","seq":2,"kind":"llm_thought","ts":1747500000.234,"payload":{"stop_reason":"tool_use",...}}
{"trace_id":"trace-1f2e3d","seq":3,"kind":"tool_request","ts":1747500000.235,"payload":{"tool":"lsof_port",...}}
...
```

可以直接给 SIEM / ELK / journalctl 抽取：

```bash
# 跟踪最新事件
tail -f var/audit.jsonl | jq .

# 按工具名筛选
jq 'select(.payload.tool == "svc_restart")' var/audit.jsonl

# 找最近所有 DENY
jq 'select(.kind == "safety_check" and .payload.decision == "deny")' var/audit.jsonl
```

JSONL 是"流式审计"的标准格式，与 SQLite 的"权威源"互为对照。

---

## 9. test_audit.py 测试覆盖

### 9.1 7 段完整推理链

```python
def test_full_reasoning_chain_persisted(tmp_path):
    logger, store = _make_logger(tmp_path)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text":"查 80 端口"})
    logger.event(trace, EventKind.LLM_THOUGHT, {"text":"我先调 lsof_port"})
    logger.event(trace, EventKind.TOOL_REQUEST, {"tool":"lsof_port", "argv":[...]})
    logger.event(trace, EventKind.SAFETY_CHECK, {"decision":"allow", ...})
    logger.event(trace, EventKind.EXECUTION, {"argv":[...]})
    logger.event(trace, EventKind.EXECUTION_RESULT, {"returncode":0, ...})
    logger.event(trace, EventKind.AGENT_REPLY, {"text":"80 端口由 nginx 占用"})
    logger.close(trace)

    events = store.get_events(trace.trace_id)
    assert len(events) == 7
    assert [e["seq"] for e in events] == list(range(1, 8))   # seq 严格 1..7
    assert events[0]["kind"] == EventKind.USER_INPUT.value
    assert events[-1]["kind"] == EventKind.AGENT_REPLY.value
    assert EventKind.SAFETY_CHECK.value in [e["kind"] for e in events]
```

### 9.2 JSONL 也写了

```python
def test_jsonl_appended(tmp_path):
    logger, _ = _make_logger(tmp_path)
    trace = Trace(user="tester")
    logger.open(trace)
    logger.event(trace, EventKind.USER_INPUT, {"text":"hello"})
    logger.close(trace)
    content = (tmp_path/"audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    rec = json.loads(content[0])
    assert rec["trace_id"] == trace.trace_id
    assert rec["kind"] == "user_input"
```

### 9.3 list_traces 按时间倒序

```python
def test_list_traces_orders_by_recency(tmp_path):
    logger, store = _make_logger(tmp_path)
    t1 = Trace(user="a"); logger.open(t1); logger.event(t1, EventKind.USER_INPUT, {"text":"1"}); logger.close(t1)
    time.sleep(0.01)
    t2 = Trace(user="b"); logger.open(t2); logger.event(t2, EventKind.USER_INPUT, {"text":"2"}); logger.close(t2)
    rows = store.list_traces(limit=10)
    assert rows[0]["trace_id"] == t2.trace_id
    assert rows[1]["trace_id"] == t1.trace_id
```

### 9.4 按 kind 检索

```python
def test_filter_events_by_kind(tmp_path):
    # ... logger.event(SAFETY_CHECK, {"decision":"deny"}) ...
    hits = list(store.find_events_by_kind(EventKind.SAFETY_CHECK))
    assert len(hits) == 1
    assert hits[0]["payload"]["decision"] == "deny"
```

### 9.5 共享 trace 事件序列化（H2 锚点）

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

    assert not interleaved_before_first_completed     # 没有交错
    assert store.seqs == [1, 2]                       # seq 顺序正确
    lines = (tmp_path/"audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]  # JSONL 行序号正确
```

这个测试用 `_BlockingStore` 故意让第一个 event 卡住，验证第二个 event 必须等第一个完成才能 append。是 trace lock 的关键回归测试。

---

## 10. 不变量

1. **每条 trace 的 seq 从 1 开始严格递增**：1, 2, 3, ...
2. **同一 trace 内事件落盘顺序 = seq 顺序**：受 `trace._lock` 保护
3. **不同 trace 之间 JSONL 行可以交错**，但每行原子（受 `_jsonl_lock` 保护）
4. **SQLite 是权威源，JSONL 是流式镜像**：JSONL 故障不影响 SQLite
5. **JSONL 关闭后再来的 event 静默丢弃**：不抛 NoneType 错误（H1 修复）
6. **`audit.close(trace)` 是幂等的**：不会重复创建数据库行（INSERT OR REPLACE）

---

## 11. 下一步

继续 → [09-config.md](./09-config.md) 看配置系统怎么工作。
