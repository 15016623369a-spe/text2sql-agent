import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.tools.db import ReadOnlyDB


class ReadOnlyDBTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "sample ? #.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("CREATE TABLE items(value TEXT); INSERT INTO items VALUES ('a;b'), ('c');")
        conn.close()

    def test_queries_and_complete_results(self):
        with ReadOnlyDB(self.path) as db:
            self.assertEqual(db.query("WITH data AS (SELECT value FROM items) SELECT * FROM data", limit=None),
                             (["value"], [("a;b",), ("c",)]))
            self.assertEqual(db.query("SELECT 'x;y'", limit=1)[1], [("x;y",)])
            self.assertEqual(db.query("SELECT value FROM items WHERE 0")[1], [])

    def test_blocks_write_attach_pragma_and_multiple_statements(self):
        with ReadOnlyDB(self.path) as db:
            for sql in ("DELETE FROM items", "ATTACH DATABASE ':memory:' AS aux",
                        "PRAGMA query_only = OFF", "SELECT load_extension('x')",
                        "SELECT 1; SELECT 2", "-- only a comment"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                    db.query(sql)
            self.assertEqual(db.query("SELECT COUNT(*) FROM items")[1], [(2,)])

    def test_interrupts_slow_query_and_clears_deadline(self):
        with ReadOnlyDB(self.path, timeout=0.05) as db:
            with self.assertRaises(TimeoutError):
                db.query(
                    "WITH RECURSIVE n(x) AS (VALUES (1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                    "SELECT SUM(x) FROM n"
                )
            self.assertEqual(db.query("SELECT 1")[1], [(1,)])
