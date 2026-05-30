# 04 · LLM 后端层

> 文件：`kyagent/agent/llm.py`
> 配套：`tests/test_openai_backend.py`、`tests/test_httpx_backend.py`

---

## 1. 设计目标

LLM 后端要做到三件事：
1. **统一接口**：`Agent.core` 不知道下游是 Claude 还是 GPT 还是规则路由
2. **可替换**：换后端只改一个 YAML 字段，不动 Agent 代码
3. **离线可跑**：MockBackend 让 CI / 比赛评测可以不依赖外部 API

实现策略：内部用 Anthropic 风格的数据结构（TextBlock / ToolUseBlock / ToolResultBlock + AssistantMessage），mock、SDK 后端和 `openai_httpx / deepseek_httpx / qwen_httpx` 都翻译到这套统一表示。

---

## 2. 统一的消息模型（llm.py:25-57）

四个 dataclass 类，对应 Anthropic 的 content blocks：

```python
@dataclass
class TextBlock:
    text: str
    type: str = "text"

@dataclass
class ToolUseBlock:
    id: str               # 调用 ID，关联后续 tool_result
    name: str             # 工具名（必须在 registry 中）
    input: dict[str, Any] # 调用参数（要过 Tool.validate）
    type: str = "tool_use"

@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"

@dataclass
class AssistantMessage:
    blocks: list[TextBlock | ToolUseBlock]
    stop_reason: str = "end_turn"
    raw: Any = None       # 原始响应保留，方便调试

    def texts(self) -> list[str]: ...       # 提取所有 text
    def tool_uses(self) -> list[ToolUseBlock]: ...  # 提取所有 tool_use
```

`stop_reason` 的取值与 Anthropic API 对齐：`"tool_use"` / `"end_turn"` / `"max_tokens"` / `"content_filter"`，Agent 主循环只关心是不是 `tool_uses()` 为空（决定终止）。

---

## 3. LlmBackend 抽象基类（llm.py:60）

```python
class LlmBackend:
    name = "base"

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> AssistantMessage:
        raise NotImplementedError
```

只有一个抽象方法 `chat`。输入永远是：
- `system: str` — 系统提示词（SYSTEM_PROMPT）
- `messages: list[dict]` — 多轮历史（Anthropic 风格的 dict）
- `tools: list[dict]` — 工具描述列表（含 `name/description/input_schema`）

输出：`AssistantMessage`，内部包含若干 blocks 和一个 stop_reason。

---

## 4. AnthropicBackend（llm.py:75-135）

调用真实 Claude API。**核心特性是 prompt cache。**

### 4.1 初始化

```python
def __init__(self, model, max_tokens, api_key_env="ANTHROPIC_API_KEY",
             prompt_cache=True):
    import anthropic
    from anthropic import Anthropic

    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"环境变量 {api_key_env} 未设置")
    self._client = Anthropic(api_key=key)
    self.model = model
    self.max_tokens = max_tokens
    self.prompt_cache = prompt_cache
```

- 把 `import anthropic` 推迟到运行时，避免没装 SDK 也能 import 整个 kyagent
- 显式从环境变量取 API key，绝不硬编码

### 4.2 chat 实现：prompt cache 的两个断点

```python
def chat(self, system, messages, tools):
    kwargs = {
        "model": self.model,
        "max_tokens": self.max_tokens,
        "messages": messages,
    }
    # 断点 1：system prompt
    if self.prompt_cache and system:
        kwargs["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        kwargs["system"] = system

    if tools:
        if self.prompt_cache:
            # 断点 2：最后一个工具
            tools_with_cache = [dict(t) for t in tools]
            tools_with_cache[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = tools_with_cache
        else:
            kwargs["tools"] = tools

    resp = self._client.messages.create(**kwargs)
```

Anthropic 的 prompt cache 是 **前缀缓存**：从开头到 cache_control 断点之间的内容被缓存。把 cache_control 挂在系统 prompt 末尾 + 工具列表末尾，等于让 Claude 缓存 (system + tools) 整个前缀。

效果：第二次 ask() 时（相同 system + tools），TTFT 降 13-31%，输入 token 成本降 41-80%。这是 commit e276c77 的核心优化之一。

### 4.3 响应解析

```python
blocks = []
for blk in resp.content:
    if blk.type == "text":
        blocks.append(TextBlock(text=blk.text))
    elif blk.type == "tool_use":
        blocks.append(ToolUseBlock(id=blk.id, name=blk.name, input=dict(blk.input)))
return AssistantMessage(blocks=blocks, stop_reason=resp.stop_reason or "end_turn", raw=resp)
```

Anthropic SDK 返回的 `resp.content` 已经是 list of blocks。我们只搬到自己的 dataclass 里。`raw=resp` 保留原始响应方便排查。

---

## 5. OpenAIBackend（llm.py:141-345）

OpenAI 协议比 Anthropic 协议复杂——tool calls 是 message 上的字段，而不是 content block。需要双向翻译。

### 5.1 兼容覆盖面（代码协议层）

`OpenAIBackend` 在协议层不只对 OpenAI 官方有效。下表是**代码兼容性参考**，不是生产推荐清单：

| 服务 | base_url | model |
|---|---|---|
| OpenAI 官方 | `https://api.openai.com/v1` (默认) | `gpt-4o-mini` 等 |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` / `deepseek-v4-pro` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| vLLM / Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` 等 |

只要服务遵循 OpenAI Chat Completions 协议（tools / tool_calls / tool_choice），就能直接接。

> **部署推广边界**：当前阶段（含龙芯部署）**只推 DeepSeek 一个真实后端**。LoongArch Old World 使用 `deepseek_httpx`，不安装 openai SDK；上表其他条目仅用于说明协议适配能力。

### 5.2 初始化（llm.py:161）

```python
def __init__(self, model, max_tokens, temperature=0.2,
             api_key_env="OPENAI_API_KEY", base_url=None, organization=None):
    import openai
    from openai import OpenAI

    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"环境变量 {api_key_env} 未设置")
    client_kwargs = {"api_key": key}
    if base_url:
        client_kwargs["base_url"] = base_url
    if organization:
        client_kwargs["organization"] = organization
    self._client = OpenAI(**client_kwargs)
    self.model = model
    self.max_tokens = max_tokens
    self.temperature = temperature
```

`base_url` 留空就走官方端点。`organization` 仅在多组织账号下需要。

### 5.3 chat() 主入口

```python
def chat(self, system, messages, tools):
    oai_messages = self._to_openai_messages(system, messages)
    oai_tools = self._to_openai_tools(tools) if tools else None

    kwargs = {
        "model": self.model,
        "messages": oai_messages,
        "max_tokens": self.max_tokens,
        "temperature": self.temperature,
    }
    if oai_tools:
        kwargs["tools"] = oai_tools
        kwargs["tool_choice"] = "auto"

    resp = self._client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    return self._from_openai_choice(choice, raw=resp)
```

`tool_choice="auto"` 让 LLM 自己决定是否调工具（OpenAI 默认就是 auto，但显式写出避免后续兼容服务的默认变化）。

### 5.4 Anthropic → OpenAI：tools 翻译

```python
# llm.py:211
@staticmethod
def _to_openai_tools(tools):
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out
```

字段对照：
| Anthropic | OpenAI |
|---|---|
| `name` | `function.name` |
| `description` | `function.description` |
| `input_schema` | `function.parameters`（同样是 JSON Schema） |
| 外层 dict | 外层 `{"type":"function","function":{...}}` |

### 5.5 Anthropic → OpenAI：messages 翻译（llm.py:225）

这是整个文件最复杂的部分。要处理 4 种 Anthropic 风格的 message：

| Anthropic role | content | OpenAI 翻译 |
|---|---|---|
| user | str | `{role:"user", content: str}` |
| user | list 含 tool_result | 拆成多条 `{role:"tool", tool_call_id, content}` |
| assistant | list 含 tool_use | `{role:"assistant", content, tool_calls:[...]}` |
| system | str | `{role:"system", content}` |

关键代码：

```python
for m in messages:
    role = m.get("role")
    content = m.get("content")
    if role == "user":
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        if isinstance(content, list):
            text_buf, tool_msgs = [], []
            for c in content:
                ctype = c.get("type")
                if ctype == "text":
                    text_buf.append(c.get("text", ""))
                elif ctype == "tool_result":
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": c.get("tool_use_id", ""),
                        "content": cls._flatten_tool_result(c.get("content")),
                    })
            if text_buf:
                out.append({"role":"user", "content":"\n".join(text_buf)})
            out.extend(tool_msgs)
            continue
    elif role == "assistant":
        text_parts, tool_calls = [], []
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "text":
                    text_parts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    tool_calls.append({
                        "id": c.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": c.get("name", ""),
                            "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                        },
                    })
        msg = {"role": "assistant", "content": "\n".join(text_parts) or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)
```

注意几个要点：
- `tool_use` 的 `input` (dict) 要 `json.dumps` 成字符串才能塞进 OpenAI 的 `arguments`
- `tool_result.content` 在 Anthropic 协议里可能是 str 或 `[{type:"text",text:"..."}, ...]`，`_flatten_tool_result` 统一拍平成 str（OpenAI 的 `role:"tool"` 只接受 str content）
- assistant 没有 text 时 `content` 必须是 `None`（OpenAI 的 schema 要求）

### 5.6 OpenAI → 内部统一表示（llm.py:319）

```python
@classmethod
def _from_openai_choice(cls, choice, raw):
    msg = choice.message
    blocks = []

    text = getattr(msg, "content", None)
    if text:
        blocks.append(TextBlock(text=text))

    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if fn is None: continue
        name = getattr(fn, "name", "") or ""
        args_raw = getattr(fn, "arguments", "") or ""
        try:
            parsed = json.loads(args_raw) if args_raw else {}
            if not isinstance(parsed, dict):
                parsed = {"_raw": parsed}
        except json.JSONDecodeError:
            parsed = {"_raw": args_raw}
        blocks.append(ToolUseBlock(id=getattr(tc, "id", "") or "", name=name, input=parsed))

    finish_reason = getattr(choice, "finish_reason", None) or "stop"
    stop_reason = cls._STOP_MAP.get(finish_reason, finish_reason)
    return AssistantMessage(blocks=blocks, stop_reason=stop_reason, raw=raw)
```

关键鲁棒性：
- `arguments` 可能是非法 JSON（LLM 偶尔会输出 `{"foo: bar}` 这类），用 `try/except` 兜底为 `{"_raw": args_raw}`
- `arguments` 可能解析出非 dict（数组、字符串），也兜底为 `{"_raw": parsed}`
- 用 `getattr` 而不是属性访问，对 SDK 字段缺失友好

### 5.7 stop_reason 映射

```python
_STOP_MAP = {
    "tool_calls": "tool_use",       # OpenAI 用 tool_calls，统一成 tool_use
    "function_call": "tool_use",    # 兼容老 function_call API
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filter",
}
```

Agent.core 只会判断 `tool_uses()` 是否为空，所以 stop_reason 主要是审计 + 调试用。

---

## 6. MockBackend（llm.py:356-488）

**不调用任何外部 API**。完全用正则路由 + 规则匹配产生 tool_use。两个目的：
1. 离线 demo / CI 跑测试
2. benchmarks/bench_ask.py 用它当后端跑性能（避免网络延迟干扰）

### 6.1 主流程：阶段一 vs 阶段二

```python
def chat(self, system, messages, tools):
    last = messages[-1] if messages else None
    # 阶段二：上一轮已发起 tool_use，user 在送 tool_result 进来
    if last and last["role"] == "user" and isinstance(last["content"], list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in last["content"]
    ):
        return self._summarize(last["content"])

    # 阶段一：按 user 文本路由到工具
    text = self._extract_user_text(messages)
    if not text:
        return AssistantMessage(blocks=[TextBlock(text="（mock 后端）需要更具体的问题。")])

    tool_name, args = self._route(text)
    if tool_name is None:
        return AssistantMessage(blocks=[TextBlock(text=self._fallback_reply(text))])

    names = {t["name"] for t in tools}
    if tool_name not in names:
        return AssistantMessage(blocks=[
            TextBlock(text=f"（mock）希望调用 {tool_name} 但该工具未注册，已退回文本回复。")
        ])
    return AssistantMessage(
        blocks=[
            TextBlock(text=f"我先通过工具 `{tool_name}` 感知一下系统再回答。"),
            ToolUseBlock(id=f"mock-{uuid.uuid4().hex[:8]}", name=tool_name, input=args),
        ],
        stop_reason="tool_use",
    )
```

**阶段切换不靠状态**，靠观察 `messages[-1]` 是不是 tool_result。这让 Mock 是 **无状态的**，多线程安全，多次调用结果可预测。

### 6.2 路由规则（llm.py:410 `_route`）

按关键词优先级顺序：

| 触发词 | 路由到工具 | 参数 |
|---|---|---|
| "重启" / "restart" + unit | `svc_restart` | `{"unit":...}` |
| "服务状态" / "status" + unit | `svc_status` | `{"unit":...}` |
| "端口" / "port" + 数字 | `lsof_port` | `{"port":int}` |
| "监听" / "listen" | `net_listen` | `{"proto":"tcp"}` |
| "cpu" / "进程" / "占用" | `process_list` | `{"sort_by":"cpu/mem","limit":10}` |
| "磁盘" / "disk" | `fs_df` | `{}` |
| "日志" / "log" / "错误" | `log_journal` | `{"lines":50, "priority":...}` |
| "防火墙" | `svc_status` | `{"unit":"firewalld"}` |
| "软件包" | `pkg_installed` | `{}` |
| 其它 | `None` → fallback 文本 | — |

注意一些细节：
- `_UNIT_RE` 匹配的服务名白名单（`sshd?|nginx|httpd?|mysqld?|...`），不会把任意字符串当 unit
- "内存"出现时 sort_by 自动切到 mem
- 日志触发词加上"错误"会自动加 `priority=err`，"ssh"会自动加 `unit=sshd`

### 6.3 阶段二总结（llm.py:466）

```python
def _summarize(self, tool_results):
    parts = []
    for r in tool_results:
        content = r.get("content", "")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        if r.get("is_error"):
            parts.append(f"[!] 工具返回错误：\n{content}")
        else:
            snippet = content[:1200]
            parts.append(
                "下面是工具返回的关键内容（已截取前 1200 字）：\n"
                "```\n"
                f"{snippet}\n"
                "```\n"
                "（mock 后端不做推理总结，真实部署请配置真实 LLM 后端）"
            )
    return AssistantMessage(
        blocks=[TextBlock(text="\n\n".join(parts) or "（无结果）")],
        stop_reason="end_turn",
    )
```

阶段二直接把 tool_result 内容截 1200 字贴回给用户。没有真正的推理总结——离线模式下没法做真总结。注释里明确告诉用户"真实部署请配置真实 LLM 后端"。

---

## 7. 工厂函数 build_backend（llm.py:494）

```python
def build_backend(cfg) -> LlmBackend:
    name = (cfg.agent.llm_backend or "mock").lower()
    if name == "mock":
        return MockBackend()
    if name == "anthropic":
        return AnthropicBackend(
            model=cfg.agent.anthropic.model,
            max_tokens=cfg.agent.anthropic.max_tokens,
            api_key_env=cfg.agent.anthropic.api_key_env,
        )
    if name == "openai":
        return OpenAIBackend(
            model=cfg.agent.openai.model,
            max_tokens=cfg.agent.openai.max_tokens,
            temperature=cfg.agent.openai.temperature,
            api_key_env=cfg.agent.openai.api_key_env,
            base_url=cfg.agent.openai.base_url,
            organization=cfg.agent.openai.organization,
        )
    raise ValueError(f"未知 LLM 后端：{name}")
```

`cfg.agent.llm_backend` 默认来自 `configs/default.yaml` 的 `deepseek_httpx`，也可以由项目根 `kyagent.json` 顶层 `llm_backend` 覆盖；显式环境变量 `KYAGENT_LLM_BACKEND` 优先级最高。DeepSeek key 可来自 `DEEPSEEK_API_KEY`，也可来自项目根 `kyagent.json` 的 `deepseek_api_key` 或 `deepseek.api_key`；环境变量优先。真实后端缺 key 时直接报错；如需离线演示，必须显式设置 `llm_backend=mock`。拼写错误仍直接报错。

---

## 7.5 流式输出（v2）

为支持 TUI 实时显示思考过程，`LlmBackend` 增加了一个并行接口：

```python
def chat_stream(
    self,
    system: str,
    messages: list[dict],
    tools: list[dict],
    on_delta: Callable[[str], None],
) -> AssistantMessage:
    ...
```

返回值仍是 `AssistantMessage`（与 `chat()` 同形），区别仅是 reasoning text 在生成过程中被逐 chunk 推给 `on_delta`。基类提供 fallback：调用一次 `chat()`，把最终 text 一次性发给 `on_delta`，再返回结果——这样任何只实现 `chat()` 的后端都自动具备"流式 API、非流式表现"。

四个具体后端的策略：

- **MockBackend** — 拿到最终 text 后按空格切块，逐块回调 `on_delta`，方便离线模拟流式 UI。
- **HttpxBackend** — 走 OpenAI SSE 协议（默认 `deepseek_httpx` 的真实路径）：`POST` 时带 `stream=True`，用 `httpx.stream` + `iter_lines` 读 `data: {...}` / `data: [DONE]` 行，按 `choice` index 累积 `tool_calls`，每行 `delta.content` 推给 `on_delta`。纯 Python，零 Rust。
- **OpenAIBackend** — SDK `stream=True`，遍历 chunk 拿 `delta.content` 推给 `on_delta`，结束后合成 `AssistantMessage`。
- **AnthropicBackend** — 不实现，走基类 fallback。Anthropic SDK 的 `messages.stream()` 内部触发 jiter 的 Rust 编译路径，对 LoongArch Old World 不友好；保持现有"不在默认部署路径上拉 jiter"的策略。

Agent 主循环里只有 TUI 通道会走 `chat_stream`：CLI 的 `ask` / `chat` 仍调用 `chat()`，行为与旧版一致。

---

## 8. 测试覆盖（tests/test_openai_backend.py）

这个测试用例集很值得读，它演示了"如何不依赖真实 OpenAI SDK / 网络 测一个 SDK 适配器"：

```python
@pytest.fixture
def backend(monkeypatch):
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return OpenAIBackend(model="gpt-4o-mini", max_tokens=512, temperature=0.0)
```

把 `openai` 模块整个换成假对象，`OpenAIBackend` 在 import 阶段加载假 SDK，构造时连"假"client。

覆盖的场景：
- tools 翻译 → function envelope
- user 纯字符串 → 直接 passthrough
- user 含 tool_result → 拆成 `role:"tool"` 多条
- user 含 list of text blocks → 合并成一条
- assistant 含 tool_use → 翻译成 tool_calls 字段
- 响应只有 text → AssistantMessage 只含 TextBlock
- 响应含 tool_call → 解析出 ToolUseBlock，参数 JSON 解码
- 非法 JSON arguments → 兜底为 `{"_raw": ...}`
- chat 调用透传 model / max_tokens / temperature / tool_choice
- build_backend 工厂能根据 cfg 构造 OpenAI 实例
- 缺失 API key 抛 RuntimeError

---

## 9. 一些不直观的设计选择

### 9.1 为什么内部用 Anthropic 风格而不是 OpenAI 风格？

- Anthropic 的 content blocks 模型更"声明式"——一条 assistant message 可以同时含 text + tool_use，互不冲突
- OpenAI 的 tool_calls 在 message 上，content 为 None 时容易和"无回复"混淆
- 项目最早写的就是 Anthropic，OpenAI 后加，所以选了不动主路径的方案

代价：OpenAIBackend 内部要做两次翻译（请求时 Ant→OAI，响应时 OAI→Ant）。但翻译逻辑被完整测试覆盖。

### 9.2 为什么 system prompt 放在 chat() 参数而不是 messages 里？

- Anthropic API 的 system 是独立顶层参数，不放在 messages 里
- OpenAI API 的 system 在 messages 里
- 两边都需要的话，把 system 作为单独参数最干净

OpenAIBackend 在 `_to_openai_messages` 开头把 system 加进去。

### 9.3 为什么 Mock 不做真总结？

- 真总结需要语义理解，本质就是 LLM
- 让 Mock 做"假总结"会误导用户以为离线也能用——明确告诉用户"配 Anthropic"是诚实做法

---

## 10. 关键不变量

1. **chat() 是无状态的** —— Backend 不维护对话历史，messages 全部由 Agent 传入
2. **chat() 返回的 AssistantMessage 必定包含 blocks**（哪怕只有一个 TextBlock）
3. **tool_use 的 input 必定是 dict**（哪怕 OpenAI 返回了非法 JSON 也兜底为 `{"_raw":...}`）
4. **AssistantMessage.raw 保留原始响应**，方便审计回看时反推 LLM 真实输出

---

## 11. 下一步

继续 → [05-safety-layer.md](./05-safety-layer.md) 看安全护栏怎么工作。
