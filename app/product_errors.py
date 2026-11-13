"""Public error reasons and recovery hints for chat requests."""
from __future__ import annotations

import logging

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.telemetry import Trace

_logger = logging.getLogger(__name__)
_HINTS = {
    "access_required": "请通过已配置的入口登录，再打开会话。",
    "cross_site_request": "请从应用页面重新发起请求。",
    "invalid_request": "请检查问题、会话编号和请求字段。",
    "request_too_large": "请缩短问题或补充线索，再重新发送。",
    "request_timeout": "请检查连接后重新发送。",
    "model_not_configured": "请配置模型地址和密钥，或关闭模型选项后使用演示查询。",
    "database_unavailable": "请在侧栏选择已配置的数据源。",
    "account_busy": "请等当前任务完成后再发送。",
    "service_busy": "请稍后重试。",
    "model_request_error": "请稍后重试。若持续失败，请检查模型连接和配额。",
    "model_output_error": "请补充明确的指标、时间或分组条件，再重新提问。",
    "policy_blocked": "请改问只读查询，或使用演示库的审批入口修改订单备注。",
    "demo_scope": "可以换一种问法，或切换到已配置的 BIRD 数据源。",
    "postgres_scope": "请核对侧栏已选的数据表，再提交查询。",
    "timeout": "请缩小时间范围，或减少要查询的字段和分组。",
    "execution_error": "请核对数据源和字段范围，再试一次。",
    "missing_table": "请确认当前数据源包含需要的表。",
    "missing_column": "请说明要查询的字段，或确认所选数据源包含该字段。",
    "repeated_sql_error": "请补充明确的指标、时间或分组条件，再重新提问。",
    "recovery_budget_exhausted": "请简化问题或补充数据表和字段线索，再重试。",
    "workflow_error": "请稍后重试。若问题持续，请提供追踪编号。",
}
_HTTP_REASONS = {
    400: "invalid_request", 401: "access_required", 403: "cross_site_request",
    404: "database_unavailable", 408: "request_timeout", 409: "model_not_configured",
    413: "request_too_large", 422: "invalid_request", 429: "account_busy", 503: "service_busy",
}
_CAUSES = {
    "model_request_error": "模型服务没有完成请求。",
    "model_output_error": "模型没有返回可执行的查询。",
    "policy_blocked": "查询未通过只读安全检查。",
    "demo_scope": "演示库只暴露 orders 表。",
    "postgres_scope": "查询引用了当前 PostgreSQL 范围之外的表。",
    "timeout": "查询超过了执行时间限制。",
    "missing_table": "查询引用的数据表在当前范围内找不到。",
    "missing_column": "查询引用的字段在当前范围内找不到。",
    "repeated_sql_error": "SQL 重试后仍出现同一个错误。",
    "recovery_budget_exhausted": "SQL 在允许的重试次数内仍无法执行。",
}


def public_error(reason: object, message: str, trace_id: str) -> dict:
    if not isinstance(reason, str) or reason not in _HINTS:
        reason = "workflow_error"
    return {"kind": "error", "message": _CAUSES.get(reason, message), "stop_reason": reason,
            "suggestion": _HINTS[reason], "trace_id": trace_id}


def is_chat_request(request: Request) -> bool:
    return request.method == "POST" and request.url.path == "/api/v1/chat"


async def chat_http_error(request: Request, status: int, detail: str, *, headers: dict | None = None) -> JSONResponse:
    trace = Trace("chat", lambda _: None)
    reason = _HTTP_REASONS.get(status, "workflow_error")
    error = public_error(reason, detail, trace.id)
    if status != 401:
        report = trace.finish("error", reason)
        try:
            await run_in_threadpool(request.app.state.traces.put, report, owner=request.state.owner)
        except Exception as exc:
            _logger.error("Local trace persistence failed (%s)", type(exc).__name__)
    return JSONResponse(
        {"detail": detail, **error}, status_code=status,
        headers={**(headers or {}), "X-Trace-Id": trace.id, "Cache-Control": "no-store"},
    )
