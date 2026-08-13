from pathlib import Path

from fastapi.testclient import TestClient

from excel_agent.api import app


def test_frontend_uses_local_assets_and_vendor_serves_offline():
    frontend = Path(__file__).parents[2] / "src" / "excel_agent" / "frontend" / "index.html"
    html = frontend.read_text(encoding="utf-8")
    assert "https://cdn.jsdelivr.net" not in html
    assert "fonts.googleapis.com" not in html
    assert "vendor/marked.umd.min.js" in html
    assert "vendor/echarts.min.js" in html
    assert "js/app.js" in html
    assert "styles/base.css" in html
    assert "https://" not in html
    assert 'id="theme-toggle"' not in html
    assert "data-theme" not in html

    app_js = (frontend.parent / "js" / "app.js").read_text(encoding="utf-8")
    assert "excelmind.theme" not in app_js

    with TestClient(app) as client:
        assert client.get("/vendor/marked.umd.min.js").status_code == 200
        assert client.get("/vendor/echarts.min.js").status_code == 200
        assert client.get("/js/app.js").status_code == 200
        assert client.get("/styles/base.css").status_code == 200


def test_stage1_workbench_is_available_and_uses_v2_api():
    with TestClient(app) as client:
        response = client.get("/v2")
    assert response.status_code == 200
    assert "ExcelMind v2" in response.text
    assert "/api/v2/tasks" in response.text


def test_current_workbench_branding_uses_smart_query_title():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "智能问数助手" in response.text
    assert "SmartQuery · 可信上传式分析工作台" in response.text
    assert "ExcelMind v2" not in response.text
