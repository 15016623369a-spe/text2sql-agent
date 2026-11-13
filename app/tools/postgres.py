"""Read-only access to a deployer-configured PostgreSQL schema."""
from __future__ import annotations

import math
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from app.tools.db import ReadOnlyViolation, quote_identifier, result_row_bytes

POSTGRES_SOURCE_ID = "postgres"
MAX_POSTGRES_TABLES = 100
_MAX_SQL_BYTES = 256 * 1024
_MAX_ROW_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_COLUMNS = 100
_FETCH_BATCH = 32
_SAMPLE_ROWS = 8
_MAX_SAMPLE_VALUES = 3
_MAX_SAMPLE_TEXT = 80
_SSL_MODES = {"disable", "require", "verify-ca", "verify-full"}

# Approved functions resolve in pg_catalog. This excludes server metadata, file,
# session, lock, sequence and extension functions regardless of the reader grants.
_FUNCTIONS = frozenset("""
    abs acos age asin atan atan2 avg btrim cardinality cast ceil ceiling char_length
    character_length concat concat_ws corr cos count covar_pop covar_samp
    cume_dist date_bin date_part date_trunc dense_rank exp extract first_value
    floor greatest initcap lag last_value lead least left length ln log lower
    lpad ltrim make_date make_interval max min mod now nth_value ntile nullif
    octet_length overlay percent_rank percentile_cont percentile_disc pi
    position power rank regexp_count regexp_instr regexp_like regexp_match
    regexp_matches regexp_replace regexp_substr replace right round row_number
    rpad rtrim sign sin split_part sqrt stddev stddev_pop stddev_samp strpos
    substr substring sum tan to_char to_date to_number to_timestamp translate
    timezone trim trunc unnest upper variance var_pop var_samp width_bucket
""".split())
_CAST_TYPES = frozenset("""
    bit bool bpchar bytea date float4 float8 int2 int4 int8 interval json jsonb
    numeric text time timestamp timestamptz timetz uuid varbit varchar
""".split())
_DATE_VALUES = {
    "SVFOP_CURRENT_DATE", "SVFOP_CURRENT_TIME", "SVFOP_CURRENT_TIME_N",
    "SVFOP_CURRENT_TIMESTAMP", "SVFOP_CURRENT_TIMESTAMP_N", "SVFOP_LOCALTIME",
    "SVFOP_LOCALTIME_N", "SVFOP_LOCALTIMESTAMP", "SVFOP_LOCALTIMESTAMP_N",
}

_RELATION_CATALOG = """
SELECT relation.oid, relation.relname, left(obj_description(relation.oid, 'pg_class'), 1000)
FROM pg_class relation
JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = %s AND relation.relkind = 'r'
  AND relation.relpersistence <> 't' AND NOT relation.relrowsecurity
  AND NOT EXISTS (
      SELECT 1 FROM pg_inherits
      WHERE inhrelid = relation.oid OR inhparent = relation.oid
  )
  AND NOT EXISTS (
      SELECT 1 FROM pg_attribute attribute
      JOIN pg_type column_type ON column_type.oid = attribute.atttypid
      JOIN pg_namespace type_schema ON type_schema.oid = column_type.typnamespace
      WHERE attribute.attrelid = relation.oid AND attribute.attnum > 0
        AND NOT attribute.attisdropped AND (type_schema.nspname <> 'pg_catalog' OR column_type.typtype <> 'b')
  )
  AND has_table_privilege(relation.oid, 'SELECT')
  AND NOT has_table_privilege(relation.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
"""
_RELATIONS = _RELATION_CATALOG + "ORDER BY relation.relname LIMIT %s"
_LOCKED_RELATIONS = _RELATION_CATALOG + "AND relation.relname = ANY(%s::text[])"
_COLUMNS = """
SELECT attrelid, attname, format_type(atttypid, atttypmod),
       left(col_description(attrelid, attnum), 1000)
FROM pg_attribute
WHERE attrelid = ANY(%s::oid[]) AND attnum > 0 AND NOT attisdropped
ORDER BY attrelid, attnum
"""
_FOREIGN_KEYS = """
SELECT constraint_.conrelid, constraint_.conname, source.attname, target_table.relname, target.attname
FROM pg_constraint constraint_
JOIN pg_class target_table ON target_table.oid = constraint_.confrelid
CROSS JOIN LATERAL unnest(constraint_.conkey, constraint_.confkey) AS pair(source_num, target_num)
JOIN pg_attribute source ON source.attrelid = constraint_.conrelid AND source.attnum = pair.source_num
JOIN pg_attribute target ON target.attrelid = constraint_.confrelid AND target.attnum = pair.target_num
WHERE constraint_.contype = 'f'
  AND constraint_.conrelid = ANY(%s::oid[]) AND constraint_.confrelid = ANY(%s::oid[])
ORDER BY constraint_.conrelid, constraint_.conname, source.attnum
"""
_ROLE = """
SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,
       has_schema_privilege(namespace.oid, 'USAGE'),
       has_schema_privilege(namespace.oid, 'CREATE')
FROM pg_roles
JOIN pg_namespace namespace ON namespace.nspname = %s
WHERE rolname = CURRENT_USER
"""


@dataclass(frozen=True)
class PostgresSource:
    host: str = field(repr=False)
    port: int
    database: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)
    schema: str
    sslmode: str = "verify-full"
    sslrootcert: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> PostgresSource | None:
        host = os.environ.get("RAG_POSTGRES_HOST", "").strip()
        if not host:
            return None
        try:
            port = int(os.environ.get("RAG_POSTGRES_PORT", "5432"))
        except ValueError:
            raise ValueError("RAG_POSTGRES_PORT must be a valid TCP port") from None
        database = os.environ.get("RAG_POSTGRES_DATABASE", "").strip()
        user = os.environ.get("RAG_POSTGRES_USER", "").strip()
        password = os.environ.get("RAG_POSTGRES_PASSWORD", "")
        schema = os.environ.get("RAG_POSTGRES_SCHEMA", "").strip()
        sslmode = os.environ.get("RAG_POSTGRES_SSLMODE", "verify-full").strip()
        sslrootcert = os.environ.get("RAG_POSTGRES_SSLROOTCERT", "").strip() or None
        if not 1 <= port <= 65535:
            raise ValueError("RAG_POSTGRES_PORT must be a valid TCP port")
        if not database or not user or not schema:
            raise ValueError("Set RAG_POSTGRES_DATABASE, RAG_POSTGRES_USER and RAG_POSTGRES_SCHEMA")
        if sslmode not in _SSL_MODES:
            raise ValueError("RAG_POSTGRES_SSLMODE must be disable, require, verify-ca or verify-full")
        if os.environ.get("RAG_DEPLOYMENT") == "production":
            if sslmode != "verify-full":
                raise ValueError("Production PostgreSQL requires RAG_POSTGRES_SSLMODE=verify-full")
            if any(not address or address.startswith(("/", "@")) for address in map(str.strip, host.split(","))):
                raise ValueError("Production PostgreSQL requires a TCP host")
        if schema in {"information_schema", "pg_catalog"} or schema.startswith("pg_"):
            raise ValueError("RAG_POSTGRES_SCHEMA must name an application schema")
        return cls(host, port, database, user, password, schema, sslmode, sslrootcert)


class PostgresQueryError(sqlite3.DatabaseError):
    """A PostgreSQL error with public-safe metadata."""

    def __init__(self, message: str, sqlstate: str | None = None, identifier: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.identifier = identifier


class PostgresUnavailable(PostgresQueryError):
    """The configured PostgreSQL source could not be opened."""


class PostgresScopeViolation(ReadOnlyViolation):
    """A relation is outside the configured schema or selected tables."""


def _safe_name(names) -> str | None:
    parts = tuple(item.sval for item in names or ())
    return parts[-1] if parts and (len(parts) == 1 or parts[:-1] == ("pg_catalog",)) else None


def _prepare_sql(sql: str, schema: str, tables: frozenset[str]) -> tuple[str, set[str]]:
    from pglast import ast, parse_sql
    from pglast.parser import ParseError
    from pglast.stream import RawStream

    if len(sql.encode("utf-8")) > _MAX_SQL_BYTES:
        raise ReadOnlyViolation("Query exceeds the SQL size limit")
    try:
        statements = parse_sql(sql)
    except (ParseError, ValueError):
        raise PostgresQueryError("PostgreSQL syntax error", "42601") from None
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
        raise ReadOnlyViolation("Expected one read-only SELECT")
    relations: set[str] = set()

    def visit(node, ctes: frozenset[str]) -> None:
        if isinstance(node, tuple):
            for child in node:
                visit(child, ctes)
            return
        if not isinstance(node, ast.Node):
            return
        if isinstance(node, ast.SelectStmt):
            if node.intoClause or node.lockingClause:
                raise ReadOnlyViolation("SELECT INTO and row locking are disallowed")
            if clause := node.withClause:
                if clause.recursive:
                    ctes = ctes.union(item.ctename for item in clause.ctes)
                for item in clause.ctes:
                    if not isinstance(item.ctequery, ast.SelectStmt):
                        raise ReadOnlyViolation("CTEs must contain read-only SELECTs")
                    visit(item.ctequery, ctes)
                    ctes = ctes.union((item.ctename,))
        elif isinstance(node, ast.RangeVar):
            if node.catalogname or node.schemaname not in (None, schema):
                raise PostgresScopeViolation("Table is outside the configured PostgreSQL schema")
            if node.schemaname is None and node.relname in ctes:
                return
            if node.relname not in tables:
                raise PostgresScopeViolation("Table is outside the selected PostgreSQL tables")
            # Keep the server search_path on pg_catalog. Only validated table
            # references are explicitly qualified into the application schema.
            node.schemaname = schema
            node.inh = False
            relations.add(node.relname)
        elif isinstance(node, ast.FuncCall) and _safe_name(node.funcname) not in _FUNCTIONS:
            raise ReadOnlyViolation("Function is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.TypeName) and _safe_name(node.names) not in _CAST_TYPES:
            raise ReadOnlyViolation("Type cast is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.A_Expr) and node.name and _safe_name(node.name) is None:
            raise ReadOnlyViolation("Operator is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.SortBy) and node.useOp and _safe_name(node.useOp) is None:
            raise ReadOnlyViolation("Sort operator is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.SubLink) and node.operName and _safe_name(node.operName) is None:
            raise ReadOnlyViolation("Subquery operator is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.CollateClause) and _safe_name(node.collname) is None:
            raise ReadOnlyViolation("Collation is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, ast.SQLValueFunction) and node.op.name not in _DATE_VALUES:
            raise ReadOnlyViolation("Session metadata is outside the approved PostgreSQL SQL subset")
        elif isinstance(node, (ast.RangeTableSample, ast.RangeTableFunc, ast.XmlExpr, ast.ParamRef)):
            raise ReadOnlyViolation("Expression is outside the approved PostgreSQL SQL subset")
        for attribute in node:
            if attribute != "withClause":
                visit(getattr(node, attribute), ctes)

    try:
        statement = statements[0].stmt
        visit(statement, frozenset())
        return RawStream()(statement), relations
    except RecursionError:
        raise ReadOnlyViolation("Query exceeds the SQL nesting limit") from None


def _raise_query_error(error) -> None:
    sqlstate = error.sqlstate
    if sqlstate in {"57014", "55P03", "25P04"}:
        raise TimeoutError("PostgreSQL query exceeded its deadline") from None
    if sqlstate in {"42501", "25006"}:
        raise ReadOnlyViolation("PostgreSQL reader permissions rejected the query") from None
    if sqlstate is None or sqlstate.startswith("08"):
        raise PostgresUnavailable("PostgreSQL source is unavailable", sqlstate) from None
    labels = {
        "42601": "PostgreSQL syntax error", "42P01": "PostgreSQL table is undefined",
        "42703": "PostgreSQL column is undefined", "42702": "PostgreSQL column reference is ambiguous",
        "42883": "PostgreSQL function or operator does not match the provided types",
        "42803": "PostgreSQL grouping is invalid", "42804": "PostgreSQL expression types do not match",
    }
    identifier = error.diag.column_name if sqlstate == "42703" else error.diag.table_name if sqlstate == "42P01" else None
    raise PostgresQueryError(labels.get(sqlstate, "PostgreSQL could not execute the query"), sqlstate, identifier) from None


class PostgresDB:
    """Use one short read-only connection for catalog, dry-run, or result streaming."""

    def __init__(self, source: PostgresSource, *, tables: tuple[str, ...] | None = None, timeout: float = 5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Query timeout must be finite and positive")
        if tables is not None and (not tables or len(set(tables)) != len(tables)):
            raise PostgresScopeViolation("Choose a nonempty set of distinct PostgreSQL tables")
        self._source = source
        self._tables = tables
        self._timeout = timeout
        self._conn = None
        self._relations: dict[str, tuple[int, str | None]] = {}

    def __enter__(self) -> PostgresDB:
        try:
            import psycopg
        except ImportError:
            raise PostgresUnavailable("PostgreSQL driver is unavailable") from None

        self._driver = psycopg
        source = self._source
        tls = {}
        if source.sslrootcert:
            tls["sslrootcert"] = source.sslrootcert
        elif source.sslmode == "verify-full":
            tls["sslrootcert"] = "system"
        try:
            conn = psycopg.connect(
                host=source.host, port=source.port, dbname=source.database, user=source.user,
                password=source.password, sslmode=source.sslmode, connect_timeout=3,
                gssencmode="disable",
                options="-c search_path=pg_catalog -c standard_conforming_strings=on",
                **tls,
            )
        except psycopg.Error as exc:
            raise PostgresUnavailable("PostgreSQL source is unavailable", exc.sqlstate) from None
        self._conn = conn
        try:
            if source.sslmode != "disable" and not conn.pgconn.ssl_in_use:
                raise PostgresUnavailable("The configured PostgreSQL source did not establish TLS")
            conn.read_only = True
            conn.execute("SET LOCAL search_path TO pg_catalog")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout TO '30s'")
            deadline = time.monotonic() + self._timeout
            self._arm_timeout(deadline)
            with conn.cursor() as cursor:
                cursor.execute(_ROLE, (source.schema,))
                role = cursor.fetchone()
                if role != (False, True, False):
                    raise PostgresUnavailable("PostgreSQL requires a restricted reader role and application schema")
                self._arm_timeout(deadline)
                cursor.execute(_RELATIONS, (source.schema, MAX_POSTGRES_TABLES + 1))
                relations = {name: (oid, comment) for oid, name, comment in cursor}
            if len(relations) > MAX_POSTGRES_TABLES:
                raise PostgresUnavailable("Configured PostgreSQL schema exceeds the catalog table limit")
            if self._tables is not None and not relations.keys() >= set(self._tables):
                raise PostgresScopeViolation("A selected table is outside the configured PostgreSQL catalog")
            self._relations = relations if self._tables is None else {name: relations[name] for name in self._tables}
        except psycopg.Error as exc:
            self.__exit__()
            _raise_query_error(exc)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._relations.clear()

    def _connection(self):
        if self._conn is None:
            raise RuntimeError("Open the PostgreSQL source first")
        return self._conn

    def _arm_timeout(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("PostgreSQL query exceeded its deadline")
        timeout = str(max(1, math.ceil(remaining * 1000)))
        with self._connection().cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (timeout,))

    def _qualified_table(self, name: str) -> str:
        return f"{quote_identifier(self._source.schema)}.{quote_identifier(name)}"

    def _lock_tables(self, names: set[str], deadline: float) -> None:
        if not names:
            return
        # Prevent the catalog check and execution from straddling a replacement
        # of an approved table by a different relation with the same name.
        qualified = ", ".join(f"ONLY {self._qualified_table(name)}" for name in sorted(names))
        with self._connection().cursor() as cursor:
            self._arm_timeout(deadline)
            cursor.execute(f"LOCK TABLE {qualified} IN ACCESS SHARE MODE")
            self._arm_timeout(deadline)
            cursor.execute(_LOCKED_RELATIONS, (self._source.schema, sorted(names)))
            locked = {name: oid for oid, name, _ in cursor}
        if locked != {name: self._relations[name][0] for name in names}:
            raise PostgresScopeViolation("The PostgreSQL table changed after catalog validation")

    def _validated(self, sql: str) -> tuple[str, set[str]]:
        try:
            return _prepare_sql(sql, self._source.schema, frozenset(self._relations))
        except ImportError:
            raise PostgresUnavailable("PostgreSQL SQL parser is unavailable") from None

    def dry_run(self, sql: str) -> None:
        deadline = time.monotonic() + self._timeout
        validated, relations = self._validated(sql)
        conn = self._connection()
        try:
            self._lock_tables(relations, deadline)
            self._arm_timeout(deadline)
            with conn.cursor() as cursor:
                cursor.execute("EXPLAIN (COSTS OFF) " + validated)
        except self._driver.Error as exc:
            _raise_query_error(exc)

    @contextmanager
    def scan(self, sql: str, *, max_columns: int | None = None):
        """Stream typed result batches under a single execution deadline."""
        deadline = time.monotonic() + self._timeout
        validated, relations = self._validated(sql)
        conn = self._connection()
        try:
            self._lock_tables(relations, deadline)
            self._arm_timeout(deadline)
            with conn.cursor(name=f"rag_{uuid4().hex}") as cursor:
                cursor.execute(validated)
                columns = [column.name for column in cursor.description]
                if max_columns is not None and len(columns) > max_columns:
                    raise ReadOnlyViolation("Query returned too many columns")

                def rows():
                    while True:
                        self._arm_timeout(deadline)
                        try:
                            batch = cursor.fetchmany(_FETCH_BATCH)
                        except (ValueError, RecursionError):
                            raise ReadOnlyViolation("Query returned an unsupported result value") from None
                        if not batch:
                            break
                        for row in batch:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("PostgreSQL query exceeded its deadline")
                            if result_row_bytes(row) > _MAX_ROW_BYTES:
                                raise ReadOnlyViolation("Query row exceeds the size limit")
                            yield row

                yield columns, rows()
        except self._driver.Error as exc:
            _raise_query_error(exc)

    def query(
        self, sql: str, limit: int | None = 20, *,
        max_columns: int | None = None, max_result_bytes: int | None = None,
    ) -> tuple[list[str], list[tuple]]:
        with self.scan(sql, max_columns=max_columns) as (columns, rows):
            values = []
            size = 0
            while limit is None or len(values) < limit:
                row = next(rows, None)
                if row is None:
                    break
                if max_result_bytes is not None:
                    size += result_row_bytes(row)
                    if size > max_result_bytes:
                        raise ReadOnlyViolation("Query result exceeds the preview size limit")
                values.append(row)
            return columns, values

    def catalog(self, *, include_samples: bool = False) -> list[dict]:
        """Read schema, column comments and relationships, with opt-in value previews."""
        conn = self._connection()
        if not self._relations:
            return []
        deadline = time.monotonic() + self._timeout
        tables = {
            oid: {"name": name, "schema": self._source.schema, "comment": comment,
                  "columns": [], "foreign_keys": []}
            for name, (oid, comment) in self._relations.items()
        }
        oids = list(tables)
        try:
            with conn.cursor() as cursor:
                self._arm_timeout(deadline)
                cursor.execute(_COLUMNS, (oids,))
                for oid, name, column_type, comment in cursor:
                    columns = tables[oid]["columns"]
                    if len(columns) >= _MAX_CATALOG_COLUMNS:
                        raise PostgresUnavailable("PostgreSQL table exceeds the catalog column limit")
                    columns.append({"name": name, "type": column_type, "comment": comment, "samples": []})
                self._arm_timeout(deadline)
                cursor.execute(_FOREIGN_KEYS, (oids, oids))
                for oid, constraint, name, target_table, target_column in cursor:
                    tables[oid]["foreign_keys"].append({
                        "constraint": constraint, "column": name,
                        "target_table": target_table, "target_column": target_column,
                    })
                if include_samples:
                    self._lock_tables(set(self._relations), deadline)
                    for table in tables.values():
                        self._sample_values(cursor, table, deadline)
        except self._driver.Error as exc:
            _raise_query_error(exc)
        return list(tables.values())

    def _sample_values(self, cursor, table: dict, deadline: float) -> None:
        columns = table["columns"]
        if not columns:
            return
        projection = ", ".join(f"left({quote_identifier(col['name'])}::text, {_MAX_SAMPLE_TEXT})" for col in columns)
        name = self._qualified_table(table["name"])
        self._arm_timeout(deadline)
        cursor.execute(f"SELECT {projection} FROM ONLY {name} LIMIT {_SAMPLE_ROWS}")
        for row in cursor:
            for col, value in zip(columns, row, strict=True):
                samples = col["samples"]
                if value is not None and len(samples) < _MAX_SAMPLE_VALUES and value not in samples:
                    samples.append(value)
