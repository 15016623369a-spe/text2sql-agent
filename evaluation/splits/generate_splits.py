"""生成可复现、互不重叠的 dev / eval 题目 ID 划分。

按 (db_id, difficulty) 分层，用「最大余数法」做比例分层抽样（各层分配名额
与其题量成正比），固定随机种子保证可复现。
产物：dev_ids.json、eval_ids.json、manifest.json。
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

from app.config import PROJECT_ROOT

JSONL = PROJECT_ROOT / "data" / "bird" / "mini_dev_sqlite.jsonl"
OUT_DIR = Path(__file__).resolve().parent
SEED = 42
DEV_N = 50
EVAL_N = 200


def load_questions(path: Path) -> list[dict]:
    qs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                qs.append(json.loads(line))
    return qs


def proportional_allocate(sizes: dict, n: int) -> dict:
    """最大余数法：按各层题量比例分配 n 个名额（floor 后按余数从大到小补足）。"""
    total = sum(sizes.values())
    alloc = {}
    remainders = []
    for k in sorted(sizes):
        s = sizes[k]
        exact = s * n / total
        base = int(exact)
        alloc[k] = base
        remainders.append((exact - base, k))
    left = n - sum(alloc.values())
    for _, k in sorted(remainders, key=lambda x: -x[0])[:left]:
        alloc[k] += 1
    return alloc


def dist(qs: list[dict]) -> dict:
    d = defaultdict(int)
    for q in qs:
        d[q["difficulty"]] += 1
    return dict(sorted(d.items()))


def main() -> int:
    questions = load_questions(JSONL)
    rng = random.Random(SEED)

    strata: dict[tuple, list[dict]] = {}
    for q in questions:
        strata.setdefault((q["db_id"], q["difficulty"]), []).append(q)
    for k in sorted(strata):
        rng.shuffle(strata[k])

    dev_alloc = proportional_allocate({k: len(v) for k, v in strata.items()}, DEV_N)
    dev = []
    for k in sorted(strata):
        dev.extend(strata[k][: dev_alloc[k]])
    dev_ids = {q["question_id"] for q in dev}

    remaining: dict[tuple, list[dict]] = {}
    for k in sorted(strata):
        remaining[k] = [q for q in strata[k] if q["question_id"] not in dev_ids]

    eval_alloc = proportional_allocate(
        {k: len(v) for k, v in remaining.items()}, EVAL_N
    )
    eval_qs = []
    for k in sorted(remaining):
        eval_qs.extend(remaining[k][: eval_alloc[k]])
    eval_ids = [q["question_id"] for q in eval_qs]

    assert len(dev_ids) == DEV_N, len(dev_ids)
    assert len(eval_ids) == EVAL_N, len(eval_ids)
    assert not (dev_ids & set(eval_ids)), "dev 与 eval 有重叠"
    assert len(set(eval_ids)) == len(eval_ids), "eval 存在重复 ID"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "dev_ids.json").write_text(
        json.dumps(sorted(dev_ids), indent=2) + "\n", encoding="utf-8"
    )
    (OUT_DIR / "eval_ids.json").write_text(
        json.dumps(sorted(eval_ids), indent=2) + "\n", encoding="utf-8"
    )

    manifest = {
        "source": JSONL.relative_to(PROJECT_ROOT).as_posix(),
        "seed": SEED,
        "stratify_by": ["db_id", "difficulty"],
        "total_questions": len(questions),
        "dev_n": len(dev_ids),
        "eval_n": len(eval_ids),
        "unused_n": len(questions) - len(dev_ids) - len(eval_ids),
        "method": "最大余数法比例分层抽样（各层名额 ∝ 题量；层内按固定种子 shuffle）",
        "population_difficulty": dist(questions),
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("总体难度分布:", dist(questions))
    print("dev  难度分布:", dist(dev))
    print("eval 难度分布:", dist(eval_qs))
    print("dev  覆盖 db:", len({q["db_id"] for q in dev}))
    print("eval 覆盖 db:", len({q["db_id"] for q in eval_qs}))
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
