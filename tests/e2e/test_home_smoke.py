from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


def test_homepage_loads_local_markdown_and_chart_assets():
    asyncio.run(_test_homepage_loads_local_markdown_and_chart_assets())


async def _test_homepage_loads_local_markdown_and_chart_assets():
    playwright = pytest.importorskip("playwright.async_api")
    executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
    if executable and not Path(executable).exists():
        executable = None
    if not executable:
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
        executable = next((str(path) for path in candidates if path.exists()), None)
    if not executable:
        pytest.skip("未找到可用 Chromium 浏览器")

    frontend = Path(__file__).parents[2] / "src" / "excel_agent" / "frontend" / "index.html"
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path=executable)
        page = await browser.new_page()
        await page.goto(frontend.as_uri())
        assert await page.title()
        marked_loaded = "typeof marked !== 'undefined' && typeof marked.parse === 'function'"
        assert await page.evaluate(marked_loaded)
        assert await page.evaluate("typeof echarts !== 'undefined'")
        await browser.close()
