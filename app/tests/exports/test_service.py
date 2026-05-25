import io
import json
import uuid
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from app.core.exceptions import APIError, ConflictError, NotFoundError, ValidationError
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.exports.schemas import ExportCreate
from app.exports.service import ExportService


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
async def test_create_export_json_success(service, mock_repo):
    export_in = ExportCreate(format=ExportFormat.json, modules=["todo", "spending"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None

    service._count_module_rows = AsyncMock(return_value=10)

    service._load_module_payload = AsyncMock(
        side_effect=lambda ws_id, mod: {
            "todo": {"todos": [{"id": 1, "title": "Todo Item"}]},
            "spending": {
                "categories": [{"id": 10, "name": "Food"}],
                "transactions": [],
                "budgets": [],
            },
        }.get(mod)
    )

    record_id = uuid.uuid4()
    mock_record = ExportRecord(
        id=123,
        public_id=record_id,
        workspace_id=1,
        requested_by=2,
        format=ExportFormat.json,
        scope={"modules": ["spending", "todo"]},
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
    assert content["data"]["todo"] == {"todos": [{"id": 1, "title": "Todo Item"}]}
    assert content["data"]["spending"]["categories"] == [{"id": 10, "name": "Food"}]

    assert audit_logger.log.call_count == 2


@pytest.mark.asyncio
async def test_create_export_csv_success(service, mock_repo):
    export_in = ExportCreate(format=ExportFormat.csv, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None
    service._count_module_rows = AsyncMock(return_value=5)

    service._load_module_payload = AsyncMock(
        return_value={"todos": [{"id": "1", "title": "T1", "completed": "False"}]}
    )

    record_id = uuid.uuid4()
    mock_record = ExportRecord(
        id=124,
        public_id=record_id,
        workspace_id=1,
        requested_by=2,
        format=ExportFormat.csv,
        scope={"modules": ["todo"]},
        status=ExportStatus.pending,
    )
    mock_repo.create.return_value = mock_record
    mock_repo.save.side_effect = lambda r, refresh=True: r

    result = await service.create_export(
        workspace_id=1, requested_by=2, export_in=export_in, audit_logger=audit_logger
    )

    assert result.status == ExportStatus.ready
    assert result.artifact_mime_type == "application/zip"
    assert result.artifact_filename == "lifestack-export-csv.zip"

    zip_buffer = io.BytesIO(result.artifact_blob)
    with ZipFile(zip_buffer, mode="r") as archive:
        assert "todo/todos.csv" in archive.namelist()
        csv_content = archive.read("todo/todos.csv").decode("utf-8")
        assert "completed,id,title" in csv_content
        assert "False,1,T1" in csv_content


@pytest.mark.asyncio
async def test_create_export_failure_handling(service, mock_repo):
    export_in = ExportCreate(format=ExportFormat.json, modules=["todo"])
    audit_logger = AsyncMock()
    mock_repo.get_pending_for_workspace.return_value = None
    service._count_module_rows = AsyncMock(return_value=5)

    service._load_module_payload = AsyncMock(side_effect=RuntimeError("Database failure"))

    mock_record = ExportRecord(
        id=125,
        public_id=uuid.uuid4(),
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

    assert result.status == ExportStatus.failed
    assert "Database failure" in result.error_message


@pytest.mark.asyncio
async def test_get_export_success(service, mock_repo):
    export_id = uuid.uuid4()
    mock_record = ExportRecord(id=1, public_id=export_id, workspace_id=1)
    mock_repo.get_by_public_id.return_value = mock_record

    res = await service.get_export(1, export_id)
    assert res == mock_record
    mock_repo.get_by_public_id.assert_called_once_with(1, export_id, include_blob=False)


@pytest.mark.asyncio
async def test_get_export_not_found(service, mock_repo):
    export_id = uuid.uuid4()
    mock_repo.get_by_public_id.return_value = None

    with pytest.raises(NotFoundError) as exc:
        await service.get_export(1, export_id)
    assert exc.value.status_code == 404
