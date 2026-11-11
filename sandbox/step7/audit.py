"""Recompute row-level formulas with a bounded arithmetic interpreter."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from protocol import AuditPlan, Formula, FormulaBinary, FormulaColumn, FormulaNumber, ResultTable, TaskResult
from tasks import clean_label, numeric_values

_MAX_DEPTH = 4
_TOP_ERRORS = 10
_ABS_TOLERANCE = 0.01
_SYMBOL = {"add": "+", "subtract": "−", "multiply": "×", "divide": "÷"}


@dataclass
class Calculated:
    values: pd.Series
    zero_divisor: pd.Series
    columns: set[int]
    text: str


def _column(frame: pd.DataFrame, cache: dict[int, pd.Series], index: int) -> pd.Series:
    if index >= len(frame.columns):
        raise ValueError("核对计划引用了不存在的列")
    if index not in cache:
        cache[index] = numeric_values(frame.iloc[:, index])
    return cache[index]


def _calculate(formula: Formula, frame: pd.DataFrame, cache: dict[int, pd.Series], depth: int = 0) -> Calculated:
    if depth > _MAX_DEPTH:
        raise ValueError("公式过于复杂，请拆分成多个中间列再核对")
    if isinstance(formula, FormulaColumn):
        values = _column(frame, cache, formula.column)
        return Calculated(values, pd.Series(False, index=frame.index), {formula.column}, clean_label(frame.columns[formula.column]))
    if isinstance(formula, FormulaNumber):
        return Calculated(
            pd.Series(formula.value, index=frame.index, dtype="float64"),
            pd.Series(False, index=frame.index), set(), f"{formula.value:g}",
        )
    assert isinstance(formula, FormulaBinary)
    left = _calculate(formula.left, frame, cache, depth + 1)
    right = _calculate(formula.right, frame, cache, depth + 1)
    zero = left.zero_divisor | right.zero_divisor
    if formula.kind == "divide":
        zero |= right.values.eq(0) & left.values.notna()
        values = left.values.div(right.values.mask(right.values.eq(0)))
    else:
        operation = {"add": pd.Series.add, "subtract": pd.Series.sub, "multiply": pd.Series.mul}[formula.kind]
        values = operation(left.values, right.values)
    text = f"({left.text} {_SYMBOL[formula.kind]} {right.text})"
    return Calculated(
        values.replace([float("inf"), -float("inf")], float("nan")),
        zero, left.columns | right.columns, text,
    )


def _row_value(value):
    return None if pd.isna(value) else clean_label(value, 90)


def audit_frame(frame: pd.DataFrame, plan: AuditPlan, *, data_offset: int = 1) -> TaskResult:
    if len({check.target for check in plan.checks}) != len(plan.checks):
        raise ValueError("待核对的派生列重复")
    if len(set(plan.labels)) != len(plan.labels) or any(index >= len(frame.columns) for index in plan.labels):
        raise ValueError("记录定位字段无效")

    cache: dict[int, pd.Series] = {}
    lines = [f"按所列公式逐行重算。默认绝对容差为 {_ABS_TOLERANCE:g}（以各目标列单位计），差值为原值减重算值。"]
    summary = []
    details = []
    for check in plan.checks:
        if not check.unit or not check.unit.strip() or check.basis is None:
            return TaskResult(status="clarification", message="请确认重算公式、目标列和输入列的单位后再核对。")
        input_units = {item.column: item.unit for item in check.input_units}
        stored = _column(frame, cache, check.target)
        computed = _calculate(check.formula, frame, cache)
        if not computed.columns or check.target in computed.columns:
            raise ValueError("重算公式必须引用原始数据列，且不能引用当前待核对的派生列本身")
        if computed.columns != input_units.keys() or len(input_units) != len(check.input_units) or any(
            not unit.strip() for unit in input_units.values()
        ):
            return TaskResult(status="clarification", message="请为公式里的每个输入列指定单位后再核对。")
        invalid_input = computed.values.isna() & ~computed.zero_divisor
        missing_stored = stored.isna() & computed.values.notna()
        difference = (stored - computed.values).replace([float("inf"), -float("inf")], float("nan"))
        comparable = difference.notna()
        absolute = difference.abs()
        over = absolute.sub(_ABS_TOLERANCE).gt(1e-10)
        ranked = absolute[over].sort_values(ascending=False, kind="stable")
        max_error = float(absolute[comparable].max()) if comparable.any() else None
        name = clean_label(frame.columns[check.target])
        formula = computed.text[1:-1] if computed.text.startswith("(") and computed.text.endswith(")") else computed.text

        compared, deviations = int(comparable.sum()), len(ranked)
        missing, bad, zero = int(missing_stored.sum()), int(invalid_input.sum()), int(computed.zero_divisor.sum())
        other = len(frame) - compared - missing - bad - zero
        inputs_text = "、".join(f"{clean_label(frame.columns[index])}（{unit}）" for index, unit in input_units.items())
        basis = "按表头单位推断" if check.basis == "inferred" else "按明确给出的公式与单位"
        summary.append([name, check.unit, formula, inputs_text, basis, compared, deviations, max_error, missing, bad, zero, other])
        lines.append(f"{name} = {formula}。结果单位 {check.unit}，{basis}。")
        if compared:
            lines.append(
                f"可核对 {compared:,} 行，超出容差 {deviations:,} 行，最大绝对偏差 {max_error:.8g}。"
                f"另有 {len(frame) - compared:,} 行无法核对，原因见汇总表。"
            )
        else:
            lines.append("没有可核对的记录。请检查原值、输入值与分母。")

        label_cols = [idx for idx in plan.labels if idx != check.target and idx not in computed.columns]
        inputs = sorted(computed.columns)[:4]
        rows = [
            [
                int(idx) + data_offset + 1, *(_row_value(frame.iloc[:, col].at[idx]) for col in label_cols),
                *(float(cache[col].at[idx]) if pd.notna(cache[col].at[idx]) else None for col in inputs),
                float(stored.at[idx]), float(computed.values.at[idx]), float(difference.at[idx]), float(error),
            ]
            for idx, error in ranked.iloc[:_TOP_ERRORS].items()
        ]
        if rows:
            details.append(ResultTable(
                title=f"偏差 · {name}",
                columns=[
                    "表格行号", *(f"定位 · {clean_label(frame.columns[i])}" for i in label_cols),
                    *(f"输入 · {clean_label(frame.columns[i])}" for i in inputs),
                    *(f"{label}（{check.unit}）" for label in ("原值", "重算值", "差值", "绝对偏差")),
                ],
                rows=rows, total_groups=deviations,
            ))
    return TaskResult(message="\n".join(lines), tables=[
        ResultTable(
            title="公式核对汇总",
            columns=[
                "派生列", "结果单位", "重算公式", "输入单位", "口径依据", "可核对行数", "超出容差行数", "最大绝对偏差",
                "原值缺失或非数值", "输入缺失或非数值", "分母为零", "其他不可比较行数",
            ],
            rows=summary, total_groups=len(summary),
        ),
        *details,
    ])
