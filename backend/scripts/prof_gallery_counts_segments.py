"""gallery_counts 内部逐段计时。"""
from __future__ import annotations

import time

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    ModelConfig,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
from app.security.permissions import is_admin_user

TENANT = "tenant_demo"
N_RESOURCES = 60
N_BINDINGS_ALL = 300


def main() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=TENANT, name="Demo"))
        admin = User(id="u", tenant_id=TENANT, username="u", password_hash=hash_password("x"), role="admin")
        db.add(admin)
        db.add(ModelConfig(tenant_id=TENANT, name="m", api_key_encrypted=encrypt_secret("k"), model="m", is_default=True, enabled=True))
        db.add(AgentProfile(id="overall", tenant_id=TENANT, name="整体", is_overall=True, status="active"))
        for i in range(5):
            db.add(AgentProfile(id=f"agent{i}", tenant_id=TENANT, name=f"员工{i}", status="active", metadata_json={"published_to_gallery": True}))
        db.commit()

        all_rows = []
        for i in range(N_RESOURCES):
            s = Skill(tenant_id=TENANT, skill_id=f"s{i}", name=f"SOP{i}", content_json={}, status="published")
            g = GeneralSkill(tenant_id=TENANT, slug=f"g{i}", name=f"技能{i}", skill_markdown="x" * 100, status="published")
            k = KnowledgeBase(tenant_id=TENANT, name=f"库{i}", status="active")
            t = Tool(tenant_id=TENANT, name=f"t{i}", method="GET", url="https://x", enabled=True)
            all_rows += [("skill", s), ("general_skill", g), ("knowledge_base", k), ("tool", t)]
            db.add_all((s, g, k, t))
        db.commit()
        for total, (rtype, row) in enumerate(all_rows):
            db.add(AgentResourceBinding(tenant_id=TENANT, agent_id="overall", resource_type=rtype, resource_id=row.id, status="active"))
            if total < N_BINDINGS_ALL:
                db.add(AgentResourceBinding(tenant_id=TENANT, agent_id=f"agent{total % 5}", resource_type=rtype, resource_id=row.id, status="active"))
        db.commit()

        from app.api.agents import _agent_hidden_from_staffdeck, _agent_visible_to_user
        from app.agents.branching import (
            build_binding_visibility_prefetch,
            is_open_gallery_resource,
        )
        from app.api.knowledge_bases import _knowledge_base_stats

        def _t(label, fn):
            s = time.perf_counter()
            r = fn()
            print(f"{label}: {(time.perf_counter() - s) * 1000:.1f} ms")
            return r

        _t("_tenant_bindings", lambda: db.exec(select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == TENANT)).all())
        bindings = db.exec(select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == TENANT)).all()
        prefetch = _t("build_prefetch", lambda: build_binding_visibility_prefetch(db, TENANT, bindings))
        kb_stats = _t("kb_stats", lambda: _knowledge_base_stats(db, TENANT))

        # agents 判定
        _t("_tenant_bindings_first", lambda: db.exec(select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == TENANT)).all())
        t1 = time.perf_counter()
        bindings = db.exec(select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == TENANT)).all()
        t2 = time.perf_counter()
        prefetch = build_binding_visibility_prefetch(db, TENANT, bindings)
        t3 = time.perf_counter()
        kb_stats = _knowledge_base_stats(db, TENANT)
        t4 = time.perf_counter()
        _agents_rows = db.exec(select(AgentProfile).where(AgentProfile.tenant_id == TENANT)).all()
        t5 = time.perf_counter()
        agents_count = sum(
            1
            for row in _agents_rows
            if not row.is_overall
            and not _agent_hidden_from_staffdeck(row)
            and (is_admin_user(admin) or _agent_visible_to_user(row, admin))
        )
        t6 = time.perf_counter()
        kb_rows = db.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT)).all()
        t7 = time.perf_counter()
        kb_visible = [
            row for row in kb_rows
            if row.status == "active"
            and is_open_gallery_resource(db, TENANT, "knowledge_base", row, prefetch=prefetch)
            and not (row.name == "默认知识库" and kb_stats.get(row.id, {}).get("document_count", 0) == 0
                     and kb_stats.get(row.id, {}).get("bucket_count", 0) == 0
                     and kb_stats.get(row.id, {}).get("chunk_count", 0) == 0)
        ]
        t8 = time.perf_counter()
        gs_rows = db.exec(select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT)).all()
        t9 = time.perf_counter()
        gs_visible = [row for row in gs_rows if row.status == "published" and is_open_gallery_resource(db, TENANT, "general_skill", row, prefetch=prefetch)]
        t10 = time.perf_counter()
        sk_rows = db.exec(select(Skill).where(Skill.tenant_id == TENANT)).all()
        t11 = time.perf_counter()
        sk_visible = [row for row in sk_rows if row.status == "published" and is_open_gallery_resource(db, TENANT, "skill", row, prefetch=prefetch)]
        t12 = time.perf_counter()
        tool_rows = db.exec(select(Tool).where(Tool.tenant_id == TENANT)).all()
        t13 = time.perf_counter()
        tool_visible = [row for row in tool_rows if row.enabled and is_open_gallery_resource(db, TENANT, "tool", row, prefetch=prefetch)]
        t14 = time.perf_counter()

        def ms(a, b): return f"{(b - a) * 1000:.1f} ms"
        print(f"q_bindings: {ms(t1, t2)}  build_prefetch: {ms(t2, t3)}  kb_stats: {ms(t3, t4)}")
        print(f"q_agents: {ms(t4, t5)}  judge_agents: {ms(t5, t6)}")
        print(f"q_kb: {ms(t6, t7)}  judge_kb: {ms(t7, t8)}")
        print(f"q_gs: {ms(t8, t9)}  judge_gs: {ms(t9, t10)}")
        print(f"q_skills: {ms(t10, t11)}  judge_skills: {ms(t11, t12)}")
        print(f"q_tools: {ms(t12, t13)}  judge_tools: {ms(t13, t14)}")

        from app.api.gallery import gallery_counts as _gc
        _t("full_call_1st", lambda: _gc(tenant_id=TENANT, db=db, current_user=admin))
        _t("full_call_2nd", lambda: _gc(tenant_id=TENANT, db=db, current_user=admin))
        _profile_full(db, admin)


def _profile_full(db, admin):
    import time, cProfile, pstats, io
    from app.api.gallery import gallery_counts as _gc
    prof = cProfile.Profile()
    prof.enable()
    _gc(tenant_id=TENANT, db=db, current_user=admin)
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(18)
    print(buf.getvalue())


if __name__ == "__main__":
    main()
