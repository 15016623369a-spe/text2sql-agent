"""Rank schema objects locally and preserve declared foreign-key paths."""
from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from collections import Counter, deque
from contextlib import closing
from difflib import get_close_matches
from itertools import combinations
from pathlib import Path

from app.tools.db import ReadOnlyDB, quote_identifier

VERSION = "bm25-evidence-fk-1"
DENSE_TABLE_BUDGET = 2
_LOOKUP_ROWS = 256
_LOOKUP_COLUMNS = 12
_LOOKUP_TEXT_CHARS = 60
_MAX_EVIDENCE_TABLES = 3
_LOOKUP_LABEL = re.compile(r"[\w][\w /&'-]*")

_STOP_WORDS = set("a an and as at be by for from give in is it of on or that the this to what which with".split())


def _tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", text)
    result = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        if word not in _STOP_WORDS:
            result.append(word)
    return result


class _BM25:
    def __init__(self, documents: dict[str, str]):
        self._counts = {name: Counter(_tokens(text)) for name, text in documents.items()}
        self._lengths = {name: sum(counts.values()) for name, counts in self._counts.items()}
        self._mean_length = sum(self._lengths.values()) / max(1, len(documents)) or 1.0
        frequencies = Counter(term for counts in self._counts.values() for term in counts)
        self._idf = {
            term: math.log1p((len(documents) - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in frequencies.items()
        }

    def rank(self, query: str) -> list[tuple[str, float]]:
        terms = sorted(set(_tokens(query)) & self._idf.keys())
        scores = []
        for name, counts in self._counts.items():
            norm = 0.25 + 0.75 * self._lengths[name] / self._mean_length
            score = sum(
                self._idf[term] * counts[term] * 2.5 / (counts[term] + 1.5 * norm)
                for term in terms if term in counts
            )
            scores.append((name, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))


def _bridges(seeds: list[str], graph: dict[str, set[str]]) -> set[str]:
    bridges = set()
    for start, goal in combinations(seeds, 2):
        parents: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue and goal not in parents:
            current = queue.popleft()
            for neighbor in sorted(graph[current] - parents.keys()):
                parents[neighbor] = current
                queue.append(neighbor)
        if goal not in parents:
            continue
        current = parents[goal]
        while current is not None and current != start:
            bridges.add(current)
            current = parents[current]
    return bridges - set(seeds)


def _descriptions(path: Path) -> list[str]:
    if not path.is_file():
        return []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return [
        value.strip()
        for row in csv.DictReader(io.StringIO(text))
        for name in ("column_name", "column_description")
        if (value := row.get(name))
    ]


def _phrase(text: str) -> str:
    return " " + " ".join(_tokens(text)) + " "


def _lookup_labels(db: ReadOnlyDB, table: str, columns: set[str]) -> dict[str, set[str]]:
    names = sorted(columns)
    projections = []
    for name in names:
        column = quote_identifier(name)
        projections.append(
            f"CASE WHEN typeof({column}) = 'text' THEN substr({column}, 1, {_LOOKUP_TEXT_CHARS + 1}) END"
        )
    sql = f"SELECT {', '.join(projections)} FROM {quote_identifier(table)} LIMIT {_LOOKUP_ROWS + 1}"
    try:
        _, rows = db.query(sql, limit=_LOOKUP_ROWS + 1)
    except (sqlite3.Error, TimeoutError, UnicodeError):
        return {}
    if len(rows) > _LOOKUP_ROWS:
        return {}
    labels = {}
    for name, values in zip(names, zip(*rows)):
        phrases = {
            _phrase(value)
            for value in values
            if isinstance(value, str) and 4 <= len(value) <= _LOOKUP_TEXT_CHARS
            and _LOOKUP_LABEL.fullmatch(value) and any(character.isalpha() for character in value)
        }
        if phrases := {phrase for phrase in phrases if phrase.strip()}:
            labels[name] = phrases
    return labels


class SchemaIndex:
    def __init__(self, db_path: Path, ddl: dict[str, str]):
        self.ddl = ddl
        self.columns: dict[str, set[str]] = {}
        self.foreign_keys: set[tuple[str, str, str, str]] = set()
        self._graph: dict[str, set[str]] = {name: set() for name in ddl}
        canonical_names = {name.casefold(): name for name in ddl}
        description_files = {
            path.stem.casefold(): path
            for path in (db_path.parent / "database_description").glob("*.csv")
        }
        documents = {}

        # The metadata queries are fixed, parameterized, and separate from generated SQL.
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            for name in ddl:
                self.columns[name] = {
                    row[0] for row in conn.execute("SELECT name FROM pragma_table_xinfo(?)", (name,))
                }
                description = description_files.get(name.casefold())
                notes = _descriptions(description) if description is not None else []
                documents[name] = " ".join([name, *sorted(self.columns[name]), *notes])
                for other, source_col, target_col in conn.execute(
                    'SELECT "table", "from", "to" FROM pragma_foreign_key_list(?)', (name,)
                ):
                    target = canonical_names.get(other.casefold())
                    if target is None or target == name:
                        continue
                    self._graph[name].add(target)
                    self._graph[target].add(name)
                    if target_col:
                        self.foreign_keys.add((name, source_col, target, target_col))
        self._ranker = _BM25(documents)
        self._labels = {}
        parents = {target for _, _, target, _ in self.foreign_keys}
        with ReadOnlyDB(db_path) as db:
            for table in sorted(parents):
                columns = self.columns[table]
                if 2 <= len(columns) <= _LOOKUP_COLUMNS:
                    self._labels[table] = _lookup_labels(db, table, columns)

    def _evidence(self, query: str, ranked: list[tuple[str, float]], top: list[str]):
        question = _phrase(re.sub(r'"[^"]{61,}"', " ", query))
        value_columns = {}
        priorities = {}
        for table, fields in self._labels.items():
            for column, labels in fields.items():
                specificity = max((len(label.split()) for label in labels if label in question), default=0)
                if specificity:
                    value_columns.setdefault(table, []).append(column)
                    priorities[table] = max(specificity, priorities.get(table, 0))
        candidates = [
            table for table, _ in ranked
            if table not in top and (table in value_columns or _phrase(table) in question)
        ]
        candidates.sort(key=lambda table: -priorities.get(table, 0))
        return candidates[:_MAX_EVIDENCE_TABLES], value_columns

    def select(
        self, query: str, top_k: int, *, expand: bool = True, dense_tables: list[str] | None = None,
    ) -> tuple[str, dict]:
        ranked = self._ranker.rank(query)
        top = [name for name, _ in ranked[:top_k]]
        evidence, value_columns = self._evidence(query, ranked, top) if expand else ([], {})
        fallback = not ranked or ranked[0][1] == 0
        dense_added = []
        if fallback:
            chosen, added = set(self.ddl), set()
        else:
            seeds = top + evidence
            added = _bridges(seeds, self._graph)
            chosen = set(seeds) | added
            if dense_tables is not None:
                candidates = dict.fromkeys(table for table in dense_tables if table in self.ddl and table not in chosen)
                dense_added = list(candidates)[:DENSE_TABLE_BUDGET]
                if dense_added:
                    added = _bridges(seeds + dense_added, self._graph)
                    chosen.update(dense_added)
                    chosen.update(added)
        ordered = [name for name in self.ddl if name in chosen]
        schema = "\n\n".join(self.ddl[name] for name in ordered)
        selection = {
            "top_tables": top,
            "tables": ordered,
            "bridge_tables": sorted(added),
            "evidence_tables": evidence,
            "value_matched_columns": {
                table: columns for table, columns in value_columns.items() if table in chosen
            },
            "total_tables": len(self.ddl),
            "full_schema_fallback": fallback,
        }
        if dense_tables is not None:
            selection.update(dense_ranked_tables=dense_tables, dense_added_tables=dense_added)
        return schema, selection

    def expand_for_identifier(self, selection: dict, query: str, kind: str, identifier: str) -> dict | None:
        """Resolve a missing object against this database's known tables and columns."""
        selected = set(selection["tables"])
        tables = {name.casefold(): name for name in self.ddl}
        raw = identifier.strip()
        qualifier, _, tail = raw.rpartition(".")
        name = tail.strip('"`[]').casefold()
        owner = tables.get(qualifier.strip('"`[]').casefold())

        if kind == "missing_table":
            table = tables.get(raw.casefold()) or tables.get(name)
            if table is None and len(name) >= 4:
                nearby = get_close_matches(name, tables, n=2, cutoff=0.82)
                if len(nearby) == 1:
                    table = tables[nearby[0]]
            candidates = {table} if table is not None else set()
        else:
            names = {name, raw.casefold()}
            matches = {
                table for table, columns in self.columns.items()
                if any(column.casefold() in names for column in columns)
            }
            if owner is not None:
                candidates = {owner} if owner in matches else set()
            elif matches & selected:
                candidates = set()
            else:
                candidates = matches

        if not (candidates - selected):
            return None
        rank = self._ranker.rank(query)
        additions = [table for table, _ in rank if table in candidates and table not in selected][:2]
        seeds = [*selection["tables"], *additions]
        bridges = _bridges(seeds, self._graph)
        scope = selected | set(additions) | bridges
        _, value_columns = self._evidence(query, rank, list(scope))
        return {
            **selection,
            "tables": [table for table in self.ddl if table in scope],
            "bridge_tables": sorted(set(selection["bridge_tables"]) | bridges),
            "recovery_tables": sorted(set(selection.get("recovery_tables", [])) | (scope - selected)),
            "value_matched_columns": {
                table: columns for table, columns in value_columns.items() if table in scope
            },
        }

    def coverage(self, selection: dict, reads: set[tuple[str, str]]) -> dict:
        tables = {table for table, _ in reads if table in self.ddl}
        columns = {(table, column) for table, column in reads if column and table in self.ddl}
        selected = set(selection["tables"])
        top = set(selection["top_tables"])

        def visible(column: tuple[str, str], scope: set[str]) -> bool:
            table, name = column
            return table in scope and name in self.columns[table]

        missing_columns = {column for column in columns if not visible(column, selected)}
        missing_keys = {
            (src, key, dst, other)
            for src, key, dst, other in self.foreign_keys
            if (src, key) in columns and (dst, other) in columns
            and ((src, key) in missing_columns or (dst, other) in missing_columns)
        }
        return {
            "reference_tables": sorted(tables),
            "reference_columns": sorted(columns),
            "top_table_hits": len(tables & top),
            "top_column_hits": sum(visible(column, top) for column in columns),
            "missing_tables": sorted(tables - selected),
            "missing_columns": sorted(missing_columns),
            "missing_declared_fk_pairs": sorted(missing_keys),
        }
