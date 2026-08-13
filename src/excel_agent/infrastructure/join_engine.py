"""安全联表建议与确定性风险检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..errors import AppError


@dataclass(frozen=True)
class JoinSuggestion:
    left_dataset_id: str
    right_dataset_id: str
    candidates: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_dataset_id": self.left_dataset_id,
            "right_dataset_id": self.right_dataset_id,
            "candidates": [dict(item) for item in self.candidates],
        }


def _stats(frame: pd.DataFrame, key: str) -> dict[str, Any]:
    series = frame[key]
    non_null = series.dropna()
    unique_ratio = float(non_null.nunique(dropna=True) / len(non_null)) if len(non_null) else 0.0
    return {
        "null_ratio": float(series.isna().mean()) if len(series) else 0.0,
        "unique_ratio": unique_ratio,
        "non_null_count": int(len(non_null)),
    }


def _types_compatible(left: pd.Series, right: pd.Series) -> bool:
    left_kind = left.dtype.kind
    right_kind = right.dtype.kind
    if left_kind == right_kind:
        return True
    numeric = set("biufc")
    return left_kind in numeric and right_kind in numeric


def _expected_join_rows(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    left_keys: list[str],
    right_keys: list[str],
    join_type: str = "inner",
) -> int:
    left_values = left[left_keys].dropna().astype(str).astype(str).agg("\x1f".join, axis=1)
    right_values = right[right_keys].dropna().astype(str).astype(str).agg("\x1f".join, axis=1)
    left_counts = left_values.value_counts()
    right_counts = right_values.value_counts()
    matched = int(
        sum(int(left_counts[key]) * int(right_counts.get(key, 0)) for key in left_counts.index)
    )
    if join_type == "inner":
        return matched
    if join_type == "left":
        unmatched = left_counts[~left_counts.index.isin(right_counts.index)].sum()
        return int(matched + unmatched)
    unmatched = right_counts[~right_counts.index.isin(left_counts.index)].sum()
    return int(matched + unmatched)


def suggest_join(
    frame_left: pd.DataFrame, frame_right: pd.DataFrame, *, left_id: str, right_id: str
) -> JoinSuggestion:
    candidates: list[dict[str, Any]] = []
    for left_key in frame_left.columns:
        for right_key in frame_right.columns:
            left_stats = _stats(frame_left, str(left_key))
            right_stats = _stats(frame_right, str(right_key))
            type_compatible = _types_compatible(frame_left[left_key], frame_right[right_key])
            left_values = set(frame_left[left_key].dropna().astype(str))
            right_values = set(frame_right[right_key].dropna().astype(str))
            matched = len(left_values.intersection(right_values))
            match_ratio = matched / max(len(left_values), 1)
            same_name = str(left_key).casefold() == str(right_key).casefold()
            if not same_name and (not type_compatible or match_ratio == 0):
                continue
            expected_rows = _expected_join_rows(
                frame_left,
                frame_right,
                left_keys=[str(left_key)],
                right_keys=[str(right_key)],
            )
            relation = "one_to_one"
            if left_stats["unique_ratio"] < 0.999 and right_stats["unique_ratio"] >= 0.999:
                relation = "many_to_one"
            elif left_stats["unique_ratio"] >= 0.999 and right_stats["unique_ratio"] < 0.999:
                relation = "one_to_many"
            elif left_stats["unique_ratio"] < 0.999 and right_stats["unique_ratio"] < 0.999:
                relation = "many_to_many"
            candidates.append(
                {
                    "left_key": str(left_key),
                    "right_key": str(right_key),
                    "left_type": str(frame_left[left_key].dtype),
                    "right_type": str(frame_right[right_key].dtype),
                    "type_compatible": type_compatible,
                    "match_ratio": round(match_ratio, 6),
                    "relation": relation,
                    "left_stats": left_stats,
                    "right_stats": right_stats,
                    "expected_output_rows": expected_rows,
                    "expected_growth_factor": round(
                        expected_rows / max(len(frame_left), len(frame_right), 1), 6
                    ),
                }
            )
    candidates.sort(key=lambda item: (-item["match_ratio"], item["left_key"], item["right_key"]))
    return JoinSuggestion(left_id, right_id, tuple(candidates))


def execute_safe_join(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    left_keys: list[str],
    right_keys: list[str],
    join_type: str = "inner",
    max_growth_factor: float = 2.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if len(left_keys) != len(right_keys) or not left_keys:
        raise AppError("JOIN_KEYS_INVALID", "联表字段数量不一致", 422)
    if join_type not in {"inner", "left", "right"}:
        raise AppError("JOIN_TYPE_INVALID", "联表类型不支持", 422)
    if any(key not in left.columns for key in left_keys):
        raise AppError("JOIN_KEY_NOT_FOUND", "左侧联表字段不存在", 404)
    if any(key not in right.columns for key in right_keys):
        raise AppError("JOIN_KEY_NOT_FOUND", "右侧联表字段不存在", 404)
    for left_key, right_key in zip(left_keys, right_keys):
        if not _types_compatible(left[left_key], right[right_key]):
            raise AppError("JOIN_KEY_TYPE_MISMATCH", "联表字段类型不兼容", 422)
    left_unique = len(left.drop_duplicates(subset=left_keys)) == len(left)
    right_unique = len(right.drop_duplicates(subset=right_keys)) == len(right)
    relation = (
        "one_to_one"
        if left_unique and right_unique
        else (
            "many_to_one"
            if not left_unique and right_unique
            else ("one_to_many" if left_unique and not right_unique else "many_to_many")
        )
    )
    if relation == "many_to_many":
        raise AppError("JOIN_MANY_TO_MANY_BLOCKED", "many-to-many联表默认禁止", 409)
    result = left.merge(
        right,
        left_on=left_keys,
        right_on=right_keys,
        how=join_type,
        suffixes=("_left", "_right"),
    )
    max_input = max(len(left), len(right), 1)
    growth_factor = len(result) / max_input
    if growth_factor > max_growth_factor:
        raise AppError(
            "JOIN_ROW_EXPLOSION",
            "联表结果行数膨胀超过安全阈值",
            409,
            {"growth_factor": round(growth_factor, 4), "max_growth_factor": max_growth_factor},
        )
    left_values = set(
        left[left_keys].dropna().astype(str).astype(str).agg("\x1f".join, axis=1)
    )
    right_values = set(
        right[right_keys].dropna().astype(str).astype(str).agg("\x1f".join, axis=1)
    )
    match_ratio = len(left_values.intersection(right_values)) / max(len(left_values), 1)
    return result, {
        "left_keys": list(left_keys),
        "right_keys": list(right_keys),
        "join_type": join_type,
        "relation": relation,
        "match_ratio": round(match_ratio, 6),
        "left_rows": int(len(left)),
        "right_rows": int(len(right)),
        "output_rows": int(len(result)),
        "growth_factor": round(growth_factor, 6),
    }


__all__ = ["JoinSuggestion", "execute_safe_join", "suggest_join"]
