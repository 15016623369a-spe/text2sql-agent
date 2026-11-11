import tempfile
import unittest
from pathlib import Path

from app.column_notes import ColumnNote, load_column_notes, render_column_notes


class ColumnNotesTests(unittest.TestCase):
    def test_csv_alias_and_same_text_for_two_columns(self):
        content = (
            "original_column_name,column_name,column_description,data_format,value_description\n"
            " x ,Résumé, ,text,0 means missing\n"
            "y,Résumé,,text,0 means missing\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fields.csv"
            for encoding in ("utf-8-sig", "latin-1"):
                with self.subTest(encoding=encoding):
                    path.write_bytes(content.encode(encoding))
                    notes = load_column_notes(path)
                    self.assertEqual(notes["x"], ColumnNote("Résumé", "0 means missing"))
                    self.assertEqual(notes["y"], notes["x"])

    def test_verified_precedence_and_prompt_budget(self):
        cols = {"schools": {"CDSCode", "unused"}}
        metadata = {
            "schools": {"cdscode": ColumnNote("School identifier", "integer")},
            "private_table": {"code": ColumnNote("School code", "not selected")},
        }
        verified = {
            ("schools", "CDSCode"): ("Digits stored as text with leading zeroes.", "school code"),
            ("schools", "unused"): ("An unrelated blob.", "blob"),
        }
        result = render_column_notes(cols, metadata, "school code", verified, max_notes=1, max_chars=130)
        self.assertEqual(len(result), 1)
        self.assertIn('"schools"."CDSCode"', result[0])
        self.assertIn("leading zeroes", result[0])
        self.assertNotIn("integer", result[0])
        self.assertNotIn("private_table", result[0])
        self.assertLessEqual(len(result[0]), 130)
        self.assertEqual(render_column_notes(cols, metadata, "school", verified, max_notes=1, max_chars=5), [])
