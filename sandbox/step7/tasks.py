"""Execute bounded pandas operations on a frame inside the worker."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from html import escape

import pandas as pd
import plotly.graph_objects as go

from protocol import (
    MAX_GROUP_ROWS, AggregatePlan, Filter, ProvenancePlan, ResultScope, ResultTable, TaskRequest, TaskResult,
)

_EMPTY = {"", "na", "n/a", "none", "null", "nan", "<na>", "nat", "缺失"}
_OBSERVED = {
    "真实", "真实值", "真实观测", "实际观测", "原始观测", "原始值", "原始数据", "原始", "观测", "观测值",
    "实测", "实测值", "实际值", "实报", "官方公布", "官方数据", "未填充", "未填补", "未插补",
    "未估算", "未插值", "不填充", "不填补", "不插补", "observed", "actual", "original", "measured", "raw",
}
_IMPUTED = {
    "估算", "估算值", "估算填充", "估算填补", "估计", "填充", "填补", "补齐", "插补", "插值", "外推",
    "已填充", "已填补", "已插补", "已估算", "已插值", "线性插值", "均值填补", "均值填充", "模型估算",
    "imputed", "estimated", "filled", "interpolated", "extrapolated",
}
_TRUE = {"1", "true", "yes", "y", "是"}
_FALSE = {"0", "false", "no", "n", "否"}
_MAX_GROUP_COMBINATIONS = 1600
_STAT_LABELS = {
    "rows": "样本行数", "count": "非空数", "sum": "合计", "mean": "均值", "median": "中位数",
    "min": "最小值", "max": "最大值", "missing": "缺失数",
}


def clean_label(value, limit: int = 120) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()[:limit]


def _normalized(value) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"^([01])\.0+$", r"\1", text)


def _present(column: pd.Series) -> pd.Series:
    return column.notna() & ~column.astype("string").str.strip().str.casefold().isin(_EMPTY)


def numeric_values(column: pd.Series) -> pd.Series:
    text = column.astype("string").str.strip().mask(~_present(column))
    thousands = text.str.fullmatch(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?", na=False)
    values = pd.to_numeric(text.mask(thousands, text.str.replace(",", "", regex=False)), errors="coerce")
    return values.astype("float64").replace([float("inf"), -float("inf")], float("nan"))


_FILTER_SYMBOLS = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "in": "属于",
                   "between": "介于（含端点）", "contains": "包含"}


def _filter_mask(column: pd.Series, condition: Filter) -> pd.Series:
    op, value = condition.operator, condition.value
    present = _present(column)
    if op == "contains":
        return present & column.map(_normalized).str.contains(_normalized(value), regex=False)
    values = value if isinstance(value, list) else [value]
    if op in {"eq", "ne", "in"}:
        numbers = [item for item in values if not isinstance(item, str)]
        strings = [_normalized(item) for item in values if isinstance(item, str)]
        matches = column.map(_normalized).isin(strings) if strings else pd.Series(False, index=column.index)
        if numbers:
            numeric = numeric_values(column)
            matches |= numeric.isin(numbers)
            if op == "ne":
                present &= numeric.notna()
        return present & (~matches if op == "ne" else matches)
    numeric = numeric_values(column)
    if op == "between":
        return numeric.between(*values, inclusive="both")
    compare = {"gt": numeric.gt, "ge": numeric.ge, "lt": numeric.lt, "le": numeric.le}
    return compare[op](value)


def apply_filters(frame: pd.DataFrame, filters: list[Filter]) -> tuple[pd.DataFrame, ResultScope]:
    selected = frame
    conditions = []
    for condition in filters:
        if condition.column >= len(frame.columns):
            raise ValueError("筛选条件引用了不存在的字段")
        column = selected.iloc[:, condition.column]
        selected = selected.loc[_filter_mask(column, condition)]
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        formatted = "、".join(clean_label(value, 80) for value in values)
        conditions.append(f"{clean_label(column.name)} {_FILTER_SYMBOLS[condition.operator]} {formatted}")
    return selected, ResultScope(input_rows=len(frame), matched_rows=len(selected), conditions=conditions)


def empty_selection(scope: ResultScope) -> TaskResult:
    return TaskResult(
        status="clarification", scope=scope,
        message=f"筛选条件「{' 且 '.join(scope.conditions)}」匹配 0 / {scope.input_rows:,} 行。请检查筛选值或放宽条件。",
    )


def _metric_name(label: str) -> str:
    name = re.sub(r"\([^()]*\)", "", _normalized(label))
    return re.sub(r"[\s_-]+", "", name)


def _marker_matches(metric: str, marker: str) -> bool:
    name = _metric_name(marker)
    base = re.sub(
        r"(?:(?:是否|有无)?(?:为|已)?(?:填充|填补|插补|插值|估算|真实观测|原始观测)"
        r"(?:标志|标记|状态)?|数据来源|来源标记|来源|状态|标记|isfilled|isestimated|isimputed|provenance|source|flag)$",
        "", name,
    )
    return base in {"", "数据", _metric_name(metric)}


def _marker_direction(label: str) -> str | None:
    name = re.sub(r"[\s_-]+", "", _normalized(label))
    if re.search(r"是否(?:为|经过|已)?(?:填充|填补|插补|插值|估算)|(?:is|was)(?:imputed|filled|estimated|interpolated)", name):
        return "imputed"
    if re.search(r"是否(?:为)?(?:真实|原始|实际观测|实测|观测)|(?:is|was)(?:observed|actual|measured)", name):
        return "observed"
    return None


def _source_status(value, one: str | None) -> str:
    code = _normalized(value)
    if code in _OBSERVED:
        return "observed"
    if code in _IMPUTED:
        return "imputed"
    if one and code in _TRUE:
        return one
    if one and code in _FALSE:
        return "observed" if one == "imputed" else "imputed"
    return "unknown"


def _groups(frame: pd.DataFrame, columns: list[int]):
    if not columns:
        return [((), range(len(frame)))]
    names = [
        frame.iloc[:, col].astype("string").str.strip().where(_present(frame.iloc[:, col]))
        for col in columns
    ]
    groups = frame.groupby(names, sort=True, dropna=False).indices
    if len(groups) > _MAX_GROUP_COMBINATIONS:
        raise ValueError("分组过多，请改用年份、省份等类别列，或减少分组字段")
    return [
        (tuple("（缺失）" if pd.isna(value) else clean_label(value, 64)
               for value in (key if isinstance(key, tuple) else (key,))), index)
        for key, index in groups.items()
    ]


def _table(headings: list[str], groups: list[int], columns: list[str], rows: list[list]) -> ResultTable:
    dimensions = [headings[col] for col in groups]
    return ResultTable(
        title="按" + " × ".join(dimensions) if groups else "整体",
        columns=dimensions + columns, rows=rows[:MAX_GROUP_ROWS], total_groups=len(rows),
    )


def _share(count: int, denominator: int) -> float | None:
    return round(100 * count / denominator, 2) if denominator else None


def _provenance(
    frame: pd.DataFrame, headings: list[str], request: TaskRequest, plan: ProvenancePlan, scope: ResultScope,
) -> TaskResult:
    markers = Counter(field.marker for field in plan.fields)
    statuses = {}
    errors = []
    for field in plan.fields:
        metric, marker = headings[field.metric], headings[field.marker]
        shared = request.shared_marker or re.search(r"共同|全指标|所有指标|整行|shared|record", marker, re.I)
        if not shared and (markers[field.marker] > 1 or not _marker_matches(metric, marker)):
            errors.append(
                f"「{marker}」无法确认是「{metric}」的来源标记。请指定对应的标记列。"
                "若一列适用于整行，请补充「所有指标共用同一来源标记列」。"
            )
            continue
        inferred = _marker_direction(marker)
        flags = frame.iloc[:, field.marker].map(lambda value: _source_status(value, inferred or request.binary_one))
        present = _present(frame.iloc[:, field.metric])
        known = flags.isin({"observed", "imputed"})
        if present.any() and not (present & known).any():
            errors.append(
                f"「{metric}」的来源列「{marker}」没有可确认的标记。若它使用 0/1，"
                "请在问题中写明含义，例如「0=真实观测，1=估算填充」。"
            )
        statuses[field.metric] = (flags.where(present, "unknown"), present)
    if errors:
        return TaskResult(status="clarification", message="\n".join(dict.fromkeys(errors)))

    tables, findings = [], []
    lines = ["分母为每个指标在该分组中的数据行数。空值和无法识别的标记计入「来源未确认」。"]
    for dimensions in [[], *plan.groups]:
        rows = []
        concentrations: dict[int, list[tuple[int, int, str]]] = {field.metric: [] for field in plan.fields}
        for group, index in _groups(frame, dimensions):
            size = len(index)
            for field in plan.fields:
                flags, present = statuses[field.metric]
                counts = flags.iloc[index].value_counts()
                observed, imputed = int(counts.get("observed", 0)), int(counts.get("imputed", 0))
                unknown = size - observed - imputed
                missing = size - int(present.iloc[index].sum())
                name, marker = headings[field.metric], headings[field.marker]
                rows.append([*group, name, marker, size, observed, _share(observed, size),
                             imputed, _share(imputed, size), unknown, _share(unknown, size), missing])
                if dimensions and imputed:
                    concentrations[field.metric].append((imputed, size, " / ".join(group)))
                elif not dimensions and size:
                    lines.append(
                        f"{name}（标记列：{marker}）共 {size:,} 行。真实观测 {observed:,}（{_share(observed, size):.2f}%），"
                        f"估算填充 {imputed:,}（{_share(imputed, size):.2f}%），来源未确认 {unknown:,}（{_share(unknown, size):.2f}%）。"
                    )
        for field in plan.fields:
            if candidates := concentrations[field.metric]:
                ranked = sorted(candidates, key=lambda item: (-item[0], -item[0] / item[1], item[2]))[:3]
                group_name = " × ".join(headings[index] for index in dimensions)
                labels = "、".join(f"{label} {count:,}/{size:,}（{_share(count, size):.2f}%）" for count, size, label in ranked)
                findings.append(f"{headings[field.metric]}按{group_name}看，填充较多的分组为 {labels}。")
        tables.append(_table(headings, dimensions, [
            "指标", "来源标记列", "样本数", "真实观测数", "真实观测占比（%）", "估算填充数",
            "估算填充占比（%）", "来源未确认数", "来源未确认占比（%）", "指标缺失数",
        ], rows))
    if findings:
        lines.extend(["按填充数量比较分组：", *findings])
    elif plan.groups:
        lines.append("各分组均未确认到估算填充记录。")
    figure = None
    if plan.chart:
        names = tables[0].columns
        measures = ["真实观测占比（%）", "估算填充占比（%）", "来源未确认占比（%）"]
        figure = _figure(tables[0], [names.index("指标")], [names.index(name) for name in measures], scope)
    return TaskResult(message="\n".join(lines), tables=tables, figure=figure)


def _rank_table(title: str, names: list[str], rows: list[list], metric: int, plan: AggregatePlan) -> ResultTable:
    # Python's stable sort keeps input order for ties. Groups arrive in sorted key order.
    valid = [row for row in rows if row[metric] is not None]
    valid.sort(key=lambda row: row[metric], reverse=plan.order_by.direction == "desc")
    return ResultTable(title=title, columns=names, rows=valid[:plan.limit or MAX_GROUP_ROWS],
                       total_groups=len(valid), limit=plan.limit)


def _records(frame: pd.DataFrame, headings: list[str], plan: AggregatePlan, scope: ResultScope) -> TaskResult:
    dimensions = [headings[index] for index in plan.labels]
    measures = [headings[index] for index in plan.columns]
    identifiers = ["数据行号", *dimensions]
    numbers = [numeric_values(frame.iloc[:, index]) for index in plan.columns]
    labels = []
    for index in plan.labels:
        column = frame.iloc[:, index]
        labels.append(column.map(lambda value: clean_label(value, 64)).where(_present(column), "（缺失）"))
    rows = [
        [int(index) + 1, *(column.iloc[i] for column in labels),
         *(None if pd.isna(value) else float(value) for value in values)]
        for i, (index, values) in enumerate(zip(frame.index, zip(*numbers), strict=True))
    ]
    metric = len(identifiers) + plan.columns.index(plan.order_by.column)
    table = _rank_table("明细排名", identifiers + measures, rows, metric, plan)
    if not table.rows:
        return TaskResult(status="clarification", message="筛选后没有可排序的有效数值，请检查指标或筛选条件。")
    message = f"按{table.columns[metric]}{'降序' if plan.order_by.direction == 'desc' else '升序'}排列原表明细。"
    message += f"\n共有 {table.total_groups:,} 条可排名记录，排序值无效或缺失的记录不参与排名。并列按原表行序。"
    figure = _figure(table, list(range(len(identifiers))), [metric], scope) if plan.chart else None
    return TaskResult(message=message, tables=[table], figure=figure)


def _aggregate(frame: pd.DataFrame, headings: list[str], plan: AggregatePlan, scope: ResultScope) -> TaskResult:
    if plan.statistic == "records":
        return _records(frame, headings, plan, scope)
    label = _STAT_LABELS[plan.statistic]
    series = {}
    for index in plan.columns:
        column = frame.iloc[:, index]
        present = _present(column)
        if plan.statistic in {"count", "missing"}:
            series[index] = present
        else:
            numbers = numeric_values(column)
            if present.any() and not numbers.notna().any():
                return TaskResult(status="clarification", message=f"「{headings[index]}」没有可计算{label}的数值。请选择数值列。")
            series[index] = numbers

    tables = []
    for dimensions in [[], *plan.groups]:
        rows = []
        for group, index in _groups(frame, dimensions):
            values = []
            for column in series.values():
                selected = column.iloc[index]
                if plan.statistic in {"count", "missing"}:
                    count = int(selected.sum())
                    values.append(count if plan.statistic == "count" else len(selected) - count)
                elif selected.notna().any():
                    value = float(selected.agg(plan.statistic))
                    values.append(value if math.isfinite(value) else None)
                else:
                    values.append(None)
            rows.append([*group, *(values if plan.statistic != "rows" else [len(index)])])
        names = [f"{headings[index]} · {label}" for index in plan.columns] if plan.columns else [label]
        if dimensions and plan.order_by is not None:
            metric = len(dimensions) + (plan.columns.index(plan.order_by.column) if plan.columns else 0)
            dimension_names = [headings[index] for index in dimensions]
            tables.append(_rank_table("按" + " × ".join(dimension_names) + "排名",
                                      dimension_names + names, rows, metric, plan))
        else:
            tables.append(_table(headings, dimensions, names, rows))
    description = f"对 {len(frame):,} 行数据计算{label}。"
    if tables[0].rows:
        totals = zip(tables[0].columns, tables[0].rows[0], strict=True)
        description += "\n" + "；".join(f"{name}：{value:,.12g}" if value is not None else f"{name}：暂无值" for name, value in totals)
    if plan.statistic in {"sum", "mean", "median", "min", "max"}:
        description += "\n数值计算忽略空值、非数值和无穷值。"
    figure = None
    if plan.order_by is not None:
        if all(not table.rows for table in tables[1:]):
            return TaskResult(status="clarification", message="筛选后没有可排名的分组，请检查指标或筛选条件。")
        description += "\n先计算各组指标，再排序。并列按分组键顺序；缺少有效排序指标的分组不参与排名。"
    if plan.chart and plan.groups:
        count = len(plan.groups[0])
        figure = _figure(tables[1], list(range(count)), list(range(count, len(tables[1].columns))), scope)
    if plan.order_by is not None:
        tables = [*tables[1:], tables[0]]
    return TaskResult(message=description, tables=tables, figure=figure)


def _figure(table: ResultTable, dimensions: list[int], measures: list[int], scope: ResultScope | None = None) -> dict:
    rows = table.rows
    labels = [" / ".join(str(row[index]) for index in dimensions) for row in rows]
    categories = [str(index) for index in range(len(rows))]
    chart = go.Figure()
    for measure in measures[:6]:
        chart.add_bar(x=categories, y=[row[measure] for row in rows], name=escape(table.columns[measure]),
                      text=[escape(label) for label in labels], hovertemplate="%{text}<br>%{y}<extra></extra>")
    selection = f" · {len(rows)} / {table.total_groups} 条"
    if scope:
        selection += " · " + ("且".join(scope.conditions) if scope.conditions else "全量数据")
    chart.update_layout(
        title=escape(table.title + selection), barmode="group", template="plotly_white",
        yaxis_title=escape(" / ".join(table.columns[index] for index in measures[:6])),
    )
    chart.update_xaxes(title=escape(" × ".join(table.columns[index] for index in dimensions)),
                      type="category", categoryorder="array", categoryarray=categories,
                      tickvals=categories, ticktext=[escape(label) for label in labels])
    return json.loads(chart.to_json())


def analyze_task(frame: pd.DataFrame, request: TaskRequest) -> TaskResult:
    plan = request.plan
    headings = [clean_label(name) or f"列 {i + 1}" for i, name in enumerate(frame.columns)]
    groups = [tuple(group) for group in plan.groups]
    if len(set(groups)) != len(groups) or any(len(set(group)) != len(group) for group in groups):
        raise ValueError("分组字段重复")
    if isinstance(plan, ProvenancePlan):
        metric_ids = [field.metric for field in plan.fields]
        marker_ids = [field.marker for field in plan.fields]
        if len(set(metric_ids)) != len(metric_ids) or set(metric_ids) & set(marker_ids):
            raise ValueError("每个指标需要一个独立于指标值的来源标记列")
        field_ids = metric_ids + marker_ids
    else:
        if problem := plan.rank_problem():
            return TaskResult(status="clarification", message=problem)
        field_ids = [*plan.columns, *plan.labels]
        if len(set(field_ids)) != len(field_ids) or (plan.statistic == "rows") == bool(field_ids):
            raise ValueError("请确认统计指标或改用样本行数")
    if any(index >= len(headings) for index in [*field_ids, *(index for group in groups for index in group)]):
        raise ValueError("分析计划引用了工作表中不存在的字段")
    selected, scope = apply_filters(frame, plan.filters)
    if scope.conditions and not scope.matched_rows:
        return empty_selection(scope)
    if isinstance(plan, ProvenancePlan):
        result = _provenance(selected, headings, request, plan, scope)
    else:
        result = _aggregate(selected, headings, plan, scope)
    return result.model_copy(update={"scope": scope})
