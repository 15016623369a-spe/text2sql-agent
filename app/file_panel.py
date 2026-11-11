"""Recognize and ground requests about year-over-year changes in sample coverage."""
from __future__ import annotations

import re

from app.file_formula import normalize
from sandbox.step7.protocol import CoveragePlan, FileSchema

_YEAR = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2}|21\d{2})(?!\d)")
_ENTITY = re.compile(r"城市|地市|区县|县市|县域|地区|样本|企业|门店|city|cities|entities|panel", re.I)
_CHANGE = re.compile(
    r"覆盖(?:范围|变化|断点|差异|减少)|(?:城市|地市|样本)覆盖|同口径|同一批|固定样本|可比样本|"
    r"样本变化|退出|新增覆盖|coverage|like.for.like|balanced.panel|entry|exit",
    re.I,
)
_TIME_COLUMNS = {"年", "年份", "年度", "时间", "统计年份", "统计年度", "统计时间", "日期", "year", "date"}
_CITY_CODES = {"城市代码", "地市代码", "城市编码", "地市编码", "行政区划代码", "citycode", "cityid", "city_id", "city_code"}
_CITY_NAMES = {"城市", "城市名称", "地级市", "地市", "地市名称", "城市名", "市", "市名", "区县", "县市", "地区", "city", "cityname", "city_name"}
_REGION_NAMES = {"省份", "省", "省份名称", "所属省份", "省级行政区", "province", "region"}
_ADDITIVE = re.compile(r"总量|总值|总额|总数|用户|人口|数量|金额|销售额|营业收入|余额|产量|面积|\bgdp\b", re.I)
_NONADDITIVE = re.compile(
    r"人均|户均|平均|每百|每千|每万|比例|比重|占比|增速|增长|是否|标志|标记|来源|补录|"
    r"普及率|率(?:[（(]|$)|per[_\s-]*capita|ratio|rate|percentage|index|flag",
    re.I,
)


def comparison_years(question: str) -> list[int]:
    return [int(year) for year in dict.fromkeys(_YEAR.findall(question))][:2]


def coverage_question(question: str) -> bool:
    if not _ENTITY.search(question):
        return False
    if _CHANGE.search(question):
        return True
    return len(comparison_years(question)) == 2 and bool(re.search(
        r"\d\s*个\s*(?:城市|地市|区县|县市|地区|样本|企业|门店)|"
        r"(?:城市|地市|样本|企业|门店)(?:数|数量|个数).{0,10}(?:变化|减少|增加|多少|骤降)|"
        r"(?:多少|几个|少了|多了|减少|增减).{0,8}(?:城市|地市|样本|企业|门店)|"
        r"(?:city|entity)\s+count",
        question, re.I,
    ))


def coverage_hints(schema: FileSchema, question: str) -> dict[int, CoveragePlan]:
    """Suggest columns only when the relevant names identify them unambiguously."""
    hints = {}
    text = normalize(question)
    for sheet in schema.sheets:
        names = [normalize(column) for column in sheet.columns]
        years = [i for i, name in enumerate(names) if name in _TIME_COLUMNS]
        codes = [i for i, name in enumerate(names) if name in _CITY_CODES]
        cities = [i for i, name in enumerate(names) if name in _CITY_NAMES]
        regions = [i for i, name in enumerate(names) if name in _REGION_NAMES]
        if len(years) != 1 or len(codes) > 1 or len(cities) > 1 or len(regions) > 1 or not (codes or cities):
            continue
        labels = [*regions, *cities]
        keys = codes or labels
        eligible = [
            i for i in sheet.numeric_columns
            if i not in [*years, *keys, *labels] and names[i] and not _NONADDITIVE.search(names[i])
        ]
        measures = [i for i in eligible if _ADDITIVE.search(names[i])]
        named_measures = [i for i in eligible if re.split(r"\(", names[i], maxsplit=1)[0] in text]
        hints[sheet.index] = CoveragePlan(
            mode="coverage", year=years[0], keys=keys, labels=labels,
            years=comparison_years(question), measures=(named_measures or measures)[:3],
        )
    return hints


def preferred_sheet(schema: FileSchema, hints: dict[int, CoveragePlan], question: str) -> int | None:
    text = normalize(question)
    named = [sheet.index for sheet in schema.sheets if len(normalize(sheet.name)) >= 3 and normalize(sheet.name) in text]
    if len(named) == 1:
        return named[0] if named[0] in hints else None
    rank = {index: (len(plan.labels), len(plan.measures)) for index, plan in hints.items()}
    if not rank:
        return None
    best = [index for index, value in rank.items() if value == max(rank.values())]
    return best[0] if len(best) == 1 else None
