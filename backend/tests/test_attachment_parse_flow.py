"""聊天 PDF 附件云端解析（MinerU Phase 1）流程测试。

覆盖：任务创建指纹、run_parse_job 成功/失败/暂存缺失/超长截断、
running_job_ids_for_attachments 取最新、turn 侧 _reject_unparsed_attachments
拦截口径、mineru_client._download_markdown 的 zip 解包与空 md 报错。
 rq 调度本身不在此测（与 ingest_jobs 同款兜底，已由既有模式覆盖）。
"""

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AttachmentParseJob, utc_now
from app.session import attachment_parse_jobs, mineru_client
from app.session.attachment_parse_jobs import (
    RUNNING_STATUSES,
    create_parse_job,
    run_parse_job,
    running_job_ids_for_attachments,
)
from app.session.attachment_store import (
    read_staged_parse_result,
    stage_chat_attachment,
)
from app.session.attachments import MAX_EXTRACTED_TEXT_CHARS
from app.session.mineru_client import MinerUError, _download_markdown
from app.session.session_schema import ChatAttachmentRead


@pytest.fixture()
def db_session(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    # run_parse_job 内部用 app.db.engine 开新 Session，测试替换为内存库
    monkeypatch.setattr(attachment_parse_jobs, "engine", engine)
    with Session(engine) as session:
        yield session


def _pdf_attachment(size: int = 1024, attachment_id: str = "file_parse01") -> ChatAttachmentRead:
    return ChatAttachmentRead(
        id=attachment_id,
        filename="合同扫描件.pdf",
        content_type="application/pdf",
        size=size,
        kind="pdf",
        sha256=None,
    )


def _stage_pdf(tmp_dir_key: str = "tenant_demo", user: str = "user_demo",
               attachment_id: str = "file_parse01") -> ChatAttachmentRead:
    data = b"%PDF-1.4 fake scanned pdf bytes"
    attachment = _pdf_attachment(len(data), attachment_id)
    return stage_chat_attachment(
        attachment,
        data,
        tenant_id=tmp_dir_key,
        user_id=user,
    )


def test_create_parse_job_saves_attachment_fingerprint(db_session: Session) -> None:
    staged = _stage_pdf()
    job = create_parse_job(
        db_session,
        staged,
        tenant_id="tenant_demo",
        user_id="user_demo",
    )
    assert job.status == "queued"
    assert job.attachment_id == staged.id
    assert job.metadata_json["filename"] == staged.filename
    assert job.metadata_json["size"] == staged.size
    assert job.metadata_json["sha256"] == staged.sha256


def test_run_parse_job_success_writes_parse_result(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage_pdf()
    job = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")
    progress_events: list[tuple[str, float]] = []

    def fake_parse(data: bytes, filename: str, *, progress_cb=None) -> str:
        assert data.startswith(b"%PDF-1.4")
        assert filename == "合同扫描件.pdf"
        if progress_cb:
            progress_cb("parsing", 40.0)
            progress_cb("parsing", 85.0)
        return "# 合同全文\n\n甲乙双方约定如下条款。"

    monkeypatch.setattr(attachment_parse_jobs, "parse_pdf_bytes", fake_parse)
    run_parse_job(job.id)

    db_session.expire_all()
    refreshed = db_session.get(AttachmentParseJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "succeeded"
    assert refreshed.progress == 100.0
    assert refreshed.metadata_json["text_chars"] == len("# 合同全文\n\n甲乙双方约定如下条款。")
    assert refreshed.metadata_json["truncated"] is False
    markdown = read_staged_parse_result(
        tenant_id="tenant_demo",
        user_id="user_demo",
        attachment_id=staged.id,
    )
    assert markdown is not None and markdown.startswith("# 合同全文")


def test_run_parse_job_records_mineru_error(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage_pdf()
    job = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")

    def fake_parse(data: bytes, filename: str, *, progress_cb=None) -> str:
        raise MinerUError("MinerU 解析失败：页数超限")

    monkeypatch.setattr(attachment_parse_jobs, "parse_pdf_bytes", fake_parse)
    run_parse_job(job.id)

    db_session.expire_all()
    refreshed = db_session.get(AttachmentParseJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert "页数超限" in (refreshed.error or "")


def test_run_parse_job_missing_staging_fails(db_session: Session) -> None:
    attachment = _pdf_attachment()
    job = create_parse_job(db_session, attachment, tenant_id="tenant_demo", user_id="user_demo")
    run_parse_job(job.id)

    db_session.expire_all()
    refreshed = db_session.get(AttachmentParseJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert "重新上传" in (refreshed.error or "")


def test_run_parse_job_truncates_long_markdown(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _stage_pdf()
    job = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")
    long_markdown = "字" * (MAX_EXTRACTED_TEXT_CHARS + 5000)
    monkeypatch.setattr(attachment_parse_jobs, "parse_pdf_bytes", lambda *a, **k: long_markdown)
    run_parse_job(job.id)

    db_session.expire_all()
    refreshed = db_session.get(AttachmentParseJob, job.id)
    assert refreshed is not None
    assert refreshed.status == "succeeded"
    assert refreshed.metadata_json["truncated"] is True
    markdown = read_staged_parse_result(
        tenant_id="tenant_demo",
        user_id="user_demo",
        attachment_id=staged.id,
    )
    assert markdown is not None
    assert len(markdown) <= MAX_EXTRACTED_TEXT_CHARS + 100
    assert "内容已截断" in markdown


def test_running_jobs_prefers_latest_per_attachment(db_session: Session) -> None:
    staged = _stage_pdf()
    old_job = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")
    new_job = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")
    # created_at 同秒会并列，显式错开
    base = datetime.now(UTC).replace(tzinfo=None)
    old_job.created_at = base - timedelta(minutes=5)
    new_job.created_at = base
    old_job.status = "failed"
    new_job.status = "parsing"
    db_session.add(old_job)
    db_session.add(new_job)
    db_session.commit()

    jobs = running_job_ids_for_attachments(
        db_session,
        tenant_id="tenant_demo",
        attachment_ids=[staged.id, "file_missing"],
    )
    assert set(jobs) == {staged.id}
    assert jobs[staged.id].id == new_job.id
    assert jobs[staged.id].status in RUNNING_STATUSES


def test_turn_guard_blocks_running_and_allows_terminal(db_session: Session) -> None:
    from app.api.chat import ChatTurnRequest, _reject_unparsed_attachments

    staged = _stage_pdf()
    running = create_parse_job(db_session, staged, tenant_id="tenant_demo", user_id="user_demo")
    running.status = "parsing"
    running.progress = 42.0
    db_session.add(running)
    db_session.commit()

    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        message="帮我总结这份合同",
        attachments=[staged],
    )
    with pytest.raises(HTTPException) as exc_info:
        _reject_unparsed_attachments(db_session, request)
    assert exc_info.value.status_code == 409
    assert "42%" in str(exc_info.value.detail)

    running.status = "succeeded"
    running.progress = 100.0
    db_session.add(running)
    db_session.commit()
    _reject_unparsed_attachments(db_session, request)  # 不抛异常即通过

    # 解析失败的 PDF 放行：模型可走 extract_document_text 的 L1 引导兜底
    running.status = "failed"
    db_session.add(running)
    db_session.commit()
    _reject_unparsed_attachments(db_session, request)


def test_turn_guard_ignores_non_pdf_attachments(db_session: Session) -> None:
    from app.api.chat import ChatTurnRequest, _reject_unparsed_attachments

    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        message="看图",
        attachments=[
            ChatAttachmentRead(
                id="file_img01",
                filename="图.png",
                content_type="image/png",
                size=10,
                kind="image",
            )
        ],
    )
    _reject_unparsed_attachments(db_session, request)  # 无 PDF：直接通过


def test_download_markdown_prefers_full_md(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("other/layout.md", "# 布局文件")
        zf.writestr("full.md", "# 正式全文")
    payload = archive.getvalue()

    class FakeResponse:
        status_code = 200
        content = payload

    monkeypatch.setattr(mineru_client.requests, "get", lambda url, timeout: FakeResponse())
    assert _download_markdown({"full_zip_url": "https://example.com/x.zip"}) == "# 正式全文"


def test_download_markdown_raises_without_md(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("images/p1.jpg", b"jpg")
    payload = archive.getvalue()

    class FakeResponse:
        status_code = 200
        content = payload

    monkeypatch.setattr(mineru_client.requests, "get", lambda url, timeout: FakeResponse())
    with pytest.raises(MinerUError, match="markdown"):
        _download_markdown({"full_zip_url": "https://example.com/x.zip"})


def test_created_at_defaults_to_utc_now() -> None:
    job = AttachmentParseJob(tenant_id="t", user_id="u", attachment_id="a", filename="f.pdf")
    assert job.created_at is not None
    assert abs((utc_now() - job.created_at).total_seconds()) < 5
