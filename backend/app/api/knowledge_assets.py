"""知识库图片资产的读取端点。

``/api/knowledge/assets/{id}?sig=xxx`` 是**能力 URL**：markdown 正文里引用的就是它，
``<img src>`` 无法携带 ``Authorization`` 头（前端用 Bearer token），所以签名放在
query 里。签名不过期，历史消息里的图长期可看。

无签名时回退 Bearer 校验（同一租户即可），方便内部工具/调试直接取图。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session

from app.db import get_session
from app.db.models import KnowledgeAsset
from app.knowledge.assets import asset_file_path, verify_asset_signature
from app.security.auth import get_current_user, security

router = APIRouter(prefix="/api/knowledge/assets", tags=["knowledge:assets"])

#: 可以直接当 ``<img>`` 渲染的类型。其余（``image/svg+xml``、未知类型）一律
#: 降级成下载：同源渲染 SVG 里的脚本能读到 localStorage 的登录态，等于把
#: 上传通道做成 XSS 通道。Word 里的图基本是 png/jpeg，降级影响面很小。
_RENDERABLE_MIME = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)


@router.get("/{asset_id}")
def read_knowledge_asset(
    asset_id: str,
    sig: str | None = Query(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_session),
) -> FileResponse:
    asset = db.get(KnowledgeAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="知识资产不存在")
    if not verify_asset_signature(asset.id, sig):
        _ensure_token_owner(asset, credentials, db)
    path = asset_file_path(asset)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="知识资产文件缺失")
    mime = (asset.mime or "").split(";")[0].strip().lower()
    renderable = mime in _RENDERABLE_MIME
    headers = {
        # 内容按 sha256 存储，同 id 的内容不会变，可放心让浏览器缓存
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
        # 必须允许跨源嵌入：浏览器插件把聊天面板注入到宿主的 OA 页面里，
        # `<img src="https://<api>/api/knowledge/assets/...">` 是跨源请求，
        # `same-origin` 会让它静默裂图（图片不显示，控制台也没有明显报错）。
        # 放开的风险可控：可执行的 html/svg 已被 _RENDERABLE_MIME 排除并降级成
        # attachment，这里能被 `cross-origin` 嵌入的只有静态位图。
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if not renderable:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(
        path,
        media_type=mime if renderable else "application/octet-stream",
        headers=headers,
    )


def _ensure_token_owner(
    asset: KnowledgeAsset,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> None:
    if credentials is None:
        raise HTTPException(status_code=403, detail="缺少资产访问签名")
    try:
        user = get_current_user(credentials, db)
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="无权访问该知识资产") from exc
    if getattr(user, "tenant_id", None) != asset.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该知识资产")
