"""阶段0 Composition Root 与应用生命周期。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .application.analysis_service import AnalysisService
from .application.stage1_services import Stage1Limits, Stage1Service
from .config import AppConfig, get_config
from .infrastructure.embedding import EmbeddingProviderRegistry
from .infrastructure.model_provider import ModelProviderRegistry
from .infrastructure.stage1_repositories import DatasetStore, TaskRepository
from .infrastructure.stage1_semantic import SemanticCatalog
from .infrastructure.task_knowledge import TaskKnowledgeStore
from .logging_config import configure_logging
from .security import TempResourceManager


@dataclass
class AppContainer:
    config: AppConfig
    executor: ThreadPoolExecutor
    temp_resources: TempResourceManager
    logger: Any
    task_repository: TaskRepository
    dataset_store: DatasetStore
    semantic_catalog: SemanticCatalog
    stage1_service: Stage1Service
    provider_registry: ModelProviderRegistry
    embedding_registry: EmbeddingProviderRegistry
    knowledge_store: TaskKnowledgeStore
    analysis_service: AnalysisService
    shutting_down: bool = False
    active_requests: int = 0
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def create(cls, config: AppConfig | None = None) -> "AppContainer":
        actual_config = config or get_config()
        temp_resources = TempResourceManager(actual_config.runtime.temp_root)
        temp_resources.clean_orphans()
        task_repository = TaskRepository(
            root_for_task=lambda task_id: str(temp_resources.task_directory(task_id)),
            max_tasks=actual_config.runtime.max_tasks,
        )
        dataset_store = DatasetStore()
        semantic_catalog = SemanticCatalog.from_file(actual_config.semantic_model_path)
        task_repository.semantic_model_version = semantic_catalog.model.version
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="excel-agent")
        stage1_service = Stage1Service(
            repository=task_repository,
            dataset_store=dataset_store,
            semantic_catalog=semantic_catalog,
            temp_resources=temp_resources,
            executor=executor,
            limits=Stage1Limits(
                max_upload_bytes=actual_config.runtime.max_upload_bytes,
                max_rows=actual_config.runtime.max_dataset_rows,
                max_columns=actual_config.runtime.max_dataset_columns,
                max_sheets=actual_config.runtime.max_dataset_sheets,
                operation_timeout_seconds=actual_config.runtime.operation_timeout_seconds,
            ),
        )
        provider_registry = ModelProviderRegistry.from_config(actual_config, semantic_catalog)
        embedding_registry = EmbeddingProviderRegistry.from_config(actual_config.embedding)
        knowledge_store = TaskKnowledgeStore(
            root=actual_config.runtime.temp_root,
            short_document_tokens=actual_config.knowledge_base.short_document_tokens,
            chunk_tokens=actual_config.knowledge_base.chunk_tokens,
            chunk_overlap_tokens=actual_config.knowledge_base.chunk_overlap_tokens,
            min_similarity=actual_config.knowledge_base.min_similarity,
            max_results=actual_config.knowledge_base.max_results,
            embedding_registry=embedding_registry,
        )
        analysis_service = AnalysisService(
            repository=task_repository,
            dataset_store=dataset_store,
            semantic_catalog=semantic_catalog,
            provider_registry=provider_registry,
            embedding_registry=embedding_registry,
            executor=executor,
            knowledge_store=knowledge_store,
            analysis_timeout_seconds=120.0,
        )
        return cls(
            config=actual_config,
            executor=executor,
            temp_resources=temp_resources,
            logger=configure_logging(),
            task_repository=task_repository,
            dataset_store=dataset_store,
            semantic_catalog=semantic_catalog,
            stage1_service=stage1_service,
            provider_registry=provider_registry,
            embedding_registry=embedding_registry,
            knowledge_store=knowledge_store,
            analysis_service=analysis_service,
        )

    async def close(self) -> None:
        async with self._close_lock:
            if self.shutting_down:
                return
            self.shutting_down = True
            await self.analysis_service.close()
            self.provider_registry.close()
            self.embedding_registry.close()
            # The executor does not receive new work after ``shutting_down``.
            # ``cancel_futures`` is safe for queued tasks; running pandas work
            # remains cooperative and is not force-killed.
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.dataset_store.clear()
            self.knowledge_store.clear()
            self.task_repository.clear()
            self.temp_resources.cleanup()


@asynccontextmanager
async def app_lifespan(app: Any):
    container = AppContainer.create()
    app.state.container = container
    try:
        yield
    finally:
        await container.close()
