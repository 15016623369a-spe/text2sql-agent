"""Resolve short clarification answers without using the SQL model."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Literal

from app.commerce_demo import AS_OF, Metric, QueryContext, Region

Slot = Literal["period", "metric", "region", "unknown"]
CANCEL_CLARIFICATION = "取消这次澄清"

REGION = re.compile(r"华南|华东|华北")
_MONTH_NUMBERS = {name: index for index, name in enumerate(
    ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1,
)}
_ISO_MONTH = r"20\d{2}-(?:0[1-9]|1[0-2])"
_CN_MONTH = r"(?:20\d{2}年)?(?:1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月(?:份)?"
PERIOD = (
    rf"上月|上个月|本月|这个月|(?<![\d-]){_ISO_MONTH}(?![\d-])"
    rf"|(?<!\d){_CN_MONTH}(?![0-9一二三四五六七八九十]+(?:日|号))"
)
SCOPE = rf"(?:{PERIOD}|(?:华南|华东|华北)(?:地区|区)?)"
MEASURE = r"(?:净|毛)?(?:营收|销售额|营业额)"
REFUND = r"不扣除退款|不扣退款|扣除退款|扣退款"
GROSS = ("不扣除退款", "不扣退款", "毛营收", "毛销售额", "毛营业额")
NET = ("扣除退款", "扣退款", "净营收", "净销售额", "净营业额")
METRIC: dict[str, Metric] = {**dict.fromkeys(GROSS, "gross"), **dict.fromkeys(NET, "net")}
_METRIC_IN_QUESTION = re.compile(rf"{REFUND}|(?:净|毛)(?:营收|销售额|营业额)")
_NEW_REQUEST = re.compile(
    r"^(?:请|我想|给我|麻烦)?(?:帮我|统计|汇总|比较|对比|列出|计算|查找|查询|查一下|查下|看看|看一下|读取|分析|导出|告诉我|展示)"
    r"|多少|几(?:笔|条|个|所|人)|哪些|哪(?:个|一)年|是否|是什么|如何|怎么|为什么"
)
_DEMO_PERIOD = rf"(?:{PERIOD}|那个月|某个月)"
_COUNT = re.compile(
    rf"(?:请|帮我)?(?:演示库)?(?:统计)?(?:{_DEMO_PERIOD}的?)?(?:订单(?:总数|数量|数|笔数)|"
    r"(?:一共|总共|共)?(?:有)?(?:多少|几)(?:笔|个)?订单)(?:是多少|多少|呢)?"
)
_GROUPED_COUNT = re.compile(
    rf"(?:请|帮我)?(?:分别)?(?:统计|计算|汇总)?(?:{_DEMO_PERIOD}的?)?"
    r"(?P<monthly>每(?:个)?月|逐月|按月)?的?(?P<regional>各(?:个)?地区|按地区|分地区)?"
    r"(?:统计|计算|汇总)?的?订单(?:总数|数量|数|笔数)(?:是多少|多少|呢)?"
)
_MONTH_ORDERS = re.compile(rf"(?:请|帮我)?统计{_DEMO_PERIOD}的?订单")
_GROUPED_REVENUE = re.compile(
    rf"(?:请|帮我)?(?:分别)?(?:统计|计算|汇总)?(?:{_DEMO_PERIOD}的?)?(?:各(?:个)?地区|按地区)的?{MEASURE}"
    r"(?:\((?:" + REFUND + r")\))?(?:是多少|多少|呢)?"
)
_REFUND_TOTAL = re.compile(rf"(?:请|帮我)?(?:统计|计算)?(?:{PERIOD}的?)?(?:订单的?)?退款总(?:额|金额)(?:是多少|多少|呢)?")
_AVERAGE = re.compile(rf"(?:请|帮我)?(?:统计|计算)?(?:{PERIOD}的?)?订单的?平均(?:金额|订单金额)(?:是多少|多少|呢)?")
_SCOPE_PREFIX = r"只看|仅看|只要|限定(?:为|在)?|改成|改为|换成|再看|那(?:么)?"
_EXCLUDE_REGION = re.compile(r"(?:去掉|排除|不看)(华南|华东|华北)(?:地区|区)?(?:呢|吧)?")
_UNPARSED_FILTER = re.compile(r"^(?:只看|仅看|只要|限定|去掉|排除|不看|改成|改为|换成)")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip("。?!！")


def period(text: str, *, reference: date = AS_OF) -> str | None:
    if text in {"上月", "上个月"}:
        return "last_month"
    if text in {"本月", "这个月"}:
        return "this_month"
    if re.fullmatch(_ISO_MONTH, text):
        return text
    if not re.fullmatch(_CN_MONTH, text):
        return None
    year, sep, month = text.removesuffix("份").removesuffix("月").partition("年")
    if not sep:
        year, month = str(reference.year), year
    number = int(month) if month.isdecimal() else _MONTH_NUMBERS[month]
    return f"{year}-{number:02d}"


def calendar_month(value: str, *, reference: date = AS_OF) -> str:
    if value == "last_month":
        year = reference.year - (reference.month == 1)
        month = reference.month - 1 or 12
        return f"{year}-{month:02d}"
    return reference.strftime("%Y-%m") if value == "this_month" else value


def resolved_demo_query_label(
    context: QueryContext, intent: str = "revenue", *, excluded_regions: tuple[Region, ...] = (),
) -> str | None:
    """Describe the executed scope of a known demo query."""
    if intent in {"revenue", "regional_revenue"}:
        if context.metric is None or context.period is None:
            return None
        metric = "净营收" if context.metric == "net" else "毛营收"
        title = f"各地区{metric}" if intent == "regional_revenue" else metric
    elif not (context.period or context.region or excluded_regions):
        return None
    else:
        title = {
            "order_count": "订单笔数",
            "monthly_order_count": "各月订单笔数",
            "regional_order_count": "各地区订单笔数",
            "monthly_regional_order_count": "各月各地区订单笔数",
            "refund_total": "退款总额",
            "average_amount": "平均订单金额",
        }.get(intent)
        if title is None:
            return None
    period_label = calendar_month(context.period) if context.period else None
    scope = " ".join(part for part in (period_label, context.region, title) if part)
    if excluded_regions:
        scope += f"（排除{'、'.join(excluded_regions)}）"
    return scope


def parse_answer(text: str, *, reference: date = AS_OF) -> dict | None:
    tokens = re.split(r"[,、;]", compact(text))
    updates = {}
    for token in tokens:
        token = re.sub(rf"^(?:{_SCOPE_PREFIX}|就按|按|是)", "", token)
        token = re.sub(r"(?:就好|就行|吧|呢)$", "", token)
        if token in METRIC:
            name, value = "metric", METRIC[token]
        elif value := period(token, reference=reference):
            name = "period"
        elif region := re.fullmatch(r"(华南|华东|华北)(?:地区|区)?", token):
            name, value = "region", region[1]
        elif token in {"全部地区", "所有地区", "不限地区", "全国"}:
            name, value = "region", None
        else:
            return None
        if name in updates:
            return None
        updates[name] = value
    return updates


def parse_refinement(text: str, *, reference: date = AS_OF) -> dict | None:
    """Recognize a scope-only follow-up, never a new metric or task."""
    if region := _EXCLUDE_REGION.fullmatch(compact(text)):
        return {"excluded_region": region[1]}
    slots = parse_answer(text, reference=reference)
    return slots if slots and "metric" not in slots else None


def ambiguous_refinement(text: str) -> bool:
    normalized = compact(text)
    return bool(_UNPARSED_FILTER.match(normalized) and not standalone_request(text)
                and not re.search(rf"{MEASURE}|订单(?:总数|数量|数|笔数)|退款总额|平均金额", normalized))


def stated_slots(question: str, *, reference: date = AS_OF) -> dict:
    text = compact(question)
    periods = {_month for token in re.findall(PERIOD, text)
               if (_month := period(token, reference=reference)) is not None}
    metrics = {METRIC[token] for token in _METRIC_IN_QUESTION.findall(text)}
    regions = set(REGION.findall(text))
    slots = {}
    # Several values typically indicate a comparison or an explicit range.
    for name, values in (("period", periods), ("metric", metrics), ("region", regions)):
        if len(values) == 1:
            slots[name] = next(iter(values))
    return slots


def clarification_slot(prompt: str) -> Slot:
    text = compact(prompt).lower()
    if re.search(r"净营收|毛营收|扣(?:除)?退款|gross|netrevenue|退款口径|毛净", text):
        return "metric"
    if re.search(r"哪(?:一?个|一)?月|几月份|几月|月份|哪一年|哪年|年份|日期|时间|起止|month|date|period|year", text):
        return "period"
    if re.search(r"地区|区域|省份|哪个市|region|province", text):
        return "region"
    return "unknown"


def standalone_request(text: str) -> bool:
    return bool(_NEW_REQUEST.search(text.strip()) or (len(compact(text)) >= 8 and text.strip().endswith(("?", "？"))))


def demo_aggregate_intent(question: str) -> str | None:
    text = compact(question)
    if _COUNT.fullmatch(text) or _MONTH_ORDERS.fullmatch(text):
        return "order_count"
    grouped = _GROUPED_COUNT.fullmatch(text)
    if grouped and grouped["monthly"]:
        return "monthly_regional_order_count" if grouped["regional"] else "monthly_order_count"
    if grouped and grouped["regional"]:
        return "regional_order_count"
    if _GROUPED_REVENUE.fullmatch(text):
        return "regional_revenue"
    if _REFUND_TOTAL.fullmatch(text):
        return "refund_total"
    if _AVERAGE.fullmatch(text):
        return "average_amount"
    return None
