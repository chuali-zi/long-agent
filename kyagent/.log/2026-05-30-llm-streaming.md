# 2026-05-30 LLM 流式输出

## 改动范围
`kyagent/agent/llm.py` 单文件 + 新增 `tests/test_llm_streaming.py`。

## 实现策略
- **LlmBackend 基类**：新增默认 `chat_stream`，回退 `chat()` 后把 TextBlock
  拼接整段 on_delta 一次，try/except 包裹回调避免 UI 拖崩后端。
- **MockBackend**：按空格切 5–10 段，每段 0.01s sleep；空 text 跳过。
- **HttpxBackend**：`stream=True` POST，`client.stream("POST",...)` +
  `iter_lines` 解析 SSE：跳过非 `data:` 行、`[DONE]` 退出、JSON 块按
  index 累积 tool_calls；text 增量实时 on_delta，**绝不**推 tool JSON。
  流终止后构造伪 OpenAI dict 走 `_from_openai_dict` 同一路径，stop_reason
  映射 / args 解析零行为漂移。网络异常 (`_RETRY_EXC_TYPES`) 整段重试，
  最多 `max_retries` 次，重试前发 `[重试 LLM 流]\n` 提示。4xx / 重试用
  尽 fail-fast。
- **OpenAIBackend**：SDK 原生 stream=True，按 chunk.delta 取 content /
  tool_calls，逻辑与 HttpxBackend 镜像；同样复用 `_from_openai_dict`。
- **AnthropicBackend**：保留基类默认（jiter 在龙芯不可用）。

## 测试覆盖（13 条）
基类 fallback / 回调异常吞 / 空 text；Mock 多块拼接 == chat 文本 / 无
TextBlock 不发 delta；Httpx SSE 纯文本 / `[DONE]` 终止 / tool_call 分段
累积且 on_delta 不含 JSON / ReadTimeout 重试成功 / ConnectError 耗尽 /
HTTP 400 fail-fast。

## 验证
`pytest tests/test_llm_streaming.py tests/test_httpx_backend.py
tests/test_openai_backend.py -q` → 79 passed。全量套件 264 passed。

## 偏离
SSE 路径下 HTTP 状态码级重试（429/5xx）未做细粒度退避，仅按方案要求
对网络异常整段重试 —— 简化处理，与任务原文一致。
