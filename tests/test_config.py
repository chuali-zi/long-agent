from __future__ import annotations

import json
from pathlib import Path

from kyagent.config import Config, load_config


ROOT = Path(__file__).resolve().parents[1]


def test_config_default_llm_backend_is_real_httpx_backend():
    cfg = Config()
    assert cfg.agent.llm_backend == "deepseek_httpx"


def test_config_default_tool_and_token_budgets_are_expanded():
    cfg = Config()

    assert cfg.agent.max_iterations == 40
    assert cfg.agent.anthropic.max_tokens == 20480
    assert cfg.agent.openai.max_tokens == 20480
    assert cfg.agent.deepseek.max_tokens == 20480
    assert cfg.agent.qwen.max_tokens == 20480


def test_shipped_backend_configs_use_expanded_budgets():
    provider_attr_by_config = {
        "default.yaml": "anthropic",
        "deepseek.yaml": "deepseek",
        "openai.yaml": "openai",
        "qwen.yaml": "qwen",
    }

    for filename, provider_attr in provider_attr_by_config.items():
        cfg = load_config(ROOT / "configs" / filename)

        assert cfg.agent.max_iterations == 40
        assert getattr(cfg.agent, provider_attr).max_tokens == 20480


def test_project_root_json_llm_backend_overrides_yaml_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KYAGENT_LLM_BACKEND", raising=False)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "default.yaml"
    config_path.write_text(
        "agent:\n"
        "  llm_backend: mock\n",
        encoding="utf-8",
    )
    (tmp_path / "kyagent.json").write_text(
        json.dumps({"llm_backend": "qwen_httpx"}),
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert cfg.agent.llm_backend == "qwen_httpx"


def test_env_llm_backend_takes_precedence_over_project_root_json(tmp_path, monkeypatch):
    monkeypatch.setenv("KYAGENT_LLM_BACKEND", "openai_httpx")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "default.yaml"
    config_path.write_text(
        "agent:\n"
        "  llm_backend: ${KYAGENT_LLM_BACKEND:-deepseek_httpx}\n",
        encoding="utf-8",
    )
    (tmp_path / "kyagent.json").write_text(
        json.dumps({"llm_backend": "qwen_httpx"}),
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert cfg.agent.llm_backend == "openai_httpx"


def test_project_root_json_deepseek_api_key_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "default.yaml"
    config_path.write_text(
        "agent:\n"
        "  llm_backend: deepseek_httpx\n",
        encoding="utf-8",
    )
    (tmp_path / "kyagent.json").write_text(
        json.dumps({"deepseek_api_key": "sk-json"}),
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert not hasattr(cfg.agent.deepseek, "api_key")


def test_project_root_json_nested_deepseek_api_key_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "default.yaml"
    config_path.write_text(
        "agent:\n"
        "  llm_backend: deepseek_httpx\n",
        encoding="utf-8",
    )
    (tmp_path / "kyagent.json").write_text(
        json.dumps({"deepseek": {"api_key": "sk-nested"}}),
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert not hasattr(cfg.agent.deepseek, "api_key")


def test_default_config_executor_account_can_follow_runtime_env(monkeypatch):
    monkeypatch.setenv("KYAGENT_EXECUTOR_ACCOUNT", "opsagent")

    cfg = load_config(ROOT / "configs" / "default.yaml")

    assert cfg.executor.account == "opsagent"


def test_opt_install_defaults_runtime_state_paths_to_runtime_dirs(tmp_path, monkeypatch):
    install_prefix = tmp_path / "opt" / "kyagent"
    config_dir = install_prefix / "configs"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "deepseek.yaml"
    config_path.write_text(
        "audit:\n"
        "  database: ${KYAGENT_AUDIT_DB:-./var/audit.db}\n"
        "  jsonl_file: ${KYAGENT_AUDIT_JSONL:-./var/audit.jsonl}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KYAGENT_INSTALL_PREFIX", str(install_prefix))
    monkeypatch.delenv("KYAGENT_AUDIT_DB", raising=False)
    monkeypatch.delenv("KYAGENT_AUDIT_JSONL", raising=False)
    monkeypatch.delenv("KYAGENT_PLAN_DB", raising=False)

    cfg = load_config(config_path)

    assert cfg.audit.database == "/var/lib/kyagent/audit.db"
    assert cfg.audit.jsonl_file == "/var/log/kyagent/audit.jsonl"
    assert cfg.planning.database == "/var/lib/kyagent/plans.db"


def test_opt_install_runtime_env_state_paths_take_precedence(tmp_path, monkeypatch):
    install_prefix = tmp_path / "opt" / "kyagent"
    config_dir = install_prefix / "configs"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "deepseek.yaml"
    config_path.write_text(
        "audit:\n"
        "  database: ${KYAGENT_AUDIT_DB:-./var/audit.db}\n"
        "  jsonl_file: ${KYAGENT_AUDIT_JSONL:-./var/audit.jsonl}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KYAGENT_INSTALL_PREFIX", str(install_prefix))
    monkeypatch.setenv("KYAGENT_AUDIT_DB", "/srv/kyagent/audit.db")
    monkeypatch.setenv("KYAGENT_AUDIT_JSONL", "/srv/kyagent/audit.jsonl")
    monkeypatch.setenv("KYAGENT_PLAN_DB", "/srv/kyagent/plans.db")

    cfg = load_config(config_path)

    assert cfg.audit.database == "/srv/kyagent/audit.db"
    assert cfg.audit.jsonl_file == "/srv/kyagent/audit.jsonl"
    assert cfg.planning.database == "/srv/kyagent/plans.db"
