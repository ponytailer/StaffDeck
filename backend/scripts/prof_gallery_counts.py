"""gallery_counts 性能排查基准（本地，不给生产库跑）。

用法：backend/.venv/bin/python scripts/prof_gallery_counts.py
构造 N 个绑定 / 资源，逐段计时，找最慢的环节。
"""
from __future__ import annotations

import time

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    Tool,
)
from app.security.auth import hash_password

TENANT = "tenant_demo"
N_RESOURCES = 60  # 每类资源数量
N_BINDINGS_ALL = 300  # 租户绑定总数（其他 agent 也各挂一些）


def main() -> None:
    from app.db.models import ModelConfig, Tenant, User
    from app.security.encryption import encrypt_secret

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=TENANT, name="Demo"))
        db.add(User(id="u", tenant_id=TENANT, username="u", password_hash=hash_password("x")))
        db.add(ModelConfig(tenant_id=TENANT, name="m", api_key_encrypted=encrypt_secret("k"), model="m", is_default=True, enabled=True))
        db.add(AgentProfile(id="overall", tenant_id=TENANT, name="整体", is_overall=True, status="active"))
        for i in range(5):
            db.add(AgentProfile(id=f"agent{i}", tenant_id=TENANT, name=f"员工{i}", status="active", metadata_json={"published_to_gallery": True}))
        db.commit()

        skills = []
        gskills = []
        kbs = []
        tools = []
        for i in range(N_RESOURCES):
            s = Skill(tenant_id=TENANT, skill_id=f"s{i}", name=f"SOP{i}", content_json={}, status="published")
            g = GeneralSkill(tenant_id=TENANT, slug=f"g{i}", name=f"技能{i}", skill_markdown="x" * 100, status="published")
            k = KnowledgeBase(tenant_id=TENANT, name=f"库{i}", status="active")
            t = Tool(tenant_id=TENANT, name=f"t{i}", method="GET", url="https://x", enabled=True)
            skills.append(s); gskills.append(g); kbs.append(k); tools.append(t)
            db.add_all((s, g, k, t))
        db.commit()
        all_rows = []
        for row in skills: all_rows.append(("skill", row))
        for row in gskills: all_rows.append(("general_skill", row))
        for row in kbs: all_rows.append(("knowledge_base", row))
        for row in tools: all_rows.append(("tool", row))
        # overall 全量 + 散布到其他 agent
        total = 0
        for rtype, row in all_rows:
            db.add(AgentResourceBinding(tenant_id=TENANT, agent_id="overall", resource_type=rtype, resource_id=row.id, status="active"))
            total += 1
            if total < N_BINDINGS_ALL:
                db.add(AgentResourceBinding(tenant_id=TENANT, agent_id=f"agent{total % 5}", resource_type=rtype, resource_id=row.id, status="active"))
        db.commit()

        from app.api.gallery import gallery_counts, _tenant_bindings
        from app.agents.branching import build_binding_visibility_prefetch
        from app.api.knowledge_bases import _knowledge_base_stats
        from app.db.models import User as _User

        user = db.exec(select(_User).where(_User.id == "u")).one()
        user.role = "admin"
        db.add(user)
        db.commit()

        import time as _t

        def _time(label, fn):
            s = _t.perf_counter()
            r = fn()
            print(f"{label}: {(_t.perf_counter() - s) * 1000:.1f} ms")
            return r

        bindings = _time("_tenant_bindings", lambda: _tenant_bindings(db, TENANT))
        prefetch = _time("build_prefetch", lambda: build_binding_visibility_prefetch(db, TENANT, bindings))
        kb_stats = _time("kb_stats", lambda: _knowledge_base_stats(db, TENANT))
        _time("gallery_counts_total", lambda: gallery_counts(tenant_id=TENANT, db=db, current_user=user))

        # 逐模块可见性判定耗时（prefectd 已热身）
        from app.agents.branching import is_open_gallery_resource
        resources = {
            "skill": db.exec(select(Skill).where(Skill.tenant_id == TENANT)).all(),
            "general_skill": db.exec(select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT)).all(),
            "knowledge_base": db.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT)).all(),
            "tool": db.exec(select(Tool).where(Tool.tenant_id == TENANT)).all(),
        }
        for rtype, rows in resources.items():
            s = _t.perf_counter()
            for row in rows:
                is_open_gallery_resource(db, TENANT, rtype, row, prefetch=prefetch)
            print(f"judge_{rtype}({len(rows)}): {(_t.perf_counter() - s) * 1000:.1f} ms")


if __name__ == "__main__":
    main()
