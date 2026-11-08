"""渠道功能移除后的回归：SQLModel 元数据里不应再注册任何 `channel_*` 表，
否则新库会凭空建出 8 张空表。

老库残留表的清理迁移已随历史迁移层一并下线（新库由 create_all 直接建，
不会再产生这些表），相关用例同步移除。
"""

import app.db.models  # noqa: F401  注册全部模型
from sqlmodel import SQLModel


def test_no_channel_tables_registered_in_metadata() -> None:
    registered = sorted(name for name in SQLModel.metadata.tables if name.startswith("channel_"))
    assert registered == []
