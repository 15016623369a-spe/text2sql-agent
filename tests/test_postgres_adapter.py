"""Regression checks for the PostgreSQL policy and database configuration."""
import os
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from app.tools.db import ReadOnlyViolation, result_row_bytes
from app.tools.postgres import (
    PostgresDB, PostgresQueryError, PostgresScopeViolation, PostgresSource, PostgresUnavailable, _prepare_sql,
)


class PostgresConfigurationTests(unittest.TestCase):
    def test_source_is_opt_in_and_keeps_credentials_out_of_repr(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(PostgresSource.from_env())
        values = {
            "RAG_POSTGRES_HOST": "hidden-host",
            "RAG_POSTGRES_DATABASE": "hidden-database",
            "RAG_POSTGRES_USER": "hidden-user",
            "RAG_POSTGRES_PASSWORD": "hidden-password",
            "RAG_POSTGRES_SCHEMA": "retail",
            "RAG_POSTGRES_SSLROOTCERT": "/hidden-ca",
        }
        with patch.dict(os.environ, values, clear=True):
            source = PostgresSource.from_env()
        self.assertEqual((source.port, source.schema, source.sslmode, source.sslrootcert),
                         (5432, "retail", "verify-full", "/hidden-ca"))
        self.assertNotIn("hidden", repr(source))
        for tables in ((), ("orders", "orders")):
            with self.subTest(tables=tables), self.assertRaises(PostgresScopeViolation):
                PostgresDB(source, tables=tables)

    def test_production_only_accepts_verify_full_for_configured_sources(self):
        values = {
            "RAG_DEPLOYMENT": "production", "RAG_POSTGRES_HOST": "database.example",
            "RAG_POSTGRES_DATABASE": "warehouse", "RAG_POSTGRES_USER": "reader", "RAG_POSTGRES_SCHEMA": "retail",
        }
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(PostgresSource.from_env().sslmode, "verify-full")
            for mode in ("disable", "require", "verify-ca"):
                with self.subTest(mode=mode), patch.dict(os.environ, {"RAG_POSTGRES_SSLMODE": mode}):
                    with self.assertRaisesRegex(ValueError, "Production PostgreSQL requires .*verify-full"):
                        PostgresSource.from_env()
            for host in ("/tmp/socket", "@socket", "database.example,"):
                with self.subTest(host=host), patch.dict(os.environ, {"RAG_POSTGRES_HOST": host}):
                    with self.assertRaisesRegex(ValueError, "Production PostgreSQL requires a TCP host"):
                        PostgresSource.from_env()
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production"}, clear=True):
            self.assertIsNone(PostgresSource.from_env())

    def test_verifying_mode_closes_connection_when_tls_is_absent(self):
        source = PostgresSource("/socket", 5432, "warehouse", "reader", "", "retail")
        connection = Mock()
        connection.pgconn.ssl_in_use = False
        with patch("psycopg.connect", return_value=connection) as connect:
            with self.assertRaisesRegex(PostgresUnavailable, "did not establish TLS"):
                with PostgresDB(source):
                    self.fail("The adapter opened an unencrypted connection")
        self.assertEqual(connect.call_args.kwargs["gssencmode"], "disable")
        connection.close.assert_called_once_with()
        connection.execute.assert_not_called()

    def test_nested_values_count_against_shared_preview_size(self):
        row = (["a" * 100, {"key": [Decimal("12.34"), "b" * 500]}],)
        self.assertGreater(result_row_bytes(row), 2_400)
        self.assertEqual(result_row_bytes((42, "test", b"ab")), 34)


class PostgresStatementTests(unittest.TestCase):
    def prepare(self, statement):
        return _prepare_sql(statement, "retail", frozenset({"orders", "Order Items"}))

    def test_rewrites_approved_tables_and_preserves_ctes_and_quoted_identifiers(self):
        sql, relations = self.prepare(
            'WITH item AS (SELECT order_id FROM "Order Items") '
            'SELECT COUNT(*) FROM item JOIN retail.orders AS o ON item.order_id = o.id'
        )
        self.assertIn('ONLY retail."Order Items"', sql)
        self.assertIn('ONLY retail.orders AS o', sql)
        self.assertIn("FROM item", sql)
        self.assertEqual(relations, {"orders", "Order Items"})

        sql, relations = self.prepare(
            "WITH RECURSIVE steps(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM steps WHERE n < 3) "
            "SELECT SUM(n) FROM steps"
        )
        self.assertIn("FROM steps", sql)
        self.assertEqual(relations, set())

    def test_disallows_other_schema_and_selected_table_escape_in_nested_queries(self):
        for query in (
            "SELECT * FROM pg_catalog.pg_class",
            "SELECT * FROM other.orders",
            "SELECT * FROM test.retail.orders",
            "SELECT * FROM customers",
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM customers)",
            "SELECT * FROM customers, (WITH customers AS (SELECT 1) SELECT * FROM customers) AS allowed",
            "WITH x AS (WITH customers AS (SELECT 1) SELECT * FROM customers) SELECT * FROM customers",
            "WITH x AS (SELECT * FROM customers), customers AS (SELECT 1) SELECT * FROM x",
            "WITH customers AS (SELECT 1) SELECT * FROM retail.customers",
            "SELECT * FROM orders UNION ALL SELECT * FROM customers",
        ):
            with self.subTest(query=query), self.assertRaises(PostgresScopeViolation):
                self.prepare(query)

    def test_disallows_mutation_multi_statement_locking_and_side_effect_functions(self):
        for query in (
            "DELETE FROM orders",
            "SELECT 1; DELETE FROM orders",
            "SELECT * INTO other_table FROM orders",
            "SELECT * FROM (SELECT * FROM orders FOR UPDATE) AS locked",
            "WITH x AS (UPDATE orders SET id=1 RETURNING *) SELECT * FROM x",
            "SELECT pg_catalog.set_config('search_path', 'public', false)",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT nextval('some_sequence')",
            "SELECT pg_advisory_lock(42)",
            "SELECT pg_sleep(2)",
            "SELECT retail.count(*) FROM orders",
            "SELECT 1 OPERATOR(retail.+) 2",
            "SELECT 1 ORDER BY 1 USING OPERATOR(retail.<)",
            "SELECT 1 OPERATOR(retail.=) ANY (SELECT 1)",
            "SELECT 'private'::regclass",
            "SELECT current_user",
        ):
            with self.subTest(query=query), self.assertRaises(ReadOnlyViolation):
                self.prepare(query)

    def test_statement_literals_remain_data_and_syntax_diagnostic_is_sanitized(self):
        query, relations = self.prepare("SELECT 'SELECT 1; DROP TABLE orders' AS example FROM orders; -- ignored")
        self.assertIn("'SELECT 1; DROP TABLE orders'", query)
        self.assertEqual(relations, {"orders"})
        with self.assertRaises(PostgresQueryError) as error:
            self.prepare("SELECT * FROM sensitive_secret WHERE (")
        self.assertEqual(error.exception.sqlstate, "42601")
        self.assertNotIn("sensitive_secret", str(error.exception))
