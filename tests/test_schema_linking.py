import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.schema_linking import SchemaIndex, _BM25
from app.text2sql import schema_objects
from app.tools.db import ReadOnlyDB


class SchemaLinkingTests(unittest.TestCase):
    def test_bm25_matches_identifier_words(self):
        ranker = _BM25({"items": "deliveryDate status", "people": "email addresses"})
        self.assertEqual(ranker.rank("delivery dates")[0][0], "items")
        self.assertEqual(ranker.rank("unmatched"), [("items", 0), ("people", 0)])

    def test_bridges_and_reference_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    "CREATE TABLE buyers(id INTEGER PRIMARY KEY, nickname TEXT);"
                    "CREATE TABLE products(id INTEGER PRIMARY KEY, serial TEXT);"
                    "CREATE TABLE lines(buyer INTEGER REFERENCES buyers(id), product INTEGER REFERENCES products(id));"
                )
            with ReadOnlyDB(path) as db:
                index = SchemaIndex(path, schema_objects(db))
                schema, selection = index.select("nicknames and serials", 2)
                self.assertEqual(set(selection["top_tables"]), {"buyers", "products"})
                self.assertEqual(selection["bridge_tables"], ["lines"])
                self.assertIn("CREATE TABLE lines", schema)
                sql = (
                    "SELECT b.nickname, p.serial FROM buyers b JOIN lines l ON l.buyer=b.id "
                    "JOIN products p ON l.product=p.id"
                )
                coverage = index.coverage(selection, db.references(sql))
                self.assertEqual(coverage["reference_tables"], ["buyers", "lines", "products"])
                self.assertIn(("lines", "product"), coverage["reference_columns"])
                self.assertEqual(coverage["missing_columns"], [])
                self.assertLess(coverage["top_column_hits"], len(coverage["reference_columns"]))
                self.assertTrue(index.select("unmatched", 1)[1]["full_schema_fallback"])
                with self.assertRaises(sqlite3.DatabaseError):
                    db.query("PRAGMA query_only=OFF")


    def test_named_and_lookup_tables_extend_top_k_and_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    "CREATE TABLE areas(id INTEGER PRIMARY KEY, country TEXT);"
                    "CREATE TABLE events(id INTEGER PRIMARY KEY, area INTEGER REFERENCES areas(id));"
                    "CREATE TABLE splits(event INTEGER REFERENCES events(id), lapTime TEXT);"
                    "CREATE TABLE groups(id INTEGER PRIMARY KEY, name TEXT);"
                    "CREATE TABLE members(id INTEGER, group_id INTEGER REFERENCES groups(id));"
                    "INSERT INTO areas VALUES(1, 'Italy');"
                    "INSERT INTO groups VALUES(1, 'Female');"
                )
            with ReadOnlyDB(path) as db:
                index = SchemaIndex(path, schema_objects(db))
                schema, selection = index.select("lap time in Italy for male participants", 1)
                legacy = index.select("lap time in Italy for male participants", 1, expand=False)[1]
                self.assertEqual(legacy["tables"], ["splits"])
                self.assertEqual(selection["top_tables"], ["splits"])
                self.assertEqual(selection["evidence_tables"], ["areas"])
                self.assertEqual(selection["bridge_tables"], ["events"])
                self.assertEqual(selection["value_matched_columns"], {"areas": ["country"]})
                self.assertNotIn("Italy", schema)
                self.assertIn("areas", index.select("lap time by area", 1)[1]["tables"])
