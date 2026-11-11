import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.dense_schema import best_tables, index_name, schema_documents
from app.schema_linking import SchemaIndex
from app.text2sql import schema_objects
from app.tools.db import ReadOnlyDB


class DenseSchemaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "example.sqlite"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE notes(id INTEGER PRIMARY KEY, memo TEXT);
                CREATE TABLE regions(id INTEGER PRIMARY KEY, code TEXT);
                CREATE TABLE mapping(note_id INTEGER REFERENCES notes(id), region_id INTEGER REFERENCES regions(id));
                CREATE TABLE schedules(id INTEGER PRIMARY KEY, starts TEXT);
                CREATE TABLE invoices(id INTEGER PRIMARY KEY, due TEXT);
                INSERT INTO notes VALUES (1, 'should not enter the vector index');
            """)
        with ReadOnlyDB(self.path) as db:
            self.index = SchemaIndex(self.path, schema_objects(db))

    def test_documents_cover_tables_and_columns_and_reindex_metadata_changes(self):
        descriptions = self.path.parent / "database_description"
        descriptions.mkdir()
        csv = descriptions / "regions.csv"
        csv.write_text("original_column_name,column_name,column_description,value_description\n"
                       "code,Region code,Administrative district identifier,Three uppercase letters\n")
        docs = schema_documents(self.path, self.index)
        self.assertEqual(len(docs), len(self.index.ddl) + sum(map(len, self.index.columns.values())))
        region_code = next(doc for doc in docs if doc.table == "regions" and doc.column == "code")
        self.assertIn("Administrative district identifier", region_code.text)
        self.assertIn("Three uppercase letters", region_code.text)
        self.assertEqual(region_code.metadata["kind"], "column")
        self.assertNotIn("should not enter", "\n".join(doc.text for doc in docs))
        self.assertEqual(index_name(docs), index_name(schema_documents(self.path, self.index)))
        csv.write_text(csv.read_text().replace("Three", "Four"))
        self.assertNotEqual(index_name(docs), index_name(schema_documents(self.path, self.index)))

    def test_cosine_hits_deduplicate_and_keep_best_distance(self):
        ranking = best_tables([
            {"metadatas": [[{"table": "A"}, {"table": "B"}]], "distances": [[0.4, 0.2]]},
            {"metadatas": [[{"table": "A"}, {"table": "C"}]], "distances": [[0.1, 0.2]]},
        ], 2)
        self.assertEqual(ranking, ["A", "B"])

    def test_dense_additions_are_optional_bounded_and_keep_the_foreign_key_path(self):
        legacy_schema, legacy = self.index.select("memo", 1)
        schema, selection = self.index.select(
            "memo", 1, dense_tables=["unknown", "notes", "regions", "regions", "schedules", "invoices"],
        )
        self.assertEqual(legacy["tables"], ["notes"])
        self.assertNotIn("dense_added_tables", legacy)
        self.assertEqual(selection["top_tables"], legacy["top_tables"])
        self.assertEqual(selection["dense_added_tables"], ["regions", "schedules"])
        self.assertEqual(selection["bridge_tables"], ["mapping"])
        self.assertIn(legacy_schema, schema)
        self.assertNotIn("invoices", schema)
        self.assertEqual(self.index.select("memo", 1)[1], legacy)
