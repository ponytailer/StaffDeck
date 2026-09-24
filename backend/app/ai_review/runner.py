"""一次评审的执行器：克隆仓库 → 调 ocr CLI → 解析 JSON 结果。

OCR（alibaba/open-code-review）是 Go 写的 CLI：``ocr review --from origin/base
--to origin/head --format json --audience agent``。它自己读 git diff、按文件
打包喂给配置好的 LLM，产出行级评论（见 open-codereview.ai/docs/cli-reference）。

    凭据来源分两层：
    - **代码平台 token**（拉私有仓库 clone）：织进 clone URL，仓库克隆完即删临时目录；
    - **LLM 凭证**：通过 ``OCR_LLM_URL / OCR_LLM_TOKEN / OCR_LLM_MODEL`` 环境变量
      注入（OCR 文档支持的完整 ``OCR_LLM_*`` 配置来源），不落盘到 ocr 配置文件。

    自定义规则（可选 ``rule_file_content``，即 ocr 的 rule.json）同样只进临时目录，
    通过最高优先级的 ``--rule`` 参数注入，随 workdir 一起清理。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.ai_review.platform_client import git_clone_url
from app.config import get_settings

logger = logging.getLogger(__name__)


class ReviewRunError(Exception):
    """评审执行失败，message 直接写进任务行给前端看。"""


def resolve_ocr_binary() -> str:
    """定位 ocr 可执行文件；给绝对路径就用路径，否则查 PATH。"""
    settings = get_settings()
    configured = (settings.ai_review_ocr_binary or "ocr").strip()
    if not configured:
        configured = "ocr"
    if "/" in configured or configured.endswith((".exe",)):
        if Path(configured).is_file():
            return configured
        raise ReviewRunError(f"ocr 可执行文件不存在：{configured}（检查 AI_REVIEW_OCR_BINARY 配置）")
    found = shutil.which(configured)
    if not found:
        raise ReviewRunError(
            "服务器上没有找到 ocr CLI（alibaba/open-code-review）。"
            "安装：npm install -g @alibaba-group/open-code-review，"
            "或把 AI_REVIEW_OCR_BINARY 指向可执行文件路径。"
        )
    return found


def _run(cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None, timeout: int, what: str) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewRunError(f"{what}超时（{timeout}s）") from exc
    except OSError as exc:
        raise ReviewRunError(f"{what}执行失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise ReviewRunError(f"{what}失败（exit {proc.returncode}）：{detail}")
    return proc.stdout


def _ensure_language(ocr_binary: str, env: dict[str, str]) -> None:
    """评审意见语言：ocr config set language（非交互、幂等；失败不打断主流程）。"""
    settings = get_settings()
    language = (settings.ai_review_language or "").strip()
    if not language:
        return
    try:
        _run([ocr_binary, "config", "set", "language", language], cwd=None, env=env,
             timeout=30, what="ocr 配置评审语言")
    except ReviewRunError as exc:
        logger.warning("ocr 语言配置失败（继续用默认语言）：%s", exc)


def run_review(
    *,
    platform: str,
    repo_url: str,
    credential_token: str,
    source_branch: str,
    target_branch: str,
    mr_title: str,
    mr_description: str,
    requirements: str,
    rule_file_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行一次评审，返回 ``{"ocr_status", "comments", "summary", "model", "session_id"}``。

    只做一件事：给 ocr 一个能算出 merge-base diff 的克隆，然后把 JSON 读回来。
    任何一步失败都抛 ``ReviewRunError``（调用方把 message 写进任务行）。
    """
    settings = get_settings()
    ocr_binary = resolve_ocr_binary()

    if not source_branch or not target_branch:
        raise ReviewRunError("PR/MR 快照缺少源或目标分支，无法计算 diff 范围。")

    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0"})
    # LLM 端点以 ocr 自身配置为准；backend 侧 AI_REVIEW_LLM_* 配置存在时才用 env 覆盖。
    # （评审 LLM 的配置由 ocr CLI 自己管理，backend 不做硬校验。）
    if (settings.ai_review_llm_url or "").strip():
        env["OCR_LLM_URL"] = settings.ai_review_llm_url
    if (settings.ai_review_llm_token or "").strip():
        env["OCR_LLM_TOKEN"] = settings.ai_review_llm_token
    if (settings.ai_review_llm_model or "").strip():
        env["OCR_LLM_MODEL"] = settings.ai_review_llm_model
    if (settings.ai_review_llm_use_anthropic or "").strip().lower() in ("1", "true", "yes"):
        env["OCR_LLM_USE_ANTHROPIC"] = "true"
    else:
        env.pop("OCR_LLM_USE_ANTHROPIC", None)

    clone_url = git_clone_url(platform, repo_url, credential_token)
    workdir = Path(tempfile.mkdtemp(prefix="staffdeck-ai-review-"))
    try:
        _run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", clone_url, str(workdir / "repo")],
            cwd=workdir, env=env, timeout=settings.ai_review_clone_timeout_seconds, what="克隆仓库",
        )
        repo = workdir / "repo"
        # 双保险：克隆可能因浅配置漏掉某分支，显式 fetch 两个参与 diff 的 ref
        _run(
            [
                "git", "fetch", "origin",
                f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
                f"+refs/heads/{source_branch}:refs/remotes/origin/{source_branch}",
            ],
            cwd=repo, env=env, timeout=settings.ai_review_clone_timeout_seconds, what="拉取分支",
        )

        _ensure_language(ocr_binary, env)

        # 组装 --background：标题 + 描述 + 用户附加要求（ocr 只当上下文，不影响规则）
        background = f"MR/PR 标题：{mr_title}"
        if (mr_description or "").strip():
            background += f"\n描述：{mr_description.strip()[:2000]}"
        if (requirements or "").strip():
            background += f"\n本次评审额外要求：\n{requirements.strip()[:4000]}"

        # 自定义规则文件（ocr rule.json）：写进临时目录，以最高优先级 --rule 注入，
        # 覆盖项目/全局配置文件与系统内置规则（merge_system_rule 可与内置合并）
        rule_args: list[str] = []
        if rule_file_content:
            rule_path = workdir / "staffdeck-rule.json"
            rule_path.write_text(
                json.dumps(rule_file_content, ensure_ascii=False), encoding="utf-8"
            )
            rule_args = ["--rule", str(rule_path)]

        result_path = workdir / "review-result.json"
        _run(
            [
                ocr_binary, "review",
                "--from", f"origin/{target_branch}",
                "--to", f"origin/{source_branch}",
                "--background", background,
                *rule_args,
                "--format", "json",
                "--audience", "agent",
                "--output", str(result_path),
            ],
            cwd=repo, env=env, timeout=settings.ai_review_review_timeout_seconds, what="ocr 评审",
        )

        if not result_path.is_file():
            raise ReviewRunError("ocr 没有产出结果文件（--output 未生成）")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ReviewRunError("ocr 输出不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise ReviewRunError("ocr 输出结构异常")
        summary = payload.get("summary") or {}
        llm = payload.get("llm") or {}
        return {
            "ocr_status": str(payload.get("status") or ""),
            "comments": payload.get("comments") if isinstance(payload.get("comments"), list) else [],
            "summary": summary if isinstance(summary, dict) else {},
            "model": str((llm or {}).get("model") or ""),
            "session_id": str(payload.get("session_id") or ""),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
