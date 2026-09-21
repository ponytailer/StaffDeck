"""full_call 首调 cProfile，定位 4s 去向。"""
from __future__ import annotations

import cProfile
import io
import pstats
import time

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

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

TENANT = "tenant_demo"


def main() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=TENANT, name="Demo"))
        admin = User(id="u", tenant_id=TENANT, username="u", password_hash=hash_password("x"), role="admin")
        db.add(admin)
        db.add(ModelConfig(tenant_id=TENANT, name="m", api_key_encrypted=encrypt_secret("k"), model="m", is_default=True, enabled=True))
        db.add(AgentProfile(id="overall", tenant_id=TENANT, name="整体", is_overall=True, status="active"))
        db.commit()
        for i in range(60):
            db.add_all((
                Skill(tenant_id=TENANT, skill_id=f"s{i}", name=f"S{i}", content_json={}, status="published"),
                GeneralSkill(tenant_id=TENANT, slug=f"g{i}", name=f"G{i}", skill_markdown="x", status="published"),
                KnowledgeBase(tenant_id=TENANT, name=f"K{i}", status="active"),
                Tool(tenant_id=TENANT, name=f"T{i}", method="GET", url="https://x", enabled=True),
            ))
        db.commit()
        from sqlmodel import select
        for rtype, model in (("skill", Skill), ("general_skill", GeneralSkill), ("knowledge_base", KnowledgeBase), ("tool", Tool)):
            for row in db.exec(select(model).where(model.tenant_id == TENANT)).all():
                db.add(AgentResourceBinding(tenant_id=TENANT, agent_id="overall", resource_type=rtype, resource_id=row.id, status="active"))
        db.commit()

        from app.api.gallery import gallery_counts
        import tracemalloc

        steps = []
        t0 = time.perf_counter()
        prof = cProfile.Profile()
        prof.enable()
        counts = gallery_counts(tenant_id=TENANT, db=db, current_user=admin)
        prof.disable()
        t1 = time.perf_counter()
        print("counts:", counts.model_dump())
        print(f"first call wall: {(t1 - t0) * 1000:.1f} ms")
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(15)
        print(buf.getvalue())


if __name__ == "__main__":
    main()
