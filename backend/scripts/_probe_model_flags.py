"""一次性探针：列出 model_configs 的角色标记（只读）。"""

from sqlmodel import Session, select

from app.db import engine
from app.db.models import ModelConfig


def main() -> None:
    with Session(engine) as s:
        rows = s.exec(select(ModelConfig)).all()
        for r in rows:
            flags = []
            if r.is_default:
                flags.append("DEFAULT")
            if r.is_intent_recognition:
                flags.append("INTENT")
            if not r.enabled:
                flags.append("DISABLED")
            print(
                f"name={r.name!r} model={r.model!r} "
                f"user={str(r.user_id or '')[:10]:10s} tenant={r.tenant_id[:10]} "
                f"[{' '.join(flags)}] id={r.id}"
            )


if __name__ == "__main__":
    main()
