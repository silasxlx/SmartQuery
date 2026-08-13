"""模型Provider能力协议与结构化输出适配。

阶段2业务层只依赖本文件定义的协议。真实模型使用OpenAI兼容接口时必须声明
原生结构化输出或Tool Calling；测试使用 ``MockModelProvider``，不访问外网。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import ProviderConfig
from ..domain.analysis import SemanticResolution
from ..errors import AppError
from .stage1_semantic import SemanticCatalog


class SemanticResolutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = "aggregate"
    metric_ids: list[str] = Field(default_factory=list)
    dimension_ids: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    time_grain: str | None = None
    aggregation: str | None = None
    order_by: str | None = None
    ascending: bool = False
    limit: int = 20
    binding_candidates: dict[str, list[str]] = Field(default_factory=dict)
    missing_concepts: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    clarification_draft: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None

    def to_domain(self) -> SemanticResolution:
        return SemanticResolution(**self.model_dump())


class ClarificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    summary: dict[str, Any]
    confirmed: bool = False


class AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


@dataclass(frozen=True)
class ModelProviderCapabilities:
    structured_output: str
    supports_answer_streaming: bool
    timeout_seconds: float = 60.0


class ModelProvider(Protocol):
    name: str
    capabilities: ModelProviderCapabilities

    async def structured(
        self,
        *,
        schema: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any],
    ) -> BaseModel: ...

    async def answer(self, *, system: str, user: str, context: dict[str, Any]) -> str: ...


T = TypeVar("T", bound=BaseModel)


class UnavailableModelProvider:
    name = "unavailable"
    capabilities = ModelProviderCapabilities("none", False)

    def __init__(self, reason: str = "模型服务未配置") -> None:
        self.reason = reason

    async def structured(self, **_: Any) -> BaseModel:
        raise AppError("MODEL_CREDENTIAL_MISSING", self.reason, 503, {"field": "model.api_key"})

    async def answer(self, **_: Any) -> str:
        raise AppError("MODEL_CREDENTIAL_MISSING", self.reason, 503, {"field": "model.api_key"})


class MockModelProvider:
    """确定性测试Provider；可根据问题和语义模型生成最小结构化解析。"""

    name = "mock"
    capabilities = ModelProviderCapabilities("native", True)

    def __init__(self, catalog: SemanticCatalog | None = None) -> None:
        self.catalog = catalog
        self.calls: list[dict[str, Any]] = []

    def _find(self, question: str, kind: str) -> list[str]:
        if self.catalog is None:
            return []
        tokens = question.casefold()
        matches: list[str] = []
        for member in self.catalog.model.members:
            if member.kind != kind:
                continue
            names = (member.member_id, member.name, *member.aliases)
            if any(str(name).casefold() in tokens for name in names):
                matches.append(member.member_id)
        return matches

    def _resolution(self, question: str) -> SemanticResolutionPayload:
        if re.search(r"不存在.*字段|未知字段|unknown\s+field", question, re.IGNORECASE):
            return SemanticResolutionPayload(
                intent="aggregate",
                metric_ids=["unknown_semantic_member"],
            )
        metrics = self._find(question, "metric")
        dimensions = self._find(question, "dimension")
        if not metrics:
            metrics = ["count"] if re.search(r"订单数|数量|笔数|订单", question) else ["amount"]
        if not dimensions and re.search(r"网点|机构|支行|门店", question):
            dimensions = ["branch"]
        if re.search(r"趋势|按月|按日|按季度|按年", question):
            intent = "trend"
        elif re.search(r"排行|排名|最高|最低|top", question, re.IGNORECASE):
            intent = "rank"
        elif dimensions:
            intent = "group"
        else:
            intent = "aggregate"
        time_grain = None
        for phrase, grain in (
            ("按日", "day"),
            ("按周", "week"),
            ("按月", "month"),
            ("按季度", "quarter"),
            ("按年", "year"),
        ):
            if phrase in question:
                time_grain = grain
                break
        aggregation = None
        if "平均" in question or re.search(r"average|mean", question, re.IGNORECASE):
            aggregation = "mean"
        elif (
            not dimensions
            and ("最小" in question or "最低" in question)
            or (not dimensions and re.search(r"minimum|min", question, re.IGNORECASE))
        ):
            aggregation = "min"
        elif (
            not dimensions
            and "最大" in question
            or (not dimensions and re.search(r"maximum|max", question, re.IGNORECASE))
        ):
            aggregation = "max"
        ascending = bool(re.search(r"最低|最少|升序", question))
        return SemanticResolutionPayload(
            intent=intent,
            metric_ids=metrics,
            dimension_ids=dimensions,
            time_grain=time_grain,
            aggregation=aggregation,
            order_by=metrics[0] if intent == "rank" else None,
            ascending=ascending,
            limit=20,
        )

    async def structured(
        self,
        *,
        schema: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any],
    ) -> BaseModel:
        self.calls.append({"schema": schema.__name__, "user": user, "context": context})
        payload: BaseModel
        if schema is SemanticResolutionPayload:
            payload = self._resolution(user)
        else:
            payload = schema.model_validate({})
        return schema.model_validate(payload.model_dump())

    async def answer(self, *, system: str, user: str, context: dict[str, Any]) -> str:
        result = context.get("result", {})
        rows = result.get("data", []) if isinstance(result, dict) else []
        if not rows:
            return "没有符合条件的数据。"
        if len(rows) == 1:
            values = ", ".join(f"{key}={value}" for key, value in rows[0].items())
            return f"根据当前数据，结果为：{values}。"
        return f"根据当前数据，共返回{len(rows)}条结果。"


class OpenAICompatibleProvider:
    """轻量OpenAI兼容适配器；真正调用延迟到请求时。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.provider
        self.capabilities = ModelProviderCapabilities(
            structured_output=config.structured_output,
            supports_answer_streaming=config.supports_answer_streaming,
            timeout_seconds=max(0.001, min(float(config.timeout_seconds), 60.0)),
        )
        if self.capabilities.structured_output not in {"native", "tool"}:
            raise AppError("MODEL_PROVIDER_UNSUPPORTED", "模型Provider不支持结构化输出", 500)
        if not config.api_key:
            self._client = None
        else:
            from langchain_openai import ChatOpenAI

            self._client = ChatOpenAI(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url or None,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout=max(0.001, min(float(config.timeout_seconds), 60.0)),
                max_retries=0,
            )

    async def structured(
        self,
        *,
        schema: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any],
    ) -> BaseModel:
        if self._client is None:
            raise AppError(
                "MODEL_CREDENTIAL_MISSING", "模型服务未配置密钥", 503, {"field": "model.api_key"}
            )
        method = "json_schema" if self.config.structured_output == "native" else "function_calling"
        runnable = self._client.with_structured_output(schema, method=method, strict=True)
        result = await runnable.ainvoke(
            [
                ("system", system),
                (
                    "human",
                    (
                        f"{user}\n上下文（仅限必要摘要）："
                        f"{json.dumps(context, ensure_ascii=False, default=str)}"
                    ),
                ),
            ]
        )
        return schema.model_validate(result)

    async def answer(self, *, system: str, user: str, context: dict[str, Any]) -> str:
        if self._client is None:
            raise AppError(
                "MODEL_CREDENTIAL_MISSING", "模型服务未配置密钥", 503, {"field": "model.api_key"}
            )
        result = await self._client.ainvoke(
            [
                ("system", system),
                (
                    "human",
                    f"{user}\nEvidence：{json.dumps(context, ensure_ascii=False, default=str)}",
                ),
            ]
        )
        content = getattr(result, "content", result)
        return str(content)


class ModelProviderRegistry:
    def __init__(self, providers: dict[str, ModelProvider], active: str) -> None:
        self.providers = providers
        self.active = active

    @classmethod
    def from_config(
        cls, config: Any, catalog: SemanticCatalog | None = None
    ) -> "ModelProviderRegistry":
        providers: dict[str, ModelProvider] = {}
        for name, provider_config in config.model.providers.items():
            if provider_config.structured_output not in {"native", "tool"}:
                raise AppError("MODEL_PROVIDER_UNSUPPORTED", "模型Provider不支持结构化输出", 500)
            if provider_config.provider == "mock":
                providers[name] = MockModelProvider(catalog)
            elif provider_config.api_key:
                providers[name] = OpenAICompatibleProvider(provider_config)
            else:
                providers[name] = UnavailableModelProvider()
        if not providers:
            providers["default"] = UnavailableModelProvider()
        return cls(providers, config.model.active)

    def register(self, name: str, provider: ModelProvider) -> None:
        self.providers[name] = provider

    def active_provider(self) -> ModelProvider:
        provider = self.providers.get(self.active)
        if provider is None:
            raise AppError("MODEL_PROVIDER_NOT_FOUND", "激活的模型Provider不存在", 503)
        return provider

    def close(self) -> None:
        for provider in self.providers.values():
            client = getattr(provider, "_client", None)
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    continue


async def structured_call(
    provider: ModelProvider,
    *,
    schema: type[T],
    system: str,
    user: str,
    context: dict[str, Any],
    timeout_seconds: float = 60.0,
) -> T:
    """执行一次结构化调用；网络重试和结构修复预算彼此独立。"""

    timeout_seconds = max(0.001, min(float(timeout_seconds), 60.0))
    network_attempts = 0
    validation_repairs = 0
    last_error: Exception | None = None

    def retryable_network_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        return isinstance(status_code, int) and 500 <= status_code <= 599

    while network_attempts < 2:
        try:
            raw = await asyncio.wait_for(
                provider.structured(schema=schema, system=system, user=user, context=context),
                timeout=timeout_seconds,
            )
            if isinstance(raw, schema):
                return raw
            return schema.model_validate(raw)
        except AppError:
            raise
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            network_attempts += 1
            if network_attempts >= 2:
                raise AppError("MODEL_TIMEOUT", "模型调用超时或网络不可用", 504) from exc
            await asyncio.sleep(0.05)
        except Exception as exc:
            if not retryable_network_error(exc):
                if not isinstance(
                    exc, (ValidationError, ValueError, TypeError, json.JSONDecodeError)
                ):
                    raise AppError("MODEL_ERROR", "模型调用失败", 502) from exc
                last_error = exc
                if validation_repairs >= 2:
                    raise AppError(
                        "STRUCTURED_OUTPUT_INVALID", "structured output invalid", 502
                    ) from exc
                validation_repairs += 1
                user = (
                    f"Please return strict structured output for {schema.__name__}. "
                    f"Original request: {user}"
                )
                continue
            last_error = exc
            network_attempts += 1
            if network_attempts >= 2:
                raise AppError("MODEL_TIMEOUT", "模型调用超时或网络不可用", 504) from exc
            await asyncio.sleep(0.05)
    raise AppError("MODEL_ERROR", "模型调用失败", 502) from last_error


__all__ = [
    "AnswerPayload",
    "ClarificationPayload",
    "MockModelProvider",
    "ModelProvider",
    "ModelProviderCapabilities",
    "ModelProviderRegistry",
    "SemanticResolutionPayload",
    "structured_call",
]
