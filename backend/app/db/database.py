from collections.abc import Generator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.db.database_path import normalize_database_url


def _normalize_database_url(url: str) -> str:
    """Backward-compatible import seam for callers and tests."""
    return normalize_database_url(url)


settings = get_settings()

database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    """建表（**唯一**的 schema 动作）。

    这个项目没有 alembic，新库/新表的全部字段与索引都由 ``create_all`` 一次建全
    （模型里已声明 ``server_default`` / ``index=True`` / ``__table_args__``）。

    **历史上那层「给老表逐条补列」的迁移已在 2026-11-08 删除**（当时 8 个迁移：
    agent_profiles.kind / free_model_enabled、model_configs.is_global_free、
    ui_configs.show_knowledge_citations、mcp_servers.timeout_seconds、
    general_skills.category / download_count、ai_review_tasks 两列 + 部分唯一索引，
    以及 Tool 绑定语义的一次性数据回填）。删除前逐项核对过线上库：
    所有列与索引均已在位，回填待处理项为 0。

    ⚠️ 删除后的**代价**（改这里之前先读）：
    - 停留在旧 schema 的库不再自动补齐 —— 例如从很早的备份恢复、或别人的旧环境。
      这类库启动后第一次查到该表会 ``column ... does not exist``。
    - 真遇到了就**手动/一次性脚本** ``ALTER TABLE ... ADD COLUMN ...``（或从新库
      dump 恢复）修一次，**不要**再把「逐条补列」加回启动流程：那样每次启动都在
      猜测 schema，久了没人敢删，且顺序稍错就出现「先读后补列」的 ORM 报错。

    新增「给已有表加列」的正确姿势（本项目现行约定）：
    1. 改模型（带 ``server_default``，老行自动补齐，不需要数据回填）；
    2. 新库由 ``create_all`` 建全，无需额外代码；
    3. 线上已有库用一次性脚本 ALTER（或引入 alembic 后走 revision），
       脚本不进 ``init_db``。
    """
    import app.db.models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
