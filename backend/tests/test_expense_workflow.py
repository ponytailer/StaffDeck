"""报销流程助手 API 测试（直调端点函数；浏览器操作全部 mock，不真开浏览器）。

auth 依赖由 FastAPI 在真实装配时注入，这里单测只关心业务分支。
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile

import app.api.expense_workflow as api_mod
import app.core.invoice_amount as invoice_amount
import app.core.oa_browser as oa
from app.api.expense_workflow import (
    ExpenseRunRequest,
    InspectRequest,
    SumAmountsRequest,
    get_expense_config,
    inspect_expense_page,
    run_expense,
    sum_amounts,
    upload_expense_attachment,
)

MINIMAL_CONFIG = {
    "version": 1,
    "browser": {
        "headless": True,
        "chrome_path": "/does/not/matter",
        "user_data_dir": "x",
        "login_check": {"url_contains": ["login"]},
    },
    "save_button": {"label": "保存", "selector": "#save"},
    "scenarios": [
        {
            "id": "communication",
            "name": "通讯费用",
            "enabled": True,
            "url": "https://oa.example.com/form",
            "reason_prefill": "{last_month}月通讯费报销",
            "fields": {
                "scene": {"label": "报销场景", "selector": "#scene", "mode": "input", "value": "通讯费用"},
                "amount": {"label": "报销金额", "selector": "#amount", "mode": "input", "from": "amount"},
            },
        }
    ],
}


@pytest.fixture(name="config_file")
def fixture_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置指到临时文件，避免污染真实 config/expense_workflow.json。"""
    path = tmp_path / "expense_workflow.json"
    path.write_text(json.dumps(MINIMAL_CONFIG, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(oa, "_CONFIG_PATH", path)
    return path


LIBRARY_CONFIG = {
    "version": 2,
    "browser": {
        "headless": True,
        "chrome_path": "",
        "user_data_dir": "x",
        "login_check": {"url_contains": ["login"]},
    },
    "save_button": {"label": "保存", "selector": "#save"},
    "field_library": {
        "expense_type": {"label": "报销类型", "selector": "div[data-fieldmark=\"f1\"]", "mode": "select", "value": "公务限额费用"},
        "scene": {"label": "报销场景", "selector": "div[data-fieldmark=\"f2\"]", "mode": "select"},
        "amount": {"label": "报销金额", "selector": "#amount", "mode": "input", "from": "amount"},
    },
    "scenarios": [
        {
            "id": "communication",
            "name": "通讯费用",
            "enabled": True,
            "url": "https://oa.example.com/form",
            "reason_prefill": "{last_month}月通讯费报销",
            "fields": ["expense_type", "scene", "amount"],
            "field_overrides": {"scene": {"value": "通讯费用"}},
        }
    ],
}


@pytest.fixture(name="library_config_file")
def fixture_library_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "expense_workflow.json"
    path.write_text(json.dumps(LIBRARY_CONFIG, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(oa, "_CONFIG_PATH", path)
    return path


def test_config_endpoint_resolves_field_library(library_config_file):
    """fields 数组引用公共字段库时，下发给前端的 fields 仍按 label/mode 展开，且不漏定位器。"""
    data = get_expense_config()
    fields = data["scenarios"][0]["fields"]
    assert [f["key"] for f in fields] == ["expense_type", "scene", "amount"]
    by_key = {f["key"]: f for f in fields}
    assert by_key["expense_type"]["label"] == "报销类型"
    assert by_key["expense_type"]["mode"] == "select"
    assert by_key["amount"]["mode"] == "input"
    assert all("selector" not in f for f in fields)


def test_scenario_fields_resolves_library_with_override(library_config_file):
    """oa_browser 侧：库定义 + 场景覆盖合并；未知 key 显式转成待配置。"""
    oa._STATE.config = json.loads(oa._CONFIG_PATH.read_text(encoding="utf-8"))
    scenario = LIBRARY_CONFIG["scenarios"][0]
    resolved = dict(oa._scenario_fields(scenario))
    # 覆盖生效：scene 的 value 来自 field_overrides
    assert resolved["scene"]["value"] == "通讯费用"
    # 其余字段沿用库定义
    assert resolved["expense_type"]["value"] == "公务限额费用"
    assert resolved["amount"]["from"] == "amount"
    # 未知 key → 待配置定位器
    scenario_unknown = {**scenario, "fields": ["expense_type", "nope"]}
    resolved_unknown = dict(oa._scenario_fields(scenario_unknown))
    assert resolved_unknown["nope"]["selector"] == oa.TODO_SELECTOR


def test_config_endpoint_returns_scenarios_without_selectors(config_file):
    data = get_expense_config()
    assert data["scenarios"][0]["id"] == "communication"
    assert data["scenarios"][0]["reason_prefill"] == "{last_month}月通讯费报销"
    # 下发的场景信息里不应包含 OA 内部定位器/URL
    assert "url" not in data["scenarios"][0]
    assert all("selector" not in f for f in data["scenarios"][0]["fields"])


def test_run_rejects_bad_amount(config_file):
    req = ExpenseRunRequest(scenario_id="communication", amount="abc", reason="x", all_e_ticket=False)
    with pytest.raises(HTTPException) as exc_info:
        run_expense(req)
    assert "金额" in exc_info.value.detail


def test_run_requires_attachment_when_all_e_ticket(config_file):
    req = ExpenseRunRequest(scenario_id="communication", amount="100.00", reason="x", all_e_ticket=True)
    with pytest.raises(HTTPException) as exc_info:
        run_expense(req)
    assert "附件" in exc_info.value.detail


def test_run_need_login_branch(config_file):
    """登录页判定命中时原样返回 need_login（不清空表单的语义由前端保证：不重置 state）。"""

    def fake_run(scenario, values):
        assert values["amount"] == "100.00"
        assert values["reason"] == "7月通讯费报销"
        assert values["invoice_type"] == "普通发票"  # 默认发票类型透传给 OA 填充
        return {
            "status": "need_login",
            "message": "已打开浏览器，但当前是 OA 登录页。",
            "steps": [{"field": "导航", "status": "filled", "detail": "…"}],
        }

    with patch.object(oa, "run_expense_scenario_sync", side_effect=fake_run), patch.object(
        oa, "load_expense_config", return_value=MINIMAL_CONFIG
    ):
        req = ExpenseRunRequest(
            scenario_id="communication", amount="100.00", reason="7月通讯费报销", all_e_ticket=False
        )
        data = run_expense(req)
    assert data["status"] == "need_login"
    assert data["steps"][0]["field"] == "导航"


def test_run_unknown_scenario_404(config_file):
    req = ExpenseRunRequest(scenario_id="nope", amount="1.00", reason="x", all_e_ticket=False)
    with pytest.raises(HTTPException) as exc_info:
        run_expense(req)
    assert exc_info.value.status_code == 404


def test_inspect_returns_page_fields(config_file):
    """inspect 端点把浏览器线程返回的字段/按钮原样透传。"""

    def fake_inspect(scenario):
        assert scenario["id"] == "communication"
        return {
            "status": "done",
            "message": "ok",
            "url": "https://oa.example.com/form",
            "fields": [{"tag": "input", "type": "text", "selector": "#amount", "label": "报销金额", "placeholder": "", "value": "", "readonly": False}],
            "buttons": [{"text": "保存", "selector": "#save"}],
            "steps": [],
        }

    with patch.object(api_mod, "inspect_in_browser_thread", side_effect=fake_inspect), patch.object(
        oa, "load_expense_config", return_value=MINIMAL_CONFIG
    ):
        data = inspect_expense_page(InspectRequest(scenario_id="communication"))
    assert data["status"] == "done"
    assert data["fields"][0]["selector"] == "#amount"
    assert data["buttons"][0]["text"] == "保存"


def test_inspect_unknown_scenario_404(config_file):
    with pytest.raises(HTTPException) as exc_info:
        inspect_expense_page(InspectRequest(scenario_id="nope"))
    assert exc_info.value.status_code == 404


def test_browser_status_includes_mode():
    status = oa.browser_status()
    assert "open" in status and "url" in status and "mode" in status


def test_find_chrome_binary_explicit_path_wins(tmp_path):
    """chrome_path 显式配置且存在时优先使用，不做自动探测。"""
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert oa._find_chrome_binary(str(fake)) == fake


def test_find_chrome_binary_returns_none_when_nothing_found(monkeypatch):
    """显式路径不存在且探测候选/PATH/playwright 自带内核全落空时返回 None。"""
    fake_pw = type("FakePw", (), {})()
    fake_pw.chromium = type("FakeChromium", (), {"executable_path": "/does/not/exist/chromium"})()
    monkeypatch.setattr(oa, "_STATE", oa._BrowserState(pw=fake_pw))
    monkeypatch.setattr(oa, "_CHROME_CANDIDATES", {"darwin": [], "win32": [], "linux": []})
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert oa._find_chrome_binary("") is None
    assert oa._find_chrome_binary("/does/not/matter") is None


def test_no_browser_error_message_is_actionable(monkeypatch):
    """找不到浏览器时的报错必须包含两条可操作出路（前端 notify 直接展示）。"""
    monkeypatch.setattr(oa, "_STATE", oa._BrowserState(attach_error="connect ECONNREFUSED"))
    with pytest.raises(oa.OaBrowserError) as exc_info:
        raise oa._no_browser_error()
    message = str(exc_info.value)
    assert "未在本机找到可用的 Chrome/Edge 浏览器" in message
    assert "remote-debugging-port=9222" in message  # 出路①：调试端口复用当前浏览器
    assert "chrome_path" in message  # 出路②：手动配置路径
    assert "ECONNREFUSED" in message  # 附带 CDP 失败原因
    assert oa._STATE.attach_error == ""  # 消费后清空，避免下次重复拼接


def test_upload_attachment_returns_server_path(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "ATTACHMENT_DIR", tmp_path / "oa-attachments")
    upload = UploadFile(file=io.BytesIO(b"%PDF-1.4 fake"), filename="出租车票.pdf")
    data = asyncio.run(upload_expense_attachment(upload))
    assert data["filename"] == "出租车票.pdf"
    saved = Path(data["path"])
    assert saved.exists() and saved.read_bytes() == b"%PDF-1.4 fake"


def test_upload_rejects_non_pdf_extension(tmp_path, monkeypatch):
    """OA 相关票据区只收 pdf：扩展名不是 .pdf 直接拒绝。"""
    monkeypatch.setattr(api_mod, "ATTACHMENT_DIR", tmp_path / "oa-attachments")
    upload = UploadFile(file=io.BytesIO(b"%PDF-1.4 fake"), filename="发票截图.png")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(upload_expense_attachment(upload))
    assert "PDF" in exc_info.value.detail


def test_upload_rejects_fake_pdf_content(tmp_path, monkeypatch):
    """扩展名是 .pdf 但内容缺少 %PDF- 文件头同样拒绝（防改后缀绕过）。"""
    monkeypatch.setattr(api_mod, "ATTACHMENT_DIR", tmp_path / "oa-attachments")
    upload = UploadFile(file=io.BytesIO(b"\x89PNG\r\n\x1a\n fake image"), filename="假发票.pdf")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(upload_expense_attachment(upload))
    assert "%PDF" in exc_info.value.detail


def test_extract_invoice_total_patterns():
    """电子发票价税合计的三种常见版式都能解析。"""
    text_vat = "价税合计（大写）壹佰元整（小写）¥100.00"
    text_paren_half = "价税合计(小写)￥1,234.56"
    text_no_xiaoxie = "合  计 ¥ 88.50 ¥ 5.31 价税合计 93.81"
    assert invoice_amount.extract_invoice_total_text(text_vat) == "100.00"
    assert invoice_amount.extract_invoice_total_text(text_paren_half) == "1234.56"
    assert invoice_amount.extract_invoice_total_text(text_no_xiaoxie) == "93.81"
    assert invoice_amount.extract_invoice_total_text("与金额无关的文本") is None


def _write_text_pdf(path: Path, text: str) -> None:
    """手写一个最小可提取文本的 PDF（Helvetica，WinAnsi 无法编码中文则用占位行）。"""
    # 用英文标注价税合计结构无法命中中文正则；这里直接把中文以 UTF-16BE 十六进制字符串
    # 嵌入（PDF 文本串支持），pypdf 的 extract_text 能还原。
    hex_text = text.encode("utf-16-be").hex()
    content = f"BT /F1 12 Tf 50 700 Td <{hex_text}> Tj ET".encode("latin-1")
    stream_len = len(content)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(stream_len).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /Identity-H >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    path.write_bytes(bytes(out))


def test_sum_amounts_endpoint_with_real_pdf(tmp_path):
    """端到端：手写含价税合计文本的 PDF → 端点解析出金额并累加。"""
    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.pdf"
    _write_text_pdf(p1, "价税合计（大写）壹佰元整（小写）¥100.00")
    _write_text_pdf(p2, "价税合计（小写）¥23.45")
    data = sum_amounts(SumAmountsRequest(paths=[str(p1), str(p2)]))
    assert [i["amount"] for i in data["items"]] == ["100.00", "23.45"]
    assert data["total"] == "123.45"


def test_sum_amounts_endpoint_empty_and_missing():
    assert sum_amounts(SumAmountsRequest(paths=[]))["total"] == "0.00"
    data = sum_amounts(SumAmountsRequest(paths=["/does/not/exist.pdf"]))
    assert data["total"] == "0.00"
    assert data["items"][0]["error"] == "文件不存在"


def test_real_config_file_loads_and_has_communication_scenario():
    """仓库里的真实配置文件必须能被加载且包含通讯费用场景（防手滑改坏）。"""
    config = oa.load_expense_config()
    ids = [s["id"] for s in config["scenarios"]]
    assert "communication" in ids
    communication = next(s for s in config["scenarios"] if s["id"] == "communication")
    assert communication["url"].startswith("https://oa.fosunholiday.com/")
