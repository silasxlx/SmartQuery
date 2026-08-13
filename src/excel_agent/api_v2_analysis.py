"""阶段2问数、分析、联表和任务知识API。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .api_v2 import get_container
from .errors import AppError
from .infrastructure.join_engine import suggest_join
from .runtime import AppContainer
from .security import validate_upload_filename

router = APIRouter(prefix="/api/v2", tags=["v2-analysis"])


class ChatV2Request(BaseModel):
    message: str = Field(default="", max_length=4000)
    dataset_id: str
    clarification_id: str | None = None
    analysis_id: str | None = None
    confirm: bool | None = None
    physical_field_id: str | None = None
    draft_version: int | None = None
    metric_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    formula: str | None = Field(default=None, max_length=1000)
    unit: str | None = Field(default=None, max_length=100)


class JoinSuggestionRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str


class JoinCreateRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str
    left_keys: list[str]
    right_keys: list[str]
    join_type: str = "inner"
    display_name: str = Field(default="联表Dataset", max_length=200)


def _event_line(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/tasks/{task_id}/chat")
async def chat_v2(
    task_id: str,
    request: ChatV2Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    response = {
        "confirm": request.confirm,
        "physical_field_id": request.physical_field_id,
        "draft_version": request.draft_version,
        "metric_id": request.metric_id,
        "name": request.name,
        "formula": request.formula,
        "unit": request.unit,
        "message": request.message,
        "analysis_id": request.analysis_id,
    }
    return await container.analysis_service.submit(
        task_id,
        dataset_id=request.dataset_id,
        question=request.message,
        clarification_id=request.clarification_id,
        analysis_id=request.analysis_id,
        response=response,
    )


@router.post("/tasks/{task_id}/chat/stream")
async def chat_v2_stream(
    task_id: str,
    request: ChatV2Request,
    http_request: Request,
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    response = {
        "confirm": request.confirm,
        "physical_field_id": request.physical_field_id,
        "draft_version": request.draft_version,
        "metric_id": request.metric_id,
        "name": request.name,
        "formula": request.formula,
        "unit": request.unit,
        "message": request.message,
        "analysis_id": request.analysis_id,
    }

    async def generate():
        stream_analysis_id = request.analysis_id
        try:
            async for event in container.analysis_service.stream_events(
                task_id,
                dataset_id=request.dataset_id,
                question=request.message,
                clarification_id=request.clarification_id,
                analysis_id=request.analysis_id,
                response=response,
            ):
                data = dict(event["data"])
                if event["event"] == "started":
                    stream_analysis_id = data.get("analysis_id") or stream_analysis_id
                if event["event"] == "error":
                    data.setdefault(
                        "request_id", getattr(http_request.state, "request_id", "unknown")
                    )
                yield _event_line(event["event"], data)
        except AppError as exc:
            yield _event_line(
                "error",
                {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": getattr(http_request.state, "request_id", "unknown"),
                },
            )
            yield _event_line(
                "done",
                {"analysis_id": stream_analysis_id, "status": "failed"},
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/tasks/{task_id}/analyses/{analysis_id}")
async def get_analysis(
    task_id: str,
    analysis_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.analysis_service.snapshot(task_id, analysis_id)


@router.post("/tasks/{task_id}/analyses/{analysis_id}/cancel")
async def cancel_analysis(
    task_id: str,
    analysis_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.analysis_service.cancel(task_id, analysis_id)


@router.delete("/tasks/{task_id}/analyses/{analysis_id}")
async def delete_analysis(
    task_id: str,
    analysis_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    container.analysis_service.delete(task_id, analysis_id)
    return {"analysis_id": analysis_id, "status": "deleted"}


@router.get("/tasks/{task_id}/analyses")
async def list_analyses(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return {
        "analyses": [item.to_dict() for item in container.task_repository.list_analyses(task_id)]
    }


@router.post("/tasks/{task_id}/join-suggestions")
async def join_suggestions(
    task_id: str,
    request: JoinSuggestionRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    left = container.dataset_store.frame(task_id, request.left_dataset_id)
    right = container.dataset_store.frame(task_id, request.right_dataset_id)
    return suggest_join(
        left,
        right,
        left_id=request.left_dataset_id,
        right_id=request.right_dataset_id,
    ).to_dict()


@router.post("/tasks/{task_id}/joins")
async def create_join(
    task_id: str,
    request: JoinCreateRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.create_joined_dataset(
        task_id,
        left_dataset_id=request.left_dataset_id,
        right_dataset_id=request.right_dataset_id,
        left_keys=request.left_keys,
        right_keys=request.right_keys,
        join_type=request.join_type,
        display_name=request.display_name,
    )


@router.get("/tasks/{task_id}/knowledge/documents")
async def list_knowledge_documents(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    container.task_repository.require(task_id)
    return {
        "documents": [
            item.to_dict(include_content=False) for item in container.knowledge_store.list(task_id)
        ]
    }


@router.post("/tasks/{task_id}/knowledge/documents")
async def upload_knowledge_document(
    task_id: str,
    file: UploadFile = File(...),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.task_repository.task_lock(task_id):
        validate_upload_filename(
            file.filename,
            allowed_extensions={".md", ".txt", ".markdown"},
        )
        content_bytes = await file.read(container.config.runtime.max_upload_bytes + 1)
        if len(content_bytes) > container.config.runtime.max_upload_bytes:
            raise AppError("UPLOAD_LIMIT_EXCEEDED", "知识文档超过大小限制", 413)
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError("KNOWLEDGE_ENCODING_INVALID", "知识文档必须使用UTF-8", 422) from exc
        document = container.knowledge_store.add(
            task_id,
            source_name=file.filename or "knowledge.md",
            content=content,
        )
        return document.to_dict(include_content=False)


@router.get("/tasks/{task_id}/knowledge/documents/{document_id}")
async def get_knowledge_document(
    task_id: str,
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.knowledge_store.get(task_id, document_id).to_dict()


@router.delete("/tasks/{task_id}/knowledge/documents/{document_id}")
async def delete_knowledge_document(
    task_id: str,
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.task_repository.task_lock(task_id):
        container.knowledge_store.delete(task_id, document_id)
    return {"document_id": document_id, "status": "deleted"}


@router.get("/tasks/{task_id}/semantic-metrics")
async def list_semantic_metrics(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    task = container.task_repository.require(task_id)
    metrics = [member.to_dict() for member in container.semantic_catalog.metric_definitions()]
    return {"global_metrics": metrics, "task_metrics": list(task.semantic_extensions)}


@router.delete("/tasks/{task_id}/semantic-metrics/{metric_id}")
async def delete_semantic_metric(
    task_id: str,
    metric_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.task_repository.task_lock(task_id):
        task = container.task_repository.require(task_id)
        before = len(task.semantic_extensions)
        task.semantic_extensions = [
            item for item in task.semantic_extensions if item.get("metric_id") != metric_id
        ]
        if len(task.semantic_extensions) == before:
            raise AppError("SEMANTIC_METRIC_NOT_FOUND", "任务临时指标不存在", 404)
        task.touch()
    return {"metric_id": metric_id, "status": "deleted"}


__all__ = ["router"]
