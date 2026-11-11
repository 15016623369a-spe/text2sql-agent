"""Strict payloads used at V3 generation and recovery boundaries."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_RECOVERIES = 3
SqlText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Phase = Literal["dry_run", "execute"]
FailureKind = Literal[
    "missing_table", "missing_column", "sql_repair", "policy_blocked", "timeout", "execution_error",
]


class _StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SqlCandidate(_StrictPayload):
    sql: SqlText


class SqlFailure(_StrictPayload):
    type: str
    phase: Phase
    kind: FailureKind
    detail: str
    identifier: str | None = None


class DryRunCheck(_StrictPayload):
    status: Literal["passed", "failed"]
    latency_seconds: float = Field(ge=0)
    error: SqlFailure | None = None


class RecoveryFeedback(_StrictPayload):
    action: Literal["sql_repair", "schema_expansion"]
    retry_count: int = Field(ge=1, le=MAX_RECOVERIES)
    previous_sql: SqlText
    sqlite_error: SqlFailure
    instruction: str = "Revise the failed SQL using the supplied schema and original question."
