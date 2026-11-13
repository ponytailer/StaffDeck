"""知识库二进制资产（原文档里的图片）的落盘与访问。

设计取向（2026-10-21）：**图片不做理解，只当答案的补充**。入库时把图抽出来单独
存盘，在 markdown 正文里用标准语法引用（``![说明](/api/knowledge/assets/xxx)``），
于是分块、检索、证据、引用卡片、知识库预览全部沿用现有文字链路，不需要改检索。

访问方式：
- markdown 里的 URL 带 ``sig``（HMAC，能力 URL）。``<img>`` 请求带不了
  ``Authorization`` 头（前端是 Bearer token），没有签名就只能 401；
- 签名不过期：历史消息、引用卡片里的图长期可看，不会「过几天全裂」；
- 端点仍支持 Bearer 回退（无签名时按租户校验），便于内部工具直接取。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import paths
from app.config import get_settings
from app.db.models import KnowledgeAsset, KnowledgeChunk

#: markdown 里资产 URL 的路径前缀（同源相对路径，前端 img 可直接加载）。
ASSET_PUBLIC_PREFIX = "/api/knowledge/assets"
#: 解析器产出的占位协议：入库时替换成真实 URL。
ASSET_PLACEHOLDER_SCHEME = "kasset://"

ASSET_MAX_BYTES = 12 * 1024 * 1024
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "image/emf": ".emf",
    "image/x-emf": ".emf",
    "image/wmf": ".wmf",
}
_EXTENSION_FALLBACK = ".bin"
#: 解析器留下的图片占位：``![图注](kasset://0)``
_PLACEHOLDER_PATTERN = re.compile(r"!\[([^\]]*)\]\(kasset://(\d+)\)")


def asset_root() -> Path:
    data_root = paths.user_data_dir().resolve()
    root = (data_root / "knowledge_assets").resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_relative_to(data_root):
        raise ValueError("knowledge asset root escapes user data directory")
    return root


def _extension_for(mime: str) -> str:
    return _MIME_EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), _EXTENSION_FALLBACK)


def _storage_key(tenant_id: str, sha256: str, mime: str) -> str:
    return f"{tenant_id}/{sha256[:2]}/{sha256}{_extension_for(mime)}"


def asset_file_path(asset: KnowledgeAsset) -> Path:
    root = asset_root()
    path = (root / asset.storage_key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("knowledge asset path escapes storage root")
    return path


def asset_signature(asset_id: str) -> str:
    secret = (get_settings().app_secret or "").encode("utf-8")
    return hmac.new(secret, asset_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def verify_asset_signature(asset_id: str, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(asset_signature(asset_id), str(signature))


def asset_public_path(asset_id: str) -> str:
    """markdown 里使用的资产 URL（带签名，可直接放进 ``<img src>``）。"""

    return f"{ASSET_PUBLIC_PREFIX}/{asset_id}?sig={asset_signature(asset_id)}"


def save_asset(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    data: bytes,
    mime: str,
    document_id: str | None = None,
    page: int | None = None,
    section_path: str | None = None,
    order_index: int = 0,
    alt: str | None = None,
    width: int | None = None,
    height: int | None = None,
    kind: str = "image",
) -> KnowledgeAsset:
    """落盘并按内容去重；同租户同内容的图只存一份。"""

    if len(data) > ASSET_MAX_BYTES:
        raise ValueError("图片超过 12MB 上限")
    digest = hashlib.sha256(data).hexdigest()
    existing = db.exec(
        select(KnowledgeAsset)
        .where(KnowledgeAsset.tenant_id == tenant_id)
        .where(KnowledgeAsset.sha256 == digest)
        .where(KnowledgeAsset.kind == kind)
    ).first()
    if existing is not None:
        _ensure_asset_file(existing, data)
        return existing

    storage_key = _storage_key(tenant_id, digest, mime)
    asset = KnowledgeAsset(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        kind=kind,
        mime=(mime or "image/png").split(";")[0].strip().lower() or "image/png",
        size_bytes=len(data),
        sha256=digest,
        storage_key=storage_key,
        width=width,
        height=height,
        page=page,
        section_path=section_path,
        order_index=order_index,
        alt=(alt or "").strip() or None,
        metadata_json={},
    )
    _ensure_asset_file(asset, data)
    db.add(asset)
    db.flush()
    return asset


def _ensure_asset_file(asset: KnowledgeAsset, data: bytes) -> None:
    """写文件（已存在且大小一致则跳过；不一致时以库里的 sha256 为准重写）。"""

    path = asset_file_path(asset)
    try:
        if path.is_file() and path.stat().st_size == len(data):
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def materialize_image_placeholders(text: str, assets_by_index: dict[int, KnowledgeAsset]) -> str:
    """把解析器留下的 ``kasset://N`` 占位换成以资产 id 为定位符的引用。

    **刻意不写真实 URL**（2026-11-13 改）：

    - 签名 URL 长约 66 字符，写进 chunk 会被 900 字切块从中间切断，两半都不再是
      合法图片引用；
    - 更关键的是回答阶段要求模型复述它——抄错 ``sig`` 就是 403、抄错 id 就是 404，
      而且都是静默裂图（文字看着完全正常）。

    所以 chunk 里只留 ``![说明](kasset://kasset_xxx)``：短、稳定、不含域名。
    检索打分前照旧整段剥离；证据阶段由 ``image_refs`` 换成 ``{{img:N}}`` 短标记，
    真实 URL 只在最终正文里由后端拼一次。

    找不到对应资产时退化成纯文本标记（``[图片 N]``），避免正文里留下无法解析的伪协议。
    """

    if not text or ASSET_PLACEHOLDER_SCHEME not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        alt = (match.group(1) or "").strip()
        index = int(match.group(2))
        asset = assets_by_index.get(index)
        if asset is None:
            return f"[图片 {index + 1}]"
        label = alt or asset.alt or f"图片 {index + 1}"
        return f"![{label}]({ASSET_PLACEHOLDER_SCHEME}{asset.id})"

    return _PLACEHOLDER_PATTERN.sub(_replace, text)


def asset_count_from_text(text: str) -> int:
    return len(_PLACEHOLDER_PATTERN.findall(text or "")) if text else 0


def asset_read_payload(asset: KnowledgeAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "kind": asset.kind,
        "mime": asset.mime,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "page": asset.page,
        "alt": asset.alt,
        "url": asset_public_path(asset.id),
        "document_id": asset.document_id,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
    }


def purge_knowledge_base_assets(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
) -> int:
    """删知识库时回收图片资产（记录 + 磁盘文件），返回删掉的资产条数。

    **必须在 chunk 已经删完之后调用**：判断「还有没有人用」的唯一依据是现存
    chunk 正文里是否还有 ``kasset://<id>``。

    不能只按 ``knowledge_base_id`` 判：``save_asset`` 是按 ``(tenant, sha256)``
    去重的，同一张图被 A 库先入库、B 库再引用时，记录上的 ``knowledge_base_id``
    仍是 A。只按归属删，删 A 就把 B 还在用的图打碎了。

    磁盘再按 ``storage_key`` 判一次：同内容可能有多条记录，还有记录就不 unlink。
    """

    rows = db.exec(
        select(KnowledgeAsset)
        .where(KnowledgeAsset.tenant_id == tenant_id)
        .where(KnowledgeAsset.knowledge_base_id == knowledge_base_id)
    ).all()
    if not rows:
        return 0
    root = asset_root()
    removed = 0
    for row in rows:
        still_referenced = db.exec(
            select(KnowledgeChunk.id)
            .where(KnowledgeChunk.tenant_id == tenant_id)
            .where(KnowledgeChunk.content.contains(f"{ASSET_PLACEHOLDER_SCHEME}{row.id}"))
        ).first()
        if still_referenced is not None:
            continue
        storage_key = row.storage_key
        db.delete(row)
        db.flush()
        removed += 1
        shared_by_other = db.exec(
            select(KnowledgeAsset).where(KnowledgeAsset.storage_key == storage_key)
        ).first()
        if shared_by_other is not None:
            continue
        try:
            path = (root / storage_key).resolve()
            if path.is_relative_to(root):
                path.unlink(missing_ok=True)
        except OSError:
            # 文件删不掉不该让「删知识库」失败：记录已经删了，最坏留个孤儿文件
            continue
    return removed
