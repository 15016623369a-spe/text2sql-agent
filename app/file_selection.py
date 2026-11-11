"""Ground simple rankings and check that model plans preserve explicit selection."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.file_formula import normalize
from sandbox.step7.protocol import (
    MAX_RANK_ROWS, AggregatePlan, AuditPlan, CoveragePlan, FileSchema, Filter, OrderBy, Plan, SheetSchema,
)

_NUMBER = r"[0-9]+|[一二两三四五六七八九十百]+"
_TOP = re.compile(
    rf"(?:(?P<direction>最多|最高|最大|最少|最低|最小|倒数|前|后)\s*(?:的\s*)?(?P<n>{_NUMBER})(?![0-9年月日])|"
    rf"\b(?P<english>top|bottom)\s*(?P<en>{_NUMBER})(?![0-9]))", re.I,
)
_ORDINAL = re.compile(rf"第\s*(?:{_NUMBER})\s*(?:名|位)", re.I)
_RANKING = re.compile(r"排名|排序|排行榜|\brank(?:ing)?\b|\bsort\b", re.I)
_YEAR = r"(?:18|19|20|21)[0-9]{2}"
_YEAR_FIELDS = {"年", "年份", "时间", "年度", "统计年份", "统计年度", "统计时间", "year", "yr"}
_CITIES = {"城市", "地市", "城市名称", "地级市", "市", "city"}
_PROVINCES = {"省", "省份", "省份名称", "省级行政区", "province"}
_FILTER_CUE = re.compile(
    r"筛选|过滤|剔除|排除|只(?:统计|保留|看|比较)|仅(?:统计|保留|看|比较|限)|限于|"
    r"高于|低于|大于|小于|不少于|不超过|等于|\bwhere\b|\bfilter\b", re.I,
)
_WITHIN = re.compile(
    r"(?:每|各)(?:个)?(?:年|月|省(?:份)?|城市|类别|地区|部门)"
    r"(?:各取|各自|内|分别).{0,20}(?:前|后|最多|最高|最少|最低|top)|"
    r"每(?:个)?省(?:份)?.{0,18}(?:前|后|最多|最高|最少|最低|top).{0,12}(?:城市|地市)",
    re.I,
)
_ALIASES = {
    "人口": {"常住人口", "人口", "年末常住人口", "年末人口"},
    "电信业务": {"电信业务总量", "电信业务量", "电信业务"},
}
_SIMPLE = re.compile(
    rf"(?P<year>{_YEAR})年(?:的)?(?P<metric>[^，,。；;？?]+?)"
    rf"(?P<direction>最多|最高|最大|最少|最低|最小|前|倒数)(?:的)?(?P<n>{_NUMBER})"
    r"(?:个|名|位)?(?:的)?(?P<dimension>省份|省|城市|地市)"
    r"(?P<chart>[，,]?(?:并|再)?(?:画图|绘图|生成图表))?[。？?]?"
)
_CN_DIGITS = {name: index for index, name in enumerate("零一二三四五六七八九")} | {"两": 2}


def _number(token: str) -> int:
    if token.isdecimal():
        return int(token)
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if token == "一百" or token == "百":
        return 100
    match = re.fullmatch(r"([一二三四五六七八九])?十([一二三四五六七八九])?", token)
    if match:
        return 10 * _CN_DIGITS.get(match[1], 1) + _CN_DIGITS.get(match[2], 0)
    return 0


def _direction(token: str) -> str:
    return "asc" if token in {"最少", "最低", "最小", "倒数", "后", "bottom"} else "desc"


@dataclass(frozen=True)
class Selection:
    years: Filter | None
    rank: tuple[int, str] | None
    sorting: bool
    filtering: bool
    unsupported: str | None
    explicit_scope: bool


def selection(question: str, *, coverage: bool = False, audit: bool = False) -> Selection:
    text = unicodedata.normalize("NFKC", question)
    compact = normalize(question)
    years = None
    # A bare pair of years in a coverage question identifies the comparison, not the data subset.
    explicit_scope = bool(re.search(r"(?:仅|只)(?:统计|保留|看|比较|分析)|筛选|过滤|限定范围", compact))
    if not coverage or explicit_scope:
        span = re.search(rf"(?P<start>{_YEAR})年?(?:到|至|[-—–~])(?P<end>{_YEAR})年", compact)
        since = re.search(rf"(?P<year>{_YEAR})年(?:起|以来|及以后|之后)", compact)
        until = re.search(rf"(?P<year>{_YEAR})年(?:及以前|及之前|以前|之前)", compact)
        listed = re.search(rf"((?:{_YEAR}年?(?:、|,|和|及|与))+{_YEAR})年", compact)
        exact = re.findall(rf"(?<!\d)({_YEAR})年|(?:年份|年度|year)(?:为|是|=|:)?({_YEAR})(?!\d)", compact)
        if span and int(span["start"]) <= int(span["end"]):
            years = Filter(column=0, operator="between", value=[int(span["start"]), int(span["end"])])
        elif since:
            years = Filter(column=0, operator="gt" if "之后" in since[0] else "ge", value=int(since["year"]))
        elif until:
            years = Filter(column=0, operator="le" if "及" in until[0] else "lt", value=int(until["year"]))
        else:
            values = list(dict.fromkeys(int(year) for year in (
                re.findall(_YEAR, listed[1]) if listed else [a or b for a, b in exact]
            )))
            if len(values) == 1:
                years = Filter(column=0, operator="eq", value=values[0])
            elif 1 < len(values) <= 20:
                years = Filter(column=0, operator="in", value=values)
    ranking = _TOP.search(text) if not audit and not coverage else None
    rank = (_number(ranking["n"] or ranking["en"]), _direction((ranking["direction"] or ranking["english"]).casefold())) if ranking else None
    unsupported = None
    if rank and not 1 <= rank[0] <= MAX_RANK_ROWS:
        unsupported = f"一次可以返回前 1 到 {MAX_RANK_ROWS} 名，请调整条数。"
    elif not (audit or coverage) and _ORDINAL.search(text):
        unsupported = "当前可以返回前 N 名。请改问「前 N 名」，再从表中查看指定名次。"
    elif rank and _WITHIN.search(text):
        unsupported = "当前可以对所有分组做一次总排名。按每组分别选前 N 名，请先指定要分析的分组。"
    return Selection(
        years, rank, bool(rank or (not (audit or coverage) and _RANKING.search(text))),
        bool(years or (not audit and _FILTER_CUE.search(text))), unsupported, explicit_scope,
    )


def _metric_label(column: str) -> str:
    return re.sub(r"\([^()]*\)$", "", normalize(column))


def grounded_rank(schema: FileSchema, question: str, wanted: Selection) -> tuple[int, AggregatePlan] | None:
    """Use a local plan only when the entire question matches this supported pattern."""
    text = normalize(question)
    names = [sheet for sheet in schema.sheets if len(normalize(sheet.name)) >= 3 and text.startswith(normalize(sheet.name) + ":")]
    if len(names) == 1:
        text = text.removeprefix(normalize(names[0].name) + ":")
    match = _SIMPLE.fullmatch(text)
    if not match or wanted.rank is None or wanted.years is None:
        return None
    # Control words inside the metric must go to the model or a clarification.
    metric = match["metric"]
    if _FILTER_CUE.search(metric) or re.search(r"按|每|各|均值|平均|中位数|最多|最大|最小|和|与|或|不含", metric):
        return None
    is_province = match["dimension"] in _PROVINCES
    if is_province and (
        re.search(r"人均|均值|平均|比例|占比|比率|增速|增长率|每|率$", metric)
        or not re.search(r"人口|业务|收入|金额|总量|总数|销售额|用户|产量|面积", metric)
    ):
        return None
    metrics = _ALIASES.get(metric, {metric})
    candidates = []
    for sheet in names or schema.sheets:
        years = [i for i in sheet.numeric_columns if normalize(sheet.columns[i]) in _YEAR_FIELDS]
        columns = [i for i in sheet.numeric_columns if _metric_label(sheet.columns[i]) in metrics]
        dimensions = [i for i, name in enumerate(sheet.columns)
                      if normalize(name) in (_PROVINCES if is_province else _CITIES)]
        if len(years) == len(columns) == len(dimensions) == 1:
            candidates.append((sheet, years[0], columns[0], dimensions[0]))
    if not candidates:
        return None
    # Prefer the completed analysis sheet only when explicitly labeled as such.
    filled = [item for item in candidates if normalize(item[0].name) in {"填充数据", "清洗数据"}]
    if len(candidates) != 1 and len(filled) != 1:
        return None
    sheet, year, value, dimension = (filled or candidates)[0]
    count, direction = wanted.rank
    return sheet.index, AggregatePlan(
        mode="aggregate", statistic="sum" if is_province else "records", columns=[value],
        groups=[[dimension]] if is_province else [], labels=[] if is_province else [dimension],
        chart=bool(match["chart"]), filters=[wanted.years.model_copy(update={"column": year})],
        order_by=OrderBy(column=value, direction=direction), limit=count,
    )


def _named_filters(question: str, sheet: SheetSchema):
    """Extract unambiguous, explicitly written field comparisons."""
    text = unicodedata.normalize("NFKC", question)
    operators = {"=": "eq", "等于": "eq", "!=": "ne", ">=": "ge", "<=": "le", ">": "gt", "<": "lt",
                 "≥": "ge", "≤": "le", "大于": "gt", "小于": "lt", "包含": "contains"}
    for index, name in enumerate(sheet.columns):
        label = re.escape(unicodedata.normalize("NFKC", name))
        pattern = rf'{label}\s*(>=|<=|!=|=|>|<|≥|≤|等于|大于|小于|包含)\s*["“「]?([^，,；;。！？?」”"\s]+)'
        for match in re.finditer(pattern, text):
            symbol, token = match.groups()
            if len(token) > 80:
                continue
            if index in sheet.numeric_columns and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", token):
                yield index, operators[symbol], float(token)
            elif operators[symbol] in {"eq", "ne", "contains"}:
                yield index, operators[symbol], token


def selection_problem(plan: Plan, sheet: SheetSchema, wanted: Selection, question: str) -> str | None:
    filters = getattr(plan, "filters", [])
    if wanted.filtering and not filters:
        return "当前计划未保留问题里的筛选条件。请明确筛选列与对应值。"
    for condition in filters:
        if condition.column >= len(sheet.columns):
            return "筛选计划引用了不存在的列，请确认筛选字段。"
        if condition.operator in {"gt", "ge", "lt", "le", "between"} and condition.column not in sheet.numeric_columns:
            return f"「{sheet.columns[condition.column]}」未识别为数值列，无法做大小比较。请确认筛选字段。"
    for index, operator, value in _named_filters(question, sheet) if not isinstance(plan, AuditPlan) else ():
        if not any(item.column == index and item.operator == operator and _same_values(item.value, value) for item in filters):
            return f"当前计划未保留「{sheet.columns[index]}」的筛选条件，请确认筛选值。"
    if wanted.years:
        if isinstance(plan, CoveragePlan):
            year_columns = {plan.year}
        else:
            year_columns = {i for i, label in enumerate(sheet.columns) if normalize(label) in _YEAR_FIELDS}
        if not any(condition.column in year_columns and condition.operator == wanted.years.operator
                   and _same_values(condition.value, wanted.years.value) for condition in filters):
            return "当前计划未保留所问年份。请指定存放年份的列及筛选范围。"
    if isinstance(plan, AggregatePlan):
        fields = [*plan.columns, *plan.labels, *(index for group in plan.groups for index in group)]
        if any(index >= len(sheet.columns) for index in fields):
            return "分析计划引用了不存在的字段，请确认统计列和分组列。"
        if problem := plan.rank_problem():
            return problem
        if plan.statistic not in {"count", "missing", "rows"} and not set(plan.columns) <= set(sheet.numeric_columns):
            return "统计或排序指标未识别为数值列，请确认指标字段。"
    if wanted.sorting and (not isinstance(plan, AggregatePlan) or plan.order_by is None):
        return "当前计划没有排序。请说明按哪个指标排序，以及要返回多少条。"
    if wanted.rank and (plan.limit, plan.order_by.direction) != wanted.rank:
        return f"当前计划未保留所问的排名方向或前 {wanted.rank[0]} 名要求，请确认排序指标。"
    return None


def _same_values(actual, expected) -> bool:
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return len(actual) == len(expected) and all(any(_same_values(value, item) for value in actual) for item in expected)
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and actual == expected
    return actual == expected
