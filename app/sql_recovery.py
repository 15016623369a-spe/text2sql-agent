"""Bounded, error-driven Text-to-SQL recovery for read-only queries."""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
from collections import Counter
from collections.abc import Callable
from time import perf_counter
from typing import Literal

from pydantic import ValidationError
from typing_extensions import TypedDict

from app.llm import completion_text
from app.telemetry import span
from app.recovery_graph import build_graph
from app.recovery_models import MAX_RECOVERIES, DryRunCheck, FailureKind, Phase, RecoveryFeedback, SqlCandidate, SqlFailure
from app.tools.db import ReadOnlyViolation
from app.tools.postgres import PostgresQueryError, PostgresScopeViolation, PostgresUnavailable

VERSION = "sqlite-dry-run-recovery-2"
_MISSING = re.compile(r"^no such (table|column): (.+)$", re.IGNORECASE)
_REPAIRABLE = re.compile(r"syntax error$|^incomplete input$|^unrecognized token:|^ambiguous column name:|^no such function:", re.I)
_POLICY_ERRORS = {sqlite3.SQLITE_AUTH, sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY}
_RECOVERABLE = {"missing_table", "missing_column", "sql_repair"}


class DemoScopeViolation(ReadOnlyViolation):
    """The read query targets an object outside the public demo schema."""


class _State(TypedDict, total=False):
    messages: list[dict[str, str]]
    selection: dict
    trace: list[dict]
    action: str
    added_tables: list[str]
    started: float
    retry_count: int
    prediction: tuple[list[str], list[tuple]]
    stop_reason: str | None


def classify_error(exc: sqlite3.Error | OSError, phase: Phase) -> SqlFailure:
    """Classify an adapter error for routing and structured feedback."""
    detail = str(exc)
    code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
    kind: FailureKind = "execution_error"
    identifier = None
    if isinstance(exc, TimeoutError) or code == sqlite3.SQLITE_INTERRUPT:
        kind = "timeout"
    elif isinstance(exc, PostgresUnavailable):
        kind = "database_unavailable"
    elif isinstance(exc, DemoScopeViolation):
        kind = "demo_scope"
    elif isinstance(exc, PostgresScopeViolation):
        kind = "postgres_scope"
    elif (
        isinstance(exc, ReadOnlyViolation) or code in _POLICY_ERRORS
        or detail.casefold().startswith("not authorized") or "one statement at a time" in detail
    ):
        kind = "policy_blocked"
    elif isinstance(exc, PostgresQueryError):
        if exc.sqlstate in {"42P01", "42703"} and exc.identifier:
            kind = "missing_table" if exc.sqlstate == "42P01" else "missing_column"
            identifier = exc.identifier
        elif exc.sqlstate in {"42601", "42702", "42703", "42803", "42804", "42883", "42P01"}:
            kind = "sql_repair"
    elif match := _MISSING.fullmatch(detail):
        kind = "missing_table" if match[1].casefold() == "table" else "missing_column"
        identifier = match[2]
    elif _REPAIRABLE.search(detail):
        kind = "sql_repair"
    return SqlFailure(type=type(exc).__name__, phase=phase, kind=kind, detail=detail, identifier=identifier)


def run_recovery(
    messages: list[dict[str, str]],
    selection: dict,
    *,
    request_model: Callable[[list[dict[str, str]]], dict],
    validate_sql: Callable[[str], None],
    execute_sql: Callable[[str], tuple[list[str], list[tuple]]],
    expand_scope: Callable[[dict, str, str], dict | None],
    build_prompt: Callable[[dict], list[dict[str, str]]],
    max_recoveries: int,
    dialect: str = "SQLite",
) -> tuple[dict, tuple[list[str], list[tuple]] | None]:
    if not 0 <= max_recoveries <= MAX_RECOVERIES:
        raise ValueError(f"Recovery budget must be between 0 and {MAX_RECOVERIES}")

    def generate(state: _State) -> _State:
        started = perf_counter()
        attempt = {
            "action": state["action"], "added_tables": state["added_tables"],
            "tables": state["selection"]["tables"], "model_input": state["messages"],
            "usage": {}, "sql": None, "retry_count": state["retry_count"],
        }
        stop = None
        try:
            response = request_model(state["messages"])
        except urllib.error.HTTPError as exc:
            attempt.update(status="model_request_error", error={"type": type(exc).__name__, "http_status": exc.code})
            stop = "model_request_error"
        except (OSError, ValueError, IndexError, KeyError, TypeError) as exc:
            attempt.update(status="model_request_error", error={"type": type(exc).__name__})
            stop = "model_request_error"
        else:
            usage = response.get("usage")
            attempt["usage"] = usage if isinstance(usage, dict) else {}
            try:
                candidate = SqlCandidate.model_validate_json(completion_text(response))
                attempt.update(sql=candidate.sql, status="generated")
            except ValidationError as exc:
                attempt.update(status="model_output_error", error={
                    "type": type(exc).__name__,
                    "validation_issues": [issue["type"] for issue in exc.errors(include_input=False)],
                })
                stop = "model_output_error"
            except (ValueError, TypeError, IndexError, KeyError) as exc:
                attempt.update(status="model_output_error", error={"type": type(exc).__name__})
                stop = "model_output_error"
        if stop:
            attempt["latency_seconds"] = round(perf_counter() - started, 6)
        return {"trace": [*state["trace"], attempt], "stop_reason": stop, "started": started}

    def dry_run(state: _State) -> _State:
        attempt = dict(state["trace"][-1])
        started = perf_counter()
        stop = None
        try:
            with span("sql_dry_run"):
                validate_sql(attempt["sql"])
        except (sqlite3.Error, OSError) as exc:
            failure = classify_error(exc, "dry_run")
            attempt.update(status="dry_run_error", error=failure.model_dump(exclude_none=True))
            check = DryRunCheck(status="failed", latency_seconds=round(perf_counter() - started, 6), error=failure)
            attempt["latency_seconds"] = round(perf_counter() - state["started"], 6)
            if failure.kind not in _RECOVERABLE:
                stop = failure.kind
        else:
            attempt["status"] = "validated"
            check = DryRunCheck(status="passed", latency_seconds=round(perf_counter() - started, 6))
        attempt["dry_run"] = check.model_dump(exclude_none=True)
        return {"trace": [*state["trace"][:-1], attempt], "stop_reason": stop}

    def execute(state: _State) -> _State:
        attempt = dict(state["trace"][-1])
        update: _State = {}
        try:
            with span("sql_execute"):
                update["prediction"] = execute_sql(attempt["sql"])
        except (sqlite3.Error, OSError) as exc:
            failure = classify_error(exc, "execute")
            attempt.update(status="prediction_error", error=failure.model_dump(exclude_none=True))
            if failure.kind not in _RECOVERABLE:
                update["stop_reason"] = failure.kind
        else:
            attempt["status"] = "executed"
            update["stop_reason"] = "executed"
        attempt["latency_seconds"] = round(perf_counter() - state["started"], 6)
        update["trace"] = [*state["trace"][:-1], attempt]
        return update

    def route(state: _State) -> Literal["repair", "expand", "stop"]:
        if state["stop_reason"]:
            return "stop"
        return "expand" if "identifier" in state["trace"][-1]["error"] else "repair"

    def prepare_retry(state: _State) -> _State:
        trace = state["trace"]
        attempt = trace[-1]
        failure = attempt["error"]
        sql = attempt["sql"]
        signature = sql.rstrip().removesuffix(";").rstrip(), failure
        if any(
            (previous["sql"].rstrip().removesuffix(";").rstrip(), previous.get("error")) == signature
            for previous in trace[:-1]
        ):
            return {"stop_reason": "repeated_sql_error"}
        if state["retry_count"] >= max_recoveries:
            return {"stop_reason": "recovery_budget_exhausted"}

        current = state["selection"]
        expanded = expand_scope(current, failure["kind"], failure["identifier"]) if "identifier" in failure else None
        selection = expanded if expanded is not None else current
        added = [table for table in selection["tables"] if table not in current["tables"]]
        action = "schema_expansion" if added else "sql_repair"
        retry_count = state["retry_count"] + 1
        feedback = RecoveryFeedback(
            action=action, retry_count=retry_count, previous_sql=sql,
            sqlite_error=SqlFailure.model_validate(failure),
        )
        if dialect == "PostgreSQL":
            fields = feedback.model_dump(exclude_none=True)
            fields["postgres_error"] = fields.pop("sqlite_error")
            serialized = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        else:
            serialized = feedback.model_dump_json(exclude_none=True)
        retry = build_prompt(selection)
        messages = [
            *retry[:-1],
            {**retry[-1], "content": retry[-1]["content"] + "\n\nExecution feedback:\n" + serialized},
        ]
        return {
            "selection": selection, "messages": messages, "action": action,
            "added_tables": added, "retry_count": retry_count,
        }

    graph = build_graph(_State, generate, dry_run, execute, route, prepare_retry)
    state = graph.invoke(
        {"messages": messages, "selection": selection, "trace": [], "action": "initial",
         "added_tables": [], "retry_count": 0},
        config={"recursion_limit": 4 * (max_recoveries + 1) + 2},
    )
    trace = state["trace"]
    final = trace[-1]
    usage: Counter[str] = Counter()
    for attempt in trace:
        usage.update({key: value for key, value in attempt["usage"].items() if type(value) is int})
    record = {
        "sql": final["sql"], "status": final["status"], "model_calls": len(trace), "usage": dict(usage),
        "attempts": trace, "recovery_attempts": state["retry_count"], "recovery_stop_reason": state["stop_reason"],
        "first_sql_executed": trace[0]["status"] == "executed", "first_ex": 0,
        "final_schema_linking": state["selection"],
    }
    if error := final.get("error"):
        record["error"] = error["type"]
        if "detail" in error:
            record["error_detail"] = error["detail"]
        if "http_status" in error:
            record["http_status"] = error["http_status"]
    return record, state.get("prediction")
