"""mcp_servers.timeout_seconds 的启动期补列迁移。

背景：MCP 服务器需要能声明「派生工具的默认调用超时」，否则远程 MCP 的慢操作会被
全局 8 秒卡死，而 MCP 子工具在界面上不可单独编辑，等于无解。新表由 create_all 建；
这里固定住给**已存在**的表补列的路径（本地 dev 与线上都是老表）。
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.db import database


def _legacy_engine(tmp_path):
    """造一张没有 timeout_seconds 的旧结构 mcp_servers 表。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-timeout.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE mcp_servers (id VARCHAR PRIMARY KEY, name VARCHAR)"))
        conn.execute(text("INSERT INTO mcp_servers (id, name) VALUES ('legacy', 'genoffice')"))
    return engine


def test_migration_adds_column_and_keeps_legacy_rows_on_the_global_default(
    tmp_path, monkeypatch
) -> None:
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_mcp_server_timeout()

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(mcp_servers)"))}
        assert "timeout_seconds" in columns
        # 历史行保持 NULL = 沿用全局 settings.tool_timeout_seconds，行为不变
        assert conn.execute(text("SELECT timeout_seconds FROM mcp_servers")).scalar_one() is None
        assert conn.execute(text("SELECT name FROM mcp_servers")).scalar_one() == "genoffice"


def test_migration_is_idempotent(tmp_path, monkeypatch) -> None:
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_mcp_server_timeout()
    database._migrate_mcp_server_timeout()

    with engine.begin() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
    assert columns.count("timeout_seconds") == 1


def test_migration_is_a_noop_when_the_table_is_absent(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_mcp_server_timeout()  # 不应抛异常
