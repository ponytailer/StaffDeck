"""AI Reviewer API 的路由层测试。

沿用 test_enterprise_session_visibility.py 的做法：路由函数直接以
「内存 SQLite + 显式 current_user」调用（不建 lifespan、不连远程 PG）；
平台客户端与 rq 调度一律 monkeypatch，测试不外联 GitHub/GitLab。
另有一条 TestClient 用例确认路由注册与匿名 401（与 laya 测试同范式）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import ai_review
from app.db.models import AiReviewCredential, AiReviewPreset, AiReviewTask, AiReviewWorkspace, Tenant, User

TENANT = "tenant_airev_test"


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _user() -> User:
    return User(
        id="user_admin",
        tenant_id=TENANT,
        username="admin",
        password_hash="x",
    )


def _seed_tenant(db: Session) -> User:
    db.add(Tenant(id=TENANT, name="AI Review Test"))
    user = _user()
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------------------
# 凭证：token 只写不读
# ---------------------------------------------------------------------------


def test_credential_upsert_masks_token_and_updates_in_place() -> None:
    db = _session()
    user = _seed_tenant(db)

    written = ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(
            tenant_id=TENANT, platform="github", token="ghp_aaaa1111bbbb2222"
        ),
        current_user=user,
        db=db,
    )
    assert written["token_set"] is True
    assert written["token_last4"] == "2222"
    assert "ghp_" not in str(written)

    listing = ai_review.list_credentials(tenant_id=TENANT, current_user=user, db=db)
    by_platform = {row["platform"]: row for row in listing}
    assert set(by_platform) == {"github", "gitlab"}
    assert by_platform["github"]["token_set"] is True
    assert by_platform["github"]["token_last4"] == "2222"
    assert by_platform["gitlab"]["token_set"] is False
    # 全量 token 绝不出现在任何返回里
    assert all("ghp_" not in str(row) for row in listing)

    # 覆盖写：换 token
    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(
            tenant_id=TENANT, platform="github", token="ghp_new9999"
        ),
        current_user=user,
        db=db,
    )
    rows = list(
        db.exec(
            select(AiReviewCredential).where(
                AiReviewCredential.tenant_id == TENANT,
                AiReviewCredential.platform == "github",
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].token == "ghp_new9999"


def test_credential_gitlab_keeps_base_url_and_clears_github_base_url() -> None:
    db = _session()
    user = _seed_tenant(db)

    gitlab = ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(
            tenant_id=TENANT,
            platform="gitlab",
            token="glpat-1234",
            base_url="https://gitlab.example.com/",
        ),
        current_user=user,
        db=db,
    )
    assert gitlab["base_url"] == "https://gitlab.example.com"

    github = ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(
            tenant_id=TENANT, platform="github", token="ghp_x", base_url="https://ignored"
        ),
        current_user=user,
        db=db,
    )
    # github 走官方 API，base_url 一律忽略清空
    assert github["base_url"] == ""


def test_credential_rejects_bad_platform_and_empty_github_token() -> None:
    db = _session()
    user = _seed_tenant(db)

    with pytest.raises(HTTPException) as exc:
        ai_review.upsert_credential(
            ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="gitee", token="t"),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        ai_review.upsert_credential(
            ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="github", token="  "),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 422

    # 跨租户访问一律 403
    with pytest.raises(HTTPException) as exc:
        ai_review.list_credentials(tenant_id="tenant_other", current_user=user, db=db)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def test_workspace_create_resolves_repo_path_and_dedupes() -> None:
    db = _session()
    user = _seed_tenant(db)

    created = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT,
            name="主仓库",
            platform="github",
            repo_url="https://github.com/alibaba/open-code-review.git",
            default_branch="main",
        ),
        current_user=user,
        db=db,
    )
    assert created["repo_path"] == "alibaba/open-code-review"
    assert created["task_count"] == 0

    # 同仓库重复创建 → 409
    with pytest.raises(HTTPException) as exc:
        ai_review.create_workspace(
            ai_review.WorkspaceCreateRequest(
                tenant_id=TENANT,
                name="再来一次",
                platform="github",
                repo_url="https://github.com/alibaba/open-code-review.git",
            ),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 409

    # 非法仓库地址 → 422（resolve_repo_url 校验）
    with pytest.raises(HTTPException) as exc:
        ai_review.create_workspace(
            ai_review.WorkspaceCreateRequest(
                tenant_id=TENANT, name="bad", platform="github", repo_url="notaurl"
            ),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 422


def test_workspace_update_rename_and_repo_url_re_resolve() -> None:
    db = _session()
    user = _seed_tenant(db)
    created = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT,
            name="旧名",
            platform="github",
            repo_url="https://github.com/foo/bar.git",
        ),
        current_user=user,
        db=db,
    )
    workspace_id = created["id"]

    updated = ai_review.update_workspace(
        workspace_id,
        tenant_id=TENANT,
        request=ai_review.WorkspaceUpdateRequest(
            name="新名",
            repo_url="https://github.com/foo/baz.git",
            default_branch=" develop ",
        ),
        current_user=user,
        db=db,
    )
    assert updated["name"] == "新名"
    assert updated["repo_path"] == "foo/baz"
    assert updated["default_branch"] == "develop"

    # 不存在的 workspace → 404
    with pytest.raises(HTTPException) as exc:
        ai_review.update_workspace(
            "airevws_missing",
            tenant_id=TENANT,
            request=ai_review.WorkspaceUpdateRequest(name="x"),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 404


def test_workspace_delete_cascades_tasks() -> None:
    db = _session()
    user = _seed_tenant(db)
    created = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT,
            name="ws",
            platform="github",
            repo_url="https://github.com/foo/bar.git",
        ),
        current_user=user,
        db=db,
    )
    workspace_id = created["id"]
    db.add(
        AiReviewTask(
            tenant_id=TENANT,
            workspace_id=workspace_id,
            status="failed",
            mr_number=7,
            mr_title="旧任务",
        )
    )
    db.commit()

    result = ai_review.delete_workspace(
        workspace_id, tenant_id=TENANT, current_user=user, db=db
    )
    assert result["deleted"] is True
    assert db.get(AiReviewWorkspace, workspace_id) is None
    remaining = list(
        db.exec(select(AiReviewTask).where(AiReviewTask.workspace_id == workspace_id)).all()
    )
    assert remaining == []


# ---------------------------------------------------------------------------
# 预设：is_default 单选
# ---------------------------------------------------------------------------


def test_preset_default_is_single_select() -> None:
    db = _session()
    user = _seed_tenant(db)

    first = ai_review.create_preset(
        ai_review.PresetCreateRequest(
            tenant_id=TENANT, name="安全口径", content="重点关注注入与越权", is_default=True
        ),
        current_user=user,
        db=db,
    )
    second = ai_review.create_preset(
        ai_review.PresetCreateRequest(
            tenant_id=TENANT, name="性能口径", content="关注 N+1 查询", is_default=True
        ),
        current_user=user,
        db=db,
    )
    rows = {row["name"]: row for row in ai_review.list_presets(tenant_id=TENANT, current_user=user, db=db)}
    assert rows["安全口径"]["is_default"] is False  # 被新默认顶掉
    assert rows["性能口径"]["is_default"] is True

    # 显式取消默认
    ai_review.update_preset(
        second["id"],
        tenant_id=TENANT,
        request=ai_review.PresetUpdateRequest(is_default=False),
        current_user=user,
        db=db,
    )
    rows = {row["name"]: row for row in ai_review.list_presets(tenant_id=TENANT, current_user=user, db=db)}
    assert all(row["is_default"] is False for row in rows.values())

    # 删除
    deleted = ai_review.delete_preset(first["id"], tenant_id=TENANT, current_user=user, db=db)
    assert deleted["deleted"] is True
    assert db.get(AiReviewPreset, first["id"]) is None


# ---------------------------------------------------------------------------
# 任务：冻结快照 + 异步调度
# ---------------------------------------------------------------------------


def _fake_mr() -> Any:
    from app.ai_review.platform_client import PlatformMergeRequest

    return PlatformMergeRequest(
        number=42,
        title="修复登录超时",
        description="用户反馈 10 分钟掉线",
        source_branch="fix/login-timeout",
        target_branch="main",
        author="ponytailer",
        web_url="https://github.com/foo/bar/pull/42",
    )


def test_task_create_freezes_snapshot_and_schedules(monkeypatch) -> None:
    db = _session()
    user = _seed_tenant(db)
    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="github", token="ghp_ok"),
        current_user=user,
        db=db,
    )
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )
    preset = ai_review.create_preset(
        ai_review.PresetCreateRequest(
            tenant_id=TENANT, name="安全", content="关注越权", is_default=False
        ),
        current_user=user,
        db=db,
    )

    scheduled_ids: list[str] = []

    def fake_schedule(task_id: str, *, tenant_id: str | None = None) -> bool:
        scheduled_ids.append(task_id)
        return True

    monkeypatch.setattr(ai_review, "get_merge_request", lambda *a, **k: _fake_mr())
    monkeypatch.setattr("app.ai_review.jobs.schedule_ai_review", fake_schedule)

    created = ai_review.create_task(
        ai_review.TaskCreateRequest(
            tenant_id=TENANT,
            workspace_id=workspace["id"],
            mr_number=42,
            requirements="补充：不要漏掉并发问题",
            preset_ids=[preset["id"]],
        ),
        current_user=user,
        db=db,
    )
    assert created["scheduled"] is True
    assert created["status"] == "queued"
    # 快照冻结自平台详情
    assert created["mr_title"] == "修复登录超时"
    assert created["source_branch"] == "fix/login-timeout" or created["source_branch"] == "fix/login-timeout"
    assert created["web_url"].endswith("/pull/42")
    # 要求快照 = 自由文本 + 预设
    assert "补充：不要漏掉并发问题" in created["requirements"]
    assert "【全局评审要求：安全】" in created["requirements"]
    assert scheduled_ids == [created["id"]]

    # 任务列表：不含 result_json 正文，带 has_result 标记与 workspace 名
    rows = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], current_user=user, db=db
    )["items"]
    assert len(rows) == 1
    assert rows[0]["workspace_name"] == "ws"
    assert rows[0]["has_result"] is False
    assert "result_json" not in rows[0]

    # 详情：带 result_json 与 mr_description
    detail = ai_review.get_task(created["id"], tenant_id=TENANT, current_user=user, db=db)
    assert detail["mr_description"] == "用户反馈 10 分钟掉线"
    assert detail["result_json"] == []
    assert detail["summary_json"] == {}


def test_task_create_requires_credential_and_dedupes_inflight(monkeypatch) -> None:
    db = _session()
    user = _seed_tenant(db)
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="gitlab", repo_url="https://gitlab.example.com/g/p.git"
        ),
        current_user=user,
        db=db,
    )

    # 未配置 token → 400
    with pytest.raises(HTTPException) as exc:
        ai_review.create_task(
            ai_review.TaskCreateRequest(tenant_id=TENANT, workspace_id=workspace["id"], mr_number=1),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 400
    assert "access token" in str(exc.value.detail)

    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="gitlab", token="glpat-ok"),
        current_user=user,
        db=db,
    )
    monkeypatch.setattr(ai_review, "get_merge_request", lambda *a, **k: _fake_mr())

    def fake_schedule(task_id: str, *, tenant_id: str | None = None) -> bool:
        return True

    monkeypatch.setattr("app.ai_review.jobs.schedule_ai_review", fake_schedule)

    first = ai_review.create_task(
        ai_review.TaskCreateRequest(tenant_id=TENANT, workspace_id=workspace["id"], mr_number=42),
        current_user=user,
        db=db,
    )
    # 同 MR 已在排队 → 409
    with pytest.raises(HTTPException) as exc:
        ai_review.create_task(
            ai_review.TaskCreateRequest(tenant_id=TENANT, workspace_id=workspace["id"], mr_number=42),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 409

    # 终态后允许再次提交
    task_row = db.get(AiReviewTask, first["id"])
    task_row.status = "succeeded"
    db.add(task_row)
    db.commit()
    again = ai_review.create_task(
        ai_review.TaskCreateRequest(tenant_id=TENANT, workspace_id=workspace["id"], mr_number=42),
        current_user=user,
        db=db,
    )
    assert again["id"] != first["id"]


def test_task_create_maps_platform_error_to_502(monkeypatch) -> None:
    db = _session()
    user = _seed_tenant(db)
    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="github", token="ghp_ok"),
        current_user=user,
        db=db,
    )
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )

    from app.ai_review.platform_client import PlatformError

    def raise_platform_error(*args: Any, **kwargs: Any) -> None:
        raise PlatformError("仓库或 PR/MR 不存在（404）")

    monkeypatch.setattr(ai_review, "get_merge_request", raise_platform_error)
    with pytest.raises(HTTPException) as exc:
        ai_review.create_task(
            ai_review.TaskCreateRequest(tenant_id=TENANT, workspace_id=workspace["id"], mr_number=999),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 502
    assert "404" in str(exc.value.detail)
    # 失败的建任务不落任何任务行
    assert list(db.exec(select(AiReviewTask)).all()) == []


def test_task_retry_and_delete() -> None:
    db = _session()
    user = _seed_tenant(db)
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )
    task = AiReviewTask(
        tenant_id=TENANT,
        workspace_id=workspace["id"],
        status="failed",
        mr_number=3,
        mr_title="坏任务",
        error="ocr 不存在",
    )
    db.add(task)
    db.commit()

    # succeeded 不能重试
    task.status = "succeeded"
    db.add(task)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ai_review.retry_task(task.id, tenant_id=TENANT, current_user=user, db=db)
    assert exc.value.status_code == 409

    # failed 可以重试并重新入队
    task.status = "failed"
    db.add(task)
    db.commit()
    retried = ai_review.retry_task(task.id, tenant_id=TENANT, current_user=user, db=db)
    assert retried["status"] == "queued"
    assert retried["scheduled"] is True
    assert db.get(AiReviewTask, task.id).error == ""

    # 删除
    deleted = ai_review.delete_task(task.id, tenant_id=TENANT, current_user=user, db=db)
    assert deleted["deleted"] is True
    assert db.get(AiReviewTask, task.id) is None


# ---------------------------------------------------------------------------
# 自定义规则文件（ocr rule.json）：结构校验 + 三条路由
# ---------------------------------------------------------------------------


def test_rule_file_parse_accepts_json_string_and_dict() -> None:
    from app.ai_review.rule_file import parse_rule_file_config, sample_rule_file

    # dict 直接进
    parsed = parse_rule_file_config(sample_rule_file())
    assert parsed["exclude"] == ["**/*.gen.ts", "**/generated/**", "**/vendor/**"]
    assert parsed["rules"][0]["path"] == "src/api/**/*.go"
    # JSON 字符串也进；首尾空白被清掉、include=None 落空数组
    raw = json.dumps(
        {"include": None, "exclude": ["  **/vendor/**  "], "rules": [{"path": "**/*", "rule": "x"}]}
    )
    parsed = parse_rule_file_config(raw)
    assert parsed["include"] == []
    assert parsed["exclude"] == ["**/vendor/**"]
    assert parsed["rules"][0]["merge_system_rule"] is False
    # merge_system_rule=true 保留
    parsed = parse_rule_file_config(
        {"rules": [{"path": "**/*.py", "rule": "y", "merge_system_rule": True}]}
    )
    assert parsed["rules"][0]["merge_system_rule"] is True


def test_rule_file_parse_rejects_bad_structures() -> None:
    from app.ai_review.rule_file import RuleFileError, parse_rule_file_config

    def expect_error(content: Any, snippet: str) -> None:
        with pytest.raises(RuleFileError) as exc:
            parse_rule_file_config(content)
        assert snippet in str(exc.value), f"{snippet!r} not in {str(exc.value)!r}"

    # 非法 JSON 字符串 / 顶层非对象
    expect_error("{not json", "不是合法 JSON")
    expect_error([1, 2, 3], "必须是 JSON 对象")
    # 空内容：三字段全空 → 至少要有一个
    expect_error({}, "至少要有")
    # include/exclude 非法
    expect_error({"include": "src/**"}, "include 必须")
    expect_error({"exclude": [1]}, "非空 glob")
    # rules 结构问题
    expect_error({"rules": "x"}, "rules 必须是数组")
    expect_error({"rules": [42]}, "必须是 {path, rule} 对象")
    expect_error({"rules": [{"path": "", "rule": "x"}]}, "缺少 path")
    expect_error({"rules": [{"path": "a/**", "rule": "  "}]}, "缺少 rule")
    expect_error({"rules": [{"path": "p/**", "rule": "x" * 4001}]}, "超过")
    # 超量条目
    expect_error({"exclude": [f"a{i}/**" for i in range(101)]}, "不超过")


def test_rule_file_get_returns_sample_when_absent() -> None:
    from app.ai_review.rule_file import parse_rule_file_config, sample_rule_file

    db = _session()
    user = _seed_tenant(db)

    state = ai_review.get_rule_file(tenant_id=TENANT, current_user=user, db=db)
    assert state["exists"] is False
    # 示例本身就是合法结构（防手滑改坏示例）
    parse_rule_file_config(state["sample"])
    assert state["sample"] == sample_rule_file()


def test_rule_file_upsert_and_delete_roundtrip() -> None:
    from app.db.models import AiReviewRuleFile

    db = _session()
    user = _seed_tenant(db)

    saved = ai_review.upsert_rule_file(
        ai_review.RuleFileUpsertRequest(
            tenant_id=TENANT,
            name="  ",
            content={"exclude": ["**/vendor/**"], "rules": [{"path": "**/*.go", "rule": "校验请求体"}]},
        ),
        current_user=user,
        db=db,
    )
    assert saved["exists"] is True
    # 名字留空 → 落默认名
    assert saved["name"] == "自定义评审规则"
    assert saved["content"]["rules"][0]["rule"] == "校验请求体"
    assert saved["updated_at"] is not None

    # 覆盖写：同名租户只有一行
    saved2 = ai_review.upsert_rule_file(
        ai_review.RuleFileUpsertRequest(
            tenant_id=TENANT, name="安全规则", content='{"rules": [{"path": "**/*.py", "rule": "禁止裸 except"}]}'
        ),
        current_user=user,
        db=db,
    )
    assert saved2["name"] == "安全规则"
    assert saved2["content"]["rules"][0]["rule"] == "禁止裸 except"
    rows = list(db.exec(select(AiReviewRuleFile)).all())
    assert len(rows) == 1

    # 结构非法 → 422，且不落库
    with pytest.raises(HTTPException) as exc:
        ai_review.upsert_rule_file(
            ai_review.RuleFileUpsertRequest(tenant_id=TENANT, content={"include": []}),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 422
    assert len(list(db.exec(select(AiReviewRuleFile)).all())) == 1

    # GET 回读
    state = ai_review.get_rule_file(tenant_id=TENANT, current_user=user, db=db)
    assert state["exists"] is True
    assert state["content"]["rules"][0]["path"] == "**/*.py"

    # 删除后再 GET 回到「未配置 + sample」
    deleted = ai_review.delete_rule_file(tenant_id=TENANT, current_user=user, db=db)
    assert deleted["deleted"] is True
    assert ai_review.get_rule_file(tenant_id=TENANT, current_user=user, db=db)["exists"] is False

    # 重复删除 → 404
    with pytest.raises(HTTPException) as exc:
        ai_review.delete_rule_file(tenant_id=TENANT, current_user=user, db=db)
    assert exc.value.status_code == 404

    # 跨租户一律 403
    with pytest.raises(HTTPException) as exc:
        ai_review.get_rule_file(tenant_id="tenant_other", current_user=user, db=db)
    assert exc.value.status_code == 403


def test_task_list_pagination_and_search() -> None:
    db = _session()
    user = _seed_tenant(db)
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )
    titles = ["修复登录超时", "增加导出功能", "重构支付模块", "修复越权漏洞"]
    for index, title in enumerate(titles):
        db.add(
            AiReviewTask(
                tenant_id=TENANT,
                workspace_id=workspace["id"],
                status="succeeded" if index % 2 == 0 else "failed",
                mr_number=10 + index,
                mr_title=title,
                author="alice" if index % 2 == 0 else "bob",
            )
        )
    db.commit()

    # 第一页 2 条 + 精确 total
    page_one = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], page=1, page_size=2, current_user=user, db=db
    )
    assert page_one["total"] == 4
    assert page_one["page"] == 1
    assert page_one["total_pages"] == 2
    assert [task["mr_number"] for task in page_one["items"]] == [13, 12]  # created_at 倒序
    # 第二页
    page_two = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], page=2, page_size=2, current_user=user, db=db
    )
    assert [task["mr_number"] for task in page_two["items"]] == [11, 10]

    # 搜索：标题模糊 / 编号 # / 作者
    hit_title = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], search="登录", current_user=user, db=db
    )
    assert hit_title["total"] == 1
    assert hit_title["items"][0]["mr_title"] == "修复登录超时"
    hit_number = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], search="#11", current_user=user, db=db
    )
    assert hit_number["total"] == 1
    assert hit_number["items"][0]["mr_number"] == 11
    hit_author = ai_review.list_tasks(
        tenant_id=TENANT, workspace_id=workspace["id"], search="bob", current_user=user, db=db
    )
    assert hit_author["total"] == 2
    assert all(task["author"] == "bob" for task in hit_author["items"])

    # 回写记录字段在列表输出里（未回写时为空）
    assert all(task["platform_synced_at"] is None for task in page_one["items"])


def test_merge_request_list_shape(monkeypatch) -> None:
    from app.ai_review.platform_client import PlatformMergeRequest

    db = _session()
    user = _seed_tenant(db)
    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="github", token="ghp_ok"),
        current_user=user,
        db=db,
    )
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )
    fake_mr = PlatformMergeRequest(
        number=7, title="t", description="d", source_branch="a", target_branch="main",
        author="alice", web_url="https://github.com/foo/bar/pull/7",
    )
    captured: dict[str, Any] = {}

    def fake_list(credential, repo_path, page=1, page_size=20, search=""):
        captured.update(page=page, page_size=page_size, search=search)
        return [fake_mr], 42, True

    monkeypatch.setattr(ai_review, "list_merge_requests", fake_list)

    state = ai_review.list_workspace_merge_requests(
        workspace["id"], tenant_id=TENANT, page=2, page_size=10, search="fix",
        current_user=user, db=db,
    )
    assert captured == {"page": 2, "page_size": 10, "search": "fix"}
    assert state["total"] == 42
    assert state["page"] == 2
    assert state["page_size"] == 10
    assert state["has_more"] is True
    assert state["items"][0]["number"] == 7


def test_sync_task_to_platform_writes_comment_and_records(monkeypatch) -> None:
    db = _session()
    user = _seed_tenant(db)
    ai_review.upsert_credential(
        ai_review.CredentialUpsertRequest(tenant_id=TENANT, platform="github", token="ghp_ok"),
        current_user=user,
        db=db,
    )
    workspace = ai_review.create_workspace(
        ai_review.WorkspaceCreateRequest(
            tenant_id=TENANT, name="ws", platform="github", repo_url="https://github.com/foo/bar.git"
        ),
        current_user=user,
        db=db,
    )
    task = AiReviewTask(
        tenant_id=TENANT,
        workspace_id=workspace["id"],
        status="succeeded",
        mr_number=42,
        mr_title="修复登录超时",
        source_branch="fix/a",
        target_branch="main",
        result_json=[{"path": "src/a.go", "start_line": 3, "end_line": 7, "content": "缺少错误处理"}],
        summary_json={"model": "qwen3.8", "files_reviewed": 2},
    )
    db.add(task)
    db.commit()

    captured: dict[str, Any] = {}

    def fake_post(credential, repo_path, number, body):
        captured.update(repo_path=repo_path, number=number, body=body)
        return "https://github.com/foo/bar/pull/42#issuecomment-99"

    monkeypatch.setattr(ai_review, "post_merge_request_comment", fake_post)

    result = ai_review.sync_task_to_platform(
        task.id, request=ai_review.SyncToPlatformRequest(tenant_id=TENANT), current_user=user, db=db
    )
    assert result["synced"] is True
    assert result["platform_sync_url"].endswith("issuecomment-99")
    assert result["platform_synced_at"] is not None
    assert captured["number"] == 42
    assert captured["repo_path"] == "foo/bar"
    # markdown 内容关键片段
    assert "AI Reviewer 评审报告" in captured["body"]
    assert "src/a.go" in captured["body"]
    assert "L3-L7" in captured["body"]

    # 任务行记录了同步信息；列表输出带上
    row = db.get(AiReviewTask, task.id)
    assert row.platform_sync_url.endswith("issuecomment-99")
    listing = ai_review.list_tasks(tenant_id=TENANT, workspace_id=workspace["id"], current_user=user, db=db)
    assert listing["items"][0]["platform_synced_at"] is not None

    # 没结果的 job 不能回写 → 409
    empty = AiReviewTask(
        tenant_id=TENANT, workspace_id=workspace["id"], status="queued", mr_number=43
    )
    db.add(empty)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ai_review.sync_task_to_platform(
            empty.id,
            request=ai_review.SyncToPlatformRequest(tenant_id=TENANT),
            current_user=user,
            db=db,
        )
    assert exc.value.status_code == 409

    # 跨租户 403（tenant_id 与当前用户不匹配）
    other = _user()
    other.tenant_id = "tenant_other"
    with pytest.raises(HTTPException) as exc:
        ai_review.sync_task_to_platform(
            task.id,
            request=ai_review.SyncToPlatformRequest(tenant_id=TENANT),
            current_user=other,
            db=db,
        )
    assert exc.value.status_code == 403


def test_build_review_report_markdown_structure_and_truncation() -> None:
    from app.ai_review.report_sync import MAX_BODY_CHARS, build_review_report_markdown

    task = {"mr_number": 42, "mr_title": "修复登录超时", "source_branch": "fix/a", "target_branch": "main"}
    comments = [
        {"path": "src/a.go", "start_line": 3, "end_line": 7, "content": "缺少错误处理", "existing_code": "x := f()"},
        {"path": "src/b.go", "start_line": None, "end_line": None, "content": "整体建议"},
    ]
    body = build_review_report_markdown(task, comments, {"model": "qwen3.8", "files_reviewed": 2})
    assert body.startswith("## 🤖 AI Reviewer 评审报告")
    assert "模型 qwen3.8" in body
    assert "### 1. `src/a.go` · L3-L7" in body
    assert "```suggestion" not in body  # 没有建议代码就不给 suggestion fence
    assert "```suggestion" in build_review_report_markdown(
        task, [{"path": "a", "start_line": 1, "end_line": 1, "suggestion_code": "fix()"}]
    )
    # 截断保护
    huge = [{"path": "f", "start_line": 1, "end_line": 1, "content": "x" * (MAX_BODY_CHARS + 1000)}]
    truncated = build_review_report_markdown(task, huge, {})
    assert len(truncated) <= MAX_BODY_CHARS + 200
    assert "已截断" in truncated


# ---------------------------------------------------------------------------
# 路由注册与鉴权（与 laya 测试同范式，不触库）
# ---------------------------------------------------------------------------


def test_routes_are_registered_and_reject_anonymous() -> None:
    from app.main import app

    client = TestClient(app)
    paths = app.openapi()["paths"]
    assert "/api/enterprise/ai-review/credentials" in paths
    assert "/api/enterprise/ai-review/workspaces" in paths
    assert "/api/enterprise/ai-review/presets" in paths
    assert "/api/enterprise/ai-review/rules-file" in paths
    assert "/api/enterprise/ai-review/tasks" in paths
    assert "/api/enterprise/ai-review/tasks/{task_id}/sync-to-platform" in paths

    assert client.get("/api/enterprise/ai-review/credentials").status_code == 401
    assert client.get("/api/enterprise/ai-review/workspaces").status_code == 401
    assert client.get("/api/enterprise/ai-review/presets").status_code == 401
    assert client.get("/api/enterprise/ai-review/rules-file").status_code == 401
    assert client.get("/api/enterprise/ai-review/tasks").status_code == 401
    assert client.post("/api/enterprise/ai-review/tasks/any/sync-to-platform").status_code == 401
