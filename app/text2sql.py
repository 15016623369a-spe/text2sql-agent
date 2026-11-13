"""Single-generation Text-to-SQL baseline."""
from __future__ import annotations

import json

from app.tools.db import ReadOnlyDB

PROMPT_VERSION = "v1-full-schema-1"
SYSTEM_PROMPT = (
    "Generate a single read-only SQLite query that answers the question. "
    "Use only the provided schema and use the evidence when relevant. "
    "Return a JSON object with the SQL string, for example "
    '{"sql": "SELECT COUNT(*) FROM orders"}. '
    "Return no Markdown, explanations, or other fields."
)
SCHEMA_QUERY = """
SELECT name, sql FROM main.sqlite_schema
WHERE type IN ('table', 'view') AND name NOT GLOB 'sqlite_*' AND sql IS NOT NULL
ORDER BY type, name
"""


def schema_objects(db: ReadOnlyDB) -> dict[str, str]:
    _, ddl = db.query(SCHEMA_QUERY, limit=None)
    return dict(ddl)


def full_schema(db: ReadOnlyDB) -> str:
    return "\n\n".join(schema_objects(db).values())


def build_messages(schema: str, question: str, evidence: str, *, dialect: str = "SQLite") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT.replace("SQLite", dialect)},
        {
            "role": "user",
            "content": f"{dialect} schema:\n{schema}\n\nQuestion:\n{question}\n\nEvidence:\n{evidence}",
        },
    ]


def parse_sql(content: str) -> str:
    result = json.loads(content)
    if not isinstance(result, dict) or set(result) != {"sql"}:
        raise ValueError("Expected a JSON object with one sql field")
    sql = result["sql"]
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("Expected a nonempty SQL string")
    return sql.strip()
