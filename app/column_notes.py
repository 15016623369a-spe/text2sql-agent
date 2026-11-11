"""Load and render bounded column metadata for an SQL prompt."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from app.schema_linking import _tokens


@dataclass(frozen=True)
class ColumnNote:
    meaning: str
    encoding: str


def _normalize(value: str | None) -> str:
    return " ".join((value or "").split())


def load_column_notes(path: Path) -> dict[str, ColumnNote]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    notes = {}
    for row in csv.DictReader(io.StringIO(text)):
        key = (row.get("original_column_name") or "").strip().casefold()
        if not key:
            continue
        meaning_parts = []
        seen = {key}
        for name in ("column_name", "column_description"):
            value = _normalize(row.get(name))
            if value and value.casefold() not in seen:
                meaning_parts.append(value)
                seen.add(value.casefold())
        encoding = _normalize(row.get("value_description"))
        if meaning_parts or encoding:
            notes.setdefault(key, ColumnNote(" | ".join(meaning_parts), encoding))
    return notes


def render_column_notes(
    selected_columns: dict[str, set[str]],
    notes: dict[str, dict[str, ColumnNote]],
    query: str,
    verified: dict[tuple[str, str], tuple[str, str]],
    *,
    max_notes: int,
    max_chars: int,
) -> list[str]:
    query_terms = set(_tokens(query))
    ranked = []
    for table, columns in selected_columns.items():
        official = notes.get(table, {})
        for column in columns:
            note = official.get(column.strip().casefold(), ColumnNote("", ""))
            override = verified.get((table, column))
            encoding, aliases = override if override else (note.encoding, "")
            parts = []
            if note.meaning and len(note.meaning) <= 210:
                parts.append(f"meaning — {note.meaning}")
            if encoding and len(encoding) <= 290:
                parts.append(f"stored values — {encoding}")
            if not parts:
                continue

            searchable = " ".join((table, column, note.meaning, aliases or encoding))
            score = len(query_terms & set(_tokens(searchable)))
            if score:
                identifier = f"{json.dumps(table)}.{json.dumps(column)}"
                line = f"- {identifier} | " + " | ".join(parts)
                ranked.append((override is None, -score, table, column, line))

    rendered = []
    used = 0
    for _, _, _, _, line in sorted(ranked):
        if len(rendered) >= max_notes:
            break
        length = len(line) + bool(rendered)
        if used + length <= max_chars:
            rendered.append(line)
            used += length
    return rendered
