"""Recompute row-level formulas with a bounded arithmetic interpreter."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pandas as pd

from protocol import (
    AuditPlan, FileFormat, Formula, FormulaBinary, FormulaColumn, FormulaNumber, ResultTable, TaskResult,
)
from tasks import clean_label, numeric_values

_MAX_DEPTH = 4
_TOP_ERRORS = 10
_MIN_TOLERANCE = 0.01
_FLOAT_EPSILON = 1e-10
_FIXED_FORMAT = re.compile(r"(?:#,##)?[0#]+(?:\.([0#]+))?")
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


def _value_exponent(value) -> int:
    text = str(value).strip().replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return -2
    if not number.is_finite():
        return -2
    if not isinstance(value, str):
        number = number.normalize()
    return min(0, number.as_tuple().exponent)


def _rounding_tolerance(value, number_format: str = "") -> float:
    exponent = _value_exponent(value)
    if number_format and not isinstance(value, str):
        parts = number_format.split(";")
        section = 2 if value == 0 and len(parts) >= 3 else int(value < 0 and len(parts) >= 2)
        part = parts[section]
        if match := _FIXED_FORMAT.fullmatch(part.strip("()-")):
            digits = match[1] or ""
            exponent = -min(len(digits), max(digits.count("0"), -exponent))
    return 0.5 * 10.0 ** exponent


def workbook_tolerances(data: bytes, file_format: FileFormat, sheet: int, targets: set[int], *,
                        data_offset: int, rows: int) -> dict[int, dict[int, float]]:
    """Read display formats for audited cells only. Index results by original body row."""
    if file_format == "csv" or not rows:
        return {}
    tolerances: dict[int, dict[int, float]] = {target: {} for target in targets}
    if file_format == "xlsx":
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            first = min(targets)
            cells = book.worksheets[sheet].iter_rows(
                min_row=data_offset + 1, max_row=data_offset + rows, min_col=first + 1, max_col=max(targets) + 1,
            )
            for index, row in enumerate(cells):
                for target in targets:
                    cell = row[target - first]
                    value = cell.value
                    if type(value) in {int, float, str}:
                        number_format = cell.number_format if not isinstance(value, str) else ""
                        tolerances[target][index] = _rounding_tolerance(value, number_format)
        finally:
            book.close()
    else:
        from xlrd import XL_CELL_NUMBER, XL_CELL_TEXT, open_workbook

        book = open_workbook(file_contents=data, formatting_info=True, on_demand=True)
        try:
            worksheet = book.sheet_by_index(sheet)
            for index in range(min(rows, worksheet.nrows - data_offset)):
                row = data_offset + index
                for target in targets:
                    if target >= worksheet.row_len(row):
                        continue
                    cell = worksheet.cell(row, target)
                    if cell.ctype in {XL_CELL_NUMBER, XL_CELL_TEXT}:
                        fmt = None
                        if cell.ctype == XL_CELL_NUMBER and cell.xf_index is not None:
                            fmt = book.format_map.get(book.xf_list[cell.xf_index].format_key)
                        tolerances[target][index] = _rounding_tolerance(cell.value, fmt.format_str if fmt else "")
        finally:
            book.release_resources()
    return tolerances


def audit_frame(frame: pd.DataFrame, plan: AuditPlan, *, data_offset: int = 1,
                display_tolerances: dict[int, dict[int, float]] | None = None) -> TaskResult:
    if len({check.target for check in plan.checks}) != len(plan.checks):
        raise ValueError("待核对的派生列重复")
    if len(set(plan.labels)) != len(plan.labels) or any(index >= len(frame.columns) for index in plan.labels):
        raise ValueError("记录定位字段无效")

    cache: dict[int, pd.Series] = {}
    threshold = (
        f"绝对容差采用你指定的 {plan.tolerance:g}（各目标列单位）。" if plan.tolerance is not None else
        f"绝对容差按目标列的显示精度取半个末位，最低为 {_MIN_TOLERANCE:g}。"
    )
    lines = ["按所列公式逐行重算。" + threshold + "差值为原值减重算值。"]
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
        rounding_tolerance = frame.iloc[:, check.target].map(_rounding_tolerance)
        if display := (display_tolerances or {}).get(check.target):
            rounding_tolerance.update(pd.Series(display))
        applied_tolerance = (rounding_tolerance.clip(lower=_MIN_TOLERANCE) if plan.tolerance is None else
                             pd.Series(plan.tolerance, index=frame.index))
        over = absolute.sub(applied_tolerance).gt(_FLOAT_EPSILON)
        rounding = comparable & absolute.gt(_FLOAT_EPSILON) & absolute.le(rounding_tolerance + _FLOAT_EPSILON)
        material = over & ~rounding
        accepted = comparable & ~over & ~rounding & absolute.gt(_FLOAT_EPSILON)
        ranked = absolute[over].sort_values(ascending=False, kind="stable")
        max_error = float(absolute[comparable].max()) if comparable.any() else None
        name = clean_label(frame.columns[check.target])
        formula = computed.text[1:-1] if computed.text.startswith("(") and computed.text.endswith(")") else computed.text

        compared, deviations = int(comparable.sum()), len(ranked)
        missing, bad, zero = int(missing_stored.sum()), int(invalid_input.sum()), int(computed.zero_divisor.sum())
        other = len(frame) - compared - missing - bad - zero
        inputs_text = "、".join(f"{clean_label(frame.columns[index])}（{unit}）" for index, unit in input_units.items())
        basis = "按表头单位推断" if check.basis == "inferred" else "按明确给出的公式与单位"
        rounded, accepted_count = int(rounding.sum()), int(accepted.sum())
        material_count, strict_rounding_count = int(material.sum()), int((rounding & over).sum())
        summary.append([
            name, check.unit, formula, inputs_text, basis, compared, deviations, max_error, missing, bad, zero, other,
            rounded, material_count, accepted_count,
        ])
        lines.append(f"{name} = {formula}。结果单位 {check.unit}，{basis}。")
        if compared:
            lines.append(
                f"可核对 {compared:,} 行，实质超差 {material_count:,} 行，舍入级差异 {rounded:,} 行，"
                f"最大绝对偏差 {max_error:.8g}。"
                + (f"其中 {strict_rounding_count:,} 行舍入级差异超出指定容差。" if strict_rounding_count else "")
                + (f"另有 {accepted_count:,} 行在适用容差内，超过显示精度的舍入范围。" if accepted_count else "")
                + (f"另有 {len(frame) - compared:,} 行无法核对，原因见汇总表。" if compared != len(frame) else "")
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
                float(applied_tolerance.at[idx]),
            ]
            for idx, error in ranked.items()
        ]
        if rows:
            details.append(ResultTable.from_rows(
                f"偏差 · {name}", [
                    "表格行号", *(f"定位 · {clean_label(frame.columns[i])}" for i in label_cols),
                    *(f"输入 · {clean_label(frame.columns[i])}" for i in inputs),
                    *(f"{label}（{check.unit}）" for label in ("原值", "重算值", "差值", "绝对偏差", "适用容差")),
                ],
                rows, preview_rows=_TOP_ERRORS,
            ))
    return TaskResult(message="\n".join(lines), tables=[
        ResultTable(
            title="公式核对汇总",
            columns=[
                "派生列", "结果单位", "重算公式", "输入单位", "口径依据", "可核对行数", "超出容差行数", "最大绝对偏差",
                "原值缺失或非数值", "输入缺失或非数值", "分母为零", "其他不可比较行数", "舍入级差异行数",
                "实质超差行数", "其他容差内差异行数",
            ],
            rows=summary, total_groups=len(summary),
        ),
        *details,
    ])
