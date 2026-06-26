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
        """根据 input_schema 校验 + 类型 coerce。

        校验的 JSON Schema 关键字（covers MCP "Servers MUST validate all tool inputs"）：
          - required : 必填字段
          - type     : string / integer / number / boolean / array / object
          - enum     : 离散值集合
          - minimum / maximum : 数值范围
          - minLength / maxLength : 字符串长度
          - pattern  : 字符串正则
        校验失败一律抛 ToolError；MCP server / Agent.core 把它转成干净的客户端可见错误，
        不会进 traceback 兜底分支（这是 codex 报告的"traceback 泄漏路径" 的修复点）。
        """
        if not isinstance(args, dict):
            raise ToolError(f"工具参数必须是对象，收到 {type(args).__name__}")

        props = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])
        additional = self.input_schema.get("additionalProperties", False)
        cleaned: dict[str, Any] = {}

        for key in required:
            if key not in args:
                raise ToolError(f"参数 {key!r} 必填")

        for k, v in args.items():
            schema = props.get(k)
            if schema is None:
                if additional is False:
                    raise ToolError(f"未声明的字段 {k!r}")
                continue
            cleaned[k] = self._coerce_and_validate(v, schema, k)
        return cleaned

    @classmethod
    def _coerce_and_validate(cls, value: Any, schema: dict[str, Any], key: str) -> Any:
        """先做 type coerce（兼容 LLM 偶尔以字符串回传数字），再走全 schema 约束。"""
        value = cls._coerce_type(value, schema, key)
        cls._check_constraints(value, schema, key)
        return value

    @staticmethod
    def _coerce_type(value: Any, schema: dict[str, Any], key: str) -> Any:
        expected = schema.get("type")
        if expected is None:
            return value
        if expected == "string":
            if not isinstance(value, str):
                return str(value)
            return value
        if expected == "integer":
            if isinstance(value, bool):
                # JSON Schema 把 bool 视为 number 但不视为 integer 的"语义" — 我们严格化
                raise ToolError(f"{key} 期望 integer，收到 boolean")
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ToolError(f"{key} 期望 integer，收到 {value!r}")
        if expected == "number":
            if isinstance(value, bool):
                raise ToolError(f"{key} 期望 number，收到 boolean")
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ToolError(f"{key} 期望 number，收到 {value!r}")
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "y")
            return bool(value)
        if expected == "array":
            if not isinstance(value, list):
                raise ToolError(f"{key} 期望 array")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                return [
                    Tool._coerce_and_validate(item, item_schema, f"{key}[{idx}]")
                    for idx, item in enumerate(value)
                ]
            return value
        if expected == "object":
            if not isinstance(value, dict):
                raise ToolError(f"{key} 期望 object")
            props = schema.get("properties", {})
            required = schema.get("required", [])
            additional = schema.get("additionalProperties", False)
            cleaned: dict[str, Any] = {}
            for required_key in required:
                if required_key not in value:
                    raise ToolError(f"{key}.{required_key!r} 必填")
            for child_key, child_value in value.items():
                child_schema = props.get(child_key)
                if child_schema is None:
                    if additional is False:
                        raise ToolError(f"{key} 含未声明字段 {child_key!r}")
                    cleaned[child_key] = child_value
                    continue
                cleaned[child_key] = Tool._coerce_and_validate(
                    child_value, child_schema, f"{key}.{child_key}"
                )
            return cleaned
        return value

    @staticmethod
    def _check_constraints(value: Any, schema: dict[str, Any], key: str) -> None:
        # enum
        if "enum" in schema:
            allowed = schema["enum"]
            if value not in allowed:
                raise ToolError(
                    f"{key}={value!r} 不在允许集合 {allowed!r}"
                )
        # 数值范围
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                raise ToolError(
                    f"{key}={value} 小于最小值 {schema['minimum']}"
                )
            if "maximum" in schema and value > schema["maximum"]:
                raise ToolError(
                    f"{key}={value} 大于最大值 {schema['maximum']}"
                )
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                raise ToolError(
                    f"{key}={value} 必须严格大于 {schema['exclusiveMinimum']}"
                )
            if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
                raise ToolError(
                    f"{key}={value} 必须严格小于 {schema['exclusiveMaximum']}"
                )
        # 字符串约束
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                raise ToolError(
                    f"{key} 长度 {len(value)} 小于 minLength {schema['minLength']}"
                )
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise ToolError(
                    f"{key} 长度 {len(value)} 超过 maxLength {schema['maxLength']}"
                )
            if "pattern" in schema:
                import re as _re
                if not _re.search(schema["pattern"], value):
                    raise ToolError(
                        f"{key}={value!r} 不匹配 pattern {schema['pattern']!r}"
                    )
        # 数组约束
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                raise ToolError(f"{key} 长度 {len(value)} 小于 minItems")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise ToolError(f"{key} 长度 {len(value)} 超过 maxItems")

    # ---- 子类实现的接口 ------------------------------------------------

    @abc.abstractmethod
    def build_argv(self, args: dict[str, Any]) -> list[str]:
        """根据已校验过的 args 构造最终 argv。"""

    def wants_privileged_retry(self, exec_result: ExecutionResult) -> bool:
        """非提权运行后，结果是否「被权限蒙蔽」、值得用 root 再跑一次。

        默认 False。感知工具（lsof_port / net_listen）在面对 root 起的监听进程时，
        以普通用户跑会得到「空 / 无进程名」的歧义结果，容易被误读成「端口空闲 /
        孤悬 socket」。这些工具覆盖本方法，仅在那种歧义场景返回 True，让流水线
        以 ``requires_root=True`` 单次重跑同一 argv（最小权限：正常结果不提权）。

        约束：仅在工具本身 ``requires_root=False`` 且当前非 root 时才会被流水线询问，
        重跑后不再二次询问，因此不会递归。
        """
        return False

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

    def enable_tools(self, names: list[str]) -> "ToolRegistry":
        """Keep only configured tools; an empty allowlist leaves all tools enabled."""
        if names:
            keep = set(names)
            self._tools = {name: tool for name, tool in self._tools.items() if name in keep}
        return self

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


# ---- Trend 类工具：跨时间采样 ------------------------------------------------


class TrendTool(Tool):
    """采样型工具基类。

    与普通 Tool 的唯一约定差别是：build_argv 应当生成"内含采样间隔"的命令
    （例如 ``iostat -dx 1 2`` / ``vmstat 1 2`` / ``sar -d 1 2``），让被调用
    程序自己负责跨时间采样并输出 delta。

    这样做的理由：
      1. 不需要改动 ExecutionProxy / pipeline.py（保持"一个工具 = 一次 argv
         执行"的不变量，审计链一笔清楚）。
      2. 避免在 Python 端 sleep —— 那要求 sudoers 放行 ``sh -c "...; sleep N; ..."``
         之类的 shell 拼接命令，会显著扩大攻击面。
      3. ``iostat`` / ``vmstat`` / ``sar`` 等工具是 sysstat 包的一部分，麒麟
         服务器版默认安装；它们的 ``<interval> <count>`` 参数是稳定 ABI。

    子类常量：
      ``interval_s`` —— 默认采样间隔（秒），子类可改但通常 ≤ 3
      ``sample_count`` —— 采样次数，固定 2（第一次基线，第二次返回 delta）

    对于没有原生 interval 支持的命令（例如 ``cat /proc/diskstats``、文件大小
    扫描），不要继承 TrendTool —— 把"快照"做成普通 Tool，让 LLM 自行决定两次
    调用 + 比对。这是更老实的边界，避免给 sudoers 开口子。
    """

    interval_s: int = 2
    sample_count: int = 2
    risk_level: RiskLevel = RiskLevel.LOW
    read_only: bool = True
