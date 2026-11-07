"""个人静态资源的磁盘存储（内容寻址）。

为什么不用「上传目录 + 原文件名」：
- 原文件名进路径 = 路径穿越面（`../../etc/passwd`），要写一堆清洗逻辑还容易漏；
- 同名文件会互相覆盖，而这里的要求恰恰是「地址稳定、内容可追溯」。

改成按 sha256 定位后，文件名彻底不参与寻址：同名不同内容不会覆盖，不同人传同一
文件只占一份磁盘，读的时候还能用 sha 复核完整性。对外 URL 里的 filename 段只是
给人看的装饰，解析时忽略。

目录按 tenant 隔离。tenant_id 来自请求，必须清洗后才拼进路径 —— 这里用白名单
正则 + 目录归属校验双重兜底，和 `app/session/attachment_store.py` 同一套思路。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path

from app import paths

#: 单文件上限。现有 chat 附件是 12MB，网关 50MB；这里取中间值留余量。
STATIC_ASSET_MAX_BYTES = 20 * 1024 * 1024
#: 每人（按 owner_user_id）的 active 资源总配额。
STATIC_ASSET_USER_QUOTA_BYTES = 200 * 1024 * 1024
#: 每租户的 active 资源总配额 —— 磁盘和服务在同一台机器，不设上限会被单人拖死。
STATIC_ASSET_TENANT_QUOTA_BYTES = 2 * 1024 * 1024 * 1024

#: 解析时一律忽略 URL 里的 filename 段，但下载时仍要能按它还原展示名。
#: token 长度：`secrets.token_urlsafe(9)` ≈ 12 字符。刻意做短 —— 这个地址会被
#: 粘进知识库、由模型复述，22 位随机串抄错率高到不可接受。
STATIC_ASSET_TOKEN_BYTES = 9

#: 允许按真实类型返回的 MIME。其余一律降级 octet-stream：
#: 页面在同源下打开用户上传的 html/svg 等于把 localStorage 里的登录态送出去。
_SAFE_CONTENT_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/pdf",
        "application/zip",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]+")


def _storage_root() -> Path:
    data_root = paths.user_data_dir().resolve()
    root = (data_root / "static_assets").resolve()
    if not root.is_relative_to(data_root):
        raise ValueError("static asset storage root escapes user data directory")
    return root


def _tenant_segment(tenant_id: str) -> str:
    """tenant_id 进路径前清洗：只留 [A-Za-z0-9_-]，其余折叠成 `_`。"""
    cleaned = _SAFE_SEGMENT.sub("_", tenant_id or "").strip("_")
    return cleaned[:64] or "_"


def static_asset_path(tenant_id: str, sha256: str) -> Path:
    """内容寻址路径：`<root>/<tenant>/<sha[:2]>/<sha>`。"""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("invalid sha256 for static asset path")
    root = _storage_root()
    directory = (root / _tenant_segment(tenant_id) / sha256[:2]).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("static asset path escapes storage root")
    return directory / sha256


def write_static_asset(tenant_id: str, sha256: str, data: bytes) -> Path:
    """落盘（原子写）。同内容已存在则直接复用，不重复占空间。"""
    target = static_asset_path(tenant_id, sha256)
    if target.is_file() and target.stat().st_size == len(data):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise OSError("static asset directory cannot be a symbolic link")
    _atomic_write(target, data)
    return target


def read_static_asset(tenant_id: str, sha256: str) -> bytes | None:
    try:
        path = static_asset_path(tenant_id, sha256)
        if path.is_symlink():
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def delete_static_asset_file(tenant_id: str, sha256: str) -> None:
    """删除磁盘文件。内容寻址下同一 sha 可能被多条记录共享，调用方需先确认无其他引用。"""
    try:
        static_asset_path(tenant_id, sha256).unlink(missing_ok=True)
    except (OSError, ValueError):
        # 删不掉不影响主流程：记录已置 deleted，下载端点不会再吐内容。
        pass


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_download_name(filename: str, fallback: str = "download.bin") -> str:
    """展示用文件名：去掉目录部分与控制字符，避免 Content-Disposition 被注入换行。"""
    name = Path(filename or "").name
    name = re.sub(r'[\r\n"\\\x00-\x1f]', "_", name).strip()
    return name[:180] or fallback


def content_type_for(filename: str) -> str:
    """按扩展名推断存储用的 MIME；不在安全白名单里的一律 octet-stream。"""
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed and guessed.lower() in _SAFE_CONTENT_TYPES:
        return guessed.lower()
    return "application/octet-stream"


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


__all__ = [
    "STATIC_ASSET_MAX_BYTES",
    "STATIC_ASSET_TENANT_QUOTA_BYTES",
    "STATIC_ASSET_TOKEN_BYTES",
    "STATIC_ASSET_USER_QUOTA_BYTES",
    "content_type_for",
    "delete_static_asset_file",
    "read_static_asset",
    "safe_download_name",
    "sha256_of",
    "static_asset_path",
    "write_static_asset",
]
