"""受限指标公式解析器。

公式只在这里被解析为受控AST并解释执行，绝不使用 ``eval``、``exec`` 或
动态属性访问。执行上下文由QueryPlan编译器提供，通常只包含已经绑定的
Series和已计算的指标值。
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd


class FormulaError(ValueError):
    """公式不符合受限语言时抛出。"""


_BINARY_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_AGGREGATES = {"sum", "count", "count_distinct", "mean", "min", "max"}
_FUNCTION_ARITY = {
    "sum": 1,
    "count": 1,
    "count_distinct": 1,
    "mean": 1,
    "min": 1,
    "max": 1,
    "safe_divide": 2,
}


def _ensure_number(value: Any) -> Any:
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        raise FormulaError("公式只能处理数值或布尔值")
    return value


def _safe_divide(left: Any, right: Any) -> Any:
    if isinstance(right, pd.Series):
        result = left / right.replace(0, pd.NA)
        return result.replace([math.inf, -math.inf], pd.NA)
    try:
        if right is None or pd.isna(right) or float(right) == 0:
            return None
        result = left / right
        if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
            return None
        return result
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _aggregate(name: str, value: Any) -> Any:
    if isinstance(value, pd.Series):
        if name == "sum":
            return value.sum(skipna=True)
        if name == "count":
            return value.count()
        if name == "count_distinct":
            return value.nunique(dropna=True)
        if name == "mean":
            return value.mean(skipna=True)
        if name == "min":
            return value.min(skipna=True)
        if name == "max":
            return value.max(skipna=True)
    if name == "count":
        return 0 if value is None or pd.isna(value) else 1
    if name == "count_distinct":
        return 0 if value is None or pd.isna(value) else 1
    return value


def _eval(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, context)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)) or node.value is None:
            return node.value
        raise FormulaError("公式常量类型不允许")
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise FormulaError(f"未知指标或字段: {node.id}")
        return context[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _ensure_number(_eval(node.operand, context))
        return +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, _BINARY_OPS):
        left = _ensure_number(_eval(node.left, context))
        right = _ensure_number(_eval(node.right, context))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return _safe_divide(left, right)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _eval(node.left, context)
        right = _eval(node.comparators[0], context)
        op = node.ops[0]
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        return left >= right
    if isinstance(node, ast.IfExp):
        condition = _eval(node.test, context)
        return _eval(node.body if condition else node.orelse, context)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in _FUNCTION_ARITY:
            raise FormulaError(f"函数不在白名单中: {name}")
        if node.keywords or len(node.args) != _FUNCTION_ARITY[name]:
            raise FormulaError(f"函数参数数量无效: {name}")
        args = [_eval(arg, context) for arg in node.args]
        if name == "safe_divide":
            return _safe_divide(args[0], args[1])
        return _aggregate(name, args[0])
    raise FormulaError(f"公式语法不允许: {type(node).__name__}")


def _validate_node(
    node: ast.AST, allowed_names: set[str], *, inside_aggregate: bool = False
) -> None:
    if isinstance(node, ast.Expression):
        _validate_node(node.body, allowed_names, inside_aggregate=inside_aggregate)
        return
    if isinstance(node, ast.Constant):
        if not (isinstance(node.value, (int, float, bool)) or node.value is None):
            raise FormulaError("公式常量类型不允许")
        return
    if isinstance(node, ast.Name):
        if node.id not in allowed_names:
            raise FormulaError(f"未知指标或字段: {node.id}")
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise FormulaError("一元运算符不允许")
        _validate_node(node.operand, allowed_names, inside_aggregate=inside_aggregate)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BINARY_OPS):
            raise FormulaError("二元运算符不允许")
        _validate_node(node.left, allowed_names, inside_aggregate=inside_aggregate)
        _validate_node(node.right, allowed_names, inside_aggregate=inside_aggregate)
        return
    if isinstance(node, ast.Compare):
        if (
            len(node.ops) != 1
            or len(node.comparators) != 1
            or not isinstance(node.ops[0], _COMPARE_OPS)
        ):
            raise FormulaError("比较表达式不允许链式比较")
        _validate_node(node.left, allowed_names, inside_aggregate=inside_aggregate)
        _validate_node(node.comparators[0], allowed_names, inside_aggregate=inside_aggregate)
        return
    if isinstance(node, ast.IfExp):
        _validate_node(node.test, allowed_names, inside_aggregate=inside_aggregate)
        _validate_node(node.body, allowed_names, inside_aggregate=inside_aggregate)
        _validate_node(node.orelse, allowed_names, inside_aggregate=inside_aggregate)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in _FUNCTION_ARITY:
            raise FormulaError(f"函数不在白名单中: {name}")
        if node.keywords or len(node.args) != _FUNCTION_ARITY[name]:
            raise FormulaError(f"函数参数数量无效: {name}")
        nested = inside_aggregate or name in _AGGREGATES
        if inside_aggregate and name in _AGGREGATES:
            raise FormulaError("不允许嵌套聚合函数")
        for argument in node.args:
            _validate_node(argument, allowed_names, inside_aggregate=nested)
        return
    raise FormulaError(f"公式语法不允许: {type(node).__name__}")


def _names(tree: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


@dataclass(frozen=True)
class RestrictedExpression:
    formula: str
    tree: ast.Expression
    dependencies: tuple[str, ...]

    def evaluate(self, context: dict[str, Any]) -> Any:
        return _eval(self.tree, context)


def parse_formula(formula: str, allowed_names: Iterable[str]) -> RestrictedExpression:
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("指标公式不能为空")
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise FormulaError("指标公式语法无效") from exc
    names = set(allowed_names)
    _validate_node(tree, names)
    dependencies = tuple(sorted(_names(tree)))
    return RestrictedExpression(formula=formula, tree=tree, dependencies=dependencies)


def validate_metric_formulas(
    formulas: dict[str, str],
    field_names: Iterable[str] = (),
    *,
    units: dict[str, str | None] | None = None,
    grains: dict[str, str | None] | None = None,
) -> dict[str, RestrictedExpression]:
    """校验所有指标公式并拒绝循环依赖。"""

    allowed = set(formulas) | set(field_names)
    compiled = {
        metric_id: parse_formula(formula, allowed) for metric_id, formula in formulas.items()
    }
    graph = {
        metric_id: {name for name in expression.dependencies if name in formulas}
        for metric_id, expression in compiled.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise FormulaError("指标存在循环依赖")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for metric_id in graph:
        visit(metric_id)
    if units or grains:
        for metric_id, expression in compiled.items():
            _validate_formula_metadata(
                expression.tree.body,
                units or {},
                grains or {},
            )
    return compiled


def _validate_formula_metadata(
    node: ast.AST,
    units: dict[str, str | None],
    grains: dict[str, str | None],
) -> tuple[str | None, str | None]:
    if isinstance(node, ast.Name):
        return units.get(node.id), grains.get(node.id)
    if isinstance(node, ast.BinOp):
        left_unit, left_grain = _validate_formula_metadata(node.left, units, grains)
        right_unit, right_grain = _validate_formula_metadata(node.right, units, grains)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if left_unit and right_unit and left_unit != right_unit:
                raise FormulaError("指标单位不一致")
            if left_grain and right_grain and left_grain != right_grain:
                raise FormulaError("指标时间粒度不一致")
        return left_unit or right_unit, left_grain or right_grain
    if isinstance(node, ast.UnaryOp):
        return _validate_formula_metadata(node.operand, units, grains)
    if isinstance(node, ast.Compare):
        _validate_formula_metadata(node.left, units, grains)
        for comparator in node.comparators:
            _validate_formula_metadata(comparator, units, grains)
        return None, None
    if isinstance(node, ast.IfExp):
        _validate_formula_metadata(node.test, units, grains)
        left = _validate_formula_metadata(node.body, units, grains)
        right = _validate_formula_metadata(node.orelse, units, grains)
        return left[0] or right[0], left[1] or right[1]
    if isinstance(node, ast.Call):
        values = [_validate_formula_metadata(argument, units, grains) for argument in node.args]
        return values[0] if values else (None, None)
    return None, None


__all__ = [
    "FormulaError",
    "RestrictedExpression",
    "parse_formula",
    "validate_metric_formulas",
]
