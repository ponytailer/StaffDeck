"""HTTP transport 的 MCP 客户端错误可诊断性测试。

背景：远程 MCP 服务器（Streamable HTTP）最常见的一次接入失败是**连接配置写错**——
URL 用了 `http://` 而反向代理 301 到 `https://`，或者 Headers 里缺 `Authorization`。
httpx 默认不跟随重定向，所以两者都会抛出 `HTTPStatusError`。此前客户端只把状态码
拼成「HTTP MCP 返回异常状态码：301/401」，而重定向目标（Location）和鉴权失败的
真实原因（响应体里的 `missing or invalid bearer token`）都被丢掉了，接入者只能盲猜。

这里固定住三条契约：
1. 3xx 必须点名重定向目标并给出改 URL 的动作；
2. 401/403 必须带上服务端响应体摘要并指向 Authorization；
3. 其它错误码保留状态码 + 响应体摘要；正常流程不受影响。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.tools.mcp_client import MCPClientError, discover_mcp_server


def _response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        text=text,
        request=httpx.Request("POST", "http://mcp.test/mcp"),
    )


def _patch_post(monkeypatch: pytest.MonkeyPatch, *responses: httpx.Response) -> list[str]:
    """把 httpx.Client.post 换成一串预置响应，返回记录到的 URL 列表。"""

    pending = list(responses)
    seen: list[str] = []

    def fake_post(self, url, **kwargs):  # noqa: ANN001, ANN003
        seen.append(str(url))
        if len(pending) == 1:
            return pending[0]
        return pending.pop(0)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    return seen


def _discover(config: dict[str, object]):
    return discover_mcp_server(config, timeout_seconds=5)


def test_http_redirect_reports_location_and_asks_for_https(monkeypatch) -> None:
    _patch_post(
        monkeypatch,
        _response(301, headers={"location": "https://mcp.test/genoffice/mcp"}),
    )

    with pytest.raises(MCPClientError) as info:
        _discover({"transport": "http", "url": "http://mcp.test/genoffice/mcp"})

    message = str(info.value)
    # 必须说清是重定向、重定向去哪、以及该改哪里，而不是只给一个状态码。
    assert "重定向" in message
    assert "301" in message
    assert "https://mcp.test/genoffice/mcp" in message
    assert "url" in message


def test_http_unauthorized_surfaces_server_body_and_points_at_authorization(monkeypatch) -> None:
    _patch_post(
        monkeypatch,
        _response(401, text='{"error":"missing or invalid bearer token"}'),
    )

    with pytest.raises(MCPClientError) as info:
        _discover({"transport": "http", "url": "https://mcp.test/genoffice/mcp"})

    message = str(info.value)
    assert "401" in message
    # 服务端的真实原因不能被吞掉
    assert "missing or invalid bearer token" in message
    assert "Authorization" in message


def test_http_forbidden_is_reported_as_an_auth_failure(monkeypatch) -> None:
    _patch_post(monkeypatch, _response(403, text="Resource not accessible by personal access token"))

    with pytest.raises(MCPClientError) as info:
        _discover({"transport": "http", "url": "https://mcp.test/genoffice/mcp"})

    message = str(info.value)
    assert "403" in message
    assert "鉴权失败" in message
    assert "Authorization" in message


def test_http_server_error_keeps_status_and_body_preview(monkeypatch) -> None:
    _patch_post(monkeypatch, _response(502, text="<html>bad gateway</html>"))

    with pytest.raises(MCPClientError) as info:
        _discover({"transport": "http", "url": "https://mcp.test/genoffice/mcp"})

    message = str(info.value)
    assert "502" in message
    assert "bad gateway" in message


def test_http_body_preview_is_truncated(monkeypatch) -> None:
    _patch_post(monkeypatch, _response(500, text="x" * 2000))

    with pytest.raises(MCPClientError) as info:
        _discover({"transport": "http", "url": "https://mcp.test/genoffice/mcp"})

    message = str(info.value)
    assert "500" in message
    # 摘要上限 300 字符 + 省略号，且不能把整段响应体灌进错误信息
    assert len(message) < 600
    assert "…" in message


def test_http_discovery_still_succeeds_on_the_happy_path(monkeypatch) -> None:
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "genoffice", "version": "0.10.1467"},
            },
        }
    )
    tools = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "slides_apply",
                        "description": "Apply ops to a deck.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        }
    )
    urls = _patch_post(
        monkeypatch,
        _response(200, headers={"mcp-session-id": "sess-1"}, text=initialize),
        _response(202),
        _response(200, text=tools),
    )

    discovery = _discover({"transport": "http", "url": "https://mcp.test/genoffice/mcp"})

    assert [item["name"] for item in discovery["tools"]] == ["slides_apply"]
    assert discovery["server_capabilities"] == {}
    assert urls and urls[0] == "https://mcp.test/genoffice/mcp"
