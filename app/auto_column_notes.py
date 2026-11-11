"""Rank field metadata and derive storage hints from bounded SQLite samples."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.column_notes import ColumnNote
from app.schema_linking import _tokens
from app.tools.db import ReadOnlyDB, quote_identifier

VERSION = "sqlite-auto-column-notes-2"
SAMPLE_ROWS = 256
MAX_NOTES = 6
MAX_CHARS = 850
_MIN_RELATIVE_SCORE = 0.5
_SAMPLE_TEXT_CHARS = 96
_GENERIC = set("data value field record recorded each per show list get id identifier table row column".split())
_TEMPORAL = {"date", "month", "year", "timestamp"}
_NUMERIC = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_BOUND = re.compile(r"(?:[<>]=?|[≤≥])\s*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


_MISSING_TERMS = {"missing", "null", "empty", "blank", "unknown", "unrecorded"}
_AVERAGE_COUNT = re.compile(
    r"\b(?:average|mean|avg)\s+(?:number|count)\s+of\s+([^.!?\n]+?)\s+per\s+([\w -]+)",
    re.IGNORECASE,
)
_Field = tuple[str, str]
_ForeignKey = tuple[str, str, str, str]


def _terms(text: str) -> set[str]:
    return {word for word in _tokens(text) if len(word) > 1 and not word.isdecimal()} - _GENERIC


def rank_fields(
    fields: dict[_Field, str], query: str, *, min_relative_score: float = _MIN_RELATIVE_SCORE,
) -> list[_Field]:
    """Match column identifiers and meanings, without inheriting the table's score."""
    query_terms = _terms(query)
    name_terms = {key: _terms(key[1]) for key in fields}
    terms = {key: name_terms[key] | _terms(meaning) for key, meaning in fields.items()}
    frequency = Counter(term for words in terms.values() for term in words & query_terms)
    weights = {term: math.log1p(len(fields) / count) for term, count in frequency.items()}
    scored = []
    for key, words in terms.items():
        score = sum(weights[t] * (1.4 if t in name_terms[key] else 1) for t in sorted(words & query_terms))
        if score:
            scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    threshold = scored[0][0] * min_relative_score if scored else 0
    return [key for score, key in scored if score >= threshold]


def summarize_storage(samples: list | tuple, *, temporal: bool) -> list[str]:
    if samples and all(value is None for value in samples):
        return ["All sampled values are NULL"]
    present = [value for value in samples if value is not None]
    strings = [value for value in present if isinstance(value, str)]
    text = [value.strip() for value in strings if value.strip()]
    facts = []
    if text and len(strings) == len(present) and all(len(value) <= _SAMPLE_TEXT_CHARS for value in strings):
        if temporal and all(re.fullmatch(r"(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])", value) for value in text):
            facts.append("Sampled months are text in YYYYMM format")
        elif all(_NUMERIC.fullmatch(value) for value in text):
            if any(re.fullmatch(r"0[0-9]+", value) for value in text):
                facts.append("Sampled digit strings include leading zeros")
            else:
                facts.append("Sampled numeric values are stored as text")
        elif any(_BOUND.fullmatch(value) for value in text):
            facts.append("Sampled text includes numeric bounds with comparison prefixes")
    if any(len(value) <= _SAMPLE_TEXT_CHARS and not value.strip() for value in strings):
        missing = ["NULL"] if len(present) < len(samples) else []
        if "" in present:
            missing.append("empty strings")
        if any(0 < len(value) <= _SAMPLE_TEXT_CHARS and not value.strip() for value in strings):
            missing.append("whitespace-only strings")
        facts.append("Sample contains " + ", ".join(missing))
    return facts


def _documented_labels(encoding: str) -> list[str]:
    # BIRD uses bullets for some closed lists. Require simple literal labels.
    if not encoding.startswith("•"):
        return []
    labels = [label.strip() for label in encoding.split("•")[1:]]
    if all(re.fullmatch(r"[\w -]{1,32}", label) for label in labels):
        return labels
    return []


def _encoding(note: ColumnNote, samples: tuple) -> str:
    labels = _documented_labels(note.encoding)
    if labels:
        documented = {label.casefold() for label in labels}
        observed = sorted({value for value in samples if isinstance(value, str) and value.casefold() in documented})
        if observed:
            return "Documented categories observed with exact case: " + ", ".join(json.dumps(value) for value in observed)
    text = re.sub(r"^commonsense evidence:\s*", "", note.encoding, flags=re.IGNORECASE)
    return text if len(text) <= 180 else ""


def _field(table: str, column: str) -> str:
    return f"{json.dumps(table)}.{json.dumps(column)}"


@dataclass(frozen=True)
class _Profile:
    encoding: str
    facts: list[str]
    sampled: int
    populated: int

    @property
    def sparse(self) -> bool:
        return bool(self.sampled and self.populated * 20 <= self.sampled)


def _count_scope(query: str, keys: set[_ForeignKey]) -> tuple[str, set[str]]:
    match = _AVERAGE_COUNT.search(query)
    if not match:
        return "", set()
    metric = set(_tokens(match[1])) - {"id", "number", "count", "total"}
    entity = _terms(match[2])
    relations: dict[str, list[_ForeignKey]] = defaultdict(list)
    for key in sorted(keys):
        relations[key[0]].append(key)

    candidates = []
    for child, edges in relations.items():
        for edge in edges:
            _, source, parent, _ = edge
            scope_hits = entity & _terms(f"{source} {parent}")
            labels = " ".join(f"{other[1]} {other[2]}" for other in edges if other != edge)
            metric_hits = metric.intersection(_tokens(f"{child} {labels}"))
            if scope_hits and metric_hits:
                candidates.append(((len(metric_hits), len(scope_hits)), edge))
    if not candidates:
        return "", set()
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return "", set()
    _, (child, source, parent, target) = candidates[0]
    query_terms = _terms(query)
    siblings = {
        src for src, _, dst, _ in keys
        if dst == parent and src != child and not ((_terms(src) - entity) & query_terms)
    }
    other_lookups = {dst for src, _, dst, _ in keys if src in siblings}
    protected = {dst for src, _, dst, _ in keys if src not in siblings} | {parent, child}
    hint = (
        f"- Count scope — start from all eligible rows of {json.dumps(parent)}. "
        f"Match {_field(child, source)} to {_field(parent, target)}. "
        f"Count matching {json.dumps(child)} rows per entity. Keep zero counts unless the question "
        f"requires a match. With LEFT JOIN, count a non-NULL child column."
    )
    return hint, siblings | (other_lookups - protected)


class AutoColumnNotes:
    def __init__(self, db_path: Path, metadata: dict[str, dict[str, ColumnNote]]):
        self._db_path = db_path
        self._metadata = metadata
        self._profiles: dict[str, dict[str, _Profile | None]] = {}

    def _profile(self, db: ReadOnlyDB, table: str, notes: dict[str, ColumnNote]) -> dict[str, _Profile]:
        names = sorted(notes)
        expressions = []
        for name in names:
            identifier = quote_identifier(name)
            expressions.append(
                f"CASE typeof({identifier}) WHEN 'text' THEN substr({identifier}, 1, {_SAMPLE_TEXT_CHARS + 1}) "
                f"WHEN 'blob' THEN X'00' ELSE {identifier} END"
            )
        sql = f"SELECT {', '.join(expressions)} FROM {quote_identifier(table)} LIMIT {SAMPLE_ROWS}"
        try:
            _, rows = db.query(sql, limit=SAMPLE_ROWS)
        except (sqlite3.Error, TimeoutError, UnicodeError):
            return {}
        profiles = {}
        for name, values in zip(names, zip(*rows)):
            note = notes[name]
            temporal = bool(_terms(f"{name} {note.meaning}") & _TEMPORAL)
            populated = sum(
                value is not None and (not isinstance(value, str) or bool(value.strip()))
                for value in values
            )
            profiles[name] = _Profile(
                _encoding(note, values), summarize_storage(values, temporal=temporal), len(values), populated,
            )
        return profiles

    def _related_label(
        self, field: _Field, query_terms: set[str], keys: set[_ForeignKey],
    ) -> str:
        table, column = field
        words = _tokens(column)
        matches = []
        for src, key, dst, target in sorted(keys):
            if src != table:
                continue
            for label, profile in sorted(self._profiles.get(dst, {}).items()):
                suffix = _tokens(label)
                if not profile or not suffix or len(words) <= len(suffix) or words[-len(suffix):] != suffix:
                    continue
                role = words[:-len(suffix)]
                key_words = _tokens(key)
                specific_role = (set(role) - _terms(dst)) & query_terms
                whole_key = role == [term for term in key_words if term != "id"]
                if whole_key and not (_terms(src) & query_terms):
                    continue
                if role == key_words[:len(role)] and specific_role and not profile.sparse:
                    matches.append(
                        f"- {_field(src, key)} → {_field(dst, target)} | "
                        f"Use {_field(dst, label)} for the related label. "
                        "The matching local label is sparsely populated in the SQLite sample."
                    )
        return matches[0] if len(matches) == 1 else ""

    def render(
        self, selected_columns: dict[str, set[str]], query: str, *,
        foreign_keys: set[_ForeignKey] | None = None,
        value_matched_columns: dict[str, list[str]] | None = None,
    ) -> list[str]:
        notes = {}
        for table, columns in selected_columns.items():
            metadata = self._metadata.get(table.casefold(), {})
            for column in columns:
                notes[table, column] = metadata.get(column.strip().casefold(), ColumnNote("", ""))
        keys = {
            key for key in foreign_keys or ()
            if (key[0], key[1]) in notes and (key[2], key[3]) in notes
        }
        scope, excluded_tables = _count_scope(query, keys)
        matched = {
            (table, column) for table, columns in (value_matched_columns or {}).items()
            for column in columns if (table, column) in notes and table not in excluded_tables
        }
        fields = {key: note.meaning for key, note in notes.items() if key[0] not in excluded_tables}
        matching_fields = rank_fields(fields, query, min_relative_score=0)
        sample_tables = {table for table, _ in matching_fields} | {table for table, _ in matched}
        related = {dst for src, _, dst, _ in keys if src in sample_tables}
        pending = {
            table: selected_columns[table] - self._profiles.get(table, {}).keys()
            for table in sorted(sample_tables | related)
        }
        if any(pending.values()):
            with ReadOnlyDB(self._db_path) as db:
                for table, columns in pending.items():
                    if columns:
                        table_notes = {column: notes[table, column] for column in columns}
                        profiles = self._profile(db, table, table_notes)
                        self._profiles.setdefault(table, {}).update(
                            {column: profiles.get(column) for column in columns}
                        )

        query_terms = _terms(query)
        requests_missing = bool(query_terms & _MISSING_TERMS)
        normalized_query = query.casefold()
        lines = []
        used = 0
        redirects = {}
        for table, column in dict.fromkeys([*matching_fields, *sorted(matched)]):
            profile = self._profiles[table].get(column)
            explicit_name = any(
                quote + column.casefold() + quote in normalized_query for quote in ('"', "`")
            )
            if profile and profile.sparse and not requests_missing and not explicit_name:
                redirects[table, column] = self._related_label((table, column), query_terms, keys)
                fields.pop((table, column))

        ordering = sorted(matched - redirects.keys()) + rank_fields(fields, query)
        candidates = [scope, *redirects.values()]
        for table, column in dict.fromkeys(ordering):
            note = notes[table, column]
            profile = self._profiles.get(table, {}).get(column)
            parts = []
            if note.meaning and len(note.meaning) <= 160:
                parts.append(f"meaning — {note.meaning}")
            if encoding := profile.encoding if profile else _encoding(note, ()):
                parts.append(f"documented values — {encoding}")
            if profile:
                facts = list(profile.facts)
                if profile.sparse and profile.populated:
                    facts.append("Very few sampled rows have a populated value")
                if facts:
                    parts.append("SQLite sample — " + "; ".join(facts))
            if (table, column) in matched:
                parts.append("A stored category in this column matches the question or evidence")
            if parts:
                candidates.append(f"- {_field(table, column)} | " + " | ".join(parts))

        for line in dict.fromkeys(candidates):
            if not line:
                continue
            if len(lines) >= MAX_NOTES:
                break
            length = len(line) + bool(lines)
            if used + length <= MAX_CHARS:
                lines.append(line)
                used += length
        return lines
