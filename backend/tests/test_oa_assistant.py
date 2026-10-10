"""OA 浏览器代填（书签面板）后端测试。

只覆盖两个公开端点：`/panel.js` 下发与 `/report` 上报。面板逻辑在浏览器里跑，
这里不引入浏览器依赖。
"""

from __future__ import annotations

import asyncio
import io
import json

from fastapi import UploadFile
from starlette.requests import Request

from app.api import oa_assistant


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


def _request(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_panel_script_bakes_config() -> None:
    response = oa_assistant.panel_script()
    assert response.status_code == 200
    body = response.body.decode("utf-8")
    # 配置内嵌 → 面板不需要任何跨域请求
    assert "window.__SD_OA_CONFIG__" in body
    # 真实的字段定位器随配置下发
    assert "data-fieldmark" in body
    # 面板本体（UI/引擎）也在里面
    assert "__SD_OA_PANEL__" in body
    assert "no-store" in response.headers["cache-control"]


def test_panel_script_includes_panel_defaults() -> None:
    config = oa_assistant._load_config()
    body = oa_assistant.panel_script().body.decode("utf-8")
    assert json.dumps(config["panel"], ensure_ascii=False) in body


def test_report_accepts_json_and_garbage() -> None:
    assert asyncio.run(oa_assistant.report(_request(b'{"kind":"run"}')))["ok"] is True
    # 非 JSON 不能把上报链路打挂
    assert asyncio.run(oa_assistant.report(_request(b"not-json")))["ok"] is True
    assert asyncio.run(oa_assistant.report(_request(b"")))["ok"] is True


def _invoice_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_invoice_amounts_empty() -> None:
    response = asyncio.run(oa_assistant.invoice_amounts(files=[]))
    assert response.status_code == 200
    assert _invoice_body(response)["total"] == "0.00"


def test_invoice_amounts_rejects_non_pdf_extension() -> None:
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.txt", b"%PDF-1.4")]))
    assert response.status_code == 400
    assert "PDF" in _invoice_body(response)["detail"]


def test_invoice_amounts_rejects_bad_header() -> None:
    # 扩展名对但内容不是 PDF：不信客户端，只认 %PDF- 文件头
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.pdf", b"definitely not pdf")]))
    assert response.status_code == 400
    assert "%PDF-" in _invoice_body(response)["detail"]


def test_invoice_amounts_happy_path_sets_cors(monkeypatch) -> None:
    captured = {}

    def fake(uploads):
        captured["uploads"] = uploads
        return {"items": [{"filename": "a.pdf", "amount": "12.00", "error": None}], "total": "12.00"}

    monkeypatch.setattr(oa_assistant, "sum_invoice_amounts_from_uploads", fake)
    response = asyncio.run(oa_assistant.invoice_amounts(files=[_upload("a.pdf", b"%PDF-1.4 stub")]))
    assert response.status_code == 200
    assert _invoice_body(response)["total"] == "12.00"
    assert captured["uploads"] == [("a.pdf", b"%PDF-1.4 stub")]
    # OA 页面跨域调用需要 CORS 头
    assert response.headers["access-control-allow-origin"] == "*"