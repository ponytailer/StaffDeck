"""Agent 广场：开箱即用的定制 Agent 的后端能力。

Agent 广场里的 Agent（清单由前端 `src/data/agent-catalog.json` 管理）对普通成员是**只读**的：
只能「使用」，不能编辑、不能增删。每个 Agent 对应一个 `capability`，服务端只为这些能力
提供执行接口，Agent 自身的名字 / 描述 / 作者 / 更新时间等元信息不落库。

当前能力：

- ``slides`` —— AI 幻灯片生成。把「模板标题 + 页面文字 + 正文口述」整理成一套结构化 deck，
  前端负责按固定主题渲染成预览与 HTML 源码；`slides:export` 再把同一份 deck 落成 pptx。

模型归属是本模块的硬约束：生成必须使用**当前用户自己**在「模型配置」里配置并启用的模型，
缺省不回落租户/管理员的默认模型（与 `model_for_agent` 的宽松策略不同），否则同一个 Agent 会
用别人的额度出内容。

deck 的结构、收敛与提示词都在 `app.core.slides_deck` —— 这里是「取参数 → 打模型 → 收敛」的薄壳。
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.core.slides_deck import (
    SLIDES_OPERATION,
    SlidesDeck,
    build_slides_system_prompt,
    content_page_target,
    finalize_deck,
    sanitize_pages,
)
from app.db import get_session
from app.db.models import ModelConfig, User
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.observability.spans import llm_operation
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/agent-apps",
    tags=["enterprise:agent-apps"],
    dependencies=[Depends(get_current_user)],
)

#: pptx 的 MIME 类型（python-pptx 没有官方常量，这里写死标准值）。
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

#: 导出文件名上限：太长的标题会把 Content-Disposition 撑爆，也没人看。
_MAX_FILE_NAME_CHARS = 80

#: 与 `SlidesDeck` 一起复用的栏目页数上限（与生成端一致的 1-30 口径）。
_MIN_PAGE_COUNT = 1
_MAX_PAGE_COUNT = 30


class SlidesGenerateRequest(BaseModel):
    tenant_id: str
    model_config_id: str
    narrative: str
    template_title: str = ""
    page_label: str = ""
    page_count_mode: Literal["custom", "auto"] = "custom"
    #: **总页数**（含勾选的骨架页）—— 页面上的「页数」框填的就是它。
    #: 内容页张数由 `content_page_target()` 推导（总页数 - 骨架页，至少 1）。
    page_count: int = 5
    skeleton_pages: list[str] = Field(default_factory=list)
    #: Agent 自带的口径/样式 prompt（如「旅文汇报样式」）。由清单 JSON 提供、用户勾选后带上；
    #: 为空时不追加任何样式约束。
    style_prompt: str = ""


class SlidesExportRequest(BaseModel):
    """导出的是**前端正在展示的那一份** deck —— 所见即所得。

    因此这里不再接收左侧选项（页数/骨架页），只接收 deck 本身；即便如此也会再过一遍
    `sanitize_pages`，防止有人直接打接口塞进越界版式或超长文本。
    """

    deck: SlidesDeck
    #: 可选文件名（不含扩展名）。为空时按 deck 标题推导。
    file_name: str = ""


def _resolve_own_model(db: Session, tenant_id: str, model_config_id: str, user: User):
    """只接受「当前用户自己的、已启用的」模型配置。

    与 `model_for_agent` 的「用户模型缺失就回落租户默认」不同：Agent 广场的生成动作是
    用户显式触发的，用它人的模型代跑会串额度，所以这里缺失即报错、不回落。
    """
    if not model_config_id:
        raise HTTPException(status_code=400, detail="请先在「模型配置」里配置一个属于你的模型")
    row = db.get(ModelConfig, model_config_id)
    if row is None or row.tenant_id != tenant_id or row.user_id != user.id:
        raise HTTPException(status_code=403, detail="只能使用你在「模型配置」里配置的模型")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="该模型已停用，请换一个模型或先在「模型配置」里启用")
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


@router.post("/slides:generate", response_model=SlidesDeck)
def generate_slides(
    request: SlidesGenerateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SlidesDeck:
    ensure_tenant(db, request.tenant_id)
    narrative = (request.narrative or "").strip()
    if not narrative:
        raise HTTPException(status_code=400, detail="请先填写正文口述")
    if request.page_count_mode == "custom" and not (_MIN_PAGE_COUNT <= request.page_count <= _MAX_PAGE_COUNT):
        raise HTTPException(status_code=400, detail=f"页数需要在 {_MIN_PAGE_COUNT}-{_MAX_PAGE_COUNT} 之间")

    model = _resolve_own_model(db, request.tenant_id, request.model_config_id, current_user)

    skeleton_pages = list(request.skeleton_pages or [])
    system_prompt = build_slides_system_prompt(
        page_count_mode=request.page_count_mode,
        page_count=request.page_count,
        skeleton_pages=skeleton_pages,
        style_prompt=request.style_prompt,
    )
    target = content_page_target(request.page_count_mode, request.page_count, skeleton_pages)
    payload = {
        "模板标题": (request.template_title or "").strip(),
        "页面文字": (request.page_label or "").strip(),
        "正文口述": narrative,
        "内容页数": "由你按内容自动决定（4~8 页）" if target is None else str(target),
        "总页数": "由你决定" if target is None else str(target + len(skeleton_pages)),
        "需要额外生成的骨架页": "、".join(skeleton_pages) or "无",
    }

    try:
        with llm_operation(SLIDES_OPERATION):
            raw = LLMClient(model).generate_json(system_prompt, payload)
    except LLMError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"模型调用失败：{exc}。可以换一个模型或稍后重试。",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 统一翻译成前端可读文案
        raise HTTPException(status_code=502, detail=f"幻灯片生成失败：{exc}") from exc

    # 「选了首页就得有个首页」：收敛时按左侧选项双向对齐（漏了补、没勾的丢、超页截断）。
    return finalize_deck(
        raw,
        template_title=request.template_title,
        page_label=request.page_label,
        skeleton_pages=skeleton_pages,
        page_count_mode=request.page_count_mode,
        page_count=request.page_count,
    )


def _fallback_file_name(title: str) -> str:
    """把标题收拾成一个安全的文件名主干（不含扩展名）。"""
    cleaned = " ".join(str(title or "").split())
    # 路径分隔符与引号会破坏 Content-Disposition，统一替换掉
    for bad in ('/', "\\", '"', "'", "\r", "\n", "\t"):
        cleaned = cleaned.replace(bad, "_")
    cleaned = cleaned.strip(" ._")
    return cleaned[:_MAX_FILE_NAME_CHARS] or "slides"


def _ensure_pptx(name: str) -> str:
    return name if name.lower().endswith(".pptx") else f"{name}.pptx"


def _content_disposition(file_name: str) -> str:
    """中文文件名必须走 RFC 5987 的 ``filename*``，否则部分浏览器会截断成乱码。

    同时给一个纯 ASCII 的 ``filename`` 兜底：老客户端只认这个字段，纯中文名会被削成空串，
    干脆回落成 ``slides.pptx`` 而不是 ``.pptx``。
    """
    stem = file_name[: -len(".pptx")] if file_name.lower().endswith(".pptx") else file_name
    ascii_stem = "".join(
        char for char in stem if char.isascii() and (char.isalnum() or char in "-_ ")
    ).strip(" -_")
    ascii_fallback = _ensure_pptx(ascii_stem or "slides")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


@router.post("/slides:export")
def export_slides(request: SlidesExportRequest) -> Response:
    """把 deck 渲染成 .pptx 下载。

    只做「已生成内容的再渲染」，不调模型，所以不校验模型归属，也不消耗额度。
    """
    pages = sanitize_pages([page.model_dump() for page in (request.deck.pages or [])])
    if not pages:
        raise HTTPException(status_code=400, detail="还没有可导出的幻灯片，请先生成")

    deck = SlidesDeck(
        deck_title=request.deck.deck_title,
        page_label=request.deck.page_label,
        pages=pages,
    )

    # 延迟导入：python-pptx 只在导出路径上用到，缺少它也不该让整个 Agent 广场 500。
    try:
        from app.core.slides_pptx import build_slides_pptx
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署问题
        raise HTTPException(status_code=500, detail="服务端缺少 PPTX 导出依赖（python-pptx）") from exc

    try:
        data = build_slides_pptx(deck)
    except Exception as exc:  # noqa: BLE001 - 渲染失败要给出可读文案而不是 500 堆栈
        raise HTTPException(status_code=500, detail=f"生成 PPTX 失败：{exc}") from exc

    file_name = _ensure_pptx(_fallback_file_name(request.file_name or deck.deck_title))

    return Response(
        content=data,
        media_type=PPTX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(file_name),
            "Content-Length": str(len(data)),
            # 前端要读文件名时不必再解析 Content-Disposition
            "X-File-Name": quote(file_name),
        },
    )
