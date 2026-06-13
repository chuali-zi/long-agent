from __future__ import annotations

from kyagent.agent.prompt import SYSTEM_PROMPT


def test_prompt_distinguishes_current_and_rotated_access_logs() -> None:
    assert "当前 `access.log`" in SYSTEM_PROMPT
    assert "`access.log.N` / `access.log.N.gz`" in SYSTEM_PROMPT
    assert "audit/security/incident/database" in SYSTEM_PROMPT


def test_prompt_cron_injection_preserves_evidence() -> None:
    assert "只禁用可疑 cron 入口" in SYSTEM_PROMPT
    assert "保留脚本和相关文件证据" in SYSTEM_PROMPT


def test_prompt_requires_accurate_reporting_sections() -> None:
    assert "已确认" in SYSTEM_PROMPT
    assert "未检查" in SYSTEM_PROMPT
    assert "不在本次范围" in SYSTEM_PROMPT
    assert "file_cleanup_candidates" in SYSTEM_PROMPT


def test_prompt_allows_controlled_permission_tightening_only() -> None:
    assert "受控权限修复工具" in SYSTEM_PROMPT
    assert "收紧到 `0750`/`0755`" in SYSTEM_PROMPT
    assert "仍禁止通用危险 `chmod`" in SYSTEM_PROMPT
