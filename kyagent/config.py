"""配置加载：从 YAML + 项目根 JSON + 环境变量构建强类型配置。"""
from __future__ import annotations

import json
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
    max_tokens: int = 20480
    api_key_env: str = "ANTHROPIC_API_KEY"


class OpenAIConfig(BaseModel):
    """OpenAI Python SDK 适配后端配置。

    base_url 留空则走 https://api.openai.com/v1；填上即可对接任何 OpenAI 协议兼容服务
    （Azure OpenAI 用其专用端点、vLLM/Ollama 等本地推理服务走 /v1 路径）。
    DeepSeek / Qwen 已有专用配置节，建议优先用 llm_backend=deepseek / qwen；
    LoongArch Old World 推荐 deepseek_httpx / qwen_httpx / openai_httpx 这类纯 httpx 路径。
    """
    model: str = "gpt-4o-mini"
    max_tokens: int = 20480
    temperature: float = 0.2
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    organization: str | None = None


class DeepSeekConfig(BaseModel):
    """DeepSeek 后端（走 OpenAI 协议兼容路径）。

    官方示例使用 OpenAI API 格式 + base_url=https://api.deepseek.com。
    LoongArch Old World 推荐 deepseek_httpx，避免 openai SDK 拉入 jiter。
    Key 获取：https://platform.deepseek.com
    """
    model: str = "deepseek-v4-flash"
    max_tokens: int = 20480
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
    max_tokens: int = 20480
    temperature: float = 0.2
    api_key_env: str = "DASHSCOPE_API_KEY"
    # 留空则用国内端点；海外用户填 https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    base_url: str | None = None


class AgentConfig(BaseModel):
    name: str = "kyagent"
    # 支持的 backend: mock / anthropic / openai / deepseek / qwen /
    #               openai_httpx / deepseek_httpx / qwen_httpx
    llm_backend: str = "deepseek_httpx"
    # 兼容旧配置字段；真实后端缺 API key 时不再自动降级，统一直接报错。
    # 如需离线 demo，请显式设置 llm_backend=mock。
    fallback_to_mock: bool = False
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    qwen: QwenConfig = Field(default_factory=QwenConfig)
    max_iterations: int = 40


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
    integrity_enabled: bool = False
    hmac_key_env: str = "KYAGENT_AUDIT_HMAC_KEY"
    hmac_key_file: str | None = None
    hmac_key_id: str = "local-v1"


class McpConfig(BaseModel):
    enable_tools: list[str] = Field(default_factory=list)
    plugin_entry_points: list[str] = Field(default_factory=list)
    server_name: str = "kyagent"
    server_version: str = "0.1.0"


class RcaConfig(BaseModel):
    playbooks_file: str = "configs/rca-playbooks.yaml"


class Config(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    rca: RcaConfig = Field(default_factory=RcaConfig)
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


def _project_root_for_config(cfg_path: Path | None) -> Path:
    if cfg_path is None:
        return Path.cwd()
    if cfg_path.parent.name == "configs":
        return cfg_path.parent.parent
    return cfg_path.parent


def _apply_project_json_overrides(raw: dict[str, Any], project_root: Path) -> dict[str, Any]:
    """读取项目根 kyagent.json 的轻量覆盖项。

    当前公开支持顶层 key：
      {"llm_backend": "deepseek_httpx"}

    为避免破坏现有环境变量部署，KYAGENT_LLM_BACKEND 显式设置时优先级更高。
    项目文件中的任何密钥字段都会被忽略，密钥只能通过环境注入。
    """
    json_path = project_root / "kyagent.json"
    if not json_path.exists():
        return raw

    data = json.loads(json_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{json_path} 必须是 JSON object")

    agent = raw.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise ValueError("配置项 agent 必须是 object")

    llm_backend = data.get("llm_backend")
    if llm_backend is not None and not os.environ.get("KYAGENT_LLM_BACKEND"):
        agent["llm_backend"] = llm_backend
    # 项目文件不再承载任何密钥。历史 kyagent.json 可能仍残留 key 字段；
    # 为避免旧本地文件阻塞启动，静默忽略它们，只允许环境变量或部署 env 文件注入。
    return raw


def _is_default_audit_path(value: str | None, default_name: str) -> bool:
    if value is None:
        return False
    return Path(value).as_posix() in {f"var/{default_name}", f"./var/{default_name}"}


def _apply_opt_runtime_defaults(cfg: Config) -> None:
    """Keep production runtime state out of the read-only /opt install tree."""
    install_prefix = Path(os.environ.get("KYAGENT_INSTALL_PREFIX", "/opt/kyagent"))
    try:
        is_install_prefix = (
            cfg.base_dir.resolve() == install_prefix.expanduser().resolve()
        )
    except OSError:
        is_install_prefix = cfg.base_dir == install_prefix.expanduser()
    if not is_install_prefix:
        return

    if (
        "KYAGENT_AUDIT_DB" not in os.environ
        and _is_default_audit_path(cfg.audit.database, "audit.db")
    ):
        cfg.audit.database = "/var/lib/kyagent/audit.db"
    if (
        "KYAGENT_AUDIT_JSONL" not in os.environ
        and _is_default_audit_path(cfg.audit.jsonl_file, "audit.jsonl")
    ):
        cfg.audit.jsonl_file = "/var/log/kyagent/audit.jsonl"


def load_config(path: str | Path | None = None) -> Config:
    """加载并展开 YAML 配置，再应用项目根 kyagent.json 的轻量覆盖。"""
    cfg_path = Path(path) if path else find_default_config()
    if cfg_path is None or not cfg_path.exists():
        # 没有配置文件时使用全默认
        return Config(base_dir=Path.cwd())

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)
    project_root = _project_root_for_config(cfg_path)
    raw = _apply_project_json_overrides(raw, project_root)
    # pydantic v1 API（v2 等价为 model_validate）；锁 v1 是为了在龙芯老世界绕开 pydantic-core Rust 编译
    cfg = Config.parse_obj(raw)
    cfg.base_dir = project_root
    _apply_opt_runtime_defaults(cfg)
    return cfg
