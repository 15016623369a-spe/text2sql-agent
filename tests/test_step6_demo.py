import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.commerce_demo import NoteChange, apply_note
from app.tools.db import ReadOnlyDB


class Step6DemoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = Path(__file__).resolve().parents[1]

    def run_demo(self, *args, session="session", success=True):
        result = subprocess.run(
            [sys.executable, "-m", "app.step6_demo", "--root", str(self.root), "--session", session, *args],
            cwd=self.project, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=15,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(result.returncode, 2, result.stdout)
        return result.stderr

    def stored_note(self, order_id=1001):
        with ReadOnlyDB(self.root / "commerce.sqlite") as db:
            return db.query(f"SELECT note FROM orders WHERE id = {order_id}")[1][0][0]

    def test_clarify_and_three_followups_across_processes(self):
        clarify = self.run_demo("ask", "上月营收多少？")
        self.assertEqual(clarify["kind"], "clarification")
        self.assertEqual(self.run_demo("status"), clarify)
        self.assertEqual(self.run_demo("ask", "我没想好")["kind"], "clarification")
        answer = self.run_demo("ask", "扣除退款")
        self.assertEqual(answer["revenue_cents"], 47500)
        followups = (
            ("那华南区呢？", "last_month", "华南", 17500),
            ("那本月呢？", "this_month", "华南", 9000),
            ("那华东区呢？", "this_month", "华东", 0),
        )
        for question, period, region, cents in followups:
            with self.subTest(question=question):
                reply = self.run_demo("ask", question)
                self.assertEqual(reply["context"], {"period": period, "metric": "net", "region": region})
                self.assertEqual(reply["revenue_cents"], cents)
        self.assertEqual(self.run_demo("ask", "那华南区呢？", session="other")["kind"], "rejected")

    def test_explicit_gross_and_unsupported_scope(self):
        gross = self.run_demo("ask", "上月毛营收多少？")
        self.assertEqual(gross["revenue_cents"], 55000)
        self.assertEqual(gross["context"]["metric"], "gross")
        unsupported = self.run_demo("ask", "上月北京净营收多少？")
        self.assertEqual(unsupported["kind"], "rejected")
        self.assertEqual(self.run_demo("ask", "那华南区呢？")["revenue_cents"], 20000)

    def test_human_decisions_require_the_same_session_and_operation(self):
        pending = self.run_demo("ask", "将订单 1001 的备注改为已核对")
        approval = pending["approval_id"]
        self.assertEqual(pending["kind"], "approval")
        self.assertEqual(self.stored_note(), "")
        self.assertEqual(self.run_demo("ask", "批准"), pending)
        self.run_demo("decide", "--approval-id", approval, "--approve", session="other", success=False)
        self.run_demo("decide", "--approval-id", "0" * 32, "--approve", success=False)
        self.assertEqual(self.stored_note(), "")
        self.assertEqual(self.run_demo("decide", "--approval-id", approval, "--deny")["status"], "denied")
        self.assertEqual(self.stored_note(), "")
        pending = self.run_demo("ask", "将订单 1001 的备注改为已核对")
        approval = pending["approval_id"]
        applied = self.run_demo("decide", "--approval-id", approval, "--approve")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(self.stored_note(), "已核对")
        self.assertEqual(self.run_demo("decide", "--approval-id", approval, "--approve"), applied)
        with ReadOnlyDB(self.root / "commerce.sqlite") as db:
            self.assertEqual(db.query("SELECT COUNT(*) FROM applied_approvals")[1], [(1,)])

    def test_replayed_write_cannot_overwrite_a_later_approved_value(self):
        self.run_demo("status")
        database = self.root / "commerce.sqlite"
        first = NoteChange(approval_id="1" * 32, order_id=1001, note="第一次")
        second = NoteChange(approval_id="2" * 32, order_id=1001, note="第二次")
        self.assertEqual(apply_note(database, first)["status"], "applied")
        self.assertEqual(apply_note(database, second)["status"], "applied")
        self.assertEqual(apply_note(database, first)["status"], "applied")
        self.assertEqual(self.stored_note(), "第二次")

    def test_approved_write_can_resume_after_process_exits_between_commit_and_checkpoint(self):
        approval = self.run_demo("ask", "将订单 1001 的备注改为已核对")["approval_id"]
        crash_after_commit = """
import os
from app import conversation, step6_demo

apply = conversation.apply_note

def apply_and_exit(database, change):
    apply(database, change)
    os._exit(71)

conversation.apply_note = apply_and_exit
raise SystemExit(step6_demo.main())
"""
        crashed = subprocess.run(
            [sys.executable, "-c", crash_after_commit, "--root", str(self.root), "--session", "session",
             "decide", "--approval-id", approval, "--approve"],
            cwd=self.project, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(crashed.returncode, 71, crashed.stderr)
        self.assertEqual(self.stored_note(), "已核对")
        self.assertEqual(self.run_demo("status")["kind"], "approval_recorded")
        resumed = self.run_demo("decide", "--approval-id", approval, "--approve")
        self.assertEqual(resumed["status"], "applied")
        with ReadOnlyDB(self.root / "commerce.sqlite") as db:
            self.assertEqual(db.query("SELECT COUNT(*) FROM applied_approvals")[1], [(1,)])

    def test_write_commands_and_foreign_database_are_rejected(self):
        self.assertEqual(self.run_demo("ask", "DELETE FROM orders")["kind"], "rejected")
        self.assertEqual(self.stored_note(), "")
        self.root = self.root / "foreign"
        self.root.mkdir()
        database = self.root / "commerce.sqlite"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE untouched(id INTEGER)")
        self.assertIn("不是 Step 6 电商演示库", self.run_demo("status", success=False))
        self.assertFalse((self.root / "step6_sessions.sqlite").exists())
        with ReadOnlyDB(database) as db:
            self.assertEqual(db.query("SELECT name FROM sqlite_schema WHERE type='table'")[1], [("untouched",)])
