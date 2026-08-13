from __future__ import annotations

import asyncio
import functools
import http.server
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest


def test_stage3_workbench_complete_mock_flow():
    asyncio.run(_test_stage3_workbench_complete_mock_flow())


def test_stage3_visual_baseline_states():
    asyncio.run(_test_stage3_visual_baseline_states())


def test_stage3_result_empty_and_clipboard_fallback():
    asyncio.run(_test_stage3_result_empty_and_clipboard_fallback())


async def _test_stage3_result_empty_and_clipboard_fallback():
    playwright = pytest.importorskip("playwright.async_api")
    executable = _browser_executable()
    if not executable:
        pytest.skip("未找到可用 Chromium 浏览器")

    frontend = Path(__file__).parents[2] / "src" / "excel_agent" / "frontend"
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(frontend),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=executable)
            page = await browser.new_page()
            await _mock_v2_api(page)
            await page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
            await _prepare_task_dataset(page)
            assert await page.evaluate("localStorage.getItem('excelmind.theme')") is None

            await page.evaluate("fetch('/api/v2/test-empty')")
            await page.reload()
            await page.wait_for_selector(".analysis-result-card")
            assert "没有可展示的数据" in await page.locator(".analysis-result-card").inner_text()
            assert await page.locator(".analysis-result-card .chart-container").count() == 0

            await page.evaluate(
                "Object.defineProperty(navigator, 'clipboard', "
                "{value: undefined, configurable: true})"
            )
            await page.locator("[data-copy-analysis='analysis-empty']").click()
            await page.wait_for_selector("#toast-region .toast.success")
            assert "结果已复制" in await page.locator("#toast-region").inner_text()

            await page.evaluate("fetch('/api/v2/test-no-chart')")
            await page.reload()
            await page.wait_for_selector(".analysis-result-card")
            result = await page.locator(".analysis-result-card").last.inner_text()
            assert "结果表格" in result
            assert "2026-01" in result
            assert (
                await page.locator(".analysis-result-card").last.locator(".chart-container").count()
                == 0
            )
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


async def _test_stage3_visual_baseline_states():
    playwright = pytest.importorskip("playwright.async_api")
    executable = _browser_executable()
    if not executable:
        pytest.skip("未找到可用 Chromium 浏览器")

    frontend = Path(__file__).parents[2] / "src" / "excel_agent" / "frontend"
    baseline_dir = Path(__file__).parent / "visual_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(frontend),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=executable)
            page = await browser.new_page(viewport={"width": 1536, "height": 900})
            await _mock_v2_api(page)
            await page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
            await _capture_state(page, baseline_dir, "empty_task")

            await _prepare_task_dataset(page)
            await _capture_state(page, baseline_dir, "dataset_ready")

            await page.locator("#chat-input").fill("按月份统计销售额")
            await page.locator("#send-question").click()
            await page.wait_for_selector(".analysis-result-card")
            await _capture_state(page, baseline_dir, "analysis_completed")

            await page.evaluate("fetch('/api/v2/test-clarification')")
            await page.reload()
            await page.wait_for_selector(".clarification-card")
            await _capture_state(page, baseline_dir, "awaiting_clarification")
            await page.locator("[data-clarify-action='confirm']").press("Enter")
            await page.wait_for_selector("[data-rerun-analysis='analysis-2']")

            await page.evaluate("fetch('/api/v2/test-failed')")
            await page.reload()
            await page.wait_for_selector(".toast.error")
            await _capture_state(page, baseline_dir, "analysis_failed")

            await page.evaluate("fetch('/api/v2/test-running')")
            await page.reload()
            await page.wait_for_selector("#cancel-analysis:not(.hidden)")
            await page.keyboard.press("Escape")
            await page.wait_for_function(
                "document.querySelector('#analysis-stage')?.textContent.includes('取消')"
            )
            await _capture_state(page, baseline_dir, "analysis_cancelled")
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


async def _capture_state(page, output_dir, state_name):
    for width in (1536, 1200, 760):
        await page.set_viewport_size({"width": width, "height": 900})
        await page.screenshot(
            path=str(output_dir / f"{state_name}-{width}x900.png"),
            full_page=False,
        )


async def _prepare_task_dataset(page):
    await page.locator("#task-name").fill("阶段3验收任务")
    await page.locator("#create-task").click()
    await page.wait_for_selector("[data-task-id='task-1']")
    await page.locator("#dataset-upload").set_input_files(
        {
            "name": "sales.csv",
            "mimeType": "text/csv",
            "buffer": b"month,sales\n2026-01,10\n",
        }
    )
    await page.wait_for_selector("[data-create-dataset]", timeout=5000)
    await page.locator("[data-create-dataset]").click()
    await page.wait_for_selector("#chat-input:not([disabled])")


async def _test_stage3_workbench_complete_mock_flow():
    playwright = pytest.importorskip("playwright.async_api")
    executable = _browser_executable()
    if not executable:
        pytest.skip("未找到可用 Chromium 浏览器")

    frontend = Path(__file__).parents[2] / "src" / "excel_agent" / "frontend"
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(frontend),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                executable_path=executable,
            )
            page = await browser.new_page()
            await _mock_v2_api(page)
            await page.goto(f"http://127.0.0.1:{server.server_port}/index.html")

            await page.locator("#task-name").fill("阶段3验收任务")
            await page.locator("#create-task").click()
            await page.wait_for_selector("[data-task-id='task-1']")
            await page.locator("#dataset-upload").set_input_files(
                {
                    "name": "sales.csv",
                    "mimeType": "text/csv",
                    "buffer": b"month,sales\n2026-01,10\n",
                }
            )
            await page.wait_for_selector("[data-create-dataset]", timeout=5000)
            await page.locator("[data-create-dataset]").click()
            await page.wait_for_selector("#chat-input:not([disabled])")
            storage = await page.evaluate(
                "JSON.parse(sessionStorage.getItem('excelmind.v2.workbench'))"
            )
            assert set(storage) == {"task_id", "dataset_id"}

            await page.locator("#chat-input").fill("按月份统计销售额")
            await page.locator("#send-question").click()
            await page.wait_for_selector("text=销售额为 10")
            events = await page.evaluate(
                "fetch('/api/v2/test-events').then((response) => response.json())"
            )
            assert events["events"] == [
                "started",
                "semantic_resolving",
                "plan_validated",
                "query_executed",
                "evidence",
                "answer_delta",
                "answer",
                "chart",
                "done",
            ]
            assert await page.locator("#theme-toggle").count() == 0
            assert await page.locator(".analysis-result-card").count() == 1
            result_text = await page.locator(".analysis-result-card").inner_text()
            assert "核心结论" in result_text
            assert "结果表格" in result_text
            assert "数据集：sales.csv" in result_text
            assert await page.locator("[data-copy-analysis='analysis-1']").count() == 1
            assert await page.locator("[data-rerun-analysis='analysis-1']").count() == 1
            await page.locator("[data-copy-analysis='analysis-1']").click()
            await page.wait_for_selector("#toast-region .toast.success")
            assert "结果已复制" in await page.locator("#toast-region").inner_text()
            await page.locator("[data-rerun-analysis='analysis-1']").click()
            await page.wait_for_selector("[data-copy-analysis='analysis-2']")
            assert await page.locator(".analysis-result-card").count() >= 2
            assert await page.locator("[data-delete-analysis='analysis-1']").count() == 1
            assert await page.evaluate(
                "getComputedStyle(document.querySelector('.topbar')).backgroundColor"
            ) == "rgb(7, 26, 58)"
            assert await page.evaluate(
                "getComputedStyle(document.querySelector('.left-panel')).backgroundColor"
            ) == "rgb(255, 255, 255)"
            for width in (1536, 1200, 760):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.locator(".topbar").is_visible()
                assert await page.locator(".left-panel").is_visible()
                assert await page.locator(".center-panel").is_visible()
                assert await page.locator(".right-panel").is_visible()
            assert await page.locator(".message-content script").count() == 0
            assert await page.evaluate("window.__xss === undefined")
            await page.locator("[data-tab='evidence']").click()
            await page.wait_for_selector("#tab-evidence:not([hidden])")

            assert await page.locator("#tab-evidence .chart-container").count() == 1
            assert await page.locator("#tab-evidence script").count() == 0
            evidence_text = await page.locator("#tab-evidence").inner_text()
            assert "语义版本" in evidence_text
            await page.reload()
            await page.wait_for_selector("#chat-input:not([disabled])")
            await page.locator("[data-tab='evidence']").click()
            await page.wait_for_selector("#tab-evidence .chart-container")
            assert "语义版本" in await page.locator("#tab-evidence").inner_text()
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
            await page.locator("[data-delete-analysis='analysis-1']").click()
            await page.wait_for_timeout(200)
            assert await page.locator("[data-delete-analysis='analysis-1']").count() == 0
            await page.locator("#chat-input").fill("触发错误提示")
            await page.locator("#send-question").click()
            await page.wait_for_selector("#toast-region .toast-detail")
            assert "TASK_BUSY" in await page.locator("#toast-region").inner_text()
            await page.evaluate("fetch('/api/v2/test-running')")
            await page.reload()
            await page.wait_for_selector("#cancel-analysis:not(.hidden)")
            await page.locator("#cancel-analysis").click()
            await page.wait_for_selector("#analysis-stage")
            assert "取消" in await page.locator("#analysis-stage").inner_text()
            await page.locator("[data-tab='quality']").focus()
            await page.keyboard.press("ArrowRight")
            selected = await page.locator("[data-tab='semantics']").get_attribute("aria-selected")
            assert selected == "true"
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


def _browser_executable() -> str | None:
    import os

    configured = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
    if configured and Path(configured).exists():
        return configured
    for candidate in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(
            r"C:\Users\lenovo\AppData\Local\ms-playwright\chromium-1234"
            r"\chrome-win64\chrome.exe"
        ),
    ):
        if candidate.exists():
            return str(candidate)
    return None


async def _mock_v2_api(page):
    task = {
        "task_id": "task-1",
        "name": "阶段3验收任务",
        "status": "active",
        "dataset_ids": ["raw-1", "dataset-1"],
        "active_dataset_id": "dataset-1",
        "analysis_ids": [],
    }
    fields = [
        {
            "field_id": "dataset-1:field:0",
            "original_name": "month",
            "normalized_name": "month",
            "physical_type": "string",
            "null_ratio": 0,
            "unique_ratio": 1,
            "is_dimension_candidate": True,
        },
        {
            "field_id": "dataset-1:field:1",
            "original_name": "sales",
            "normalized_name": "sales",
            "physical_type": "decimal",
            "null_ratio": 0,
            "unique_ratio": 1,
            "is_metric_candidate": True,
        },
    ]
    bindings = [
        {
            "binding_id": "b1",
            "semantic_member_id": "sales_amount",
            "semantic_member_kind": "metric",
            "physical_field_id": "dataset-1:field:1",
            "status": "confirmed",
            "source": "exact_name",
        }
    ]
    dataset = {
        "dataset_id": "dataset-1",
        "task_id": "task-1",
        "kind": "normalized",
        "display_name": "sales.csv（规范化）",
        "status": "ready",
        "version": 1,
        "profile": {
            "row_count": 1,
            "column_count": 2,
            "schema": {"fields": fields},
            "warnings": [],
        },
        "physical_schema": {"fields": fields},
        "pending_decisions": [],
        "semantic_bindings": bindings,
    }
    evidence = {
        "analysis_id": "analysis-1",
        "task_id": "task-1",
        "dataset_id": "dataset-1",
        "dataset_version": 1,
        "question": "按月份统计销售额",
        "semantic_model_version": "v1",
        "semantic_resolution": {
            "metric_ids": ["sales_amount"],
            "dimension_ids": ["month"],
        },
        "binding_snapshot": bindings,
        "query_plan": {"intent": "trend"},
        "input_rows": 1,
        "output_rows": 1,
        "result": {
            "columns": ["month", "sales_amount"],
            "rows": [{"month": "2026-01", "sales_amount": 10}],
        },
        "warnings": [],
        "status": "completed",
    }
    analysis_snapshot = {
        "analysis_id": "analysis-1",
        "task_id": "task-1",
        "dataset_id": "dataset-1",
        "question": "按月份统计销售额",
        "status": "completed",
        "evidence": evidence,
        "answer": (
            "销售额为 10<script>window.__xss=1</script> "
            "[外链](javascript:alert(1))"
        ),
        "chart": {
            "chart_type": "bar",
            "title": "销售额趋势",
            "dimension": "month",
            "metrics": ["sales_amount"],
            "data": [{"month": "2026-01", "sales_amount": 10}],
        },
        "resources_settled": True,
    }
    created = False
    analysis_done = False
    error_mode = False
    running_mode = False
    cancelled_mode = False
    running_snapshot = {**analysis_snapshot, "status": "running", "resources_settled": False}
    cancelled_snapshot = {**analysis_snapshot, "status": "cancelled", "resources_settled": True}
    clarification_snapshot = {
        **analysis_snapshot,
        "analysis_id": "analysis-clarification",
        "status": "awaiting_clarification",
        "answer": "",
        "evidence": None,
        "chart": None,
        "clarification": {
            "analysis_id": "analysis-clarification",
            "clarification_id": "clarification-1",
            "draft_version": 1,
            "kind": "字段绑定确认",
            "summary": {
                "concept": "销售额",
                "field_name": "sales",
                "unit": "元",
                "time_grain": "月",
            },
        },
        "resources_settled": False,
    }
    failed_snapshot = {
        **analysis_snapshot,
        "analysis_id": "analysis-failed",
        "status": "failed",
        "answer": "",
        "chart": None,
        "error": {"code": "QUERY_EXECUTION_FAILED", "message": "查询执行失败"},
        "resources_settled": True,
    }
    empty_snapshot = deepcopy(analysis_snapshot)
    empty_snapshot["analysis_id"] = "analysis-empty"
    empty_snapshot["chart"] = None
    empty_snapshot["evidence"] = deepcopy(evidence)
    empty_snapshot["evidence"]["analysis_id"] = "analysis-empty"
    empty_snapshot["evidence"]["output_rows"] = 0
    empty_snapshot["evidence"]["result"] = {"columns": ["month", "sales_amount"], "rows": []}
    no_chart_snapshot = deepcopy(analysis_snapshot)
    no_chart_snapshot["analysis_id"] = "analysis-no-chart"
    no_chart_snapshot["chart"] = None
    no_chart_snapshot["evidence"] = deepcopy(evidence)
    no_chart_snapshot["evidence"]["analysis_id"] = "analysis-no-chart"
    no_chart_snapshot["evidence"]["warnings"] = ["结果使用了当前Dataset的全部可用行"]
    analysis_records = {}
    next_analysis_number = 1
    last_stream_events = []

    async def route(route):
        nonlocal analysis_done, created, error_mode, running_mode, cancelled_mode
        nonlocal next_analysis_number, last_stream_events
        request = route.request
        path = request.url.split("/api/v2", 1)[-1]
        method = request.method
        if path == "/health":
            return await route.fulfill(json={"status": "ok"})
        if path == "/test-running":
            running_mode = True
            cancelled_mode = False
            analysis_done = True
            error_mode = False
            analysis_records["analysis-1"] = running_snapshot
            return await route.fulfill(json={"status": "ok"})
        if path == "/test-clarification":
            running_mode = False
            cancelled_mode = False
            error_mode = False
            analysis_done = True
            analysis_records["analysis-clarification"] = clarification_snapshot
            return await route.fulfill(json={"status": "ok"})
        if path == "/test-failed":
            running_mode = False
            cancelled_mode = False
            error_mode = False
            analysis_done = True
            analysis_records["analysis-failed"] = failed_snapshot
            return await route.fulfill(json={"status": "ok"})
        if path == "/test-empty":
            running_mode = False
            cancelled_mode = False
            error_mode = False
            analysis_done = True
            analysis_records["analysis-empty"] = empty_snapshot
            return await route.fulfill(json={"status": "ok"})
        if path == "/test-no-chart":
            running_mode = False
            cancelled_mode = False
            error_mode = False
            analysis_done = True
            analysis_records["analysis-no-chart"] = no_chart_snapshot
            return await route.fulfill(json={"status": "ok"})
        if path == "/tasks" and method == "GET":
            tasks = [task] if created else []
            return await route.fulfill(json={"tasks": tasks, "max_tasks": 5})
        if path == "/tasks" and method == "POST":
            created = True
            return await route.fulfill(json=task)
        if path == "/tasks/task-1" and method == "GET":
            return await route.fulfill(json=task)
        if path.endswith("/uploads") and method == "POST":
            return await route.fulfill(
                json={
                    "upload_id": "upload-1",
                    "task_id": "task-1",
                    "display_filename": "sales.csv",
                    "format": "csv",
                    "size_bytes": 30,
                    "objects": [{"name": "sales.csv", "rows": 1, "columns": 2}],
                    "encoding_candidates": ["utf-8"],
                    "delimiter_candidates": [","],
                }
            )
        if path == "/tasks/task-1/datasets" and method == "GET":
            return await route.fulfill(json={"datasets": [dataset]})
        if path == "/tasks/task-1/datasets" and method == "POST":
            return await route.fulfill(json=dataset)
        if path == "/tasks/task-1/datasets/dataset-1" and method == "GET":
            return await route.fulfill(json=dataset)
        if path.endswith("/preview") and method == "GET":
            return await route.fulfill(
                json={
                    "dataset_id": "dataset-1",
                    "columns": ["month", "sales"],
                    "rows": [{"month": "2026-01", "sales": 10}],
                    "row_count": 1,
                }
            )
        if path.endswith("/profile") and method == "GET":
            return await route.fulfill(json=dataset["profile"])
        if path.startswith("/tasks/task-1/semantic-bindings"):
            return await route.fulfill(
                json={"bindings": bindings, "semantic_model_version": "v1"}
            )
        if path == "/semantic-model":
            return await route.fulfill(
                json={
                    "version": "v1",
                    "members": [
                        {"member_id": "sales_amount", "kind": "metric", "name": "销售额"},
                        {"member_id": "month", "kind": "dimension", "name": "月份"},
                    ],
                }
            )
        if path == "/tasks/task-1/semantic-metrics":
            return await route.fulfill(json={"global_metrics": [], "task_metrics": []})
        if path == "/tasks/task-1/analyses":
            if cancelled_mode and "analysis-1" in analysis_records:
                analysis_records["analysis-1"] = cancelled_snapshot
            elif running_mode:
                analysis_records["analysis-1"] = running_snapshot
            analyses = list(analysis_records.values()) if analysis_done else []
            return await route.fulfill(json={"analyses": analyses})
        if path == "/test-events":
            return await route.fulfill(json={"events": last_stream_events})
        if path.endswith("/knowledge/documents") and method == "GET":
            return await route.fulfill(json={"documents": []})
        if path.endswith("/chat/stream"):
            if error_mode:
                stream = (
                    'event: error\ndata: {"code":"TASK_BUSY",'
                    '"message":"当前任务已有分析正在执行"}\n\n'
                    'event: done\ndata: {"analysis_id":"analysis-1",'
                    '"status":"failed"}\n\n'
                )
                return await route.fulfill(
                    status=200,
                    content_type="text/event-stream",
                    body=stream,
                )
            analysis_id = f"analysis-{next_analysis_number}"
            next_analysis_number += 1
            last_stream_events = [
                "started",
                "semantic_resolving",
                "plan_validated",
                "query_executed",
                "evidence",
                "answer_delta",
                "answer",
                "chart",
                "done",
            ]
            stream_evidence = deepcopy(evidence)
            stream_evidence["analysis_id"] = analysis_id
            stream_evidence["question"] = "按月份统计销售额"
            stream_snapshot = deepcopy(analysis_snapshot)
            stream_snapshot["analysis_id"] = analysis_id
            stream_snapshot["evidence"] = stream_evidence
            analysis_records[analysis_id] = stream_snapshot
            analysis_done = True
            stream = "".join(
                [
                    f'event: started\ndata: {{"analysis_id":"{analysis_id}"}}\n\n',
                    f'event: semantic_resolving\ndata: {{"analysis_id":"{analysis_id}"}}\n\n',
                    'event: plan_validated\ndata: '
                    f'{{"analysis_id":"{analysis_id}","query_plan":{{}}}}\n\n',
                    'event: query_executed\ndata: '
                    f'{{"analysis_id":"{analysis_id}","result":{{"columns":'
                    '["month","sales_amount"],"rows":[{"month":"2026-01",'
                    '"sales_amount":10}]}}}\n\n',
                    f"event: evidence\ndata: {json.dumps(stream_evidence, ensure_ascii=False)}\n\n",
                    'event: answer_delta\ndata: '
                    f'{{"analysis_id":"{analysis_id}","sequence":0,"text":"销售额为 10"}}\n\n',
                    'event: answer_delta\ndata: '
                    f'{{"analysis_id":"{analysis_id}","sequence":0,"text":"重复分片"}}\n\n',
                    'event: answer_delta\ndata: '
                    f'{{"analysis_id":"{analysis_id}","sequence":-1,"text":"倒序分片"}}\n\n',
                    f'event: answer\ndata: {{"analysis_id":"{analysis_id}",'
                    '"answer":"销售额为 10<script>window.__xss=1</script> '
                    '[外链](javascript:alert(1))"}\n\n',
                    'event: chart\ndata: {"chart_type":"bar","title":"销售额趋势",'
                    '"dimension":"month","metrics":["sales_amount"],"data":'
                    '[{"month":"2026-01","sales_amount":10}],'
                    '"formatter":"javascript:alert(1)","url":"https://evil.example"}\n\n',
                    f'event: done\ndata: {{"analysis_id":"{analysis_id}",'
                    '"status":"completed"}\n\n',
                ]
            )
            return await route.fulfill(
                status=200,
                content_type="text/event-stream",
                body=stream,
            )
        if "/analyses/" in path:
            analysis_id = path.split("/analyses/", 1)[1].split("/", 1)[0]
            if method == "DELETE":
                if analysis_id in analysis_records:
                    analysis_records.pop(analysis_id)
                    error_mode = True
                    return await route.fulfill(
                        json={"analysis_id": analysis_id, "status": "deleted"}
                    )
                return await route.fulfill(
                    status=404,
                    json={"error": {"code": "ANALYSIS_NOT_FOUND", "message": "分析不存在"}},
                )
            if path.endswith("/cancel") and method == "POST":
                cancelled = deepcopy(analysis_records.get(analysis_id, cancelled_snapshot))
                cancelled["status"] = "cancelled"
                cancelled["resources_settled"] = True
                analysis_records[analysis_id] = cancelled
                running_mode = False
                cancelled_mode = True
                return await route.fulfill(json=cancelled)
            if analysis_id in analysis_records:
                return await route.fulfill(json=analysis_records[analysis_id])
        return await route.fulfill(
            status=404,
            json={"error": {"code": "NOT_MOCKED", "message": path, "details": {}}},
        )

    await page.route("**/api/v2/**", route)
