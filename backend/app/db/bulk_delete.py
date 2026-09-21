from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sa_delete
from sqlmodel import Session


def expunge_matching(db: Session, model: type, **criteria: Any) -> int:
    """把会话里已加载、且命中条件的对象摘出 identity map，返回摘除个数。

    为什么需要它：批量 ``DELETE`` 绕过 ORM。若不处理，已经加载进会话的同批对象
    仍被 ORM 当作 persistent，``commit()`` 会把它们 expire；之后调用方再读属性或
    ``db.get()``，SQLAlchemy 会尝试刷新一行已经不存在的记录，抛
    ``ObjectDeletedError`` / ``DetachedInstanceError``。

    这里用 ``expunge`` 而不是 ``db.delete(obj)``：expunge 保留已加载的属性值，
    与旧实现（逐行 ORM ``db.delete``）对调用方可观察的行为一致，且不会再发一次
    DELETE 造成 StaleDataError。**只有会话里已有的对象**需要处理 —— 不在
    identity map 里的行没有任何人持有引用，直接删掉即可。
    """
    count = 0
    for obj in list(db.identity_map.values()):
        if not isinstance(obj, model):
            continue
        if all(getattr(obj, key, None) == value for key, value in criteria.items()):
            db.expunge(obj)
            count += 1
    return count


def bulk_delete_where(db: Session, model: type, *criteria: Any) -> int:
    """按任意 SQL 条件批量删除，返回删除行数。

    低层原语：条件可以是 ``列 == 值`` 之外的任意表达式（如 ``!=`` / ``in_`` /
    ``notin_``）。不处理 identity map，适用于调用方不会持有该模型对象的场景；
    若调用方可能持有已加载对象，请用 ``bulk_delete_matching``。
    """
    result = db.exec(sa_delete(model).where(*criteria))
    return int(getattr(result, "rowcount", 0) or 0)


def bulk_delete_matching(db: Session, model: type, **criteria: Any) -> int:
    """按 ``列=值`` 条件批量删除，并先把会话内命中对象摘除。返回删除行数。

    替代「SELECT 全量行 → 逐行 db.delete(row)」：后者会把整行（可能含 MB 级 JSON）
    搬进内存只为丢掉。这里只发一条 DELETE，行数由数据库返回。
    """
    expunge_matching(db, model, **criteria)
    return bulk_delete_where(
        db, model, *(getattr(model, key) == value for key, value in criteria.items())
    )
