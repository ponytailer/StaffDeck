from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.db.database_path import normalize_database_url


def _normalize_database_url(url: str) -> str:
    """Backward-compatible import seam for callers and migration tests."""
    return normalize_database_url(url)


settings = get_settings()

database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    import app.db.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    # 存量表补列。create_all 只建**缺失的表**，不会给已有表加列 ——
    # 本地 dev 与线上都是老表，因此新列一律需要在这里补。
    # 每个函数自身幂等（先查 inspector，列已存在直接返回），
    # 可重复执行。新增「已有表加列」时必须登记到这里，否则线上会
    # 报 no such column（历史教训见 docs/config-flags-audit.md）。
    #
    # ⚠️ 顺序有讲究：任何**用 ORM 读**该表的迁移，都必须排在补列之后 ——
    # ORM 的 SELECT 会把模型里所有列都列出来，列还没加就会
    # `column ... does not exist`。所以纯 DDL 补列的迁移统一放最前面。
    _migrate_agent_kind()
    _migrate_free_model_schema()
    _migrate_ui_config_knowledge_citations()
    _migrate_mcp_server_timeout()
    _migrate_general_skill_category()
    _migrate_general_skill_download_count()
    _migrate_ai_review_schema()
    _migrate_tool_binding_semantics()


def _migrate_tool_binding_semantics() -> None:
    """兼容工具语义迁移：全局 Tool.enabled 已废弃，启用状态下沉到员工绑定。

    历史数据里可能存在：
    - Tool.enabled=False，但员工绑定仍是 active；
    - 私有工具绑定缺少 owner/scope 元数据，导致「我创建的工具」、归属展示失效；
    - 旧逻辑留下的 enabled 标记与新的绑定状态互相矛盾。

    这里做一次幂等回填：
    1. 全局停用的工具 → 所有相关绑定置为 inactive；
    2. 私有绑定补齐 owner_agent_id / scope / 创建者元数据；
    3. Tool.enabled 统一回 true，后续运行时只看 binding.status。
    """

    from app.db.models import AgentProfile, AgentResourceBinding, Tool, utc_now

    with Session(engine) as db:
        tools = list(db.exec(select(Tool)).all())
        if not tools:
            return
        tools_by_id = {tool.id: tool for tool in tools}
        bindings = list(
            db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.resource_type == "tool",
                    AgentResourceBinding.status != "deleted",
                )
            ).all()
        )
        if not bindings:
            return
        agents = {
            agent.id: agent
            for agent in db.exec(
                select(AgentProfile).where(
                    AgentProfile.id.in_({binding.agent_id for binding in bindings})
                )
            ).all()
        }
        # 广场绑定里的创建者信息，可用于给引用它的私有绑定回填真实工具创建者。
        gallery_metadata_by_tool: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            agent = agents.get(binding.agent_id)
            if agent and agent.is_overall:
                gallery_metadata_by_tool[binding.resource_id] = dict(
                    binding.metadata_json or {}
                )

        creator_keys = (
            "owner_user_id",
            "created_by_user_id",
            "owner_username",
            "created_by_username",
            "owner_display_name",
            "created_by_display_name",
        )
        for binding in bindings:
            tool = tools_by_id.get(binding.resource_id)
            if tool is None:
                continue
            changed = False
            metadata = dict(binding.metadata_json or {})
            agent = agents.get(binding.agent_id)
            is_overall_binding = bool(agent and agent.is_overall)
            if is_overall_binding:
                if metadata.get("scope") != "open_gallery":
                    metadata["scope"] = "open_gallery"
                    metadata["visibility"] = "open_gallery"
                    changed = True
            else:
                if not metadata.get("owner_agent_id"):
                    metadata["owner_agent_id"] = binding.agent_id
                    changed = True
                if not metadata.get("scope"):
                    metadata["scope"] = "agent_private"
                    metadata["visibility"] = "agent_private"
                    metadata["created_from_agent"] = True
                    changed = True
                if not metadata.get("owner_user_id") and not metadata.get("created_by_user_id"):
                    gallery_metadata = gallery_metadata_by_tool.get(tool.id, {})
                    if any(gallery_metadata.get(key) for key in creator_keys):
                        for key in creator_keys:
                            if gallery_metadata.get(key) and not metadata.get(key):
                                metadata[key] = gallery_metadata[key]
                                changed = True
                    elif agent is not None:
                        agent_metadata = dict(agent.metadata_json or {})
                        for key in creator_keys:
                            if agent_metadata.get(key) and not metadata.get(key):
                                metadata[key] = agent_metadata[key]
                                changed = True
            # 旧全局开关：工具被 admin 停用时，员工侧绑定也应当停用。
            if not tool.enabled and binding.status == "active":
                binding.status = "inactive"
                changed = True
            if changed:
                binding.metadata_json = metadata
                binding.updated_at = utc_now()
                db.add(binding)

        # 丢弃旧的全局 enabled 语义；之后只由员工绑定控制是否可调用。
        for tool in tools:
            if not tool.enabled:
                tool.enabled = True
                tool.updated_at = utc_now()
                db.add(tool)
        db.commit()


def _migrate_ai_review_schema() -> None:
    """ai_review_tasks 补列 + 进行中任务的唯一约束。

    新表由上面 create_all 直接建；这里是给**已存在**的表补新列 / 索引。
    索引：同一个 PR/MR 同时只允许一条 ``queued``/``running`` 任务
    （应用层在创建 / 重试时已拦一道，这里是并发写入的兜底）。
    """
    inspector = inspect(engine)
    if "ai_review_tasks" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("ai_review_tasks")}
    # PG 用 TIMESTAMP；SQLite 用 DATETIME（宽松类型别名）
    timestamp_type = (
        "TIMESTAMP" if engine.url.get_backend_name().startswith("postgresql") else "DATETIME"
    )
    with engine.begin() as conn:
        if "platform_synced_at" not in columns:
            conn.execute(
                text(f"ALTER TABLE ai_review_tasks ADD COLUMN platform_synced_at {timestamp_type}")
            )
        if "platform_sync_url" not in columns:
            conn.execute(text("ALTER TABLE ai_review_tasks ADD COLUMN platform_sync_url VARCHAR NOT NULL DEFAULT ''"))
    # 部分唯一索引：PG 与 SQLite(>=3.8) 都支持 WHERE 子句。建索引不能与上面的
    # ALTER 共处一个事务——历史库里若已存在重复的进行中任务，失败会连带回滚补列。
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_review_task_in_flight "
                    "ON ai_review_tasks (tenant_id, workspace_id, mr_number) "
                    "WHERE status IN ('queued', 'running')"
                )
            )
    except Exception as exc:  # noqa: BLE001 - 索引建不上不该阻断启动，创建接口仍有应用层校验
        print(
            f"[init_db] uq_ai_review_task_in_flight 创建失败（可能存在重复的进行中任务）：{exc}"
        )


def _migrate_free_model_schema() -> None:
    """全局免费模型补列:model_configs.is_global_free + agent_profiles.free_model_enabled。

    新表 global_free_model_usage 由 create_all 直接建;这里给**已存在**的两张表补新列。
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        if "model_configs" in set(inspector.get_table_names()):
            columns = {column["name"] for column in inspector.get_columns("model_configs")}
            if "is_global_free" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE model_configs ADD COLUMN is_global_free BOOLEAN "
                        f"{'NOT NULL DEFAULT FALSE' if engine.url.get_backend_name().startswith('postgresql') else 'NOT NULL DEFAULT 0'}"
                    )
                )
        if "agent_profiles" in set(inspector.get_table_names()):
            columns = {column["name"] for column in inspector.get_columns("agent_profiles")}
            if "free_model_enabled" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE agent_profiles ADD COLUMN free_model_enabled BOOLEAN "
                        f"{'NOT NULL DEFAULT FALSE' if engine.url.get_backend_name().startswith('postgresql') else 'NOT NULL DEFAULT 0'}"
                    )
                )


def _migrate_ui_config_knowledge_citations() -> None:
    """ui_configs 补 show_knowledge_citations：对话里是否展示「知识来源」引用卡片。

    新表由 create_all 直接建；这里给**已存在**的表补新列。
    默认 TRUE = 保持既有行为（历史租户照旧展示来源卡片）。
    """
    inspector = inspect(engine)
    if "ui_configs" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("ui_configs")}
    if "show_knowledge_citations" in columns:
        return
    default = "TRUE" if engine.url.get_backend_name().startswith("postgresql") else "1"
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE ui_configs ADD COLUMN show_knowledge_citations BOOLEAN "
                f"NOT NULL DEFAULT {default}"
            )
        )


def _migrate_mcp_server_timeout() -> None:
    """mcp_servers 补 timeout_seconds：MCP 派生工具的默认调用超时。

    新表由 create_all 直接建；这里是给**已存在**的表补新列。
    留 NULL 表示沿用全局 ``settings.tool_timeout_seconds``，因此历史数据行为不变。
    没有它时，远程 MCP 的慢操作（渲染、转 PDF、图片生成）会被全局 8 秒卡死，
    而 MCP 子工具在界面上不可单独编辑，等于无解。
    """
    inspector = inspect(engine)
    if "mcp_servers" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("mcp_servers")}
    if "timeout_seconds" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN timeout_seconds FLOAT"))


def _migrate_general_skill_category() -> None:
    """general_skills 补 category：技能广场的标签分类（办公类 / 财务类 / 人力资源类 / 编程类）。

    新表由 create_all 直接建；这里给**已存在**的表补新列，历史技能为空（未分类）。
    """
    inspector = inspect(engine)
    if "general_skills" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("general_skills")}
    if "category" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE general_skills ADD COLUMN category VARCHAR"))


def _migrate_general_skill_download_count() -> None:
    """general_skills 补 download_count：技能包累计下载次数（广场 + 分享链接）。

    新表由 create_all 直接建；这里是给**已存在**的表补新列，历史技能从 0 起算。
    """
    inspector = inspect(engine)
    if "general_skills" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("general_skills")}
    if "download_count" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE general_skills ADD COLUMN download_count INTEGER NOT NULL DEFAULT 0")
        )


def _migrate_agent_kind() -> None:
    """agent_profiles 补 kind：区分普通数字员工（standard）与调度入口（dispatcher）。

    新表 dispatcher_members 由 create_all 直接建；这里只给**已存在**的
    agent_profiles 补 kind 列。带 server_default，老行自动补齐为 standard，
    不需要额外数据回填。
    """
    inspector = inspect(engine)
    if "agent_profiles" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("agent_profiles")}
    if "kind" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE agent_profiles ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'standard'")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_profiles_kind ON agent_profiles (kind)"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
