"""通用技能分享链接：站内生成 / 公开读取 / 公开下载。"""

import base64
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

from app.api.general_skill_shares import (
    create_general_skill_share,
    download_shared_general_skill_package,
    read_general_skill_share,
    revoke_general_skill_share,
)
from app.api.general_skills import import_general_skill_package
from app.db.models import (
    AgentProfile,
    GeneralSkillShareLink,
    GeneralSkill,
    User,
)
from app.general_skills import GeneralSkillPackageUploadRequest

from tests.test_general_skills import _seed_minimal_tenant, _test_session


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        password_hash="x",
    )


def _import_published_skill(db, name: str = "天气包技能", slug: str = "weather-skill"):
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "tool-skill/SKILL.md",
            f"---\nname: {name}\nslug: {slug}\ndescription: 项目查询技能\n---\n\n# {name}\n",
        )
    return import_general_skill_package(
        GeneralSkillPackageUploadRequest(
            tenant_id="tenant_demo",
            filename=f"{slug}.zip",
            content_base64=base64.b64encode(package.getvalue()).decode("ascii"),
            status="published",
        ),
        db,
        _admin_user(),
    )


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        password_hash="x",
    )


def test_create_share_is_idempotent_and_public_info_lists_files() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True)
        )
        row = _import_published_skill(db)

        share = create_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())
        # 幂等：再次创建返回同一 token
        again = create_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())
        assert share.token == again.token

        info = read_general_skill_share(share.token, db=db)
        assert info.name == "天气包技能"
        assert info.description == "项目查询技能"
        # 公开信息只在名称/说明/文件数/总大小（files 明细不再进公开响应，也不再读大字段）
        assert info.file_count == 1
        assert info.total_bytes > 0


def test_public_download_returns_zip_without_login() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True)
        )
        _import_published_skill(db)
        share = create_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())

        response = download_shared_general_skill_package(share.token, db=db)
        assert response.media_type == "application/zip"
        content = bytes(response.body)
        assert content.startswith(b"PK")

        # 访问计数 +1
        link = db.exec(
            __import__("sqlmodel").select(GeneralSkillShareLink)
        ).first()
        assert link.access_count == 1


def test_revoked_share_link_is_rejected() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True)
        )
        _import_published_skill(db)
        share = create_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())

        revoke_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())

        with pytest.raises(HTTPException) as exc:
            read_general_skill_share(share.token, db=db)
        assert exc.value.status_code == 404


def test_archived_skill_hides_share_info() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True)
        )
        _import_published_skill(db)
        share = create_general_skill_share("weather-skill", tenant_id="tenant_demo", db=db, current_user=_admin_user())

        # 下架技能后，链接虽然有效但技能不再 published → 404
        row = db.exec(__import__("sqlmodel").select(GeneralSkill)).first()
        row.status = "archived"
        db.add(row)
        db.commit()

        with pytest.raises(HTTPException) as exc:
            read_general_skill_share(share.token, db=db)
        assert exc.value.status_code == 404
