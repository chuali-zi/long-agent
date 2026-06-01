"""Small JSON-RPC validation helpers for the MCP stdio server."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolError(Exception):
    code: int
    message: str
    req_id: Any = None


@dataclass(frozen=True)
class Request:
    method: str
    params: dict[str, Any]
    req_id: Any
    is_notification: bool


def _valid_id(value: Any) -> bool:
    return value is None or (
        isinstance(value, (str, int, float)) and not isinstance(value, bool)
    )


def validate_request(message: Any) -> Request:
    """Validate the JSON-RPC envelope and normalize omitted params."""
    if not isinstance(message, dict):
        raise ProtocolError(-32600, "invalid request")

    req_id = message.get("id")
    if message.get("jsonrpc") != "2.0":
        raise ProtocolError(-32600, "invalid request", req_id)
    if "id" in message and not _valid_id(req_id):
        raise ProtocolError(-32600, "invalid request")

    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(-32600, "invalid request", req_id)

    params = message.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "params must be an object", req_id)

    return Request(
        method=method,
        params=params,
        req_id=req_id,
        is_notification="id" not in message,
    )


def validate_initialize(params: dict[str, Any], supported_version: str) -> None:
    version = params.get("protocolVersion")
    capabilities = params.get("capabilities")
    client_info = params.get("clientInfo")
    if version != supported_version:
        raise ProtocolError(-32602, "unsupported protocolVersion")
    if not isinstance(capabilities, dict):
        raise ProtocolError(-32602, "capabilities must be an object")
    if not isinstance(client_info, dict):
        raise ProtocolError(-32602, "clientInfo must be an object")
    if not isinstance(client_info.get("name"), str) or not client_info["name"]:
        raise ProtocolError(-32602, "clientInfo.name must be a non-empty string")
    if not isinstance(client_info.get("version"), str) or not client_info["version"]:
        raise ProtocolError(-32602, "clientInfo.version must be a non-empty string")


def validate_tool_call(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not name:
        raise ProtocolError(-32602, "tool name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise ProtocolError(-32602, "arguments must be an object")
    return name, arguments
