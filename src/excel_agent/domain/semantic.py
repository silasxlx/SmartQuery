"""阶段1全局语义模型的领域类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _tuple_value(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class SemanticMember:
    member_id: str
    kind: str
    name: str
    aliases: tuple[str, ...] = ()
    allowed_types: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    description: str | None = None
    unit: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def metric_id(self) -> str:
        return self.member_id

    @property
    def relationship_id(self) -> str:
        return self.member_id

    @property
    def formula(self) -> str | None:
        value = self.extra.get("formula")
        return str(value) if value is not None else None

    @property
    def aggregation(self) -> str | None:
        value = self.extra.get("aggregation")
        return str(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.member_id,
            "kind": self.kind,
            "name": self.name,
            "aliases": list(self.aliases),
            "allowed_types": list(self.allowed_types),
            "source_refs": list(self.source_refs),
        }
        if self.description is not None:
            result["description"] = self.description
        if self.unit is not None:
            result["unit"] = self.unit
        result.update(self.extra)
        return result


@dataclass(frozen=True)
class SemanticModelVersion:
    version: str
    members: tuple[SemanticMember, ...]
    source_path: str
    verified_questions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_path": self.source_path,
            "members": [member.to_dict() for member in self.members],
            "verified_questions": [dict(item) for item in self.verified_questions],
        }


@dataclass(frozen=True)
class EntityDefinition:
    entity_id: str
    name: str
    primary_key: str | None = None
    aliases: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_member(cls, member: SemanticMember) -> "EntityDefinition":
        return cls(
            entity_id=member.member_id,
            name=member.name,
            primary_key=member.extra.get("primary_key"),
            aliases=member.aliases,
            source_refs=member.source_refs,
        )

    @property
    def member_id(self) -> str:
        return self.entity_id

    @property
    def extra(self) -> dict[str, Any]:
        return {"primary_key": self.primary_key}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entity_id,
            "kind": "entity",
            "name": self.name,
            "primary_key": self.primary_key,
            "aliases": list(self.aliases),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class DimensionDefinition:
    dimension_id: str
    name: str
    data_type: str | None = None
    time_grain: str | None = None
    aliases: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_member(cls, member: SemanticMember) -> "DimensionDefinition":
        return cls(
            dimension_id=member.member_id,
            name=member.name,
            data_type=member.extra.get("data_type"),
            time_grain=member.extra.get("time_grain"),
            aliases=member.aliases,
            source_refs=member.source_refs,
        )

    @property
    def member_id(self) -> str:
        return self.dimension_id

    @property
    def extra(self) -> dict[str, Any]:
        return {"data_type": self.data_type, "time_grain": self.time_grain}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.dimension_id,
            "kind": "dimension",
            "name": self.name,
            "data_type": self.data_type,
            "time_grain": self.time_grain,
            "aliases": list(self.aliases),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    name: str
    description: str | None = None
    aliases: tuple[str, ...] = ()
    formula: str | None = None
    aggregation: str | None = None
    unit: str | None = None
    default_time_grain: str | None = None
    default_filter: dict[str, Any] | None = None
    dependencies: tuple[str, ...] = ()
    allowed_dimensions: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_member(cls, member: SemanticMember) -> "MetricDefinition":
        return cls(
            metric_id=member.member_id,
            name=member.name,
            description=member.description,
            aliases=member.aliases,
            formula=member.extra.get("formula"),
            aggregation=member.extra.get("aggregation"),
            unit=member.unit,
            default_time_grain=member.extra.get("default_time_grain"),
            default_filter=member.extra.get("default_filter"),
            dependencies=_tuple_value(member.extra.get("dependencies")),
            allowed_dimensions=_tuple_value(member.extra.get("allowed_dimensions")),
            source_refs=member.source_refs,
        )

    @property
    def member_id(self) -> str:
        return self.metric_id

    @property
    def extra(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "aggregation": self.aggregation,
            "default_time_grain": self.default_time_grain,
            "default_filter": self.default_filter,
            "dependencies": self.dependencies,
            "allowed_dimensions": self.allowed_dimensions,
        }

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.metric_id,
            "kind": "metric",
            "name": self.name,
            "aliases": list(self.aliases),
            "source_refs": list(self.source_refs),
            "unit": self.unit,
            "aggregation": self.aggregation,
            "formula": self.formula,
            "default_time_grain": self.default_time_grain,
            "dependencies": list(self.dependencies),
            "allowed_dimensions": list(self.allowed_dimensions),
        }
        if self.description is not None:
            result["description"] = self.description
        if self.default_filter is not None:
            result["default_filter"] = dict(self.default_filter)
        return result


@dataclass(frozen=True)
class RelationshipDefinition:
    relationship_id: str
    name: str
    left_entity_id: str
    right_entity_id: str
    left_keys: tuple[str, ...]
    right_keys: tuple[str, ...]
    cardinality: str
    join_type: str = "inner"
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_member(cls, member: SemanticMember) -> "RelationshipDefinition":
        return cls(
            relationship_id=member.member_id,
            name=member.name,
            left_entity_id=str(member.extra.get("left_entity", "")),
            right_entity_id=str(member.extra.get("right_entity", "")),
            left_keys=_tuple_value(member.extra.get("left_keys", member.extra.get("left_key"))),
            right_keys=_tuple_value(
                member.extra.get("right_keys", member.extra.get("right_key"))
            ),
            cardinality=str(member.extra.get("cardinality", "")),
            join_type=str(member.extra.get("join_type", "inner")),
            source_refs=member.source_refs,
        )

    @property
    def member_id(self) -> str:
        return self.relationship_id

    @property
    def extra(self) -> dict[str, Any]:
        return {
            "left_entity": self.left_entity_id,
            "right_entity": self.right_entity_id,
            "left_keys": self.left_keys,
            "right_keys": self.right_keys,
            "cardinality": self.cardinality,
            "join_type": self.join_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.relationship_id,
            "kind": "relationship",
            "name": self.name,
            "left_entity": self.left_entity_id,
            "right_entity": self.right_entity_id,
            "left_keys": list(self.left_keys),
            "right_keys": list(self.right_keys),
            "cardinality": self.cardinality,
            "join_type": self.join_type,
            "source_refs": list(self.source_refs),
        }
