"""Persistent clarification and explicit write approval for the Step 6 demo."""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import TypedDict

from app.commerce_demo import AS_OF, ApprovalID, Metric, NoteChange, QueryContext, apply_note, query_revenue
from app.config import Settings
from app.product_assistant import is_meta_question

_WRITE = re.compile(r"(?:请\s*)?(?:把|将)?\s*订单\s*([0-9]{1,9})\s*(?:的\s*)?备注\s*(?:改为|更新为|设为)\s*(.+)")
_WRITE_HINT = re.compile(r"^(?:请\s*)?(?:删除|修改|更新|新增|插入|写入|把|将|update\b|insert\b|delete\b)", re.I)
_EXPLICIT_MUTATION = re.compile(r"^(?:请\s*)?(?:删除|修改|更新|新增|插入|写入|update\b|insert\b|delete\b)", re.I)
_REGION = re.compile(r"华南|华东|华北")
_MONTH_NUMBERS = {name: index for index, name in enumerate(
    ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1,
)}
_MONTH = r"(?:20\d{2}年)?(?:1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月"
_PERIOD = rf"上月|上个月|本月|这个月|{_MONTH}"
_SCOPE = rf"(?:{_PERIOD}|(?:华南|华东|华北)(?:地区|区)?)"
_MEASURE = r"(?:净|毛)?(?:营收|销售额|营业额)"
_FOLLOWUP = re.compile(rf"(?:那|那么)?{_SCOPE}(?:呢)?")
_QUERY = re.compile(
    rf"(?:那么|那|请问|请查|查询)?(?:{_SCOPE}的?){{0,2}}(?P<label>{_MEASURE})"
    r"(?:是多少|多少|呢)?(?:[，,]?(?:按)?(?P<refund>不扣除退款|不扣退款|扣除退款|扣退款))?"
)
_GROSS = ("不扣除退款", "不扣退款", "毛营收", "毛销售额", "毛营业额")
_NET = ("扣除退款", "扣退款", "净营收", "净销售额", "净营业额")
_CLARIFICATION: dict[str, Metric] = {**dict.fromkeys(_GROSS, "gross"), **dict.fromkeys(_NET, "net")}


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    approval_id: ApprovalID
    approve: bool


class ConversationState(TypedDict, total=False):
    question: str
    last_query: dict
    pending_query: dict | None
    pending_original: str | None
    pending_write: dict | None
    last_data_query: dict | None
    pending_data_query: dict | None
    route: Literal["clarify", "query", "assist", "interpret", "approve_write", "apply_write", "stop"]
    reply: dict


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。？！?!")


def _period(text: str) -> str | None:
    if text in {"上月", "上个月"}:
        return "last_month"
    if text in {"本月", "这个月"}:
        return "this_month"
    if not re.fullmatch(_MONTH, text):
        return None
    year, sep, month = text.removesuffix("月").partition("年")
    if not sep:
        year, month = str(AS_OF.year), year
    number = int(month) if month.isdecimal() else _MONTH_NUMBERS[month]
    return f"{year}-{number:02d}"


def _query_context(text: str, last: dict | None) -> QueryContext | None:
    text = _compact(text)
    query = _QUERY.fullmatch(text)
    followup = last is not None and (_FOLLOWUP.fullmatch(text) is not None or (
        query is not None and text.startswith(("那", "那么"))
    ))
    if not (query or followup):
        return None
    periods = re.findall(_PERIOD, text)
    regions = _REGION.findall(text)
    if len(periods) > 1 or len(regions) > 1:
        return None
    previous = QueryContext.model_validate(last) if followup else QueryContext()
    label = _CLARIFICATION.get(query["label"]) if query else None
    refund = _CLARIFICATION.get(query["refund"]) if query else None
    conflict = label is not None and refund is not None and label != refund
    return QueryContext(
        period=_period(periods[0]) if periods else previous.period,
        metric=None if conflict else (label or refund or previous.metric),
        region=regions[0] if regions else previous.region,
    )


def _clarify_reply(context: QueryContext, original: str) -> dict:
    if context.period is None:
        question, options = "你要查哪个月？可以回答「十月」或「2026 年 10 月」。", ["上月", "本月"]
    else:
        question, options = "营收要扣除退款吗？", ["扣除退款", "不扣除退款"]
    return {"kind": "clarification", "question": question, "options": options,
            "context": context.model_dump(), "original_question": original}


def _apply_answer(context: QueryContext, answer: str) -> QueryContext | None:
    tokens = re.split(r"[，,、]", _compact(answer))
    updates = {}
    for token in tokens:
        if metric := _CLARIFICATION.get(token):
            updates["metric"] = metric
        elif period := _period(token):
            updates["period"] = period
        else:
            return None
    if len(tokens) != len(updates):
        return None
    missing = {key: value for key, value in updates.items() if getattr(context, key) is None}
    return context.model_copy(update=missing) if missing else None


def build_conversation(
    database: Path,
    settings: Settings | None,
    checkpointer,
    *,
    assistant: Callable[[Path, str, dict | None, dict | None], dict] | None = None,
    data_source: str | None = None,
):
    use_demo = data_source is None
    def interpret(state: ConversationState) -> ConversationState:
        text = state["question"].strip()
        pending = state.get("pending_data_query")
        if assistant is not None and pending and pending["source"] == data_source:
            return {"route": "assist"}
        if use_demo and (saved := state.get("pending_query")):
            context = QueryContext.model_validate(saved)
            if resolved := _apply_answer(context, text):
                return {"route": "query" if resolved.period and resolved.metric else "clarify",
                        "pending_query": resolved.model_dump()}
            if assistant is not None and is_meta_question(text):
                return {"route": "assist"}
        if write := _WRITE.fullmatch(text):
            if not use_demo:
                return {"route": "stop", "reply": {"kind": "rejected", "message": "当前数据库只读。可切换到演示订单库体验备注审批。"}}
            try:
                change = NoteChange(approval_id=uuid4().hex, order_id=int(write[1]), note=write[2])
            except ValidationError:
                return {"route": "stop", "reply": {"kind": "rejected", "message": "订单号或备注格式无效"}}
            return {"route": "approve_write", "pending_write": change.model_dump()}
        write_hint = _EXPLICIT_MUTATION if assistant is not None else _WRITE_HINT
        if write_hint.search(text):
            message = "演示写操作仅支持修改订单备注" if use_demo else "当前数据库只读。"
            return {"route": "stop", "reply": {"kind": "rejected", "message": message}}
        context = _query_context(text, state.get("last_query")) if use_demo else None
        if context is None:
            if assistant is not None:
                return {"route": "assist"}
            return {"route": "stop", "reply": {"kind": "rejected", "message": "请询问演示库的营收或继续已确认的营收问题"}}
        return {"route": "query" if context.period and context.metric else "clarify",
                "pending_query": context.model_dump(), "pending_original": text, "pending_data_query": None}

    def clarify(state: ConversationState) -> ConversationState:
        context = QueryContext.model_validate(state["pending_query"])
        original = state.get("pending_original") or state["question"]
        answer = interrupt(_clarify_reply(context, original))
        if not use_demo:
            return {"route": "interpret", "question": answer, "pending_query": None, "pending_original": None}
        if resolved := _apply_answer(context, answer):
            return {"route": "query" if resolved.period and resolved.metric else "clarify",
                    "pending_query": resolved.model_dump()}
        if assistant is not None and is_meta_question(answer):
            return {"route": "assist", "question": answer, "pending_original": original}
        if _QUERY.fullmatch(_compact(answer)) or _WRITE.fullmatch(answer):
            return {"route": "interpret", "question": answer, "pending_query": None, "pending_original": None}
        if assistant is not None and re.search(r"多少|统计|比较|列出|计算|查询", answer):
            return {"route": "interpret", "question": answer, "pending_query": None, "pending_original": None}
        return {"route": "clarify"}

    def query(state: ConversationState) -> ConversationState:
        context = QueryContext.model_validate(state["pending_query"])
        reply = query_revenue(database, context, None if assistant is not None else settings)
        update: ConversationState = {"pending_query": None, "pending_original": None, "reply": reply}
        if reply["kind"] == "answer":
            update["last_query"] = context.model_dump()
            update["last_data_query"] = {
                "source": None, "question": state.get("pending_original") or state["question"], "sql": reply["sql"],
            }
        return update

    def assist(state: ConversationState) -> ConversationState:
        assert assistant is not None
        text = state["question"]
        previous = state.get("last_data_query")
        pending = state.get("pending_data_query")
        previous = previous if previous and previous["source"] == data_source else None
        pending = pending if pending and pending["source"] == data_source else None
        reply = assistant(database, text, previous, pending)
        answers = [*pending.get("answers", []), text][-5:] if pending else []
        original = pending["question"] if pending else text
        turn = {"source": data_source, "question": original}
        update: ConversationState = {"reply": reply}
        if reply["kind"] == "query":
            update["last_data_query"] = {**turn, "sql": reply["sql"]}
            if answers:
                update["last_data_query"]["answers"] = answers
            update["pending_data_query"] = None
            if use_demo:
                update["last_query"] = None
        elif reply["kind"] == "clarification":
            update["pending_data_query"] = {
                **turn, "answers": answers, "prompt": reply["question"],
            }
        elif reply["kind"] == "rejected" and not pending:
            update["pending_data_query"] = None
        return update

    def approve_write(state: ConversationState) -> ConversationState:
        change = NoteChange.model_validate(state["pending_write"])
        answer = interrupt({
            "kind": "approval", "approval_id": change.approval_id, "order_id": change.order_id,
            "note": change.note, "question": "是否批准更新这条演示订单的备注？",
        })
        decision = ApprovalDecision.model_validate(answer)
        if decision.approval_id != change.approval_id:
            raise ValueError("审批编号与待审批操作不一致")
        if decision.approve:
            return {"route": "apply_write"}
        return {"route": "stop", "pending_write": None, "reply": {
            "kind": "write_result", "status": "denied", "approval_id": change.approval_id,
            "order_id": change.order_id,
        }}

    def apply_write(state: ConversationState) -> ConversationState:
        change = NoteChange.model_validate(state["pending_write"])
        return {"pending_write": None, "reply": apply_note(database, change)}

    def route(state: ConversationState):
        return state["route"]

    graph = StateGraph(ConversationState)
    graph.add_node("interpret", interpret)
    graph.add_node("clarify", clarify)
    graph.add_node("query", query)
    graph.add_node("assist", assist)
    graph.add_node("approve_write", approve_write)
    graph.add_node("apply_write", apply_write)
    graph.add_edge(START, "interpret")
    graph.add_conditional_edges(
        "interpret", route, {"clarify": "clarify", "query": "query", "assist": "assist", "approve_write": "approve_write", "stop": END},
    )
    graph.add_conditional_edges("clarify", route, {"clarify": "clarify", "query": "query", "interpret": "interpret", "assist": "assist"})
    graph.add_conditional_edges("approve_write", route, {"apply_write": "apply_write", "stop": END})
    graph.add_edge("query", END)
    graph.add_edge("assist", END)
    graph.add_edge("apply_write", END)
    return graph.compile(checkpointer=checkpointer)
