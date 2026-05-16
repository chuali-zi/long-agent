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


class AgentConfig(BaseModel):
    name: str = "kyagent"
    llm_backend: str = "mock"
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    max_iterations: int = 8


class ExecutorConfig(BaseModel):
    account: str = "kyagent"
    timeout: int = 30
    output_cap: int = 65536
    forbid_root: bool = True
    path: list[str] = Field(default_factory=lambda: ["/usr/local/bin", "/usr/bin", "/bin"])


class SafetyPolicy(BaseModel):
    critical: str = "deny"
    high: str = "confirm"
    medium: str = "confirm"
    low: str = "allow"


class SafetyConfig(BaseModel):
    rules_file: str = "configs/safety-rules.yaml"
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
