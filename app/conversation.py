"""Persistent clarification and explicit write approval for the Step 6 demo."""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import TypedDict

from app.commerce_demo import AS_OF, ApprovalID, NoteChange, QueryContext, apply_note, query_demo_aggregate, query_revenue
from app.config import Settings
from app.conversation_slots import (
    CANCEL_CLARIFICATION, MEASURE, METRIC, PERIOD, REFUND, REGION, SCOPE,
    calendar_month, clarification_slot, compact, demo_aggregate_intent, parse_answer, parse_refinement, period,
    standalone_request, stated_slots,
)
from app.product_assistant import is_meta_question

_WRITE = re.compile(r"(?:请\s*)?(?:把|将)?\s*订单\s*([0-9]{1,9})\s*(?:的\s*)?备注\s*(?:改为|更新为|设为)\s*(.+)")
_WRITE_HINT = re.compile(r"^(?:请\s*)?(?:删除|修改|更新|新增|插入|写入|把|将|update\b|insert\b|delete\b)", re.I)
_EXPLICIT_MUTATION = re.compile(r"^(?:请\s*)?(?:删除|修改|更新|新增|插入|写入|update\b|insert\b|delete\b)", re.I)
_FOLLOWUP = re.compile(rf"(?:那|那么)?{SCOPE}(?:呢)?")
_QUERY = re.compile(
    rf"(?:那么|那|请问|请查|查询)?(?:{SCOPE}的?){{0,2}}(?P<label>{MEASURE})"
    rf"(?:\((?P<parenthesized>{REFUND})\))?(?:是多少|多少|呢)?(?:[,]?(?:按)?(?P<refund>{REFUND}))?"
)
_MAX_CLARIFICATION_MISSES = 2
_MAX_MODEL_RESUMES = 3
_PENDING_OPTIONS = {
    "period": ["上月", "本月"],
    "metric": ["扣除退款", "不扣除退款"],
    "region": ["华南", "华东", "华北", "全部地区"],
}
_CLEAR_PENDING = {
    "pending_data_query": None, "pending_query": None, "pending_original": None, "pending_misses": 0,
}


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    approval_id: ApprovalID
    approve: bool


class ConversationState(TypedDict, total=False):
    question: str
    last_query: dict | None
    pending_query: dict | None
    pending_original: str | None
    pending_misses: int
    pending_write: dict | None
    last_data_query: dict | None
    pending_data_query: dict | None
    route: Literal["clarify", "query", "assist", "interpret", "approve_write", "apply_write", "stop"]
    reply: dict


def _query_context(text: str, last: dict | None) -> QueryContext | None:
    text = compact(text)
    query = _QUERY.fullmatch(text)
    followup = last is not None and (_FOLLOWUP.fullmatch(text) is not None or (
        query is not None and text.startswith(("那", "那么"))
    ))
    if not (query or followup):
        return None
    periods = re.findall(PERIOD, text)
    regions = REGION.findall(text)
    if len(periods) > 1 or len(regions) > 1:
        return None
    previous = QueryContext.model_validate(last) if followup else QueryContext()
    metrics = {METRIC[value] for value in query.groupdict().values() if value in METRIC} if query else set()
    metric = next(iter(metrics)) if len(metrics) == 1 else previous.metric if not metrics else None
    return QueryContext(
        period=period(periods[0]) if periods else previous.period,
        metric=metric,
        region=regions[0] if regions else previous.region,
    )


def _clarify_reply(context: QueryContext, original: str) -> dict:
    slot = "period" if context.period is None else "metric"
    question = ("你要查哪个月？可以回答「十月」或「2026 年 10 月」。"
                if slot == "period" else "营收要扣除退款吗？")
    return {"kind": "clarification", "question": question, "options": _PENDING_OPTIONS[slot],
            "context": context.model_dump(), "original_question": original, "slot": slot, "source": None}


def _apply_answer(context: QueryContext, answer: str) -> QueryContext | None:
    updates = parse_answer(answer)
    if not updates or all(getattr(context, key) == value for key, value in updates.items()):
        return None
    return context.model_copy(update=updates)


def _pending_details(raw: dict, reference: date) -> dict:
    """Read both current and pre-slot checkpoints."""
    question = raw.get("source_question") or raw["question"]
    slots = raw.get("resolved_slots")
    if slots is None:
        slots = stated_slots(question, reference=reference)
        for answer in raw.get("answers", []):
            slots.update(parse_answer(answer, reference=reference) or {})
    if "period" in slots:
        slots = {**slots, "period": calendar_month(slots["period"], reference=reference)}
    return {
        "source": raw["source"], "source_question": question, "prompt": raw["prompt"],
        **({"source_tables": raw["source_tables"]} if "source_tables" in raw else {}),
        "slot": raw.get("slot") or clarification_slot(raw["prompt"]), "resolved_slots": slots,
        "answers": raw.get("answers", []), "misses": raw.get("misses", 0), "resumes": raw.get("resumes", 0),
    }


def _data_clarification(pending: dict) -> dict:
    return {
        "kind": "clarification", "question": pending["prompt"], "original_question": pending["source_question"],
        "slot": pending["slot"], "source": pending["source"], "options": _PENDING_OPTIONS.get(pending["slot"], [])
        if pending["source"] is None else [], "context": QueryContext(**pending["resolved_slots"]).model_dump(),
        **({"source_tables": pending["source_tables"]} if "source_tables" in pending else {}),
    }


def _stop_clarification(message: str) -> ConversationState:
    return {**_CLEAR_PENDING, "route": "stop", "reply": {"kind": "help", "message": message}}


def _remember_query(reply: dict, source: str | None, question: str) -> dict:
    snapshot = {
        "source": source, "question": question, "sql": reply["sql"],
        "columns": reply.get("columns", ["revenue_cents"]),
    }
    if context := reply.get("context"):
        snapshot["resolved_slots"] = context
    for key in ("demo_plan", "sql_refinement"):
        if value := reply.pop(key, None):
            snapshot[key] = value
    return snapshot


def build_conversation(
    database: Path,
    settings: Settings | None,
    checkpointer,
    *,
    assistant: Callable[[Path, str, dict | None, dict | None], dict] | None = None,
    data_source: str | None = None,
    data_tables: tuple[str, ...] | None = None,
):
    use_demo = data_source is None
    reference = AS_OF if use_demo else date.today()
    table_scope = {"source_tables": list(data_tables)} if data_tables is not None else {}

    def in_scope(record: dict | None) -> bool:
        return (record is not None and record["source"] == data_source
                and tuple(record.get("source_tables", ())) == (data_tables or ()))

    def interpret(state: ConversationState) -> ConversationState:
        text = state["question"].strip()
        if compact(text) == CANCEL_CLARIFICATION:
            return _stop_clarification("已取消这次澄清。你可以直接提出新问题。")
        write = _WRITE.fullmatch(text)
        write_hint = _EXPLICIT_MUTATION if assistant is not None else _WRITE_HINT
        is_write = bool(write or write_hint.search(text))
        is_meta = assistant is not None and is_meta_question(text)
        pending = state.get("pending_data_query")
        has_active_pending = in_scope(pending)
        if assistant is not None and has_active_pending and not (is_write or is_meta):
            pending = _pending_details(pending, reference)
            if updates := parse_answer(text, reference=reference):
                if "period" in updates:
                    updates["period"] = calendar_month(updates["period"], reference=reference)
                slots = pending["resolved_slots"] | updates
                needs_slot = pending["slot"] != "unknown" and pending["slot"] not in slots
                if needs_slot and slots == pending["resolved_slots"]:
                    pending["misses"] += 1
                    if pending["misses"] >= _MAX_CLARIFICATION_MISSES:
                        return _stop_clarification("没有确认到所需信息，已结束这次澄清。你可以直接提出新问题。")
                else:
                    pending["misses"] = 0
                pending["resolved_slots"] = slots
                pending["answers"] = [*pending["answers"], text][-5:]
                if needs_slot:
                    return {"route": "stop", "pending_data_query": pending, "reply": _data_clarification(pending)}
                return {"route": "assist", "pending_data_query": pending}
            demo_request = use_demo and (_QUERY.fullmatch(compact(text)) or demo_aggregate_intent(text))
            if not (standalone_request(text) or demo_request):
                if pending["slot"] == "unknown" or not use_demo:
                    return {"route": "assist", "pending_data_query": {
                        **pending, "answers": [*pending["answers"], text][-5:],
                    }}
                pending["misses"] += 1
                if pending["misses"] >= _MAX_CLARIFICATION_MISSES:
                    return _stop_clarification("没有确认到所需信息，已结束这次澄清。你可以直接提出新问题。")
                return {"route": "stop", "pending_data_query": pending, "reply": _data_clarification(pending)}

        if write:
            if not use_demo:
                return {**_CLEAR_PENDING, "route": "stop", "reply": {
                    "kind": "rejected", "message": "当前数据库只读。可切换到演示订单库体验备注审批。",
                }}
            try:
                change = NoteChange(approval_id=uuid4().hex, order_id=int(write[1]), note=write[2])
            except ValidationError:
                return {**_CLEAR_PENDING, "route": "stop", "reply": {"kind": "rejected", "message": "订单号或备注格式无效"}}
            return {**_CLEAR_PENDING, "route": "approve_write", "pending_write": change.model_dump()}
        if is_write:
            message = "演示写操作仅支持修改订单备注" if use_demo else "当前数据库只读。"
            return {**_CLEAR_PENDING, "route": "stop", "reply": {"kind": "rejected", "message": message}}
        if is_meta:
            return {**_CLEAR_PENDING, "route": "assist"}

        if use_demo and (saved := state.get("pending_query")):
            context = QueryContext.model_validate(saved)
            if resolved := _apply_answer(context, text):
                return {"route": "query" if resolved.period and resolved.metric else "clarify",
                        "pending_query": resolved.model_dump(), "pending_misses": 0}

        if assistant is not None and parse_refinement(text, reference=reference):
            return {**_CLEAR_PENDING, "route": "assist"}

        if use_demo and has_active_pending and demo_aggregate_intent(text) == "order_count":
            # An explicit new count starts fresh, even if the previous task needed a month.
            context = QueryContext(**stated_slots(text))
            if context.period or not re.search("那个月|某个月", compact(text)):
                reply = query_demo_aggregate(database, "order_count", context)
                return {
                    **_CLEAR_PENDING, "route": "stop", "reply": reply, "last_query": None,
                    "last_data_query": _remember_query(reply, None, text),
                }
        context = _query_context(text, state.get("last_query")) if use_demo else None
        if context is None:
            if assistant is not None:
                return {**_CLEAR_PENDING, "route": "assist"}
            return {**_CLEAR_PENDING, "route": "stop", "reply": {
                "kind": "rejected", "message": "请询问演示库的营收或继续已确认的营收问题",
            }}
        return {**_CLEAR_PENDING, "route": "query" if context.period and context.metric else "clarify",
                "pending_query": context.model_dump(), "pending_original": text}

    def clarify(state: ConversationState) -> ConversationState:
        context = QueryContext.model_validate(state["pending_query"])
        original = state.get("pending_original") or state["question"]
        answer = interrupt(_clarify_reply(context, original))
        if compact(answer) == CANCEL_CLARIFICATION:
            return _stop_clarification("已取消这次澄清。你可以直接提出新问题。")
        if not use_demo:
            return {**_CLEAR_PENDING, "route": "interpret", "question": answer}
        if resolved := _apply_answer(context, answer):
            return {"route": "query" if resolved.period and resolved.metric else "clarify",
                    "pending_query": resolved.model_dump(), "pending_misses": 0}
        if assistant is not None and is_meta_question(answer):
            return {**_CLEAR_PENDING, "route": "assist", "question": answer}
        if _QUERY.fullmatch(compact(answer)) or _WRITE.fullmatch(answer) or (
            assistant is not None and (_EXPLICIT_MUTATION.search(answer) or standalone_request(answer))
        ):
            return {**_CLEAR_PENDING, "route": "interpret", "question": answer}
        misses = state.get("pending_misses", 0) + 1
        if misses >= _MAX_CLARIFICATION_MISSES:
            return _stop_clarification("没有确认到所需信息，已结束这次澄清。你可以直接提出新问题。")
        return {"route": "clarify", "pending_misses": misses}

    def query(state: ConversationState) -> ConversationState:
        context = QueryContext.model_validate(state["pending_query"])
        reply = query_revenue(database, context, None if assistant is not None else settings)
        update: ConversationState = {**_CLEAR_PENDING, "reply": reply}
        if reply["kind"] == "answer":
            update["last_query"] = context.model_dump()
            original = state.get("pending_original") or state["question"]
            update["last_data_query"] = _remember_query(reply, None, original)
        return update

    def assist(state: ConversationState) -> ConversationState:
        assert assistant is not None
        text = state["question"]
        previous = state.get("last_data_query")
        pending = state.get("pending_data_query")
        previous = previous if in_scope(previous) else None
        pending = pending if in_scope(pending) else None
        if use_demo and previous and (legacy_context := state.get("last_query")) and not previous.get("demo_plan"):
            previous = {**previous, "resolved_slots": legacy_context, "demo_plan": {"intent": "revenue"}}
        reply = assistant(database, text, previous, pending)
        original = pending["source_question"] if pending else text
        update: ConversationState = {"reply": reply, "pending_data_query": None}
        if reply["kind"] in {"query", "answer"}:
            reply.update(table_scope)
            query_question = previous["question"] if previous and reply.get("refined_from") else original
            update["last_data_query"] = {**_remember_query(reply, data_source, query_question), **table_scope}
            if pending and "resolved_slots" not in update["last_data_query"]:
                update["last_data_query"]["resolved_slots"] = pending["resolved_slots"]
            if use_demo:
                update["last_query"] = reply["context"] if reply["kind"] == "answer" else None
        elif reply["kind"] == "clarification":
            slot = reply.get("slot") or clarification_slot(reply["question"])
            slots = pending["resolved_slots"] if pending else stated_slots(original, reference=reference)
            if "period" in slots:
                slots = {**slots, "period": calendar_month(slots["period"], reference=reference)}
            if pending and (slot in slots or compact(reply["question"]) == compact(pending["prompt"])
                            or pending["resumes"] + 1 >= _MAX_MODEL_RESUMES):
                return _stop_clarification("已收到你的补充，但这次没有生成可执行的查询。已结束澄清，你可以直接重述问题。")
            next_pending = {
                "source": data_source, "source_question": original, "prompt": reply["question"],
                "slot": slot, "resolved_slots": slots, "answers": pending["answers"] if pending else [],
                "misses": 0, "resumes": pending["resumes"] + 1 if pending else 0,
                **table_scope,
            }
            update["pending_data_query"] = next_pending
            update["reply"] = _data_clarification(next_pending)
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
    graph.add_conditional_edges("clarify", route, {"clarify": "clarify", "query": "query", "interpret": "interpret", "assist": "assist", "stop": END})
    graph.add_conditional_edges("approve_write", route, {"apply_write": "apply_write", "stop": END})
    graph.add_edge("query", END)
    graph.add_edge("assist", END)
    graph.add_edge("apply_write", END)
    return graph.compile(checkpointer=checkpointer)
