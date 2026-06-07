"""Local document/OCR intake MCP tools."""
from __future__ import annotations

import re

from kyagent.mcp.tools.base import Tool, ToolError, ToolRegistry
from kyagent.safety.patterns import RiskLevel


_PATH = re.compile(r"^[A-Za-z0-9_./:\\ -]{1,500}$")


def _safe_path(path: str) -> str:
    value = path.strip()
    if not value or not _PATH.fullmatch(value) or re.search(r"[;&|`$<>]", value):
        raise ToolError("unsafe document path")
    normalized = value.replace("\\", "/")
    if any(part in ("..", "") for part in normalized.split("/")):
        raise ToolError("path traversal is not allowed")
    return value


class _DocTool(Tool):
    risk_level = RiskLevel.LOW
    requires_root = False
    read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    kind = ""

    def build_argv(self, args: dict) -> list[str]:
        return ["python", "-m", "kyagent.document_intake", self.kind, _safe_path(args["path"])]


class DocxExtractTextTool(_DocTool):
    name = "docx_extract_text"
    description = "Extract text from a local .docx file using lightweight local parsing."
    kind = "docx"


class XlsxListSheetsTool(_DocTool):
    name = "xlsx_list_sheets"
    description = "List sheets and shared string preview from a local .xlsx file."
    kind = "xlsx"


class PdfExtractTextTool(_DocTool):
    name = "pdf_extract_text"
    description = "Extract text from a local PDF when optional pypdf backend is installed."
    kind = "pdf"


class OcrImageTextTool(_DocTool):
    name = "ocr_image_text"
    description = "OCR a local image when optional pillow+pytesseract backend is installed."
    kind = "ocr"


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(DocxExtractTextTool())
    registry.register(XlsxListSheetsTool())
    registry.register(PdfExtractTextTool())
    registry.register(OcrImageTextTool())
    return registry
