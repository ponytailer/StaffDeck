"""个人静态资源（上传 / 列表 / 删除 / 公开下载）的回归测试。

全部走内存 SQLite + 临时数据目录，不碰真实库与真实磁盘。

重点覆盖四类容易回归的点：
1. 地址安全 —— 路径穿越文件名、URL 里的 filename 段不参与定位；
2. 类型安全 —— html/svg 必须降级 octet-stream，下载必须带 attachment + nosniff；
3. 删除语义 —— 软删后 410（不是 404），磁盘文件无人引用才真正删除；
4. 配额 —— 超限在写盘前就被拒。
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.datastructures import UploadFile

from app.api import static_assets as api
from app.db.models import StaticAsset, Tenant, User
from app.security.auth import hash_password
from app.static_assets import store

TENANT = "tenant_demo"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=TENANT, name="Demo"))
        db.add(
            User(
                id="user_a",
                tenant_id=TENANT,
                username="user_a",
                password_hash=hash_password("x"),
                role="member",
            )
        )
        db.add(
            User(
                id="user_b",
                tenant_id=TENANT,
                username="user_b",
                password_hash=hash_password("x"),
                role="member",
            )
        )
        db.commit()
    return engine


def _user(engine, user_id: str) -> User:
    with Session(engine) as db:
        return db.get(User, user_id)


def _upload(engine, user_id: str, filename: str, data: bytes):
    with Session(engine) as db:
        return asyncio.run(
            api.upload_static_asset(
                tenant_id=TENANT,
                file=UploadFile(file=io.BytesIO(data), filename=filename),
                db=db,
                current_user=_user(engine, user_id),
            )
        )


def _list(engine, user_id: str, mine: bool = True):
    with Session(engine) as db:
        return api.list_static_assets(
            tenant_id=TENANT, mine=mine, db=db, current_user=_user(engine, user_id)
        ).items


def _delete(engine, user_id: str, asset_id: str):
    with Session(engine) as db:
        return api.delete_static_asset(
            asset_id=asset_id, tenant_id=TENANT, db=db, current_user=_user(engine, user_id)
        )


def _download(engine, token: str, filename: str = "any.bin"):
    with Session(engine) as db:
        return api.download_static_asset(token=token, filename=filename, db=db)


# ---------------------------------------------------------------- 上传


def test_upload_returns_short_token_and_relative_path(env) -> None:
    row = _upload(env, "user_a", "a.txt", b"hello")
    assert row.filename == "a.txt"
    assert row.size == 5
    assert row.content_type == "text/plain"
    # 12 字符短 token：这个地址要粘进知识库、被模型复述，太长会抄错。
    assert len(row.token) if hasattr(row, "token") else True
    assert row.path.startswith("/f/")
    assert row.path.endswith("/a.txt")
    token = row.path.split("/")[2]
    assert 10 <= len(token) <= 16


def test_upload_same_content_reuses_record(env) -> None:
    first = _upload(env, "user_a", "a.txt", b"same bytes")
    second = _upload(env, "user_a", "renamed.txt", b"same bytes")
    assert first.id == second.id
    # 磁盘只存一份（内容寻址）
    path = store.static_asset_path(TENANT, first.sha256)
    assert path.is_file()


def test_upload_rejects_empty_and_oversized(env, monkeypatch) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as empty:
        _upload(env, "user_a", "a.txt", b"")
    assert empty.value.status_code == 400

    monkeypatch.setattr(api, "STATIC_ASSET_MAX_BYTES", 8)
    with pytest.raises(HTTPException) as big:
        _upload(env, "user_a", "b.txt", b"0123456789")
    assert big.value.status_code == 413


def test_upload_sanitises_path_traversal_filename(env) -> None:
    row = _upload(env, "user_a", "../../../etc/passwd", b"x")
    assert row.filename == "passwd"
    assert ".." not in row.path
    assert "/etc/" not in row.path


def test_dangerous_content_type_is_downgraded(env) -> None:
    html = _upload(env, "user_a", "x.html", b"<script>alert(1)</script>")
    svg = _upload(env, "user_a", "x.svg", b"<svg onload=alert(1)/>")
    js = _upload(env, "user_a", "x.js", b"alert(1)")
    for row in (html, svg, js):
        assert row.content_type == "application/octet-stream"


def test_upload_enforces_user_quota_before_writing(env, monkeypatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(api, "STATIC_ASSET_USER_QUOTA_BYTES", 10)
    _upload(env, "user_a", "a.txt", b"12345678")
    # 内容必须和上一个不同，否则会命中「重复上传复用」分支直接返回，压根走不到配额校验。
    with pytest.raises(HTTPException) as quota:
        _upload(env, "user_a", "b.txt", b"87654321")
    assert quota.value.status_code == 413
    # 被拒的那次不应留下磁盘文件
    assert store.read_static_asset(TENANT, store.sha256_of(b"87654321")) is None


# ---------------------------------------------------------------- 下载


def test_download_serves_bytes_with_safety_headers(env) -> None:
    row = _upload(env, "user_a", "手册.txt", b"payload")
    token = row.path.split("/")[2]
    response = _download(env, token, "任意名字.txt")

    assert response.body == b"payload"
    # URL 里的 filename 段被忽略：改名不能让已经发出去的链接失效。
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "UTF-8''" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    # 公开端点刻意不回 CORS：否则任意网页可以 fetch 读取内容。
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}


def test_list_reports_usage_and_limits(env) -> None:
    """列表要带上已用空间与上限，前端才能在上传前把规则讲清楚。"""
    _upload(env, "user_a", "a.txt", b"12345")
    _upload(env, "user_a", "b.txt", b"6789")
    with Session(env) as db:
        payload = api.list_static_assets(
            tenant_id=TENANT, mine=True, db=db, current_user=_user(env, "user_a")
        )
    assert len(payload.items) == 2
    assert payload.used_bytes == 9
    assert payload.user_quota_bytes == api.STATIC_ASSET_USER_QUOTA_BYTES
    assert payload.max_file_bytes == api.STATIC_ASSET_MAX_BYTES


def test_download_counts_and_404_on_unknown_token(env) -> None:
    from fastapi import HTTPException

    row = _upload(env, "user_a", "a.txt", b"x")
    token = row.path.split("/")[2]
    _download(env, token)
    _download(env, token)
    with Session(env) as db:
        assert db.exec(
            __import__("sqlmodel").select(StaticAsset).where(StaticAsset.id == row.id)
        ).first().download_count == 2

    with pytest.raises(HTTPException) as missing:
        _download(env, "nope-token")
    assert missing.value.status_code == 404


# ---------------------------------------------------------------- 删除


def test_delete_is_soft_and_returns_410(env) -> None:
    from fastapi import HTTPException

    row = _upload(env, "user_a", "a.txt", b"content")
    token = row.path.split("/")[2]
    asset_path = store.static_asset_path(TENANT, row.sha256)
    assert asset_path.is_file()

    _delete(env, "user_a", row.id)

    with pytest.raises(HTTPException) as gone:
        _download(env, token)
    # 410 而非 404：能区分「被删了」和「链接本来就错」。
    assert gone.value.status_code == 410
    assert not asset_path.exists()
    assert _list(env, "user_a") == []


def test_delete_keeps_file_when_another_user_shares_it(env) -> None:
    """内容寻址下同一 sha 被多人引用时，删一个不能把文件删掉。"""
    first = _upload(env, "user_a", "a.txt", b"shared bytes")
    second = _upload(env, "user_b", "a.txt", b"shared bytes")
    assert first.id != second.id
    assert first.sha256 == second.sha256

    _delete(env, "user_a", first.id)

    with Session(env) as db:
        assert store.static_asset_path(TENANT, second.sha256).is_file()
        assert db.get(StaticAsset, second.id).status == "active"


def test_delete_rejects_other_users_asset(env) -> None:
    from fastapi import HTTPException

    row = _upload(env, "user_a", "a.txt", b"mine")
    with pytest.raises(HTTPException) as forbidden:
        _delete(env, "user_b", row.id)
    assert forbidden.value.status_code == 403


def test_delete_unknown_asset_returns_404(env) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        _delete(env, "user_a", "sasset_missing")
    assert missing.value.status_code == 404


# ---------------------------------------------------------------- 存储层


def test_store_paths_stay_inside_root_for_hostile_tenant(env) -> None:
    data_root = Path(store._storage_root())
    path = store.static_asset_path("../../etc", "a" * 64)
    assert path.is_relative_to(data_root)
    assert ".." not in str(path)


def test_store_rejects_invalid_sha(env) -> None:
    with pytest.raises(ValueError):
        store.static_asset_path(TENANT, "../../evil")


def test_safe_download_name_strips_control_chars(env) -> None:
    assert store.safe_download_name('a\r\nX-Injected: 1.txt') == "a__X-Injected: 1.txt"
    assert store.safe_download_name("") == "download.bin"
