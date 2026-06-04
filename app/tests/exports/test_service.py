import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.core.exceptions import APIError, ConflictError, ValidationError
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.exports.schemas import ExportCreate
from app.exports.service import ExportService
from app.todo.models import Todo


class MockAsyncIterator:
    def __init__(self, items):
        self.items = items
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        val = self.items[self.index]
        self.index += 1
        return val


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.session = AsyncMock()
    return repo


@pytest.fixture
def service(mock_repo):
    return ExportService(repository=mock_repo)


@pytest.mark.asyncio
async def test_create_export_invalid_module(service):
    export_in = ExportCreate(format=ExportFormat.json, modules=["invalid-module"])
    audit_logger = AsyncMock()
    with pytest.raises(ValidationError) as exc:
        await service.create_export(
            workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
        )
    assert "Unsupported export module" in exc.value.detail


@pytest.mark.asyncio
async def test_create_export_empty_modules(service):
    export_in = ExportCreate(format=ExportFormat.json, modules=[])
    audit_logger = AsyncMock()
    with pytest.raises(ValidationError) as exc:
        await service.create_export(
            workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
        )
    assert "At least one export module must be selected" in exc.value.detail


@pytest.mark.asyncio
async def test_create_export_conflict_pending(service, mock_repo):
    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = ExportRecord(
        workspace_id=1, requested_by=2, format=ExportFormat.json
    )

    with pytest.raises(ConflictError) as exc:
        await service.create_export(
            workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
        )
    assert "A pending export already exists" in exc.value.detail


@pytest.mark.asyncio
async def test_create_export_limit_exceeded(service, mock_repo):
    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None

    service._count_module_rows = AsyncMock(return_value=5001)

    with pytest.raises(APIError) as exc:
        await service.create_export(
            workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
        )
    assert exc.value.status_code == 413
    assert "exceeds synchronous export limit" in exc.value.detail


@pytest.mark.asyncio
async def test_create_export_json_success(service, mock_repo, monkeypatch):
    monkeypatch.setattr(settings, "EXPORT_STORAGE_BACKEND", "db")
    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None

    service._count_module_rows = AsyncMock(return_value=1)

    todo_item = Todo(id=1, workspace_id=1, user_id=2, title="Test Todo")
    mock_repo.session.stream_scalars.return_value = MockAsyncIterator([todo_item])

    record_id = uuid.uuid4()
    mock_record = ExportRecord(
        id=123,
        public_id=record_id,
        workspace_id=1,
        requested_by=2,
        format=ExportFormat.json,
        scope={"modules": ["todo"]},
        status=ExportStatus.pending,
    )
    mock_repo.create.return_value = mock_record
    mock_repo.save.side_effect = lambda r, refresh=True: r

    result = await service.create_export(
        workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
    )

    assert result.status == ExportStatus.ready
    assert result.artifact_mime_type == "application/json"
    assert result.artifact_filename == "lifestack-export.json"
    assert result.storage_key == f"db://exports/{record_id}"

    content = json.loads(result.artifact_blob.decode("utf-8"))
    assert content["workspace_id"] == 1
    assert content["data"]["todo"]["todos"][0]["title"] == "Test Todo"


@pytest.mark.asyncio
async def test_create_export_local_backend_success(service, mock_repo, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EXPORT_STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "EXPORT_LOCAL_PATH", str(tmp_path))

    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None

    service._count_module_rows = AsyncMock(return_value=1)

    todo_item = Todo(id=1, workspace_id=1, user_id=2, title="Test Todo")
    mock_repo.session.stream_scalars.return_value = MockAsyncIterator([todo_item])

    record_id = uuid.uuid4()
    mock_record = ExportRecord(
        id=123,
        public_id=record_id,
        workspace_id=1,
        requested_by=2,
        format=ExportFormat.json,
        scope={"modules": ["todo"]},
        status=ExportStatus.pending,
    )
    mock_repo.create.return_value = mock_record
    mock_repo.save.side_effect = lambda r, refresh=True: r

    result = await service.create_export(
        workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
    )

    assert result.status == ExportStatus.ready
    assert result.storage_key.startswith("local://")
    local_path = Path(result.storage_key[8:])
    assert local_path.exists()

    with open(local_path, encoding="utf-8") as f:
        content = json.load(f)
    assert content["data"]["todo"]["todos"][0]["title"] == "Test Todo"


@pytest.mark.asyncio
async def test_create_export_s3_backend_success(service, mock_repo, monkeypatch):
    monkeypatch.setattr(settings, "EXPORT_STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "EXPORT_S3_ENDPOINT", "http://mock-s3")
    monkeypatch.setattr(settings, "EXPORT_S3_BUCKET", "mock-bucket")
    monkeypatch.setattr(settings, "EXPORT_S3_ACCESS_KEY", "mock-key")
    monkeypatch.setattr(settings, "EXPORT_S3_SECRET_KEY", "mock-secret")

    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None

    service._count_module_rows = AsyncMock(return_value=1)

    todo_item = Todo(id=1, workspace_id=1, user_id=2, title="Test Todo")
    mock_repo.session.stream_scalars.return_value = MockAsyncIterator([todo_item])

    record_id = uuid.uuid4()
    mock_record = ExportRecord(
        id=123,
        public_id=record_id,
        workspace_id=1,
        requested_by=2,
        format=ExportFormat.json,
        scope={"modules": ["todo"]},
        status=ExportStatus.pending,
    )
    mock_repo.create.return_value = mock_record
    mock_repo.save.side_effect = lambda r, refresh=True: r

    mock_s3_client = MagicMock()
    monkeypatch.setattr(service, "_get_s3_client", lambda: (mock_s3_client, "mock-bucket"))

    result = await service.create_export(
        workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
    )

    assert result.status == ExportStatus.ready
    assert result.storage_key == f"s3://mock-bucket/exports/1/{record_id}/lifestack-export.json"
    assert mock_s3_client.upload_fileobj.call_count == 1
