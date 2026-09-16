"""MinerU 云端 PDF 解析客户端（v4 批量接口，单文件）。

链路：申请预签名上传 URL → PUT 原始字节 → 轮询解析结果 → 下载结果 zip →
抽取 markdown 正文。50 页扫描件实测端到端 ~46s（2026-09 探针验证，见
``scripts/_probe_mineru_parse.py``）。

鉴权只走 Bearer Token（官方 v4 不认 AK/SK 签名）。``.env`` 历史上曾把键名
误拼为 ``MINUERU_TOKEN``，这里两个键名都兼容，新环境请统一 ``MINERU_TOKEN``。
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from collections.abc import Callable
from typing import Any

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5


class MinerUError(RuntimeError):
    """MinerU 解析失败（配置缺失 / 接口报错 / 轮询超时）。"""


def mineru_token() -> str:
    """读取 MinerU Bearer Token；未配置时抛 :class:`MinerUError`。"""

    settings = get_settings()
    token = (
        os.environ.get("MINERU_TOKEN")
        or os.environ.get("MINUERU_TOKEN")
        or settings.mineru_token
        or ""
    ).strip()
    if not token:
        raise MinerUError("MINERU_TOKEN 未配置，无法进行云端 PDF 解析")
    return token


def mineru_available() -> bool:
    """上传接口据此决定是否为 PDF 附件创建解析任务。"""

    try:
        return bool(mineru_token())
    except MinerUError:
        return False


ProgressCallback = Callable[[str, float], None]
"""progress_cb(stage, progress)：stage ∈ queued/uploading/parsing/downloading/done，
progress ∈ [0, 100]。回调自身抛出的异常会被吞掉（只记日志），不影响解析主流程。"""


def parse_pdf_bytes(
    data: bytes,
    filename: str,
    *,
    progress_cb: ProgressCallback | None = None,
) -> str:
    """把一份 PDF 字节流送云端解析，返回 markdown 正文。

    任何一步失败都抛 :class:`MinerUError`（message 面向用户可读）。
    """

    settings = get_settings()
    base = (settings.mineru_api_base or "https://mineru.net").rstrip("/")
    token = mineru_token()
    headers = {"Authorization": f"Bearer {token}"}
    poll_timeout = max(60, settings.attachment_parse_poll_timeout_seconds)

    def report(stage: str, progress: float) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, progress)
        except Exception:  # noqa: BLE001 - 进度回调失败不影响解析
            logger.debug("mineru progress_cb failed", exc_info=True)

    # 1. 申请预签名上传 URL
    report("queued", 1.0)
    batch_id, upload_url = _request_upload_urls(base, headers, filename)
    # 2. 上传原始文件
    report("uploading", 5.0)
    try:
        resp = requests.put(upload_url, data=data, timeout=UPLOAD_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MinerUError(f"MinerU 上传失败：{type(exc).__name__}") from exc
    if resp.status_code not in (200, 201):
        raise MinerUError(f"MinerU 上传失败：HTTP {resp.status_code}")
    report("uploading", 15.0)

    # 3. 轮询解析结果（15% → 85% 线性映射 extracted_pages）
    item = _poll_result(base, headers, batch_id, poll_timeout, report)

    # 4. 下载结果 zip 并抽取 markdown
    report("downloading", 88.0)
    markdown = _download_markdown(item)
    if not markdown.strip():
        raise MinerUError("MinerU 解析完成但未得到文本内容（可能是纯图片或加密 PDF）")
    report("done", 100.0)
    return markdown


def _request_upload_urls(base: str, headers: dict[str, str], filename: str) -> tuple[str, str]:
    url = f"{base}/api/v4/file-urls/batch"
    payload: dict[str, Any] = {
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
        "files": [{"name": filename, "is_ocr": True, "data_id": "chat-attachment"}],
        "model_version": "vlm",
    }
    try:
        resp = requests.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MinerUError(f"MinerU 服务不可达：{type(exc).__name__}") from exc
    body = _json_body(resp)
    if resp.status_code != 200 or body.get("code") != 0:
        raise MinerUError(f"MinerU 申请上传链接失败：{_describe(resp, body)}")
    data = body.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        raise MinerUError("MinerU 响应缺少 batch_id / file_urls")
    return str(batch_id), str(file_urls[0])


def _poll_result(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    timeout_seconds: int,
    report: ProgressCallback,
) -> dict[str, Any]:
    url = f"{base}/api/v4/extract-results/batch/{batch_id}"
    started = time.time()
    while time.time() - started < timeout_seconds:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise MinerUError(f"MinerU 轮询失败：{type(exc).__name__}") from exc
        body = _json_body(resp)
        if resp.status_code != 200 or body.get("code") != 0:
            raise MinerUError(f"MinerU 轮询失败：{_describe(resp, body)}")
        results = (body.get("data") or {}).get("extract_result") or []
        if not results:
            raise MinerUError("MinerU 轮询响应缺少 extract_result")
        item = results[0]
        state = str(item.get("state") or "")
        if state == "done":
            return item
        if state == "failed":
            raise MinerUError(f"MinerU 解析失败：{item.get('err_msg') or '未知错误'}")
        pages = (item.get("extract_progress") or {}).get("extracted_pages")
        progress = 85.0 if isinstance(pages, (int, float)) and pages else 40.0
        # 页数未知时用时间渐近：让前端进度条在长解析里保持活动感
        if not pages:
            progress = min(70.0, 40.0 + (time.time() - started) / timeout_seconds * 30.0)
        report("parsing", progress)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise MinerUError(f"MinerU 解析超时（>{timeout_seconds}s），请稍后重试")


def _download_markdown(item: dict[str, Any]) -> str:
    zip_url = str(item.get("full_zip_url") or "")
    if not zip_url:
        raise MinerUError("MinerU 结果缺少 full_zip_url")
    try:
        resp = requests.get(zip_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MinerUError(f"MinerU 结果下载失败：{type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise MinerUError(f"MinerU 结果下载失败：HTTP {resp.status_code}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU 结果 zip 损坏") from exc
    candidates = [name for name in archive.namelist() if name.endswith(".md")]
    # 优先 full.md（MinerU vlm 输出的正文文件），其余取路径最短的
    candidates.sort(key=lambda name: (name.rsplit("/", 1)[-1] != "full.md", len(name)))
    if not candidates:
        raise MinerUError("MinerU 结果 zip 中没有 markdown 文件")
    return archive.read(candidates[0]).decode("utf-8", errors="replace")


def _json_body(resp: requests.Response) -> dict[str, Any]:
    if not str(resp.headers.get("content-type", "")).lower().startswith("application/json"):
        return {}
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _describe(resp: requests.Response, body: dict[str, Any]) -> str:
    message = body.get("msg") if isinstance(body, dict) else None
    return str(message) if message else f"HTTP {resp.status_code} {resp.text[:200]}"
