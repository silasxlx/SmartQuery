"""阶段1版本化语义YAML加载与字段绑定。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..domain.semantic import (
    DimensionDefinition,
    EntityDefinition,
    MetricDefinition,
    RelationshipDefinition,
    SemanticMember,
    SemanticModelVersion,
)
from ..domain.task_dataset import BindingStatus, PhysicalField, SemanticBinding
from ..errors import AppError
from .restricted_ast import FormulaError, validate_metric_formulas


def _token(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value).strip().casefold())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item).strip()]
    raise AppError("SEMANTIC_MODEL_INVALID", "语义模型字段格式无效", 500)


class SemanticCatalog:
    def __init__(self, model: SemanticModelVersion) -> None:
        self.model = model
        self.members = {member.member_id: member for member in model.members}

    @classmethod
    def from_file(cls, path: str | Path) -> "SemanticCatalog":
        model_path = Path(path)
        if not model_path.is_absolute() and not model_path.exists():
            model_path = Path.cwd() / model_path
        if not model_path.exists() and not Path(path).is_absolute():
            model_path = Path(__file__).resolve().parents[3] / Path(path)
        if not model_path.exists():
            raise AppError("SEMANTIC_MODEL_INVALID", "语义模型文件不存在", 500)
        try:
            raw = yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise AppError("SEMANTIC_MODEL_INVALID", "语义模型文件无法读取", 500) from exc
        if not isinstance(raw, dict) or not str(raw.get("version", "")).strip():
            raise AppError("SEMANTIC_MODEL_INVALID", "语义模型缺少版本号", 500)

        members: list[SemanticMember] = []
        seen_ids: set[str] = set()
        alias_owners: dict[str, str] = {}
        relationship_specs: list[tuple[str, dict[str, Any]]] = []
        sections = (
            ("entities", "entity"),
            ("dimensions", "dimension"),
            ("metrics", "metric"),
            ("relationships", "relationship"),
        )
        for section, kind in sections:
            values = raw.get(section, [])
            if values is None:
                values = []
            if not isinstance(values, list):
                raise AppError("SEMANTIC_MODEL_INVALID", f"{section}必须是列表", 500)
            for item in values:
                if not isinstance(item, dict):
                    raise AppError("SEMANTIC_MODEL_INVALID", "语义成员必须是对象", 500)
                member_id = str(item.get("id", "")).strip()
                name = str(item.get("name", "")).strip()
                if not member_id or not name or member_id in seen_ids:
                    raise AppError("SEMANTIC_MODEL_INVALID", "语义成员ID重复或缺失", 500)
                if kind in {"metric", "relationship"} and not _as_list(item.get("source_refs")):
                    raise AppError("SEMANTIC_MODEL_INVALID", "正式指标和关系必须提供来源引用", 500)
                aliases = _as_list(item.get("aliases"))
                allowed_types = _as_list(
                    item.get("allowed_types", item.get("physical_types", item.get("type")))
                )
                if kind == "metric" and not allowed_types:
                    allowed_types = ["integer", "decimal"]
                if kind == "metric" and item.get("aggregation") is not None:
                    if str(item["aggregation"]) not in {
                        "sum",
                        "count",
                        "count_distinct",
                        "mean",
                        "min",
                        "max",
                    }:
                        raise AppError("SEMANTIC_MODEL_INVALID", "指标聚合方式不支持", 500)
                if kind in {"entity", "dimension"} and not allowed_types:
                    allowed_types = ["string"]
                source_refs = _as_list(item.get("source_refs"))
                extra = {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "id",
                        "name",
                        "aliases",
                        "allowed_types",
                        "physical_types",
                        "type",
                        "source_refs",
                        "description",
                        "unit",
                    }
                }
                member = SemanticMember(
                    member_id=member_id,
                    kind=kind,
                    name=name,
                    aliases=tuple(aliases),
                    allowed_types=tuple(allowed_types),
                    source_refs=tuple(source_refs),
                    description=item.get("description"),
                    unit=item.get("unit"),
                    extra=extra,
                )
                seen_ids.add(member_id)
                if kind == "relationship":
                    relationship_specs.append((member_id, item))
                for alias in (member_id, name, *aliases):
                    normalized = _token(alias)
                    owner = alias_owners.get(normalized)
                    if owner is not None and owner != member_id:
                        raise AppError("SEMANTIC_MODEL_INVALID", "语义成员别名冲突", 500)
                    alias_owners[normalized] = member_id
                members.append(member)
        member_kinds = {member.member_id: member.kind for member in members}
        relationship_edges: dict[str, list[str]] = {}
        allowed_cardinalities = {"one_to_one", "many_to_one", "one_to_many"}
        for relationship_id, item in relationship_specs:
            left = str(
                item.get("left_entity")
                or item.get("from_entity")
                or item.get("left")
                or ""
            ).strip()
            right = str(
                item.get("right_entity")
                or item.get("to_entity")
                or item.get("right")
                or ""
            ).strip()
            cardinality = str(
                item.get("cardinality")
                or item.get("relation")
                or item.get("relationship_type")
                or item.get("type")
                or ""
            ).strip()
            left_keys = _as_list(item.get("left_keys", item.get("left_key")))
            right_keys = _as_list(item.get("right_keys", item.get("right_key")))
            if (
                not left
                or not right
                or left == right
                or member_kinds.get(left) not in {"entity", "dimension"}
                or member_kinds.get(right) not in {"entity", "dimension"}
                or cardinality not in allowed_cardinalities
                or not left_keys
                or len(left_keys) != len(right_keys)
            ):
                raise AppError("SEMANTIC_MODEL_INVALID", "关系定义不一致", 500)
            relationship_edges.setdefault(left, []).append(right)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit_relationship(node: str) -> None:
            if node in visiting:
                raise AppError("SEMANTIC_MODEL_INVALID", "关系定义存在循环", 500)
            if node in visited:
                return
            visiting.add(node)
            for child in relationship_edges.get(node, []):
                visit_relationship(child)
            visiting.remove(node)
            visited.add(node)

        for relationship_node in relationship_edges:
            visit_relationship(relationship_node)

        verified_questions = raw.get("verified_questions", []) or []
        if not isinstance(verified_questions, list):
            raise AppError("SEMANTIC_MODEL_INVALID", "verified_questions必须是列表", 500)
        verified_ids: set[str] = set()
        for item in verified_questions:
            if not isinstance(item, dict):
                raise AppError("SEMANTIC_MODEL_INVALID", "VerifiedQuestion必须是对象", 500)
            question_id = str(item.get("id") or item.get("question_id") or "").strip()
            question = str(item.get("question") or "").strip()
            if not question_id or not question or question_id in verified_ids:
                raise AppError("SEMANTIC_MODEL_INVALID", "VerifiedQuestion定义不一致", 500)
            if "expected_resolution" in item and not isinstance(
                item["expected_resolution"], dict
            ):
                raise AppError("SEMANTIC_MODEL_INVALID", "VerifiedQuestion解析格式无效", 500)
            if "expected_query_plan" in item and not isinstance(
                item["expected_query_plan"], dict
            ):
                raise AppError("SEMANTIC_MODEL_INVALID", "VerifiedQuestion计划格式无效", 500)
            referenced_ids: set[str] = set()
            expected_resolution = item.get("expected_resolution")
            if isinstance(expected_resolution, dict):
                referenced_ids.update(
                    str(value) for value in expected_resolution.get("metric_ids", [])
                )
                referenced_ids.update(
                    str(value) for value in expected_resolution.get("dimension_ids", [])
                )
                referenced_ids.update(
                    str(value.get("semantic_id"))
                    for value in expected_resolution.get("filters", [])
                    if isinstance(value, dict) and value.get("semantic_id")
                )
            expected_plan = item.get("expected_query_plan")
            if isinstance(expected_plan, dict):
                for query in expected_plan.get("queries", []):
                    if not isinstance(query, dict):
                        continue
                    referenced_ids.update(str(value) for value in query.get("metric_ids", []))
                    referenced_ids.update(str(value) for value in query.get("dimension_ids", []))
                    referenced_ids.update(
                        str(value.get("semantic_id"))
                        for value in query.get("filters", [])
                        if isinstance(value, dict) and value.get("semantic_id")
                    )
            if any(value not in member_kinds for value in referenced_ids):
                raise AppError("SEMANTIC_MODEL_INVALID", "VerifiedQuestion引用未知语义成员", 500)
            verified_ids.add(question_id)
        metric_formulas = {
            member.member_id: str(member.extra["formula"])
            for member in members
            if member.kind == "metric" and member.extra.get("formula")
        }
        allowed_formula_fields = {
            member.member_id for member in members if member.kind == "metric"
        }
        metric_units = {
            member.member_id: member.unit
            for member in members
            if member.kind == "metric"
        }
        metric_grains = {
            member.member_id: (
                member.extra.get("default_time_grain") or member.extra.get("time_grain")
            )
            for member in members
            if member.kind == "metric"
        }
        if metric_formulas:
            try:
                validate_metric_formulas(
                    metric_formulas,
                    allowed_formula_fields,
                    units=metric_units,
                    grains=metric_grains,
                )
            except FormulaError as exc:
                raise AppError("SEMANTIC_MODEL_INVALID", "指标公式校验失败", 500) from exc
        model = SemanticModelVersion(
            version=str(raw["version"]),
            members=tuple(members),
            source_path=str(model_path),
            verified_questions=tuple(item for item in verified_questions if isinstance(item, dict)),
        )
        return cls(model)

    def summary(self) -> dict[str, Any]:
        summary = self.model.to_dict()
        # The API exposes a model version, never an absolute server path.
        summary["source_path"] = Path(self.model.source_path).name
        return summary

    def _type_compatible(self, field_type: str, allowed_types: tuple[str, ...]) -> bool:
        if not allowed_types:
            return True
        normalized = {item.casefold() for item in allowed_types}
        if field_type in normalized:
            return True
        if "number" in normalized and field_type in {"integer", "decimal"}:
            return True
        if "numeric" in normalized and field_type in {"integer", "decimal"}:
            return True
        if "date" in normalized and field_type == "datetime":
            return True
        return False

    def bindings_for(
        self,
        *,
        task_id: str,
        dataset_id: str,
        fields: list[PhysicalField],
    ) -> list[SemanticBinding]:
        bindings: list[SemanticBinding] = []
        for member in self.model.members:
            names = {_token(member.member_id), _token(member.name)}
            names.update(_token(alias) for alias in member.aliases)
            matches: list[tuple[PhysicalField, str]] = []
            for field in fields:
                field_names = {_token(field.normalized_name), _token(field.original_name)}
                intersection = names.intersection(field_names)
                if intersection:
                    source = "exact_name" if _token(field.normalized_name) in {
                        _token(member.member_id),
                        _token(member.name),
                    } else "alias"
                    matches.append((field, source))
            compatible = [
                (field, source)
                for field, source in matches
                if self._type_compatible(field.physical_type, member.allowed_types)
            ]
            binding_id = f"{task_id}:{dataset_id}:{member.member_id}"
            if len(matches) == 1 and len(compatible) == 1:
                field, source = compatible[0]
                bindings.append(
                    SemanticBinding(
                        binding_id=binding_id,
                        task_id=task_id,
                        dataset_id=dataset_id,
                        semantic_member_id=member.member_id,
                        semantic_member_kind=member.kind,
                        physical_field_id=field.field_id,
                        status=BindingStatus.CONFIRMED,
                        source=source,
                        type_compatible=True,
                        candidate_field_ids=[field.field_id],
                    )
                )
            else:
                bindings.append(
                    SemanticBinding(
                        binding_id=binding_id,
                        task_id=task_id,
                        dataset_id=dataset_id,
                        semantic_member_id=member.member_id,
                        semantic_member_kind=member.kind,
                        physical_field_id=None,
                        status=BindingStatus.PENDING,
                        source="catalog",
                        type_compatible=len(compatible) == len(matches) and bool(matches),
                        candidate_field_ids=[field.field_id for field, _ in matches],
                    )
                )
        return bindings

    def get_member(self, member_id: str) -> SemanticMember | None:
        return self.members.get(member_id)

    def metric_definitions(self) -> list[MetricDefinition]:
        return [
            MetricDefinition.from_member(member)
            for member in self.model.members
            if member.kind == "metric"
        ]

    def relationship_definitions(self) -> list[RelationshipDefinition]:
        return [
            RelationshipDefinition.from_member(member)
            for member in self.model.members
            if member.kind == "relationship"
        ]

    def entity_definitions(self) -> list[EntityDefinition]:
        return [
            EntityDefinition.from_member(member)
            for member in self.model.members
            if member.kind == "entity"
        ]

    def dimension_definitions(self) -> list[DimensionDefinition]:
        return [
            DimensionDefinition.from_member(member)
            for member in self.model.members
            if member.kind == "dimension"
        ]
