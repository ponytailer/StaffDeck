"""阿里云 OpenAPI V3 签名（ACS3-HMAC-SHA256，ROA 风格公共请求头 x-acs-*）。"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import quote

from .config import (
    ACCESS_KEY_ID,
    ACCESS_KEY_SECRET,
    ALGORITHM,
    API_VERSION,
    HOST,
)


def _percent_encode(value: str) -> str:
    """RFC 3986 百分号编码（保留 A-Za-z0-9-_.~）。"""
    return quote(value, safe="-_.~")


def _canonical_uri(path: str) -> str:
    """对每个路径段编码后重新拼接，保留前导 /。"""
    segments = path.split("/")
    return "/".join(_percent_encode(seg) for seg in segments)


def _canonical_query(params: dict[str, Any]) -> str:
    """按 key 排序、百分号编码的查询串。"""
    if not params:
        return ""
    items = []
    for key in sorted(params.keys()):
        val = params[key]
        if val is None:
            continue
        items.append(f"{_percent_encode(str(key))}={_percent_encode(str(val))}")
    return "&".join(items)


def _sign(
    method: str,
    path: str,
    query: dict[str, Any],
    body_bytes: bytes,
    *,
    action: str,
    nonce: str,
    date: str,
    security_token: str | None = None,
) -> dict[str, str]:
    """构造 V3 签名所需的全部请求头（含 Authorization）。"""
    content_sha256 = hashlib.sha256(body_bytes).hexdigest()

    headers_map = {
        "host": HOST,
        "x-acs-action": action,
        "x-acs-content-sha256": content_sha256,
        "x-acs-date": date,
        "x-acs-signature-nonce": nonce,
        "x-acs-version": API_VERSION,
    }
    if security_token:
        headers_map["x-acs-security-token"] = security_token

    signed_headers = sorted(headers_map.keys())
    canonical_headers = "".join(f"{k}:{headers_map[k].strip()}\n" for k in signed_headers)
    signed_headers_str = ";".join(signed_headers)

    canonical_request = "\n".join(
        [
            method.upper(),
            _canonical_uri(path),
            _canonical_query(query),
            canonical_headers,
            signed_headers_str,
            content_sha256,
        ]
    )

    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"{ALGORITHM}\n{hashed_canonical}"
    signature = hmac.new(
        ACCESS_KEY_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers_map["Authorization"] = (
        f"{ALGORITHM} Credential={ACCESS_KEY_ID},"
        f"SignedHeaders={signed_headers_str},Signature={signature}"
    )
    return headers_map
