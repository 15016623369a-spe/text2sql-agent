"""Persist completed conversation turns for an authenticated owner and session."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from app.commerce_demo import QueryContext
from app.conversation_slots import resolved_demo_query_label

_PAGE_BYTES = 4 * 1024 * 1024


class ConversationHistory:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    turn_id TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    trace TEXT NOT NULL,
                    PRIMARY KEY (owner, session_id, sequence)
                ) WITHOUT ROWID
            """)

    def append(self, owner: str, session_id: str, prompt: str, reply: dict, trace: dict) -> None:
        # Both messages share one row so readers only see complete turns.
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute("""
                INSERT INTO conversation_turns (owner, session_id, sequence, turn_id, prompt, reply, trace)
                SELECT ?, ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ?, ?
                    FROM conversation_turns WHERE owner = ? AND session_id = ?
            """, (owner, session_id, trace["trace_id"], prompt, _encode(reply), _encode(trace), owner, session_id))

    def page(self, owner: str, session_id: str, limit: int, before: int | None) -> dict:
        sql = "SELECT sequence, turn_id, prompt, reply, trace FROM conversation_turns WHERE owner = ? AND session_id = ?"
        params = [owner, session_id]
        if before is not None:
            sql += " AND sequence < ?"
            params.append(before)
        params.append(limit + 1)
        records = []
        size, next_before = 0, None
        previous = None
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            for row in conn.execute(sql + " ORDER BY sequence DESC LIMIT ?", params):
                row_size = sum(len(value.encode()) for value in row[1:])
                if len(records) == limit or (records and size + row_size > _PAGE_BYTES):
                    next_before = records[-1][0]
                    break
                records.append(row)
                size += row_size
            if records and records[-1][0] > 1:
                predecessor = conn.execute("""
                    SELECT reply FROM conversation_turns
                    WHERE owner = ? AND session_id = ? AND sequence < ?
                        AND json_extract(reply, '$.kind') IN ('query', 'answer')
                    ORDER BY sequence DESC LIMIT 1
                """, (owner, session_id, records[-1][0])).fetchone()
                if predecessor:
                    previous = json.loads(predecessor[0])

        messages = []
        for _, turn_id, prompt, reply, summary in reversed(records):
            trace = json.loads(summary)
            content = json.loads(reply)
            if content["kind"] in {"query", "answer"}:
                if (content["kind"] == "answer" and content.get("refined_from")
                        and content.get("data_source") == "演示订单库"
                        and previous and previous["kind"] == "answer" and previous.get("data_source") == "演示订单库"
                        and previous.get("context")):
                    # Older transcripts stored the original wording even after the month was corrected.
                    try:
                        context = QueryContext.model_validate(previous["context"])
                    except ValidationError:
                        pass
                    else:
                        label = resolved_demo_query_label(
                            context, excluded_regions=tuple(previous.get("excluded_regions") or ()),
                        )
                        if label:
                            content["refined_from"] = label
                previous = content
            common = {"turn_id": turn_id, "trace_id": turn_id}
            messages.extend((
                {**common, "role": "user", "content": prompt, "created_at": trace["started_at"], "trace": None},
                {**common, "role": "assistant", "content": content,
                 "created_at": trace["ended_at"], "trace": trace},
            ))
        return {"messages": messages, "next_before": next_before}


def _encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
