"""任务级临时知识库。

实现使用任务隔离目录保存文档元数据，并提供确定性的词项重叠检索作为无
Embedding环境下的安全测试实现。未来可在同一接口内替换为任务级Chroma
和外部Embedding Provider；正式指标和绑定始终优先于检索内容。
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AppError
from .embedding import EmbeddingProviderRegistry

_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokens(value: str) -> set[str]:
    return {item.casefold() for item in _TOKEN.findall(value or "")}


def _embedding(tokens: set[str], dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vector[int.from_bytes(digest, "big") % dimensions] += 1.0
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


@dataclass
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    task_id: str
    title: str
    content: str
    source_name: str
    ordinal: int
    tokens: set[str] = field(default_factory=set)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "task_id": self.task_id,
            "title": self.title,
            "source_name": self.source_name,
            "ordinal": self.ordinal,
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass
class KnowledgeDocument:
    document_id: str
    task_id: str
    source_name: str
    title: str
    content: str
    chunks: list[KnowledgeChunk]

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "task_id": self.task_id,
            "source_name": self.source_name,
            "title": self.title,
            "content": self.content if include_content else None,
            "chunk_count": len(self.chunks),
        }


class TaskKnowledgeStore:
    def __init__(
        self,
        *,
        root: str | Path,
        short_document_tokens: int = 1200,
        chunk_tokens: int = 800,
        chunk_overlap_tokens: int = 100,
        min_similarity: float = 0.55,
        max_results: int = 3,
        id_factory: Any | None = None,
        embedding_registry: EmbeddingProviderRegistry | None = None,
    ) -> None:
        self.root = Path(root)
        self.short_document_tokens = short_document_tokens
        self.chunk_tokens = chunk_tokens
        self.chunk_overlap_tokens = chunk_overlap_tokens
        self.min_similarity = min_similarity
        self.max_results = max_results
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.embedding_registry = embedding_registry
        self._documents: dict[str, KnowledgeDocument] = {}
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._chroma_clients: dict[str, Any] = {}

    def _task_dir(self, task_id: str) -> Path:
        value = str(task_id)
        if (
            not value
            or value in {".", ".."}
            or any(char in value for char in ("/", "\\", ":", "\x00"))
        ):
            raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
        directory = (self.root / f"task-{value}").resolve()
        if self.root.resolve() not in directory.parents:
            raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
        return directory

    def _split(self, content: str) -> list[str]:
        token_count = len(_TOKEN.findall(content))
        if token_count <= self.short_document_tokens:
            return [content.strip()]
        headings = re.split(r"(?m)(?=^#{1,6}\s+)", content)
        segments = [item.strip() for item in headings if item.strip()]
        if not segments:
            segments = [content]
        chunks: list[str] = []
        for segment in segments:
            words = segment.split()
            if len(_TOKEN.findall(segment)) <= self.chunk_tokens:
                chunks.append(segment)
                continue
            if len(words) <= 1:
                matches = list(_TOKEN.finditer(segment))
                start = 0
                while start < len(matches):
                    end = min(len(matches), start + self.chunk_tokens)
                    start_char = matches[start].start()
                    end_char = matches[end - 1].end()
                    chunks.append(segment[start_char:end_char])
                    if end == len(matches):
                        break
                    start = max(start + 1, end - self.chunk_overlap_tokens)
                continue
            start = 0
            while start < len(words):
                end = min(len(words), start + self.chunk_tokens)
                chunks.append(" ".join(words[start:end]))
                if end == len(words):
                    break
                start = max(start + 1, end - self.chunk_overlap_tokens)
        return chunks or [content.strip()]

    def add(
        self, task_id: str, *, source_name: str, content: str, title: str | None = None
    ) -> KnowledgeDocument:
        task_dir = self._task_dir(task_id) / "knowledge"
        if (
            not source_name
            or "\x00" in source_name
            or "/" in source_name
            or "\\" in source_name
            or Path(source_name).is_absolute()
            or source_name.strip() != source_name
        ):
            raise AppError("KNOWLEDGE_FILENAME_INVALID", "知识文档文件名无效", 400)
        suffix = Path(source_name).suffix.casefold()
        if suffix not in {".md", ".txt", ".markdown"}:
            raise AppError("KNOWLEDGE_FORMAT_UNSUPPORTED", "任务知识仅支持Markdown或文本", 422)
        if not content.strip():
            raise AppError("KNOWLEDGE_EMPTY", "知识文档不能为空", 422)
        document_id = str(self._id_factory())
        safe_title = (title or Path(source_name).stem or "未命名")[:200]
        chunks: list[KnowledgeChunk] = []
        for ordinal, chunk_text in enumerate(self._split(content)):
            chunk = KnowledgeChunk(
                chunk_id=str(self._id_factory()),
                document_id=document_id,
                task_id=str(task_id),
                title=safe_title,
                content=chunk_text,
                source_name=source_name,
                ordinal=ordinal,
                tokens=_tokens(chunk_text),
            )
            chunks.append(chunk)
            self._chunks[chunk.chunk_id] = chunk
        document = KnowledgeDocument(
            document_id=document_id,
            task_id=str(task_id),
            source_name=source_name,
            title=safe_title,
            content=content,
            chunks=chunks,
        )
        self._documents[document_id] = document
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / f"{document_id}.txt").write_text(content, encoding="utf-8")
        except OSError as exc:
            self._documents.pop(document_id, None)
            for chunk in chunks:
                self._chunks.pop(chunk.chunk_id, None)
            raise AppError("KNOWLEDGE_WRITE_FAILED", "任务知识文档无法保存", 500) from exc
        self._index_chroma(task_id, chunks)
        return document

    def _index_chroma(self, task_id: str, chunks: list[KnowledgeChunk]) -> None:
        """将同一任务的片段写入独立Chroma目录；不可用时保留词项索引。"""

        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            return
        try:
            task_dir = self._task_dir(task_id) / "knowledge" / "chroma"
            task_dir.mkdir(parents=True, exist_ok=True)
            client = self._chroma_clients.get(str(task_id))
            if client is None:
                client = chromadb.PersistentClient(
                    path=str(task_dir), settings=Settings(anonymized_telemetry=False)
                )
                self._chroma_clients[str(task_id)] = client
            collection = client.get_or_create_collection(
                name=f"task_{str(task_id).replace('-', '_')}",
                metadata={"hnsw:space": "cosine", "task_id": str(task_id)},
            )
            embeddings = self._embed([chunk.content for chunk in chunks])
            collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=embeddings,
                documents=[chunk.content for chunk in chunks],
                metadatas=[
                    {
                        "task_id": chunk.task_id,
                        "document_id": chunk.document_id,
                        "title": chunk.title,
                        "source_name": chunk.source_name,
                    }
                    for chunk in chunks
                ],
            )
        except Exception:
            # The in-memory token index remains available when Chroma is not
            # installed, cannot open its directory, or has an old dimension.
            return

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Use the configured provider while keeping an offline fallback."""

        if self.embedding_registry is not None:
            try:
                provider = self.embedding_registry.active_provider()
                return provider.embed(texts)
            except Exception:
                pass
        return [_embedding(_tokens(text)) for text in texts]

    def list(self, task_id: str) -> list[KnowledgeDocument]:
        return [item for item in self._documents.values() if item.task_id == str(task_id)]

    def get(self, task_id: str, document_id: str) -> KnowledgeDocument:
        document = self._documents.get(str(document_id))
        if document is None or document.task_id != str(task_id):
            raise AppError("KNOWLEDGE_DOCUMENT_NOT_FOUND", "任务知识文档不存在", 404)
        return document

    def delete(self, task_id: str, document_id: str) -> None:
        document = self.get(task_id, document_id)
        self._documents.pop(document.document_id, None)
        for chunk in document.chunks:
            self._chunks.pop(chunk.chunk_id, None)
        client = self._chroma_clients.get(str(task_id))
        if client is not None:
            try:
                collection = client.get_collection(f"task_{str(task_id).replace('-', '_')}")
                collection.delete(ids=[chunk.chunk_id for chunk in document.chunks])
            except Exception:
                pass
        path = self._task_dir(task_id) / "knowledge" / f"{document_id}.txt"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def search(self, task_id: str, query: str, *, top_k: int | None = None) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        limit = min(top_k or self.max_results, self.max_results)
        client = self._chroma_clients.get(str(task_id))
        if client is not None:
            try:
                collection = client.get_collection(
                    f"task_{str(task_id).replace('-', '_')}"
                )
                result = collection.query(
                    query_embeddings=self._embed([query]),
                    n_results=limit,
                    include=["distances", "metadatas"],
                )
                ids = (result.get("ids") or [[]])[0]
                distances = (result.get("distances") or [[]])[0]
                chroma_hits: list[dict[str, Any]] = []
                for chunk_id, distance in zip(ids, distances):
                    similarity = 1.0 - float(distance)
                    chunk = self._chunks.get(str(chunk_id))
                    if chunk is not None and similarity >= self.min_similarity:
                        chroma_hits.append(
                            {**chunk.to_dict(), "score": round(similarity, 4)}
                        )
                if chroma_hits:
                    return chroma_hits
            except Exception:
                # Keep a deterministic local fallback if Chroma is unavailable
                # or an existing task index was created with another dimension.
                pass
        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self._chunks.values():
            if chunk.task_id != str(task_id):
                continue
            overlap = len(query_tokens.intersection(chunk.tokens))
            if not overlap:
                continue
            score = overlap / math.sqrt(len(query_tokens) * max(len(chunk.tokens), 1))
            if score >= self.min_similarity:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].ordinal))
        return [{**chunk.to_dict(), "score": round(score, 4)} for score, chunk in scored[:limit]]

    def delete_task(self, task_id: str) -> None:
        for document in list(self.list(task_id)):
            self.delete(task_id, document.document_id)
        client = self._chroma_clients.pop(str(task_id), None)
        if client is not None:
            try:
                client._system.stop()
            except Exception:
                pass
        task_dir = self._task_dir(task_id)
        if task_dir.exists():
            for child in sorted(task_dir.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                task_dir.rmdir()
            except OSError:
                pass

    def clear(self) -> None:
        for task_id in {item.task_id for item in self._documents.values()}:
            self.delete_task(task_id)
        self._documents.clear()
        self._chunks.clear()
        self._chroma_clients.clear()


__all__ = ["KnowledgeChunk", "KnowledgeDocument", "TaskKnowledgeStore"]
