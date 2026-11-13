"""Run the Step 6 conversation, durable checkpoints, and approval demo."""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.commerce_demo import prepare_database
from app.config import PROJECT_ROOT, Settings, load_settings
from app.conversation import ApprovalDecision, build_conversation

ROOT = PROJECT_ROOT / "data" / "step6_demo"
_CHECKPOINT_ID = 0x52414336


def _checkpoint_connection(path: Path) -> sqlite3.Connection:
    existing = path.exists()
    connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
    if existing:
        app_id, = connection.execute("PRAGMA application_id").fetchone()
        if app_id != _CHECKPOINT_ID:
            connection.close()
            raise ValueError("会话文件不是 Step 6 的 checkpoint 库")
    else:
        connection.execute(f"PRAGMA application_id = {_CHECKPOINT_ID}")
    return connection


def _show(snapshot) -> dict:
    if snapshot.interrupts:
        return snapshot.interrupts[0].value
    if snapshot.next == ("apply_write",):
        pending = snapshot.values["pending_write"]
        return {"kind": "approval_recorded", "approval_id": pending["approval_id"],
                "message": "已记录批准，可用同一审批编号继续执行"}
    if snapshot.next:
        return {"kind": "pending", "message": "会话尚有未完成步骤"}
    return snapshot.values.get("reply") or {"kind": "empty", "message": "该会话尚无对话"}


def _run(graph, config, args: argparse.Namespace) -> dict:
    snapshot = graph.get_state(config)
    if args.command == "status":
        return _show(snapshot)
    if args.command == "ask":
        if snapshot.next and snapshot.next != ("clarify",):
            return _show(snapshot)
        request = Command(resume=args.question) if snapshot.next else {"question": args.question}
    else:
        decision = ApprovalDecision(approval_id=args.approval_id, approve=args.approve)
        pending = snapshot.values.get("pending_write")
        if snapshot.next in (("approve_write",), ("apply_write",)) and pending and pending["approval_id"] == decision.approval_id:
            if snapshot.next == ("approve_write",):
                request = Command(resume=decision.model_dump())
            elif decision.approve:
                request = None
            else:
                raise ValueError("这条审批已批准，不能改为拒绝")
        else:
            previous = snapshot.values.get("reply", {})
            expected = {"applied", "not_found"} if decision.approve else {"denied"}
            if not snapshot.next and previous.get("approval_id") == decision.approval_id and previous.get("status") in expected:
                return previous
            raise ValueError("本会话没有对应的待审批操作")
    graph.invoke(request, config, durability="sync")
    return _show(graph.get_state(config))


@contextmanager
def conversation_lock(root: Path):
    """Serialize checkpoint access and any associated write for this conversation store."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".step6.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def run_conversation(
    root: Path, args: argparse.Namespace, settings: Settings | None = None, *,
    assistant=None, data_source: str | None = None, data_tables: tuple[str, ...] | None = None, lock_held: bool = False,
) -> dict:
    with nullcontext() if lock_held else conversation_lock(root):
        database = prepare_database(root)
        with closing(_checkpoint_connection(root / "step6_sessions.sqlite")) as connection:
            graph = build_conversation(
                database, settings, SqliteSaver(connection), assistant=assistant, data_source=data_source,
                data_tables=data_tables,
            )
            config = {"configurable": {"thread_id": args.session}}
            return _run(graph, config, args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Location for this demo's two SQLite files")
    parser.add_argument("--session", required=True, help="Reuse this session ID after restarting")
    parser.add_argument("--live", action="store_true", help="Use the configured model to generate the read-only SQL")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    decide = commands.add_parser("decide")
    decide.add_argument("--approval-id", required=True)
    choice = decide.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--deny", action="store_true")
    commands.add_parser("status")
    args = parser.parse_args()
    if not args.session.strip() or len(args.session) > 120:
        parser.error("会话 ID 长度必须为 1–120 个字符")

    settings = load_settings() if args.live and args.command == "ask" else None
    if settings is not None and not (settings.llm_configured and settings.llm_base_url):
        parser.error("请先配置 LLM_API_KEY 和 LLM_BASE_URL")
    try:
        result = run_conversation(args.root.resolve(), args, settings)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["kind"] == "error")


if __name__ == "__main__":
    raise SystemExit(main())
