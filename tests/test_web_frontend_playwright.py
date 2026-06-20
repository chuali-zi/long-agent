"""Browser-level smoke tests for the static Web console.

These tests intentionally exercise the shipped ``index.html`` in a real browser
without starting a backend process. API calls are intercepted in Playwright so
the test stays deterministic and does not depend on LLM credentials.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import Route, sync_playwright  # noqa: E402


ROOT = Path(__file__).parent.parent
INDEX_HTML = ROOT / "kyagent" / "web" / "static" / "index.html"
APP_URL = "http://kyagent.test/"


def _json_response(route: Route, payload: object) -> None:
    route.fulfill(
        status=200,
        headers={
            "access-control-allow-origin": "*",
            "content-type": "application/json; charset=utf-8",
        },
        body=json.dumps(payload, ensure_ascii=False),
    )


@pytest.fixture()
def chromium_page():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright Chromium is not installed: {exc}")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        try:
            yield page
        finally:
            browser.close()


def test_static_frontend_renders_and_calls_registered_apis(chromium_page):
    page = chromium_page
    calls: list[tuple[str, str, str | None]] = []

    def handle_request(route: Route) -> None:
        request = route.request
        if request.url == APP_URL:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=INDEX_HTML.read_text(encoding="utf-8"),
            )
            return
        calls.append((request.method, request.url, request.post_data))
        url = request.url
        if url.endswith("/api/health"):
            _json_response(route, {"status": "ok", "version": "0.1.0-test"})
        elif url.endswith("/api/tools"):
            _json_response(
                route,
                {
                    "count": 1,
                    "tools": [
                        {
                            "name": "disk_usage",
                            "description": "show disk usage",
                            "risk": "low",
                            "requires_root": False,
                            "read_only": True,
                            "input_schema": {},
                        }
                    ],
                },
            )
        elif "/api/safety/check" in url:
            _json_response(
                route,
                {
                    "intent": None,
                    "argv": {
                        "layer": "argv",
                        "risk": "critical",
                        "decision": "deny",
                        "hits": [
                            {"rule_id": "danger-rm-root", "risk": "critical", "matched": "rm -rf /"}
                        ],
                        "rationale": ["dangerous recursive delete"],
                    },
                    "final_risk": None,
                    "final_decision": None,
                },
            )
        elif "/api/audit/traces?limit=20" in url:
            _json_response(
                route,
                {
                    "count": 1,
                    "traces": [
                        {
                            "trace_id": "trace-browser",
                            "user": "web",
                            "started_at": 1,
                            "channel": "mock",
                        }
                    ],
                },
            )
        elif "/api/audit/traces/trace-browser" in url:
            _json_response(
                route,
                {
                    "trace_id": "trace-browser",
                    "events": [{"seq": 1, "kind": "final", "payload": {"ok": True}}],
                },
            )
        elif "/api/sessions/" in url and url.endswith("/reset"):
            _json_response(route, {"reset": True})
        else:
            route.fulfill(status=404, body="unexpected api route")

    page.route("**/*", handle_request)
    page.goto(APP_URL)

    page.locator("#askInput").wait_for(state="visible", timeout=5000)
    assert page.locator(".telemetry-orb").is_visible()
    assert page.locator("#bannerText").inner_text(timeout=5000) == "v0.1.0-test"

    page.get_by_role("button", name="工具").click()
    page.locator("#toolTable").get_by_text("disk_usage").wait_for(timeout=5000)

    page.get_by_role("button", name="安全").click()
    page.locator("#safetyInput").fill("rm -rf /")
    page.get_by_role("button", name="校验").click()
    page.locator("#safetyResult").get_by_text("danger-rm-root").wait_for(timeout=5000)

    page.get_by_role("button", name="审计").click()
    page.locator("#traceTable").get_by_text("trace-browser").wait_for(timeout=5000)
    page.locator("#traceTable a[data-id='trace-browser']").click()
    page.locator("#traceDetail").get_by_text("trace trace-browser").wait_for(timeout=5000)

    page.get_by_role("button", name="控制台").click()
    page.get_by_role("button", name="清空会话").click()
    page.locator("#chatEmpty").wait_for(timeout=5000)

    observed = {(method, url.split("/api/", 1)[-1].split("?", 1)[0]) for method, url, _ in calls}
    assert ("GET", "health") in observed
    assert ("GET", "tools") in observed
    assert ("POST", "safety/check") in observed
    assert ("GET", "audit/traces") in observed
    assert ("GET", "audit/traces/trace-browser") in observed
    assert any(
        method == "POST" and url_part.startswith("sessions/") and url_part.endswith("/reset")
        for method, url_part in observed
    )


def test_static_frontend_handles_sse_approval_choice_and_final(chromium_page):
    page = chromium_page
    calls: list[tuple[str, str, str | None]] = []

    def handle_request(route: Route) -> None:
        request = route.request
        if request.url == APP_URL:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=INDEX_HTML.read_text(encoding="utf-8"),
            )
            return
        calls.append((request.method, request.url, request.post_data))
        url = request.url
        if url.endswith("/api/health"):
            _json_response(route, {"status": "ok", "version": "0.1.0-test"})
        elif url.endswith("/api/ask/stream"):
            route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                body=(
                    'event: progress\ndata: {"kind":"thinking_delta","delta":"检查中"}\n\n'
                    'event: progress\n'
                    'data: {"kind":"tool_call_start","tool":"disk_usage","argv":{"path":"/var/log"}}\n\n'
                    'event: approval_required\n'
                    'data: {"approval_id":"apr-browser","title":"删除日志","risk":"high","body":"rm file"}\n\n'
                    'event: choice_required\n'
                    'data: {"choice_id":"choice-browser","question":"继续吗",'
                    '"options":[{"value":"yes","label":"继续"}]}\n\n'
                    'event: final\ndata: {"trace_id":"trace-final","text":"完成","denied":false}\n\n'
                ),
            )
        elif url.endswith("/api/approvals/apr-browser/approve"):
            _json_response(route, {"approval_id": "apr-browser", "status": "approved"})
        elif url.endswith("/api/choices/choice-browser/select"):
            _json_response(route, {"choice_id": "choice-browser", "status": "selected", "value": "yes"})
        else:
            route.fulfill(status=404, body="unexpected api route")

    page.route("**/*", handle_request)
    page.goto(APP_URL)

    page.locator("#askInput").fill("检查日志")
    page.locator("#sendBtn").click()
    page.locator("#approval-apr-browser").wait_for(timeout=5000)
    page.locator("#choice-choice-browser").wait_for(timeout=5000)
    page.locator(".msg.agent").get_by_text("完成").wait_for(timeout=5000)

    page.locator("#approval-apr-browser button[data-decision='approve']").click()
    page.locator("#approval-apr-browser").get_by_text("approved").wait_for(timeout=5000)
    page.locator("#choice-choice-browser button[data-value='yes']").click()
    page.locator("#choice-choice-browser").get_by_text("yes").wait_for(timeout=5000)

    ask_payload = next(
        post_data for method, url, post_data in calls if method == "POST" and url.endswith("/api/ask/stream")
    )
    assert ask_payload is not None
    assert json.loads(ask_payload)["text"] == "检查日志"
    assert any(method == "POST" and url.endswith("/api/approvals/apr-browser/approve") for method, url, _ in calls)
    assert any(method == "POST" and url.endswith("/api/choices/choice-browser/select") for method, url, _ in calls)


def test_progress_updates_single_status_column_without_tool_log_rows(chromium_page):
    page = chromium_page

    def handle_request(route: Route) -> None:
        request = route.request
        if request.url == APP_URL:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=INDEX_HTML.read_text(encoding="utf-8"),
            )
            return
        if request.url.endswith("/api/health"):
            _json_response(route, {"status": "ok", "version": "0.1.0-test"})
            return
        route.fulfill(status=404, body="unexpected api route")

    page.route("**/*", handle_request)
    page.goto(APP_URL)
    page.locator("#askInput").wait_for(state="visible", timeout=5000)

    page.evaluate(
        """() => {
          clearChatEmpty();
          handleProgress({kind: 'thinking_delta', delta: '先确认服务和磁盘水位'});
          handleProgress({kind: 'tool_call_start', tool: 'disk_usage', argv: {path: '/var/log'}});
          handleProgress({kind: 'tool_call_end', tool: 'disk_usage', meta: {ok: true}, text: '24%'});
          handleProgress({kind: 'tool_call_start', tool: 'service_status', argv: {name: 'nginx'}});
        }"""
    )

    detail = page.locator("#agentStatusDetail")
    detail.get_by_text("service_status").wait_for(timeout=5000)
    assert "先确认服务和磁盘水位" in detail.inner_text()
    assert "tools 2" in page.locator("#eventMetrics").inner_text()
    assert page.locator(".msg.tool").count() == 0
    assert page.locator(".msg.event").count() == 0


def test_backend_todos_render_as_structured_status_list_without_local_drift(chromium_page):
    page = chromium_page

    def handle_request(route: Route) -> None:
        request = route.request
        if request.url == APP_URL:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=INDEX_HTML.read_text(encoding="utf-8"),
            )
            return
        if request.url.endswith("/api/health"):
            _json_response(route, {"status": "ok", "version": "0.1.0-test"})
            return
        route.fulfill(status=404, body="unexpected api route")

    page.route("**/*", handle_request)
    page.goto(APP_URL)
    page.locator("#askInput").wait_for(state="visible", timeout=5000)

    page.evaluate(
        """() => {
          handleProgress({kind: 'plan_snapshot', meta: {plan: {
            plan_id: 'plan-browser',
            todos: [
              {todo_id: 'todo-1', content: '整理最终结论', status: 'pending', priority: 'medium'},
              {todo_id: 'todo-2', content: '查看 CPU 进程', status: 'in_progress', priority: 'high'},
              {todo_id: 'todo-3', content: '读取系统负载', status: 'completed', priority: 'medium'}
            ]
          }}});
          handleProgress({kind: 'thinking_delta', delta: 'TODO 1: 不应该覆盖后端 todo'});
          handleProgress({kind: 'tool_call_end', tool: 'process_list', meta: {ok: true}, text: 'ok'});
        }"""
    )

    todos = page.locator('#planDock [data-component="todos"] [data-slot="item"]')
    assert todos.count() == 3
    assert page.locator("#planCount").inner_text() == "1/3"
    assert todos.nth(0).get_attribute("data-status") == "in_progress"
    assert todos.nth(0).inner_text() == "查看 CPU 进程"
    assert todos.nth(1).get_attribute("data-status") == "pending"
    assert todos.nth(1).inner_text() == "整理最终结论"
    assert todos.nth(2).get_attribute("data-status") == "completed"
    assert todos.nth(2).inner_text() == "读取系统负载"
    assert page.locator('#planDock [data-component="todos"]').get_by_text("不应该覆盖后端 todo").count() == 0


def test_agent_final_markdown_renders_safely_without_new_runtime(chromium_page):
    page = chromium_page

    def handle_request(route: Route) -> None:
        request = route.request
        if request.url == APP_URL:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=INDEX_HTML.read_text(encoding="utf-8"),
            )
            return
        url = request.url
        if url.endswith("/api/health"):
            _json_response(route, {"status": "ok", "version": "0.1.0-test"})
        elif url.endswith("/api/ask/stream"):
            route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                body=(
                    "event: final\n"
                    "data: {\"trace_id\":\"trace-md\",\"denied\":false,"
                    "\"text\":\"# 巡检结论\\n\\n- **nginx**: 正常\\n- `disk_usage`: 24%\\n\\n"
                    "<script>window.__kyagent_xss = true</script>\"}\n\n"
                ),
            )
        else:
            route.fulfill(status=404, body="unexpected api route")

    page.route("**/*", handle_request)
    page.goto(APP_URL)
    page.locator("#askInput").fill("输出 markdown")
    page.locator("#sendBtn").click()

    body = page.locator(".msg.agent .body")
    body.locator("h1").get_by_text("巡检结论").wait_for(timeout=5000)
    assert body.locator("li").count() == 2
    assert body.locator("strong").get_by_text("nginx").is_visible()
    assert body.locator("code").get_by_text("disk_usage").is_visible()
    assert page.locator(".msg.agent script").count() == 0
    assert page.evaluate("window.__kyagent_xss") is None
