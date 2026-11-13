"""Summarize repeated product benchmarks and compare question-aligned profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from statistics import median

from app import product_sql, text2sql
from evaluation.run_product import PROFILES, _DATASET_HASH, _SPLITS, _metrics

_PROTOCOL = (
    "version", "path", "split", "full_split", "dataset_sha256", "split_sha256", "question_ids", "model", "provider",
    "provider_host", "temperature", "workers", "sqlite_version", "uses_official_evidence", "execution_metric",
    "sql_timeout_seconds", "model_timeout_seconds", "max_output_tokens", "max_result_rows", "observation_max_bytes",
    "schema_linking", "column_notes", "recovery",
)


def _stats(values: list[float]) -> dict:
    return {"median": median(values), "min": min(values), "max": max(values)}


def _load_run(directory: Path, legacy_profile: str | None) -> dict:
    run = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    log = (directory / "questions.jsonl").read_bytes()
    records = [json.loads(line) for line in log.splitlines() if line.strip()]
    if run["version"] != "product" or run["full_split"] is not True or run["dataset_sha256"] != _DATASET_HASH:
        raise ValueError(f"{directory}: expected the full, pinned product benchmark")
    split = run["split"]
    if split not in ("dev", "eval"):
        raise ValueError(f"{directory}: unknown benchmark split")
    manifest = (_SPLITS / f"{split}_ids.json").read_bytes()
    ids = json.loads(manifest)
    if run["split_sha256"] != hashlib.sha256(manifest).hexdigest() or run["question_ids"] != ids:
        raise ValueError(f"{directory}: benchmark IDs differ from the fixed split")
    if [item["question_id"] for item in records] != ids or any(type(item["ex"]) is not int or item["ex"] not in (0, 1)
                                                                for item in records):
        raise ValueError(f"{directory}: incomplete or invalid question scores")

    if legacy_profile is None:
        profile = run["profile"]
        run_id = run["run_id"]
        if run["features"] != asdict(PROFILES[profile]) or not isinstance(run_id, str) or not run_id:
            raise ValueError(f"{directory}: invalid run identity or profile switches")
    else:
        if any(key in run for key in ("profile", "run_id", "features")):
            raise ValueError(f"{directory}: profile-based runs must be passed without a legacy label")
        profile = legacy_profile
        run_id = "legacy-log-sha256:" + hashlib.sha256(log).hexdigest()
        # Stage counts were added with the profile runner. The older logs had no such instrumentation.
        for record in records:
            record["semantic_checks"] = {"pre_execution": 0, "post_execution": 0}
    guidance = PROFILES[profile].prompt_guidance
    system_prompt = text2sql.SYSTEM_PROMPT + (product_sql.PRODUCT_GUIDANCE if guidance else "")
    prompt_version = product_sql.PRODUCT_PROMPT_VERSION if guidance else text2sql.PROMPT_VERSION
    if run["prompt_version"] != prompt_version or run["system_prompt_sha256"] != hashlib.sha256(system_prompt.encode()).hexdigest():
        raise ValueError(f"{directory}: recorded prompt does not match the declared profile")
    if not run["source_sha256"]:
        raise ValueError(f"{directory}: source fingerprint is missing")
    metrics = _metrics(records)
    fields = ("questions", "correct", "execution_accuracy", "p95_latency_seconds", "token_totals", "benchmark_valid")
    if not metrics["benchmark_valid"] or any(metrics[field] != summary[field] for field in fields):
        raise ValueError(f"{directory}: summary does not match a complete valid run")
    return {
        "run_id": run_id, "profile": profile, "protocol": {key: run[key] for key in _PROTOCOL},
        "source_sha256": run["source_sha256"], "prompt_version": prompt_version,
        "system_prompt_sha256": run["system_prompt_sha256"], "metrics": metrics,
        "scores": {record["question_id"]: record["ex"] for record in records},
    }


def _compare(full: dict, other: dict) -> dict:
    full_scores, other_scores = full["scores"], other["scores"]
    wins = [qid for qid, score in full_scores.items() if score > other_scores[qid]]
    losses = [qid for qid, score in full_scores.items() if score < other_scores[qid]]
    delta = len(wins) - len(losses)
    return {
        "full_run_id": full["run_id"], "comparison_run_id": other["run_id"],
        "full_only_correct_ids": wins, "comparison_only_correct_ids": losses,
        "full_minus_comparison_correct": delta,
        "full_minus_comparison_pp": round(100 * delta / len(full_scores), 6),
    }


def _profile_summary(group: list[dict], *, legacy: bool) -> dict:
    metrics = [run["metrics"] for run in group]
    fields = ("correct", "p50_latency_seconds", "p95_latency_seconds", "model_calls", "model_calls_with_usage")
    result = {
        "run_ids": [run["run_id"] for run in group], "source_sha256": group[0]["source_sha256"],
        "prompt_version": group[0]["prompt_version"], "system_prompt_sha256": group[0]["system_prompt_sha256"],
        "execution_accuracy_pct": _stats([round(100 * item["execution_accuracy"], 6) for item in metrics]),
        "token_totals": {
            token: _stats([item["token_totals"][token] for item in metrics]) for token in metrics[0]["token_totals"]
        },
        "runs": [
            {"run_id": run["run_id"], **{field: item[field] for field in fields}, "token_totals": item["token_totals"]}
            for run, item in zip(group, metrics)
        ],
        **{field: _stats([item[field] for item in metrics]) for field in fields},
    }
    if not legacy:
        result["features"] = asdict(PROFILES[group[0]["profile"]])
        result["semantic_checks"] = {
            phase: _stats([item["semantic_checks"][phase] for item in metrics]) for phase in metrics[0]["semantic_checks"]
        }
    return result


def compare_runs(runs: list[dict], *, repeats: int, legacy: bool) -> dict:
    protocol = runs[0]["protocol"]
    groups = defaultdict(list)
    for run in runs:
        if run["protocol"] != protocol:
            differences = [key for key, value in protocol.items() if value != run["protocol"][key]]
            raise ValueError(f"{run['run_id']}: benchmark settings differ for {', '.join(differences)}")
        groups[run["profile"]].append(run)
    if len({run["run_id"] for run in runs}) != len(runs):
        raise ValueError("Duplicate benchmark run")
    if not {"full", "baseline"}.issubset(groups) or any(len(group) != repeats for group in groups.values()):
        raise ValueError(f"Expected {repeats} runs of each supplied profile, including baseline and full")
    comparable_sources = groups.values() if legacy else [runs]
    for group in comparable_sources:
        if any(run["source_sha256"] != group[0]["source_sha256"] for run in group):
            raise ValueError("The benchmark code changed across runs of the same source configuration")

    profiles = {profile: _profile_summary(group, legacy=legacy) for profile, group in groups.items()}
    comparisons = {}
    for profile, group in groups.items():
        if profile == "full":
            continue
        paired = [_compare(full, other) for full, other in zip(groups["full"], group)]
        comparisons[profile] = {
            "median_accuracy_difference_pp": round(
                profiles["full"]["execution_accuracy_pct"]["median"] - profiles[profile]["execution_accuracy_pct"]["median"], 6,
            ),
            "paired_deltas_pp": _stats([item["full_minus_comparison_pp"] for item in paired]),
            "paired_runs": paired,
        }
    return {
        "protocol": protocol, "legacy_source_snapshots": legacy, "repetitions_per_profile": repeats,
        "pairing": "Question IDs are matched within runs. Repetitions are paired in the supplied order without a shared model seed.",
        "profiles": profiles, "full_vs_profile": comparisons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", type=Path, nargs="*", help="Completed full-split profile runs, in repetition order")
    parser.add_argument("--legacy-baseline", type=Path, action="append", default=[], help="A historical baseline snapshot run")
    parser.add_argument("--legacy-full", type=Path, action="append", default=[], help="A historical full snapshot run")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, help="Write the report to this new file")
    args = parser.parse_args()
    legacy = bool(args.legacy_baseline or args.legacy_full)
    if args.repeats < 1 or bool(args.run_dirs) == legacy:
        parser.error("Supply profiled runs or explicitly labeled legacy runs, and a positive repetition count")
    try:
        runs = [_load_run(directory, None) for directory in args.run_dirs]
        runs.extend(_load_run(directory, "baseline") for directory in args.legacy_baseline)
        runs.extend(_load_run(directory, "full") for directory in args.legacy_full)
        report = compare_runs(runs, repeats=args.repeats, legacy=legacy)
        result = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            print(result, end="")
        else:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(result)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
