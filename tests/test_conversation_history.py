"""Verify persisted product transcripts through the public API."""
import argparse
import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        self.app = app
        self.session_id = str(uuid4())
        self.directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": self.directory, "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "",
            "RAG_LANGSMITH_EXPORT": "false", "RAG_BIRD_ROOT": "", "RAG_SANDBOX_SOCKET": "",
            "LLM_API_KEY": "",
        }))

    @property
    def session_path(self):
        return f"/api/v1/sessions/{self.session_id}"

    def ask(self, client, question, *, headers=None):
        return self.events(client, "chat", {"session_id": self.session_id, "question": question}, headers=headers)

    def events(self, client, endpoint, body, *, headers=None):
        response = client.post(f"/api/v1/{endpoint}", json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["event"], "started")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(all(event["trace_id"] == response.headers["x-trace-id"] for event in events))
        return events

    def history(self, client, *, headers=None, params=None):
        response = client.get(self.session_path + "/messages", headers=headers, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def assert_turn(self, pair, question, reply, done):
        user, assistant = pair
        self.assertEqual([user["role"], assistant["role"]], ["user", "assistant"])
        self.assertEqual(user["content"], question)
        self.assertEqual(assistant["content"], reply)
        for entry in pair:
            self.assertEqual(entry["turn_id"], done["trace_id"])
            self.assertEqual(entry["trace_id"], done["trace_id"])
            self.assertIsNotNone(datetime.fromisoformat(entry["created_at"]).tzinfo)
        self.assertIsNone(user["trace"])
        self.assertEqual(assistant["trace"]["trace_id"], done["trace_id"])
        self.assertEqual(assistant["trace"]["status"], done["status"])
        self.assertEqual(assistant["trace"]["usage"], done["usage"])
        self.assertNotIn("spans", assistant["trace"])

    def test_transcript_and_pending_survive_lifespan_restart(self):
        original = "上月营收多少？"
        with TestClient(self.app) as client:
            clarify = self.ask(client, original)
            self.assertEqual(clarify[-2]["data"]["kind"], "clarification")
            before = self.history(client)
            self.assertIsNone(before["next_before"])
            self.assert_turn(before["messages"], original, clarify[-2]["data"], clarify[-1])

        with TestClient(self.app) as client:
            self.assertEqual(self.history(client), before)
            self.assertEqual(client.get(self.session_path).json()["kind"], "clarification")
            answer = self.ask(client, "扣除退款")
            self.assertEqual(answer[-2]["data"]["revenue_cny"], "475.00")
            self.ask(client, "9 月营收是多少？")
            canceled = self.ask(client, "取消这次澄清")
            self.assertIn("已取消", canceled[-2]["data"]["message"])
            messages = self.history(client)["messages"]
            self.assertEqual(len(messages), 8)
            self.assertEqual(messages[:2], before["messages"])
            self.assert_turn(messages[2:4], "扣除退款", answer[-2]["data"], answer[-1])
            self.assert_turn(messages[6:], "取消这次澄清", canceled[-2]["data"], canceled[-1])

    def test_legacy_refinement_label_is_recovered_from_previous_answer_across_pages(self):
        with TestClient(self.app) as client:
            for question in ("9 月营收是多少？", "十月", "净营收", "只看华南"):
                self.ask(client, question)
            with sqlite3.connect(Path(self.directory) / "conversation_history.sqlite") as connection:
                connection.execute(
                    "UPDATE conversation_turns SET reply = json_set(reply, '$.refined_from', ?) WHERE sequence = 4",
                    ("9 月营收是多少？",),
                )
            for limit in (1, 4):
                with self.subTest(limit=limit):
                    latest = self.history(client, params={"limit": limit})["messages"][-1]["content"]
                    self.assertEqual(latest["refined_from"], "2026-10 净营收")
                    self.assertEqual(latest["revenue_cny"], "175.00")

    def test_accepted_workflow_errors_persist_only_the_public_reply(self):
        private = "fake-private-db-credential"
        returned = {"kind": "error", "message": "只读 SQL 未能完成执行", "stop_reason": "execution_error"}
        with TestClient(self.app) as client, patch(
            "app.product_api._conversation", side_effect=[RuntimeError(private), returned],
        ):
            thrown = self.ask(client, "你好")
            self.assertEqual(thrown[-2]["event"], "error")
            self.assertEqual(thrown[-1]["status"], "error")
            public_error = {"kind": "error", **{key: value for key, value in thrown[-2].items() if key != "event"}}
            self.assertEqual(public_error["stop_reason"], "workflow_error")
            self.assertTrue(public_error["suggestion"])
            self.assertNotIn(private, str(thrown))
            result = self.ask(client, "你能做什么？")
            result_error = result[-2]["data"]
            self.assertEqual({key: result_error[key] for key in returned}, returned)
            self.assertEqual(result_error["trace_id"], result[-1]["trace_id"])
            messages = self.history(client)["messages"]
            self.assertNotIn(private, str(messages))
            self.assert_turn(messages[:2], "你好", public_error, thrown[-1])
            self.assert_turn(messages[2:], "你能做什么？", result_error, result[-1])

    def test_approval_reply_is_restored_as_a_turn(self):
        question = "将订单 1001 的备注改为历史测试"
        with TestClient(self.app) as client:
            requested = self.ask(client, question)
            approval = requested[-2]["data"]
            self.assertEqual(approval["kind"], "approval")
            applied = self.events(client, "approvals", {
                "session_id": self.session_id, "approval_id": approval["approval_id"], "approve": True,
            })
            self.assertEqual(applied[-2]["data"]["status"], "applied")
            messages = self.history(client)["messages"]
            self.assertEqual(len(messages), 4)
            self.assert_turn(messages[:2], question, approval, requested[-1])
            self.assert_turn(messages[2:], "批准更新", applied[-2]["data"], applied[-1])

    def test_cursor_pages_preserve_chronology_and_complete_turns(self):
        questions = ["你好", "你好！", "你好。", "你好？", "您好"]
        with TestClient(self.app) as client:
            turns = [self.ask(client, question) for question in questions]
            first = self.history(client, params={"limit": 2})
            self.assertIsInstance(first["next_before"], int)
            self.ask(client, "你是谁")
            second = self.history(client, params={"limit": 2, "before": first["next_before"]})
            self.assertIsInstance(second["next_before"], int)
            third = self.history(client, params={"limit": 2, "before": second["next_before"]})
            self.assertIsNone(third["next_before"])
            self.assertEqual([len(page["messages"]) for page in (first, second, third)], [4, 4, 2])
            messages = third["messages"] + second["messages"] + first["messages"]
            for offset, (question, events) in enumerate(zip(questions, turns, strict=True)):
                self.assert_turn(messages[2 * offset:2 * offset + 2], question, events[-2]["data"], events[-1])
            self.assertEqual(self.history(client, params={"limit": 1})["messages"][0]["content"], "你是谁")
            for params in ({"limit": 0}, {"limit": 51}, {"before": 0}):
                with self.subTest(params=params):
                    response = client.get(self.session_path + "/messages", params=params)
                    self.assertEqual(response.status_code, 422)

    def test_legacy_checkpoint_can_resume_before_any_transcript_exists(self):
        from app.step6_demo import run_conversation

        root = Path(self.directory) / "conversation"
        args = argparse.Namespace(command="ask", session=self.session_id, question="9 月营收是多少？")
        self.assertEqual(run_conversation(root, args)["kind"], "clarification")
        with TestClient(self.app) as client:
            self.assertEqual(self.history(client), {"messages": [], "next_before": None})
            self.assertEqual(client.get(self.session_path).json()["context"]["period"], "2026-09")
            result = self.ask(client, "净营收")
            self.assertEqual(result[-2]["data"]["revenue_cny"], "70.00")
            self.assert_turn(self.history(client)["messages"], "净营收", result[-2]["data"], result[-1])

    def test_parallel_requests_do_not_overtake_the_previous_history_write(self):
        saving_first = Event()
        release_first = Event()
        with TestClient(self.app) as client, ThreadPoolExecutor(max_workers=2) as executor:
            store = self.app.state.traces
            put = store.put

            def pause_first(report, *, owner=""):
                if not saving_first.is_set():
                    saving_first.set()
                    if not release_first.wait(timeout=5):
                        raise TimeoutError("test did not release the first history write")
                return put(report, owner=owner)

            with patch.object(store, "put", side_effect=pause_first):
                first = executor.submit(self.ask, client, "你好")
                try:
                    self.assertTrue(saving_first.wait(timeout=5))
                    second = executor.submit(self.ask, client, "你能做什么？")
                    with self.assertRaises(FutureTimeout):
                        second.result(timeout=1)
                    self.assertEqual(self.history(client)["messages"], [])
                finally:
                    release_first.set()
                first_events = first.result(timeout=5)
                second_events = second.result(timeout=5)

            messages = self.history(client)["messages"]
            self.assertEqual(len(messages), 4)
            self.assert_turn(messages[:2], "你好", first_events[-2]["data"], first_events[-1])
            self.assert_turn(messages[2:], "你能做什么？", second_events[-2]["data"], second_events[-1])

    def test_same_session_id_has_separate_histories_for_each_account(self):
        key = "a" * 64
        alice = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        bob = {"X-Rag-Gateway-Key": key, "X-Rag-User": "bob"}
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}), TestClient(self.app) as client:
            question = "将订单 1001 的备注改为专属记录"
            first = self.ask(client, question, headers=alice)
            self.assertEqual(self.history(client, headers=bob), {"messages": [], "next_before": None})
            second = self.ask(client, "你好", headers=bob)
            self.assert_turn(self.history(client, headers=alice)["messages"], question, first[-2]["data"], first[-1])
            self.assert_turn(self.history(client, headers=bob)["messages"], "你好", second[-2]["data"], second[-1])
            response = client.get(self.session_path + "/messages")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["cache-control"], "no-store")
