from __future__ import annotations

import json

from kyagent.config import Config, load_config


def test_config_default_llm_backend_is_real_httpx_backend():
    cfg = Config()
    assert cfg.agent.llm_backend == "deepseek_httpx"


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


def test_project_root_json_deepseek_api_key_is_loaded(tmp_path, monkeypatch):
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

    assert cfg.agent.deepseek.api_key == "sk-json"


def test_project_root_json_nested_deepseek_api_key_is_loaded(tmp_path, monkeypatch):
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

    assert cfg.agent.deepseek.api_key == "sk-nested"
