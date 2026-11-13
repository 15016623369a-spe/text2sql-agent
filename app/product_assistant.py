"""Product dialogue routes that reuse the read-only database workflow."""
from __future__ import annotations

import re
from pathlib import Path

from app.commerce_demo import QueryContext, query_demo_aggregate, query_revenue
from app.config import Settings
from app.conversation_slots import (
    ambiguous_refinement, calendar_month, clarification_slot, demo_aggregate_intent, parse_refinement,
    resolved_demo_query_label, stated_slots,
)
from app.product_sql import query_database, refine_demo_sql
from app.tools.postgres import PostgresSource

_GREETING = re.compile(r"你好|您好|你是谁|hi|hello", re.I)
_HELP = re.compile(
    r"(?:请问|请)?(?:"
    r"帮助|help|"
    r"你(?:都|还)?(?:能|能够|可以|会)(?:帮我)?做(?:些)?什么|"
    r"你(?:都|还)?有(?:哪些|什么)功能|"
    r"(?:你(?:能不能|能否|能够|能|可以))?(?:(?:简单|单纯|完整|全面|详细)(?:地|的)?)?"
    r"(?:介绍(?:一?下)?|介一下?)(?:你(?:自己|的)?)?(?:的)?功能|"
    r"(?:简单)?介绍(?:一?下)?你自己|"
    r"(?:你|这个工具|这个助手)?怎么用)(?:吗|么|呢)?", re.I,
)
_DETAIL = re.compile(r"完整|全面|详细")
_SOURCE = re.compile(
    r"(?:数据|结果|数字|订单(?:数量|笔数)?).{0,12}?(?:"
    r"(?:从|是)(?:哪里|哪儿|哪)来|(?:来自|出自)(?:哪里|哪儿|哪(?:个|张)?(?:库|表|平台|系统|数据源))|"
    r"来源(?:于)?(?:是)?(?:什么|哪里|哪儿|哪个库|哪张表|哪|呢|$)|"
    r"(?:是|是否)(?:真实|真的|合成|模拟|实时))|"
    r"^(?:请问)?(?:数据来源|数据源|数据出处)(?:是什么|是哪个|是哪里|呢)?$|"
    r"^(?:请问)?(?:当前|现在)(?:用|使用|查询|选中)的?(?:是)?(?:哪个|什么)(?:数据库|数据源|库)(?:呢|吗)?$"
)


_FOLLOWUP = re.compile(
    r"^(?:那|那么|再看|改成|换成)|刚才|上一(?:轮|次)|同样|^(?:华南|华东|华北)(?:地区|区)?(?:呢)?[？?]?$"
)


_UNRESOLVED_MONTH = re.compile(r"那个月|某个月")
_TABLE_INVENTORY = re.compile(
    r"(?:请|帮我)?(?:列出|查看|显示|查询)(?:(?:当前|演示)?数据库(?:中|里)?的?)?"
    r"(?:所有|全部)(?:的)?(?:数据)?表(?:名|名称)?"
    r"|(?:当前|演示)?数据库(?:中|里|内)?(?:有哪(?:些|几张)|有什么)(?:数据)?表(?:呢)?"
)


def is_meta_question(question: str) -> bool:
    text = re.sub(r"\s+", "", question).strip("。！？?!")
    return bool(_SOURCE.search(text) or _GREETING.fullmatch(text) or _HELP.fullmatch(text)
                or _TABLE_INVENTORY.fullmatch(text))


def answer_data_turn(
    question: str,
    previous: dict | None,
    pending: dict | None,
    *,
    database: Path | PostgresSource,
    demo: bool,
    settings: Settings | None,
    evidence: str,
    tables: tuple[str, ...] | None = None,
) -> dict:
    text = re.sub(r"\s+", "", question).strip("。！？?!")
    if demo and _TABLE_INVENTORY.fullmatch(text):
        return {"kind": "error", "message": "演示库只暴露 orders 表。", "stop_reason": "demo_scope"}
    if _SOURCE.search(text):
        return _source_reply(database, demo, previous, tables)
    if _GREETING.fullmatch(text):
        return {"kind": "help", "message": "你好，我可以帮你查数据库里的数据，也可以分析你上传的表格。"}
    if _HELP.fullmatch(text):
        return {"kind": "help", "message": _capabilities(detailed=bool(_DETAIL.search(text)))}
    if not pending:
        filters = parse_refinement(question)
        legacy_followup = not demo and _FOLLOWUP.search(question)
        if filters and not legacy_followup:
            if not previous:
                return {"kind": "help", "message": "当前数据源还没有成功的查询。请说明你想按这个范围统计什么。"}
            if demo and (refined := _refine_demo_query(database, previous, filters)):
                return refined
            return {"kind": "help", "message": "请带上上一轮的指标和新的筛选范围重新提问，才能确认这次的查询条件。"}
        if not legacy_followup and ambiguous_refinement(question):
            return {"kind": "help", "message": "你是想筛选上一轮结果，还是开始新的查询？请说明指标和筛选范围。"}
    original = pending["source_question"] if pending else question
    intent = demo_aggregate_intent(original) if demo else None
    slots = pending["resolved_slots"] if pending else stated_slots(original) if demo else {}
    followup = previous if not pending and _FOLLOWUP.search(question) else None
    if demo and not followup and "period" not in slots and _UNRESOLVED_MONTH.search(original):
        return {"kind": "clarification", "slot": "period", "question": "你想查哪个月？"}
    if intent:
        slot = _missing_demo_slot(original, intent, slots, pending)
        if slot:
            prompt = "你想查哪个月？" if slot == "period" else "各地区营收要扣除退款吗？"
            return {"kind": "clarification", "slot": slot, "question": prompt}
        if pending or settings is None or intent != "order_count":
            return query_demo_aggregate(database, intent, QueryContext(**slots))
    if settings is None:
        return {"kind": "rejected", "message": (
            "这类问题需要模型生成查询。请在侧栏启用已配置的模型，或试试「上月净营收是多少？」。"
            if demo else "查询这个只读数据库需要配置模型。"
        )}
    context = {
        "previous_query": followup,
        "resolved_slots": slots,
    }
    model_question = original
    if pending:
        context["clarification_answers"] = pending["answers"]
        if demo and (month := slots.get("period")) and _UNRESOLVED_MONTH.search(original):
            year, month_number = month.split("-")
            model_question = _UNRESOLVED_MONTH.sub(f"{year}年{int(month_number)}月", original)
            context["original_question"] = original
    reply = query_database(database, model_question, evidence, settings, context=context, demo=demo, tables=tables)
    if intent and reply["kind"] == "clarification":
        slot = reply.get("slot") or clarification_slot(reply["question"])
        if slot in slots or (slot in {"period", "metric"} and intent == "order_count"):
            return query_demo_aggregate(database, intent, QueryContext(**slots))
    if demo and reply["kind"] == "query" and slots:
        reply["context"] = QueryContext(**slots).model_dump()
    return reply


def _refine_demo_query(database: Path, previous: dict, changes: dict) -> dict | None:
    original = previous["question"]
    previous_slots = previous.get("resolved_slots")
    if previous_slots is None:
        previous_slots = stated_slots(original)
    changes = changes.copy()
    excluded = changes.pop("excluded_region", None)
    context = QueryContext(**(previous_slots | changes))

    plan = previous.get("demo_plan")
    old_refinement = previous.get("sql_refinement") or {}
    previous_exclusions = tuple((plan or old_refinement).get("excluded_regions", ()))
    excluded_regions = previous_exclusions
    if "region" in changes:
        excluded_regions = ()
    if excluded is not None and excluded not in excluded_regions:
        excluded_regions = (*excluded_regions, excluded)
    if plan:
        intent = plan["intent"]
        if intent == "revenue":
            reply = query_revenue(database, context, None, excluded_regions=excluded_regions)
        else:
            reply = query_demo_aggregate(database, intent, context, excluded_regions=excluded_regions)
    else:
        baseline = old_refinement.get("base_slots", previous_slots)
        for key, value in changes.items():
            if baseline.get(key) is None:
                continue
            old = baseline[key]
            if key == "period" and value is not None:
                old, value = calendar_month(old), calendar_month(value)
            if old != value:
                return None
        base_sql = old_refinement.get("base_sql", previous["sql"])
        scope = old_refinement.get("scope", {}) | changes
        reply = refine_demo_sql(database, base_sql, QueryContext(**scope), excluded_regions)
        if reply is None:
            return None
        reply["sql_refinement"] = {"base_sql": base_sql, "base_slots": baseline, "scope": scope,
                                   "excluded_regions": list(excluded_regions)}
        reply["context"] = context.model_dump()
    if reply["kind"] in {"query", "answer"}:
        label = resolved_demo_query_label(
            QueryContext(**previous_slots), plan["intent"], excluded_regions=previous_exclusions,
        ) if plan else None
        reply["refined_from"] = label or original
        if excluded_regions:
            reply["excluded_regions"] = list(excluded_regions)
    return reply


def _missing_demo_slot(original: str, intent: str, slots: dict, pending: dict | None) -> str | None:
    if "period" not in slots and (intent == "regional_revenue" or (pending and pending["slot"] == "period")
                                  or _UNRESOLVED_MONTH.search(original)):
        return "period"
    if intent == "regional_revenue" and "metric" not in slots:
        return "metric"
    return None


def _source_reply(
    database: Path | PostgresSource, demo: bool, previous: dict | None, tables: tuple[str, ...] | None = None,
) -> dict:
    if demo:
        message = (
            "当前数据源是「演示订单库」。项目初始化时预置了合成演示订单，"
            "保存在本机 SQLite 的 orders 表中，用来演示订单数量、营收和退款查询。"
        )
    elif isinstance(database, PostgresSource):
        message = "当前数据源是部署方配置的只读 PostgreSQL。"
        if tables:
            message += "本次范围为 " + "、".join(tables) + "。"
    else:
        message = (
            f"当前数据源是你选中的「{database.stem}」只读 SQLite 数据库。"
            "库内记录的采集渠道和更新时间取决于这个数据库的提供方。"
        )
    if previous:
        return {"kind": "source", "message": message + "\n展开「查看 SQL」可核对上一次查询的表和筛选条件。",
                "sql": previous["sql"]}
    return {"kind": "source", "message": message + "\n当前数据源在本会话还没有可对应的成功查询。"}


def _capabilities(*, detailed: bool) -> str:
    if detailed:
        return (
            "数据库查询\n"
            "在演示订单库里查订单笔数、营收、退款、地区对比和明细。"
            "部署方配置只读 SQLite 或 PostgreSQL 后，可以切换数据源，用自然语言查询。\n\n"
            "追问和核对\n"
            "可以接着问「那华南呢？」。查看 SQL 可核对表和筛选条件。问数据从哪里来，可以看到当前数据源。"
            "营收口径不明确时，我会请你确认是否扣退款。\n\n"
            "文件分析\n"
            "上传 CSV 或 Excel 后，可以做筛选、分组汇总、TopN、异常值检查、公式核对和绘图。\n\n"
            "演示审批\n"
            "更新演示订单备注前，需要你在页面批准。"
        )
    return (
        "我可以查订单数量、营收、退款和明细，也可以在同一数据源里继续追问。"
        "切到「文件分析」可上传 CSV 或 Excel，做统计和绘图。"
    )
