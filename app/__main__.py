"""项目最小命令行入口：`python -m app smoke` 做冒烟检查。"""
from __future__ import annotations

import argparse
import sys
import urllib.error

from app.config import PROJECT_ROOT, load_settings
from app.llm import chat_completion, completion_text
from app.tools.db import ReadOnlyDB

BIRD_DB = (
    PROJECT_ROOT / "data" / "bird" / "dev_databases"
    / "california_schools" / "california_schools.sqlite"
)


def _sqlite_smoke() -> int:
    print("== 只读 SQLite ==")
    if not BIRD_DB.exists():
        print(f"  错误：找不到数据库 {BIRD_DB}")
        return 1
    with ReadOnlyDB(BIRD_DB) as db:
        cols, rows = db.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        print(f"  列: {cols}")
        print(f"  表(前 {len(rows)}): {[r[0] for r in rows]}")
    return 0


def _llm_api_smoke(settings) -> int:
    print("== LLM API 冒烟 ==")
    if not settings.llm_configured:
        print("  pending（未配置密钥），跳过真实 API 调用")
        return 0
    if not settings.llm_base_url:
        print("  错误：密钥已配置但 base_url 未设置")
        return 1

    try:
        status, body = chat_completion(
            settings,
            [{"role": "user", "content": "Reply with the single word: OK"}],
            max_tokens=16,
        )
        content = completion_text(body)
    except urllib.error.HTTPError as e:
        print(f"  错误：HTTP {e.code}")
        return 1
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"  错误：模型连接失败或返回格式异常（{type(e).__name__}）")
        return 1

    usage = body.get("usage", {})
    print(f"  status : {status}")
    print(f"  reply  : {str(content).strip()[:80]!r}")
    print(f"  usage  : {usage}")
    return 0


def _smoke() -> int:
    settings = load_settings()
    print("== 配置 ==")
    print(f"  provider : {settings.llm_provider}")
    print(f"  model    : {settings.llm_model}")
    print(f"  LLM 密钥  : {'已配置' if settings.llm_configured else 'pending（未配置）'}")

    rc_sqlite = _sqlite_smoke()
    rc_llm = _llm_api_smoke(settings)
    if rc_sqlite or rc_llm:
        print("== smoke 失败 ==")
        return 1
    if settings.llm_configured:
        print("== SQLite 和模型 API 检查通过 ==")
    else:
        print("== SQLite 检查通过，模型 API 待验收 ==")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "smoke", help="最小冒烟检查（配置 + 只读 SQLite + LLM API + 密钥状态）"
    )
    args = parser.parse_args()
    if args.command == "smoke":
        return _smoke()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
