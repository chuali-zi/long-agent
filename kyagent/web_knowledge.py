"""Allowlisted external knowledge retrieval helpers.

The CLI entry points here are intentionally narrow and are invoked by MCP
tools through ``python -m kyagent.web_knowledge ...``. They provide factual
lookup capability without turning the agent into a general network client.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

import httpx


DEFAULT_ALLOWED_DOMAINS = {
    "docs.python.org",
    "docs.github.com",
    "api.github.com",
    "github.com",
    "pypi.org",
    "api.osv.dev",
    "osv.dev",
    "nvd.nist.gov",
    "cve.mitre.org",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def allowed_domains() -> set[str]:
    raw = os.environ.get("KYAGENT_WEB_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED_DOMAINS)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _domain_allowed(host: str, domains: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def validate_url(url: str, *, domains: set[str] | None = None) -> str:
    domains = domains or allowed_domains()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("only https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    host = parsed.hostname.lower()
    try:
        ipaddress.ip_address(host)
        raise ValueError("IP literal URLs are not allowed")
    except ValueError as exc:
        if str(exc) == "IP literal URLs are not allowed":
            raise
    if host in {"localhost"} or host.endswith(".localhost"):
        raise ValueError("localhost URLs are not allowed")
    if not _domain_allowed(host, domains):
        raise ValueError(f"domain is not allowlisted: {host}")
    return url


def _check_resolved_ips(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"resolved IP is not public: {ip}")


def fetch_url(url: str, *, max_bytes: int = 200000) -> dict:
    url = validate_url(url)
    host = urlparse(url).hostname or ""
    _check_resolved_ips(host)
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        resp = client.get(url)
    ctype = resp.headers.get("content-type", "")
    if resp.is_redirect:
        raise ValueError("redirects are disabled for web_fetch_url")
    body = resp.content[:max_bytes]
    text = body.decode(resp.encoding or "utf-8", errors="replace")
    if "html" in ctype.lower():
        parser = _TextExtractor()
        parser.feed(text)
        text = parser.text()
    return {
        "url": url,
        "status_code": resp.status_code,
        "content_type": ctype,
        "truncated": len(resp.content) > max_bytes,
        "text": text[:max_bytes],
    }


def osv_query(package: str, ecosystem: str) -> dict:
    payload = {"package": {"name": package, "ecosystem": ecosystem}}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post("https://api.osv.dev/v1/query", json=payload)
    return {"status_code": resp.status_code, "result": resp.json()}


def github_issue_search(query: str, *, limit: int = 5) -> dict:
    params = urlencode({"q": query, "per_page": max(1, min(limit, 10))})
    url = f"https://api.github.com/search/issues?{params}"
    result = fetch_url(url, max_bytes=200000)
    try:
        data = json.loads(result["text"])
    except json.JSONDecodeError:
        data = {"raw": result["text"]}
    return {"url": url, "status_code": result["status_code"], "result": data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kyagent-web-knowledge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("url")
    p_osv = sub.add_parser("osv-query")
    p_osv.add_argument("ecosystem")
    p_osv.add_argument("package")
    p_gh = sub.add_parser("github-issues")
    p_gh.add_argument("query")
    p_gh.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "fetch":
            out = fetch_url(args.url)
        elif args.cmd == "osv-query":
            out = osv_query(args.package, args.ecosystem)
        else:
            out = github_issue_search(args.query, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
