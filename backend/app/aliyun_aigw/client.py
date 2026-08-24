"""低层签名请求封装：发送一次 V3 签名请求，返回解析后的 JSON。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import requests

from .config import HOST, USE_MOCK
from .mock import _mock_dispatch
from .signing import _canonical_query, _sign


def _request(
    method: str,
    path: str,
    *,
    action: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    security_token: str | None = None,
) -> dict[str, Any]:
    """发送一次签名请求，返回解析后的 JSON（dict）。"""
    if USE_MOCK:
        return _mock_dispatch(method, path, query, body)

    query = query or {}
    body_bytes = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    nonce = uuid.uuid4().hex
    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    headers = _sign(
        method,
        path,
        query,
        body_bytes,
        action=action,
        nonce=nonce,
        date=date,
        security_token=security_token,
    )
    if body is not None:
        headers["content-type"] = "application/json; charset=utf-8"

    canonical_query = _canonical_query(query)
    url = f"https://{HOST}{path}"
    if canonical_query:
        url += "?" + canonical_query

    resp = requests.request(
        method,
        url,
        headers=headers,
        data=body_bytes or None,
        timeout=30,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:  # noqa: BLE001
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"AI Gateway 请求失败 [{action}]: {detail}") from exc
    return resp.json()
