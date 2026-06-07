"""Local document intake helpers with lightweight default dependencies."""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def _safe_path(raw: str) -> Path:
    value = raw.strip()
    if not value or "\0" in value or re.search(r"[;&|`$<>]", value):
        raise ValueError("unsafe path")
    path = Path(value)
    if any(part in ("..", "") for part in path.parts):
        raise ValueError("path traversal is not allowed")
    return path


def _xml_text(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    parts: list[str] = []
    for elem in root.iter():
        if elem.text and elem.text.strip():
            parts.append(elem.text.strip())
    return "\n".join(parts)


def docx_text(path: Path, *, max_chars: int = 20000) -> dict:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValueError("not a docx document")
        text = _xml_text(zf.read("word/document.xml"))
    return {"path": str(path), "text": text[:max_chars], "truncated": len(text) > max_chars}


def xlsx_sheets(path: Path, *, max_chars: int = 20000) -> dict:
    with zipfile.ZipFile(path) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheets = [
            sheet.attrib.get("name", "")
            for sheet in workbook.findall(".//main:sheet", ns)
            if sheet.attrib.get("name")
        ]
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared_text = _xml_text(zf.read("xl/sharedStrings.xml"))
            shared = shared_text.splitlines()[:200]
    preview = "\n".join(shared)[:max_chars]
    return {"path": str(path), "sheets": sheets, "shared_strings_preview": preview}


def pdf_text(path: Path, *, max_chars: int = 20000) -> dict:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PDF extraction requires optional dependency: pypdf") from exc
    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return {
        "path": str(path),
        "pages": len(reader.pages),
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def ocr_image(path: Path, *, max_chars: int = 20000) -> dict:
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("OCR requires optional dependencies: pillow and pytesseract") from exc
    text = pytesseract.image_to_string(Image.open(path))
    return {"path": str(path), "text": text[:max_chars], "truncated": len(text) > max_chars}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kyagent-document-intake")
    parser.add_argument("kind", choices=["docx", "xlsx", "pdf", "ocr"])
    parser.add_argument("path")
    args = parser.parse_args(argv)
    try:
        path = _safe_path(args.path)
        if args.kind == "docx":
            out = docx_text(path)
        elif args.kind == "xlsx":
            out = xlsx_sheets(path)
        elif args.kind == "pdf":
            out = pdf_text(path)
        else:
            out = ocr_image(path)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
