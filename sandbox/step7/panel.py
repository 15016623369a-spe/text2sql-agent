"""Compare longitudinal coverage and like-for-like totals inside the worker."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict

import pandas as pd

from protocol import CoveragePlan, ResultTable, TaskResult
from tasks import apply_filters, empty_selection, clean_label, numeric_values

_PERIOD = re.compile(r"(\d{4})(?:年|\.0+|[-/]\d{1,2}(?:[-/]\d{1,2})?(?: 00:00:00)?)?")
_TOTALS = {"全国", "全国合计", "全省合计", "全市合计", "总计", "合计", "total", "grandtotal"}
_INVALID = {"", "nan", "nat", "none", "null", "<na>"}
_REGION = {"省份", "省", "省份名称", "所属省份", "省级行政区", "province", "region"}
_RATE = re.compile(
    r"人均|户均|平均|每百|每千|每万|比例|比重|占比|增速|增长|是否|标志|标记|来源|补录|"
    r"普及率|率(?:[（(]|$)|per[_\s-]*capita|ratio|rate|percentage|index|flag",
    re.I,
)


def _text(value) -> str:
    if pd.isna(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _year(value) -> int | None:
    match = _PERIOD.fullmatch(_text(value))
    if match and 1000 <= int(match[1]) <= 2999:
        return int(match[1])
    return None


def _key(value) -> str:
    return re.sub(r"^(\d+)\.0+$", r"\1", re.sub(r"\s+", "", _text(value)).casefold())


def _percent(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before <= 0:
        return None
    value = (after - before) / before * 100
    return value if math.isfinite(value) else None


def _percent_text(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "不适用"


def _sum(values) -> float:
    result = math.fsum(values)
    if not math.isfinite(result):
        raise ValueError("指标数值超出可计算范围，请检查数量级")
    return result


def _total_change(old: dict, new: dict) -> tuple[list, set]:
    common = old.keys() & new.keys()
    a, b = (_sum(values.values()) if values else None for values in (old, new))
    common_a, common_b = (_sum(values[key] for key in common) if common else None for values in (old, new))
    old_only = _sum(old[key] for key in old.keys() - common)
    new_only = _sum(new[key] for key in new.keys() - common)
    return [
        len(old), len(new), len(common), a, b, _percent(a, b), common_a, common_b,
        _percent(common_a, common_b), old_only, new_only,
    ], common


def analyze_panel(frame: pd.DataFrame, plan: CoveragePlan) -> TaskResult:
    headings = [clean_label(name) or f"列 {i + 1}" for i, name in enumerate(frame.columns)]
    dimensions = [plan.year, *plan.keys]
    fields = [*dimensions, *plan.labels, *plan.measures]
    if any(index >= len(headings) for index in fields):
        raise ValueError("样本覆盖计划引用了不存在的字段")
    if len(set(dimensions)) != len(dimensions) or len(set(plan.labels)) != len(plan.labels):
        raise ValueError("年份、实体标识或展示字段设置不正确")
    if len(set(plan.measures)) != len(plan.measures) or set(plan.measures) & set([*dimensions, *plan.labels]):
        raise ValueError("可加总指标必须独立于年份、城市和代码字段")
    if any(_RATE.search(headings[index]) for index in plan.measures):
        return TaskResult(status="clarification", message="请指定可加总的原始指标，例如总量、人口或用户数。人均、比例和来源标记需另算。")

    frame, scope = apply_filters(frame, plan.filters)
    if scope.conditions and not scope.matched_rows:
        return empty_selection(scope)
    label_fields = plan.labels or plan.keys
    region_position = next((i for i, col in enumerate(label_fields) if _key(headings[col]) in _REGION), None)
    grouped: dict[int, dict[tuple, int | None]] = defaultdict(dict)
    displays: dict[tuple, tuple] = {}
    rows_per_year: Counter = Counter()
    duplicates: Counter = Counter()
    invalid_rows = total_rows = 0
    for pos, row in enumerate(frame.itertuples(index=False, name=None)):
        year = _year(row[plan.year])
        key = tuple(_key(row[i]) for i in plan.keys)
        labels = tuple(clean_label(row[i], 85) if _key(row[i]) not in _INVALID else "" for i in label_fields)
        if any(_key(value) in _TOTALS for value in [*key, *labels]):
            total_rows += 1
            continue
        if year is None or any(value in _INVALID for value in key):
            invalid_rows += 1
            continue
        rows_per_year[year] += 1
        if key in grouped[year]:
            duplicates[year] += 1
            grouped[year][key] = None
        else:
            grouped[year][key] = pos
            displays[year, key] = labels

    years = sorted(grouped)
    if len(years) < 2:
        return TaskResult(status="clarification", message="样本覆盖对照需要至少两个有效年份，请核对年份和实体字段。")
    requested = sorted(set(plan.years))
    if len(requested) == 1:
        requested = [requested[0] - 1, requested[0]]
    before, after = requested or years[-2:]
    if before not in grouped or after not in grouped:
        return TaskResult(status="clarification", message=f"表内缺少 {before} 或 {after} 年，已识别年份范围为 {years[0]}–{years[-1]}。")

    entities = {year: set(group) for year, group in grouped.items()}
    old_keys, new_keys = entities[before], entities[after]
    exited, entered = old_keys - new_keys, new_keys - old_keys
    common = old_keys & new_keys
    fixed = set.intersection(*(entities[year] for year in years))
    lines = [
        f"{before} 年覆盖 {len(old_keys):,} 个实体，{after} 年覆盖 {len(new_keys):,} 个。"
        f"两年共有 {len(common):,} 个，退出样本 {len(exited):,} 个，新增 {len(entered):,} 个。",
        f"{years[0]}–{years[-1]} 年每个已收录年份都有记录的固定样本为 {len(fixed):,} 个实体。"
        "年度表列出了各年的覆盖范围。",
    ]
    if all(entities[year] == new_keys for year in years if year >= after):
        lines.append(f"{after} 年起，表内各年覆盖的是同一批 {len(new_keys):,} 个实体。")
    if exited and region_position is not None:
        regions = Counter(displays[before, key][region_position] for key in exited)
        regions.pop("", None)
        if regions:
            lines.append("退出样本按地区分布：" + "、".join(f"{name} {count} 个" for name, count in regions.most_common(5)) + "。")
    if invalid_rows or total_rows or duplicates:
        lines.append(
            f"已排除 {invalid_rows:,} 行年份或标识无效记录、{total_rows:,} 行汇总行。"
            f"发现 {sum(duplicates.values()):,} 行重复实体年份记录，覆盖计数去重，指标合计排除对应重复键。"
        )
    lines.append("名单可以核实覆盖断点。退出或新增的具体原因需结合原始来源说明、行政区划代码或跨年对照表确认。")

    annual_rows = []
    previous = None
    for year in years:
        keys = entities[year]
        annual_rows.append([
            year, rows_per_year[year], len(keys), len(keys & fixed),
            None if previous is None else len(previous - keys),
            None if previous is None else len(keys - previous), duplicates[year],
        ])
        previous = keys
    changes = [
        [status, *displays[year, key], " / ".join(key)]
        for status, year, difference in (("退出样本", before, exited), ("新增覆盖", after, entered))
        for key in sorted(difference)
    ]
    tables = [
        ResultTable.from_rows("年度覆盖", ["年份", "有效记录行数", "去重实体数", "全期固定样本", "较前期退出", "较前期新增", "重复行数"], annual_rows),
        ResultTable.from_rows(f"{before}→{after} 实体进出", ["变化", *(headings[i] for i in label_fields), "用于匹配的标识"], changes),
    ]
    pair_rows, series_rows = [], []
    for index in plan.measures:
        values = numeric_values(frame.iloc[:, index])
        metric = headings[index]
        valid = {
            year: {key: float(values.iloc[pos]) for key, pos in group.items() if pos is not None and pd.notna(values.iloc[pos])}
            for year, group in grouped.items()
        }
        old_values, new_values = valid[before], valid[after]
        comparison, common_valid = _total_change(old_values, new_values)
        coverage_effect = _sum(new_values[key] for key in entered if key in new_values) - _sum(
            old_values[key] for key in exited if key in old_values
        )
        availability_effect = _sum(new_values[key] for key in common - common_valid if key in new_values) - _sum(
            old_values[key] for key in common - common_valid if key in old_values
        )
        pair_rows.append([metric, *comparison, coverage_effect, availability_effect])
        fixed_valid = set.intersection(*(set(valid[year]) for year in years))
        for year in years:
            numbers = valid[year]
            series_rows.append([
                year, metric, len(numbers), _sum(numbers.values()) if numbers else None,
                len(fixed_valid), _sum(numbers[key] for key in fixed_valid) if fixed_valid else None,
            ])
        lines.append(
            f"{metric}的当年可用样本合计变化 {_percent_text(comparison[5])}，"
            f"两年都有有效值的 {len(common_valid):,} 个共同实体变化 {_percent_text(comparison[8])}。"
        )
    if pair_rows:
        tables.extend([
            ResultTable.from_rows(f"{before}→{after} 同口径对照", [
                "指标", f"{before}有效实体数", f"{after}有效实体数", "共同有效实体数",
                f"{before}当年合计", f"{after}当年合计", "当年合计变化（%）",
                f"{before}共同样本合计", f"{after}共同样本合计", "同口径变化（%）",
                f"仅{before}有有效值的合计", f"仅{after}有有效值的合计",
                "实体进出对合计的净影响", "共同实体缺失值对合计的净影响",
            ], pair_rows),
            ResultTable.from_rows("固定样本时序", ["年份", "指标", "当年有效实体数", "当年可用样本合计", "全期有效固定实体数", "固定样本合计"], series_rows),
        ])
        lines.append("两年合计差额可拆成共同有效样本的变化、实体进出的净影响、共同实体缺失值的净影响。长期趋势用各年均有有效值的固定样本。")
    else:
        lines.append("要量化覆盖变化对合计的影响，请指定可加总的原始指标。可先用两年交集比较同比，用全期固定样本比较长期走势。")
    lines.append("固定样本结果代表共同覆盖范围。发布全国总量时，请另用口径一致的全覆盖总量或补齐未覆盖地区。")
    return TaskResult(message="\n".join(lines), tables=tables, scope=scope)
