"""Product dialogue routes that reuse the read-only database workflow."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from app.product_sql import query_database

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


def is_meta_question(question: str) -> bool:
    text = re.sub(r"\s+", "", question).strip("。！？?!")
    return bool(_SOURCE.search(text) or _GREETING.fullmatch(text) or _HELP.fullmatch(text))


def answer_data_turn(
    question: str,
    previous: dict | None,
    pending: dict | None,
    *,
    database: Path,
    demo: bool,
    settings: Settings | None,
    evidence: str,
) -> dict:
    text = re.sub(r"\s+", "", question).strip("。！？?!")
    if _SOURCE.search(text):
        return _source_reply(database, demo, previous)
    if _GREETING.fullmatch(text):
        return {"kind": "help", "message": "你好，我可以帮你查数据库里的数据，也可以分析你上传的表格。"}
    if _HELP.fullmatch(text):
        return {"kind": "help", "message": _capabilities(detailed=bool(_DETAIL.search(text)))}
    if settings is None:
        return {"kind": "rejected", "message": (
            "这类问题需要模型生成查询。请在侧栏启用已配置的模型，或试试「上月净营收是多少？」。"
            if demo else "查询这个只读数据库需要配置模型。"
        )}
    context = {
        "previous_query": previous if not pending and _FOLLOWUP.search(question) else None,
        "pending_clarification": pending,
    }
    return query_database(database, question, evidence, settings, context=context, demo=demo)


def _source_reply(database: Path, demo: bool, previous: dict | None) -> dict:
    if demo:
        message = (
            "当前数据源是「演示订单库」。项目初始化时预置了合成演示订单，"
            "保存在本机 SQLite 的 orders 表中，用来演示订单数量、营收和退款查询。"
        )
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
            "配置只读 SQLite 后，也可以切换到对应数据库，用自然语言查询。\n\n"
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
