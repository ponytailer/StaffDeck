"""报销流程助手（Agent 广场 `expense` 能力）的后端。

职责只有三件事：
1. `GET  :config`  —— 下发场景清单（含事由预填模板），前端据此渲染表单；
2. `POST :attachments` —— 把用户选的附件落盘到 backend/.oa-attachments/ 并返回
   服务器绝对路径。浏览器安全模型拿不到本地文件全路径，而后端自动化浏览器
   （playwright setInputFiles）需要机器上的真实路径，所以「记录路径」= 落盘留档。
3. `POST :run` —— 组装表单值交给 `core/oa_browser.py` 驱动真实浏览器。

所有 OA 定位器/URL 都在 `config/expense_workflow.json`（独立配置文件）里维护。
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.invoice_amount import sum_invoice_amounts
from app.core.oa_browser import (
    OaBrowserError,
    browser_status,
    inspect_in_browser_thread,
    load_expense_config,
    resolve_user_path,
    run_in_browser_thread,
)
from app.security.auth import get_current_user

router = APIRouter(
    prefix="/api/enterprise/expense-workflow",
    tags=["enterprise:expense-workflow"],
    dependencies=[Depends(get_current_user)],
)

ATTACHMENT_DIR = resolve_user_path(".oa-attachments")
MAX_ATTACHMENT_MB = 20
_MONEY_RE = re.compile(r"^\d{1,10}(\.\d{1,2})?$")


def _scenario_public(scenario: dict[str, Any], library: dict[str, Any]) -> dict[str, Any]:
    """下发给前端的场景信息：只要表单渲染需要的内容，不漏 OA 内部定位器。

    fields 支持两种形态：字典（场景内联定义）/ 数组（引用 field_library 公共字段库，
    可被场景 field_overrides 覆盖）——与 oa_browser._scenario_fields 的解析保持一致。
    """
    raw = scenario.get("fields", {})
    if isinstance(raw, dict):
        entries: list[tuple[str, dict[str, Any]]] = [(key, spec) for key, spec in raw.items()]
    else:
        overrides = scenario.get("field_overrides", {}) or {}
        entries = []
        for name in raw:
            spec = dict(library.get(name) or {})
            spec.update(overrides.get(name) or {})
            entries.append((name, spec))
    fields = [
        {"key": key, "label": spec.get("label", key), "mode": spec.get("mode", "input")}
        for key, spec in entries
    ]
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "enabled": bool(scenario.get("enabled", True)),
        "reason_prefill": scenario.get("reason_prefill", ""),
        "fields": fields,
    }


@router.get("/config")
def get_expense_config() -> dict[str, Any]:
    """场景清单 + 浏览器状态。前端用 reason_prefill 模板自行渲染预填值。"""
    try:
        config = load_expense_config()
    except OaBrowserError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "scenarios": [_scenario_public(s, config.get("field_library", {})) for s in config.get("scenarios", []) if s.get("enabled", True)],
        "browser": browser_status(),
    }


@router.post("/attachments")
async def upload_expense_attachment(file: UploadFile) -> dict[str, str]:
    """附件落盘留档：返回服务器上的绝对路径，供后续自动化 set_input_files 使用。
    OA「相关票据」区只收电子发票 pdf，这里做扩展名 + 文件头双重校验。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    content = await file.read()
    if not Path(file.filename).name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="相关票据必须是 PDF 文件（.pdf）")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 PDF（缺少 %PDF- 文件头），请重新导出后再上传")
    if len(content) > MAX_ATTACHMENT_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"附件不能超过 {MAX_ATTACHMENT_MB}MB")
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", Path(file.filename).name) or "attachment"
    target = ATTACHMENT_DIR / f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}_{safe_name}"
    target.write_bytes(content)
    return {"path": str(target), "filename": file.filename, "size": str(len(content))}


class SumAmountsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, description=":attachments 返回的 PDF 路径列表")


@router.post("/sum-amounts")
def sum_amounts(req: SumAmountsRequest) -> dict[str, Any]:
    """解析已登记的电子发票 PDF 价税合计并累加，供前端自动填充报销金额。"""
    if not req.paths:
        return {"items": [], "total": "0.00"}
    return sum_invoice_amounts(req.paths)


class ExpenseRunRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    amount: str = Field(description="金额字符串，最多两位小数")
    reason: str = Field(min_length=1, max_length=200)
    company_name: str = Field(default="上海复星旅游管理有限公司", description="公司名称（OA 联想下拉匹配文本）")
    invoice_type: str = Field(default="普通发票", description="发票类型（OA 联想下拉匹配文本）")
    all_e_ticket: bool = Field(description="是否全为电子票")
    attachment_paths: list[str] = Field(default_factory=list, description="全电子票时必填（:attachments 返回的路径，相关票据区可多选）")


@router.post("/run")
def run_expense(req: ExpenseRunRequest) -> dict[str, Any]:
    """执行一次 OA 填充。status: need_login / partial / done / failed。"""
    if not _MONEY_RE.match(req.amount.strip()):
        raise HTTPException(status_code=400, detail="报销金额格式不对：请输入数字，最多两位小数（如 100.00）")
    if req.all_e_ticket and not req.attachment_paths:
        raise HTTPException(status_code=400, detail="全为电子票时必须先选择附件")

    try:
        config = load_expense_config()
        scenario = next(s for s in config.get("scenarios", []) if s["id"] == req.scenario_id)
    except OaBrowserError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail=f"场景 {req.scenario_id} 不存在或未启用") from exc

    values: dict[str, Any] = {
        "amount": req.amount.strip(),
        "reason": req.reason.strip(),
        "company_name": req.company_name.strip(),
        "invoice_type": req.invoice_type.strip(),
        "all_e_ticket": req.all_e_ticket,
        "attachment_paths": req.attachment_paths,
        "all_e_ticket=yes": req.all_e_ticket,  # 给 when 条件用
    }
    try:
        return run_in_browser_thread(scenario, values)
    except OaBrowserError as exc:
        return {"status": "failed", "message": str(exc), "steps": []}


class InspectRequest(BaseModel):
    scenario_id: str = Field(min_length=1)


@router.post("/inspect")
def inspect_expense_page(req: InspectRequest) -> dict[str, Any]:
    """打开/复用 OA 页面，dump 可见表单控件与按钮（含建议定位器），辅助补配置。"""
    try:
        config = load_expense_config()
        scenario = next(s for s in config.get("scenarios", []) if s["id"] == req.scenario_id)
    except OaBrowserError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail=f"场景 {req.scenario_id} 不存在或未启用") from exc
    try:
        return inspect_in_browser_thread(scenario)
    except OaBrowserError as exc:
        return {"status": "failed", "message": str(exc), "fields": [], "buttons": [], "steps": []}
