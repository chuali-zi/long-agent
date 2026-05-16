"""MCP 工具基类与注册器。

设计：
  - Tool 是声明式工具：name/description/input_schema/risk_level/requires_root
  - build_argv() 把 LLM 给的参数翻成最终 argv（应用层负责再过 Guardrail）
  - format_result() 把 ExecutionProxy 的结果转成给 LLM 看的 ToolResult
  - ToolRegistry 管理所有已注册工具，可序列化为 MCP tools/list 响应
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from kyagent.executor.proxy import ExecutionResult
from kyagent.safety.patterns import RiskLevel


class ToolError(Exception):
    """工具参数非法或语义错误，应作为 ToolResult.error 返回给 LLM。"""


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "data": self.data, "error": self.error}


class Tool(abc.ABC):
    """每个内置工具都继承此类。"""

    # 子类必须覆盖
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    risk_level: RiskLevel = RiskLevel.LOW
    requires_root: bool = False
    # 标记是否为只读（用于风险审计 / 工具白名单）
    read_only: bool = True

    # ---- MCP 序列化 ----------------------------------------------------

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    # ---- 参数校验 ------------------------------------------------------

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """根据 input_schema 做轻量校验。

        只校验 required + type，类型按 JSON Schema 一级简化处理。
        缺失参数 → ToolError。
        """
        props = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])
        cleaned: dict[str, Any] = {}

        for key in required:
            if key not in args:
                raise ToolError(f"参数 {key!r} 必填")

        for k, v in args.items():
            schema = props.get(k)
            if schema is None:
                # 严格模式：忽略未知字段；可扩展为报错
                continue
            cleaned[k] = self._coerce_type(v, schema, k)
        return cleaned

    @staticmethod
    def _coerce_type(value: Any, schema: dict[str, Any], key: str) -> Any:
        expected = schema.get("type")
        if expected is None:
            return value
        if expected == "string" and not isinstance(value, str):
            return str(value)
        if expected == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ToolError(f"{key} 期望 integer，收到 {value!r}")
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "y")
            return bool(value)
        if expected == "array" and not isinstance(value, list):
            raise ToolError(f"{key} 期望 array")
        return value

    # ---- 子类实现的接口 ------------------------------------------------

    @abc.abstractmethod
    def build_argv(self, args: dict[str, Any]) -> list[str]:
        """根据已校验过的 args 构造最终 argv。"""

    def format_result(self, exec_result: ExecutionResult) -> ToolResult:
        """默认格式化：成功取 stdout，失败带 stderr。子类可定制结构化输出。"""
        # windows_mock 是开发态占位，仍按成功返回，让上层 LLM 看到提示性输出
        if exec_result.skipped_reason == "windows_mock":
            return ToolResult(ok=True, content=exec_result.stdout, data=exec_result.to_dict())
        if exec_result.skipped_reason:
            return ToolResult(
                ok=False, content="",
                error=f"{exec_result.skipped_reason}: {exec_result.stderr}",
                data=exec_result.to_dict(),
            )
        if exec_result.timed_out:
            return ToolResult(
                ok=False, content=exec_result.stdout,
                error="execution timed out",
                data=exec_result.to_dict(),
            )
        if exec_result.returncode != 0:
            return ToolResult(
                ok=False, content=exec_result.stdout,
                error=exec_result.stderr or f"exit={exec_result.returncode}",
                data=exec_result.to_dict(),
            )
        return ToolResult(ok=True, content=exec_result.stdout, data=exec_result.to_dict())


class ToolRegistry:
    """已注册工具的容器。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_mcp_list(self) -> list[dict[str, Any]]:
        """tools/list 的标准响应内容。"""
        return [t.to_mcp() for t in self.all()]

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        """Anthropic Messages API 的 tools 字段格式。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self.all()
        ]
