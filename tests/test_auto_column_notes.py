import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.auto_column_notes import MAX_CHARS, AutoColumnNotes, rank_fields, summarize_storage
from app.column_notes import ColumnNote


class AutoColumnNotesTests(unittest.TestCase):
    def test_storage_facts_are_bounded_and_do_not_guess_zero_semantics(self):
        self.assertEqual(summarize_storage(["202301", "202312"], temporal=True),
                         ["Sampled months are text in YYYYMM format"])
        self.assertNotIn("YYYYMM", str(summarize_storage(["202313"], temporal=True)))
        self.assertIn("leading zeros", str(summarize_storage(["0012", "123"], temporal=False)))
        self.assertIn("comparison prefixes", str(summarize_storage([">=4", "1", "TR"], temporal=False)))
        self.assertIn("whitespace-only", str(summarize_storage([None, "", " ", 0], temporal=False)))
        self.assertEqual(summarize_storage([0, 2], temporal=False), [])
        self.assertEqual(summarize_storage([None], temporal=False), ["All sampled values are NULL"])
        self.assertEqual(summarize_storage(["0" * 97], temporal=False), [])

    def test_read_only_profile_limits_notes_to_relevant_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE "race data" ("lap""Time" TEXT, status TEXT, email TEXT)')
                conn.execute('INSERT INTO "race data" VALUES (?, ?, ?)', ("1.2", "Legal", "private@example.org"))
            notes = AutoColumnNotes(path, {"race data": {
                'lap"time': ColumnNote("Lap time in seconds", ""),
                "status": ColumnNote("Legality status", "• legal • banned"),
                "email": ColumnNote("Address", ""),
            }})
            selected = {"race data": {'lap"Time', "status", "email"}}
            lap_notes = notes.render(selected, "lap time")
            self.assertEqual(len(lap_notes), 1)
            self.assertIn("numeric values are stored as text", lap_notes[0])
            self.assertNotIn("1.2", lap_notes[0])
            self.assertNotIn("private@example.org", str(lap_notes))
            self.assertIn('"Legal"', str(notes.render(selected, "legality status")))
            self.assertLessEqual(len("\n".join(lap_notes)), MAX_CHARS)
            self.assertEqual(notes.render(selected, "race"), [])

    def test_broad_overlap_does_not_outweigh_specific_field(self):
        fields = {
            ("qualifying", "q3"): "time in qualifying 3",
            ("laps", "lapTime"): "recorded lap time",
            ("laps", "milliseconds"): "time in milliseconds",
        }
        ranked = rank_fields(fields, "fastest recorded lap time in milliseconds")
        self.assertEqual(set(ranked), {("laps", "lapTime"), ("laps", "milliseconds")})

    def test_opt_in_storage_hints_preserve_case_and_detect_encoded_amounts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE facts(status TEXT, reading TEXT, email TEXT)")
                conn.executemany("INSERT INTO facts VALUES (?, ?, ?)", [
                    ("Legal" if i % 2 else "Restricted", "3+ (300)", "private@example.org") for i in range(16)
                ])
            selected = {"facts": {"status", "reading", "email"}}
            notes = AutoColumnNotes(path, {}, value_hints=True)
            result = "\n".join(notes.render(selected, "legal status and reading and email"))
            self.assertIn('"Legal"', result)
            self.assertIn("amounts inside parentheses", result)
            self.assertNotIn("private@example.org", result)
            self.assertNotIn('"Legal"', "\n".join(AutoColumnNotes(path, {}).render(selected, "status")))

    def test_sparse_role_label_resolves_to_populated_foreign_key_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE people(id INTEGER PRIMARY KEY, DisplayName TEXT);
                    CREATE TABLE posts(id INTEGER, EditorUserId INTEGER REFERENCES people(id), EditorDisplayName TEXT);
                    CREATE TABLE comments(id INTEGER, UserId INTEGER REFERENCES people(id), UserDisplayName TEXT);
                    INSERT INTO people VALUES(1, 'example');
                    INSERT INTO posts VALUES(1, 1, NULL), (2, 1, '  ');
                    INSERT INTO comments VALUES(1, 1, NULL);
                """)
            notes = AutoColumnNotes(path, {"people": {
                "displayname": ColumnNote("Display name of a user", ""),
            }})
            selected = {
                "people": {"id", "DisplayName"},
                "posts": {"id", "EditorUserId", "EditorDisplayName"},
                "comments": {"id", "UserId", "UserDisplayName"},
            }
            keys = {("posts", "EditorUserId", "people", "id"), ("comments", "UserId", "people", "id")}
            lines = notes.render(selected, "What is the display name of the editor user?", foreign_keys=keys)
            rendered = "\n".join(lines)
            self.assertIn('"posts"."EditorUserId" → "people"."id"', lines[0])
            self.assertIn('"people"."DisplayName"', lines[0])
            self.assertNotIn('"posts"."EditorDisplayName"', rendered)
            self.assertNotIn('"comments"."UserId" →', rendered)
            missing = notes.render(selected, "Which editor display names are missing?", foreign_keys=keys)
            self.assertIn('"posts"."EditorDisplayName"', "\n".join(missing))

    def test_average_counts_use_the_eligible_parent_and_keep_zero_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE musicians(id INTEGER PRIMARY KEY);
                    CREATE TABLE shows(id INTEGER PRIMARY KEY);
                    CREATE TABLE registrations(musician_id INTEGER REFERENCES musicians(id),
                                               show_id INTEGER REFERENCES shows(id));
                    CREATE TABLE musician_awards(musician_id INTEGER REFERENCES musicians(id), award TEXT);
                    INSERT INTO musicians VALUES(1), (2);
                    INSERT INTO shows VALUES(1);
                    INSERT INTO registrations VALUES(1, 1);
                    INSERT INTO musician_awards VALUES(1, 'award');
                """)
            notes = AutoColumnNotes(path, {"musician_awards": {
                "musician_id": ColumnNote("musician taking part", ""),
            }})
            selected = {"musicians": {"id"}, "shows": {"id"}, "registrations": {"musician_id", "show_id"},
                        "musician_awards": {"musician_id", "award"}}
            keys = {
                ("registrations", "musician_id", "musicians", "id"),
                ("registrations", "show_id", "shows", "id"),
                ("musician_awards", "musician_id", "musicians", "id"),
            }
            lines = notes.render(selected, "What is the average number of shows per musician?", foreign_keys=keys)
            self.assertIn('all eligible rows of "musicians"', lines[0])
            self.assertIn('"registrations"."musician_id"', lines[0])
            self.assertIn("Keep zero counts unless the question requires a match", lines[0])
            self.assertNotIn("musician_awards", "\n".join(lines))
            self.assertLessEqual(len("\n".join(lines)), MAX_CHARS)
