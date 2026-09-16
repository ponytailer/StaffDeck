"""一次性探针：为 tenant_demo 的某个用户生成合法 token（只读，不改数据）。"""
import sys

from sqlmodel import Session, select

from app.db import engine
from app.db.models import User
from app.security.auth import create_access_token


def main() -> None:
    username = sys.argv[1] if len(sys.argv) > 1 else "huangsong"
    with Session(engine) as s:
        user = s.exec(select(User).where(User.username == username)).first()
        if user is None:
            users = s.exec(select(User).where(User.tenant_id == "tenant_demo")).all()
            print(f"user {username!r} not found; tenant_demo users:")
            for u in users:
                print(f"  {u.username!r} id={u.id} is_admin={getattr(u, 'is_tenant_admin', None)}")
            return
        print(create_access_token(user))


if __name__ == "__main__":
    main()
