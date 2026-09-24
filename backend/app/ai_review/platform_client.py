"""GitHub / GitLab 平台客户端：解析 repo、拉 PR/MR 列表与详情。

凭证来自租户级全局配置（``AiReviewCredential``）：GitHub 一个 token 走官方
``https://api.github.com``；GitLab 支持自托管，``base_url`` 存实例根地址，
API 前缀固定 ``{base}/api/v4``。

所有方法返回轻量 dict（``PlatformMergeRequest.to_dict``），HTTP 细节不外泄。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_CLONE_HOST = "https://github.com"
REQUEST_TIMEOUT_SECONDS = 30.0

PLATFORMS = ("github", "gitlab")


class PlatformError(Exception):
    """平台 API 调用失败（凭证缺失 / 网络 / 上游报错），message 可直接给前端。"""


@dataclass
class PlatformCredential:
    platform: str
    token: str = ""
    base_url: str = ""

    def gitlab_api_base(self) -> str:
        base = (self.base_url or "").rstrip("/") or "https://gitlab.com"
        return f"{base}/api/v4"


@dataclass
class PlatformMergeRequest:
    number: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    author: str
    web_url: str
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "description": self.description,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "author": self.author,
            "web_url": self.web_url,
            "updated_at": self.updated_at,
        }


def resolve_repo_url(platform: str, repo_url: str) -> tuple[str, str]:
    """把克隆地址拆成 ``(repo_path, clone_url)``。

    GitHub：``https://github.com/{owner}/{repo}.git`` → ``owner/repo``
    GitLab：``https://host/{group}/{sub}/{repo}.git`` → ``group/sub/repo``（含子组）
    """
    url = (repo_url or "").strip()
    if not url:
        raise PlatformError("仓库地址不能为空")
    path = url.split("://", 1)[-1]
    path = path.split("/", 1)[-1]
    path = path.removesuffix(".git").removesuffix("/")
    if not path or " " in path:
        raise PlatformError(f"无法从仓库地址解析出 repo 路径：{repo_url}")

    if platform == "github":
        parts = path.split("/")
        if len(parts) != 2:
            raise PlatformError("GitHub 仓库地址应为 https://github.com/{owner}/{repo}")
        return path, f"{GITHUB_CLONE_HOST}/{path}.git"
    if platform == "gitlab":
        return path, url if url.endswith(".git") else f"{url.rstrip('/')}.git"
    raise PlatformError(f"不支持的平台：{platform}")


def git_clone_url(platform: str, repo_url: str, token: str) -> str:
    """克隆地址：私有仓库把 token 织进 URL（公共仓库带 token 也无副作用）。

    token 只出现在 clone 命令行与临时目录里，仓库克隆完即删（见 runner）。
    """
    _repo_path, clone_url = resolve_repo_url(platform, repo_url)
    if not token:
        return clone_url
    scheme, rest = clone_url.split("://", 1)
    auth_user = "x-access-token" if platform == "github" else "oauth2"
    return f"{scheme}://{auth_user}:{token}@{rest}"


def _headers(credential: PlatformCredential) -> dict[str, str]:
    if credential.platform == "github":
        headers = {
            "Authorization": f"Bearer {credential.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        headers = {"Accept": "application/json"}
        if credential.token:
            headers["PRIVATE-TOKEN"] = credential.token
    return headers


def _request_json(url: str, headers: dict[str, str]) -> Any:
    try:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise PlatformError(f"访问代码平台失败：{exc}") from exc
    if response.status_code in (401, 403):
        raise PlatformError("平台 token 无效或权限不足（401/403），请到「平台设置」里更新。")
    if response.status_code == 404:
        raise PlatformError("仓库或 PR/MR 不存在（404），请确认 token 有该仓库访问权限。")
    if response.status_code >= 400:
        detail = response.text[:200]
        raise PlatformError(f"代码平台返回 {response.status_code}：{detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise PlatformError("代码平台返回了无法解析的内容") from exc


def _normalize(platform: str, payload: dict[str, Any]) -> PlatformMergeRequest:
    if platform == "github":
        head = payload.get("head") or {}
        base = payload.get("base") or {}
        user = payload.get("user") or {}
        return PlatformMergeRequest(
            number=int(payload.get("number") or 0),
            title=str(payload.get("title") or ""),
            description=str(payload.get("body") or ""),
            source_branch=str(head.get("ref") or ""),
            target_branch=str(base.get("ref") or ""),
            author=str(user.get("login") or ""),
            web_url=str(payload.get("html_url") or ""),
            updated_at=str(payload.get("updated_at") or payload.get("created_at") or ""),
        )
    author = payload.get("author") or {}
    return PlatformMergeRequest(
        number=int(payload.get("iid") or 0),
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or ""),
        source_branch=str(payload.get("source_branch") or ""),
        target_branch=str(payload.get("target_branch") or ""),
        author=str(author.get("username") or author.get("name") or ""),
        web_url=str(payload.get("web_url") or ""),
        updated_at=str(payload.get("updated_at") or payload.get("created_at") or ""),
    )


def list_merge_requests(
    credential: PlatformCredential,
    repo_path: str,
    page: int = 1,
    page_size: int = 20,
    search: str = "",
) -> tuple[list[PlatformMergeRequest], int | None, bool]:
    """拉 open 状态的 PR/MR 列表，带服务端搜索与分页。

    平台分页粒度固定 100 条/页（GitHub ``per_page=100`` / GitLab ``per_page=100``），
    这里在内存里做 search 过滤后再切 ``page``/``page_size``，保证「分页 × 搜索」语义一致：

    - 逐平台页拉取并过滤，凑够 ``page * page_size`` 条即提前停；
    - 平台页取空（返回条数 < 100）时集合已穷尽 → ``total`` 是过滤后的精确总数；
    - 平台页数撞到 ``MAX_PLATFORM_PAGES``（仓库超 1000 个 open）→ ``total=None``，
      前端按 ``has_more`` 展示下一页。

    返回 ``(items, total | None, has_more)``。
    """
    page_size = max(1, min(page_size, 100))
    search_norm = (search or "").strip().lower()
    stop = page * page_size

    matched: list[PlatformMergeRequest] = []
    platform_exhausted = False
    for platform_page in range(1, MAX_PLATFORM_PAGES + 1):
        headers = _headers(credential)
        if credential.platform == "github":
            url = (
                f"{GITHUB_API_BASE}/repos/{repo_path}/pulls"
                f"?state=open&per_page={PLATFORM_PAGE_SIZE}&page={platform_page}"
                f"&sort=updated&direction=desc"
            )
            data = _request_json(url, headers)
        else:
            quoted = quote(repo_path, safe="")
            url = (
                f"{credential.gitlab_api_base()}/projects/{quoted}/merge_requests"
                f"?state=opened&per_page={PLATFORM_PAGE_SIZE}&page={platform_page}"
                f"&order_by=updated_at&sort=desc"
            )
            data = _request_json(url, headers)

        batch = [
            _normalize(credential.platform, item)
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict)
        ]
        if len(batch) < PLATFORM_PAGE_SIZE:
            platform_exhausted = True

        for mr in batch:
            if search_norm and not _mr_matches(mr, search_norm):
                continue
            matched.append(mr)
            if len(matched) >= stop:
                break
        if len(matched) >= stop or platform_exhausted:
            break

    total: int | None = len(matched) if platform_exhausted or len(matched) < stop else None
    offset = (page - 1) * page_size
    items = matched[offset : offset + page_size]
    has_more = len(matched) > stop
    return items, total, has_more


MAX_PLATFORM_PAGES = 10
PLATFORM_PAGE_SIZE = 100


def _mr_matches(mr: PlatformMergeRequest, search_norm: str) -> bool:
    """search 同时匹配标题 / 作者 / 编号（``#42`` 直接查编号）。"""
    if search_norm.startswith("#") and search_norm[1:].isdigit():
        return mr.number == int(search_norm[1:])
    return (
        search_norm in mr.title.lower()
        or search_norm in mr.author.lower()
        or str(mr.number) == search_norm
    )


def get_merge_request(credential: PlatformCredential, repo_path: str, number: int) -> PlatformMergeRequest:
    """拉单个 PR/MR 详情（建任务时冻结快照用）。"""
    if credential.platform == "github":
        url = f"{GITHUB_API_BASE}/repos/{repo_path}/pulls/{number}"
    else:
        quoted = quote(repo_path, safe="")
        url = f"{credential.gitlab_api_base()}/projects/{quoted}/merge_requests/{number}"
    return _normalize(credential.platform, _request_json(url, _headers(credential)))


def credential_display(token: str) -> dict[str, Any]:
    """对外的凭证摘要：绝不回完整 token。"""
    token = token or ""
    return {"token_set": bool(token), "token_last4": token[-4:] if token else ""}


def _request_post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise PlatformError(f"提交到代码平台失败：{exc}") from exc
    if response.status_code in (401, 403):
        raise PlatformError("平台 token 无效或权限不足（401/403），请到「平台设置」里更新。")
    if response.status_code == 404:
        raise PlatformError("目标 PR/MR 不存在（404），或 token 缺少评论写权限。")
    if response.status_code >= 400:
        raise PlatformError(f"代码平台返回 {response.status_code}：{response.text[:200]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise PlatformError("代码平台返回了无法解析的内容") from exc
    if not isinstance(data, dict):
        raise PlatformError("代码平台返回了无法解析的内容")
    return data


def post_merge_request_comment(
    credential: PlatformCredential, repo_path: str, number: int, body: str
) -> str:
    """把一段 markdown 回写到 PR/MR 评论区，返回评论 permalink。

    GitHub 走 issue 评论接口（PR 即 issue，支持行内上下文外的通用评论）；
    GitLab 走 merge request notes。写入失败抛 ``PlatformError``，message 可直接给前端。
    """
    headers = _headers(credential)
    if credential.platform == "github":
        url = f"{GITHUB_API_BASE}/repos/{repo_path}/issues/{number}/comments"
    else:
        quoted = quote(repo_path, safe="")
        url = f"{credential.gitlab_api_base()}/projects/{quoted}/merge_requests/{number}/notes"
    data = _request_post_json(url, headers, {"body": body})
    link = str(data.get("html_url") or data.get("web_url") or "")
    return link
