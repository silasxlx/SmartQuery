"""阶段1 v2任务、上传、Dataset和语义绑定API。

该路由模块只做协议转换和错误映射，业务规则由Stage1Service负责。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field

from .errors import AppError
from .runtime import AppContainer

router = APIRouter(prefix="/api/v2", tags=["v2-task-dataset"])


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise AppError("RUNTIME_NOT_READY", "服务尚未完成初始化", 503)
    if container.shutting_down:
        raise AppError("RUNTIME_SHUTTING_DOWN", "服务正在关闭，暂不接收新请求", 503)
    return container


class TaskCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class DatasetCreateRequest(BaseModel):
    upload_id: str
    object_name: str | None = None
    encoding: str | None = None
    delimiter: str | None = None
    display_name: str | None = Field(default=None, max_length=200)


class NormalizationDecisionRequest(BaseModel):
    decision_id: str
    choice: str


class SemanticBindingDecisionRequest(BaseModel):
    binding_id: str
    physical_field_id: str | None = None
    confirm: bool = True


@router.post("/tasks")
async def create_task(
    request: TaskCreateRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.create_task(request.name)


@router.get("/tasks")
async def list_tasks(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return {
        "tasks": container.stage1_service.list_tasks(),
        "max_tasks": container.stage1_service.repository.max_tasks,
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.stage1_service.get_task(task_id)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    container.task_repository.require(task_id)
    # Let the repository perform its busy-state check before clearing graph
    # checkpoints or cancellation flags.  A running task must remain intact
    # when deletion is rejected with 409.
    await container.stage1_service.delete_task(task_id)
    container.analysis_service.clear_task(task_id)
    container.knowledge_store.delete_task(task_id)
    return {"task_id": task_id, "status": "deleted"}


@router.post("/tasks/{task_id}/uploads")
async def inspect_upload(
    task_id: str,
    file: UploadFile = File(...),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.inspect_upload(task_id, file)


@router.post("/tasks/{task_id}/datasets")
async def create_dataset(
    task_id: str,
    request: DatasetCreateRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.create_dataset(
        task_id,
        upload_id=request.upload_id,
        object_name=request.object_name,
        encoding=request.encoding,
        delimiter=request.delimiter,
        display_name=request.display_name,
    )


@router.get("/tasks/{task_id}/datasets")
async def list_datasets(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return {"datasets": container.stage1_service.list_datasets(task_id)}


@router.get("/tasks/{task_id}/datasets/{dataset_id}/preview")
async def dataset_preview(
    task_id: str,
    dataset_id: str,
    limit: int = 20,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.stage1_service.preview(task_id, dataset_id, limit)


@router.get("/tasks/{task_id}/datasets/{dataset_id}/profile")
async def dataset_profile(
    task_id: str,
    dataset_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.stage1_service.profile(task_id, dataset_id)


@router.get("/tasks/{task_id}/datasets/{dataset_id}")
async def get_dataset(
    task_id: str,
    dataset_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.stage1_service.get_dataset(task_id, dataset_id)


@router.post("/tasks/{task_id}/datasets/{dataset_id}/normalization-decisions")
async def confirm_normalization(
    task_id: str,
    dataset_id: str,
    request: NormalizationDecisionRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.confirm_normalization(
        task_id,
        dataset_id,
        decision_id=request.decision_id,
        choice=request.choice,
    )


@router.get("/tasks/{task_id}/semantic-bindings")
async def list_semantic_bindings(
    task_id: str,
    dataset_id: str | None = None,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return {
        "bindings": container.stage1_service.bindings(task_id, dataset_id),
        "semantic_model_version": container.semantic_catalog.model.version,
    }


@router.post("/tasks/{task_id}/datasets/{dataset_id}/semantic-binding-decisions")
async def confirm_semantic_binding(
    task_id: str,
    dataset_id: str,
    request: SemanticBindingDecisionRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return await container.stage1_service.confirm_binding(
        task_id,
        dataset_id,
        binding_id=request.binding_id,
        physical_field_id=request.physical_field_id,
        confirm=request.confirm,
    )


@router.get("/semantic-model")
async def get_semantic_model(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return container.stage1_service.semantic_model()


@router.get("/semantic-model/{version}")
async def get_semantic_model_version(
    version: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if version != container.semantic_catalog.model.version:
        raise AppError("SEMANTIC_MODEL_VERSION_NOT_FOUND", "语义模型版本不存在", 404)
    return container.stage1_service.semantic_model()
