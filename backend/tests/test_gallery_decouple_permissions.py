"""Independent verification of the open-gallery decoupling (general_skill).

This file is authored by the verification role and does NOT reuse the worker's
test module. It calls the API handler functions directly (the same code paths
the HTTP endpoints dispatch to) and asserts the privilege boundary end-to-end.

Privilege boundary under test:
- CREATE gallery skill: any tenant member            -> 200
- MODIFY/ARCHIVE/DELETE/OVERWRITE own gallery skill: creator or admin -> 200
- MODIFY/ARCHIVE/DELETE/OVERWRITE another member's gallery skill: admin only -> 403
- publish-to-gallery on ANOTHER member's private skill: 403 (owner check retained)
- data layer unchanged: overall agent stays implicit host of agent_resource_bindings
"""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.general_skills import (
    archive_general_skill,
    delete_general_skill,
    import_general_skill,
    import_general_skill_package,
    list_general_skills,
    publish_general_skill,
    publish_general_skill_to_gallery,
)
from app.db.models import AgentProfile, GeneralSkill, ModelConfig, Tenant, User
from app.general_skills.schema import GeneralSkillImportRequest, GeneralSkillPackageUploadRequest
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret

WEATHER_SKILL_MD = """# 中国城市天气查询工具

python weather.py -json -today <地区名称>
"""

SKILL_A_MD = "# 作者天气技能\n\n作者原创内容\n"
SKILL_B_MD = "# 成员B恶意覆盖\n\n越权内容\n"


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_minimal_tenant(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="user_demo",
            password_hash=hash_password("demo"),
        )
    )
    db.add(
        User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash="test",
        )
    )
    db.add(
        ModelConfig(
            tenant_id="tenant_demo",
            name="Fake model",
            api_key_encrypted=encrypt_secret("test-key"),
            model="fake",
            is_default=True,
            enabled=True,
        )
    )
    db.commit()


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def _member(uid: str, username: str) -> User:
    return User(id=uid, tenant_id="tenant_demo", username=username, password_hash="x")


def _add_overall(db: Session) -> None:
    db.add(
        AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# A.1 + A.2  member B cannot overwrite / publish / archive / delete member A's gallery skill
# ---------------------------------------------------------------------------
def test_member_b_cannot_overwrite_member_a_via_original_slug_and_content_preserved() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        b = _member("user_b", "other")
        db.add(a)
        db.add(b)
        db.commit()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )

        with pytest.raises(HTTPException) as exc:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    original_slug="author-weather",
                    slug="author-weather",
                    name="被恶意覆盖",
                    markdown=SKILL_B_MD,
                ),
                db,
                b,
            )
        assert exc.value.status_code == 403

        survived = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == "tenant_demo",
                GeneralSkill.slug == "author-weather",
            )
        ).first()
        assert survived is not None
        assert survived.name == "作者天气技能"
        assert "作者原创内容" in survived.skill_markdown


def test_member_b_cannot_publish_archive_delete_others_gallery_skill() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        b = _member("user_b", "other")
        db.add(a)
        db.add(b)
        db.commit()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )

        for op in ("publish", "archive", "delete"):
            with pytest.raises(HTTPException) as exc:
                if op == "publish":
                    publish_general_skill("author-weather", "tenant_demo", db, current_user=b)
                elif op == "archive":
                    archive_general_skill("author-weather", "tenant_demo", db, current_user=b)
                else:
                    delete_general_skill("author-weather", "tenant_demo", db, current_user=b)
            assert exc.value.status_code == 403, f"{op} should be 403"


# ---------------------------------------------------------------------------
# A.3  member can manage own gallery skill (all four ops succeed)
# ---------------------------------------------------------------------------
def test_member_can_manage_own_gallery_skill_all_four_ops() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        db.add(a)
        db.commit()

        first = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )
        # overwrite via original_slug (own)
        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                original_slug="author-weather",
                slug="author-weather",
                name="作者更新版本",
                markdown="# 作者更新版本\n",
            ),
            db,
            a,
        )
        assert updated.id == first.id
        assert updated.name == "作者更新版本"

        # publish + archive + delete (own)
        assert publish_general_skill("author-weather", "tenant_demo", db, current_user=a).status == "published"
        assert archive_general_skill("author-weather", "tenant_demo", db, current_user=a).status == "archived"
        result = delete_general_skill("author-weather", "tenant_demo", db, current_user=a)
        assert result == {"status": "hidden", "slug": "author-weather"}
        assert list_general_skills("tenant_demo", db) == []


# ---------------------------------------------------------------------------
# A.4  admin can manage another member's gallery skill, incl. DELETE w/o agent_id
# ---------------------------------------------------------------------------
def test_admin_can_manage_others_gallery_skill_including_delete_without_agent_id() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        db.add(a)
        db.commit()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )

        # admin overwrite via original_slug
        admin_overwrite = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                original_slug="author-weather",
                slug="author-weather",
                name="管理员维护版本",
                markdown="# 管理员维护版本\n",
            ),
            db,
            _admin_user(),
        )
        assert admin_overwrite.name == "管理员维护版本"

        assert publish_general_skill("author-weather", "tenant_demo", db, current_user=_admin_user()).status == "published"
        assert archive_general_skill("author-weather", "tenant_demo", db, current_user=_admin_user()).status == "archived"
        # the previously-defective path: admin delete WITHOUT agent_id
        result = delete_general_skill("author-weather", "tenant_demo", db, current_user=_admin_user())
        assert result == {"status": "hidden", "slug": "author-weather"}
        assert list_general_skills("tenant_demo", db) == []


# ---------------------------------------------------------------------------
# A.5  bypass attempts
# ---------------------------------------------------------------------------
def test_import_without_original_slug_reuses_existing_slug_returns_409() -> None:
    """No original_slug but same slug must be a 409 conflict, never a silent overwrite."""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        b = _member("user_b", "other")
        db.add(a)
        db.add(b)
        db.commit()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="已有天气技能",
                slug="weather-zh",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )
        with pytest.raises(HTTPException) as exc:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    name="新导入天气技能",
                    slug="weather-zh",
                    markdown="# 越权内容",
                ),
                db,
                b,
            )
        assert exc.value.status_code == 409
        survived = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == "tenant_demo", GeneralSkill.slug == "weather-zh"
            )
        ).first()
        assert survived.name == "已有天气技能"


def test_publish_to_gallery_of_others_private_skill_returns_403() -> None:
    """publish-to-gallery keeps the owner check even after decoupling."""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        priv_agent = AgentProfile(
            id="agent_priv", tenant_id="tenant_demo", name="私有员工",
            is_overall=False, metadata_json={"owner_user_id": "user_a"},
        )
        a = _member("user_a", "author")
        b = _member("user_b", "other")
        db.add(priv_agent)
        db.add(a)
        db.add(b)
        db.commit()

        # A creates a PRIVATE skill under agent_priv
        private = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="私有技能",
                slug="private-skill",
                agent_id="agent_priv",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )
        # B (different member) tries to publish A's private skill to gallery
        with pytest.raises(HTTPException) as exc:
            publish_general_skill_to_gallery(
                private.slug, "tenant_demo", "agent_priv", db, current_user=b
            )
        assert exc.value.status_code == 403


def test_import_package_cannot_overwrite_via_slug_reuse() -> None:
    """import-package / import-skillhub always mint a unique slug, so they can't
    clobber another member's gallery row by reusing the same slug."""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        _add_overall(db)
        a = _member("user_a", "author")
        b = _member("user_b", "other")
        db.add(a)
        db.add(b)
        db.commit()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="已有广场技能",
                slug="shared-slug",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )

        content = base64_md("# 借名覆盖\n")
        uploaded = import_general_skill_package(
            GeneralSkillPackageUploadRequest(
                tenant_id="tenant_demo",
                filename="skill.md",
                content_base64=content,
                slug="shared-slug",
            ),
            db,
            b,
        )
        # slug must be made unique, NOT overwrite the existing gallery row
        assert uploaded.slug != "shared-slug"
        rows = db.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == "tenant_demo")
        ).all()
        assert len(rows) == 2
        original = next(r for r in rows if r.slug == "shared-slug")
        assert original.name == "已有广场技能"


# ---------------------------------------------------------------------------
# A.6  downgrade: tenant WITHOUT is_overall agent
# ---------------------------------------------------------------------------
def test_member_can_create_without_overall_agent_but_list_is_empty() -> None:
    """When no overall agent exists, the implicit gallery host is missing.
    A member can still POST /import (row is created), but without the overall
    binding the gallery list filters it out -> list empty. Reported honestly."""
    with _test_session() as db:
        _seed_minimal_tenant(db)  # NOTE: no overall agent added
        a = _member("user_a", "author")
        db.add(a)
        db.commit()

        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="无宿主天气技能",
                slug="orphan-weather",
                markdown=SKILL_A_MD,
            ),
            db,
            a,
        )
        # import itself succeeds
        assert created.slug == "orphan-weather"

        # but with no overall agent the gallery binding is never created and
        # is_open_gallery_resource() short-circuits to False -> list empty
        listed = list_general_skills("tenant_demo", db)
        assert listed == [], "gallery list is empty without an overall agent host"


def base64_md(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")
