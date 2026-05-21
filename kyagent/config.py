"""配置加载：从 YAML + 环境变量构建强类型配置。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


def _expand_env(value: Any) -> Any:
    """递归把 `${VAR:-default}` 展开为环境变量值。"""
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class AnthropicConfig(BaseModel):
    model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    api_key_env: str = "ANTHROPIC_API_KEY"


class OpenAIConfig(BaseModel):
    """OpenAI Python SDK 适配后端配置。

    base_url 留空则走 https://api.openai.com/v1；填上即可对接任何 OpenAI 协议兼容服务
    （Azure OpenAI 用其专用端点、vLLM/Ollama 等本地推理服务走 /v1 路径）。
    DeepSeek / Qwen 已有专用配置节，建议优先用 llm_backend=deepseek / qwen。
    """
    model: str = "gpt-4o-mini"
    max_tokens: int = 4096
    temperature: float = 0.2
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    organization: str | None = None


class DeepSeekConfig(BaseModel):
    """DeepSeek 后端（走 OpenAI 协议兼容路径）。

    2026-05 官方推荐：openai Python SDK + base_url=https://api.deepseek.com。
    模型 ID 选择 deepseek-v4-flash（tools 支持完整、性价比最高）；
    deepseek-v4-pro 适合强推理；legacy 别名 deepseek-chat/deepseek-reasoner 在
    2026-07-24 退役，不建议新代码使用。
    Key 获取：https://platform.deepseek.com
    """
    model: str = "deepseek-v4-flash"
    max_tokens: int = 4096
    temperature: float = 0.2
    api_key_env: str = "DEEPSEEK_API_KEY"
    # 留空则使用预设 https://api.deepseek.com；仅在使用第三方反代时填
    base_url: str | None = None


class QwenConfig(BaseModel):
    """通义千问后端（走 DashScope OpenAI 协议兼容路径）。

    2026-05 验证：base_url=https://dashscope.aliyuncs.com/compatible-mode/v1（国内）
    或 https://dashscope-intl.aliyuncs.com/compatible-mode/v1（海外/新加坡）。
    模型选 qwen-plus（性价比），高级任务可换 qwen3-max / qwen3-coder-plus。
    Key 获取：https://bailian.console.aliyun.com （国内）
    """
    model: str = "qwen-plus"
    max_tokens: int = 4096
    temperature: float = 0.2
    api_key_env: str = "DASHSCOPE_API_KEY"
    # 留空则用国内端点；海外用户填 https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    base_url: str | None = None


class AgentConfig(BaseModel):
    name: str = "kyagent"
    # 支持的 backend: mock / anthropic / openai / deepseek / qwen
    llm_backend: str = "mock"
    # 当真实后端缺 API key 时是否自动降级到 mock。
    # true（默认）：开发者友好，CLI 打印 warning 后用 mock 跑完闭环
    # false：缺 key 直接报错，适合 CI / 生产部署
    fallback_to_mock: bool = True
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    qwen: QwenConfig = Field(default_factory=QwenConfig)
    max_iterations: int = 8


class ExecutorConfig(BaseModel):
    account: str = "kyagent"
    timeout: int = 30
    output_cap: int = 65536
    # 赛题"最小权限代理执行：非必要不使用 root"语义：
    #   forbid_root=true → 默认按 kyagent 账户跑；requires_root=True 时通过 sudoers 白名单走 sudo
    #   forbid_root_strict=true → 彻底拒绝任何 root 提升（演示 / 无 sudoers 部署）
    forbid_root: bool = True
    forbid_root_strict: bool = False
    path: list[str] = Field(default_factory=lambda: ["/usr/local/bin", "/usr/bin", "/bin"])


class SafetyPolicy(BaseModel):
    critical: str = "deny"
    high: str = "confirm"
    medium: str = "confirm"
    low: str = "allow"


class SafetyConfig(BaseModel):
    rules_file: str = "configs/safety-rules.yaml"
    # 自然语言意图层规则（赛题第 3 条"对自然语言指令的意图风险过滤" + 抗 Prompt Injection）
    intent_rules_file: str = "configs/intent-rules.yaml"
    # 是否启用自然语言意图层（默认启用 — 不启用就不符合赛题第 3 条）
    intent_check: bool = True
    policy: SafetyPolicy = Field(default_factory=SafetyPolicy)
    llm_review: bool = False


class AuditConfig(BaseModel):
    database: str = "./var/audit.db"
    jsonl_file: str | None = "./var/audit.jsonl"
    retain_days: int = 90


class McpConfig(BaseModel):
    enable_tools: list[str] = Field(default_factory=list)
    server_name: str = "kyagent"
    server_version: str = "0.1.0"


class Config(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    # 配置文件所在目录，用于解析其它相对路径
    base_dir: Path = Field(default_factory=Path.cwd)

    def resolve(self, p: str | Path) -> Path:
        path = Path(p)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()


def find_default_config() -> Path | None:
    """优先 env，其次 cwd/configs，最后 package 目录。"""
    env = os.environ.get("KYAGENT_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    for candidate in (
        Path.cwd() / "configs" / "default.yaml",
        Path(__file__).parent.parent / "configs" / "default.yaml",
    ):
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> Config:
    """加载并展开 YAML 配置。"""
    cfg_path = Path(path) if path else find_default_config()
    if cfg_path is None or not cfg_path.exists():
        # 没有配置文件时使用全默认
        return Config(base_dir=Path.cwd())

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)
    cfg = Config.model_validate(raw)
    cfg.base_dir = cfg_path.parent.parent  # configs/ 的父目录是项目根
    return cfg
