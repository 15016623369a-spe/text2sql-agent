"""Publish and gate three repetitions of the fixed product benchmark."""
import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

_DEV_IDS = Path(__file__).resolve().parents[2] / "evaluation" / "splits" / "dev_ids.json"
_FORMAT_VERSION = 2
_REPETITIONS = 3
_QUESTIONS = 50
_MAX_ACCURACY_DROP = 4
_CONVERSATION_P95_TARGET_SECONDS = 6.0
_P95_RELATIVE_MARGIN = 0.25
_P95_MIN_MARGIN_SECONDS = 0.5
_INFRASTRUCTURE_ERRORS = {"model_request_error", "schema_error", "reference_error", "observation_error"}
_FULL_FEATURES = {"value_hints": True, "prompt_guidance": True, "semantic_checks": True}
_COMPATIBILITY_FIELDS = (
    "version", "split", "dataset_sha256", "split_sha256", "question_ids", "model", "provider", "provider_host",
    "temperature", "execution_metric", "uses_official_evidence", "sql_timeout_seconds", "model_timeout_seconds",
    "max_output_tokens", "max_result_rows", "observation_max_bytes", "workers",
)
_PUBLIC_RUN_FIELDS = (*_COMPATIBILITY_FIELDS, "full_split", "path", "prompt_version", "system_prompt_sha256",
                      "source_sha256", "sqlite_version", "run_id", "profile")
_PUBLIC_METRICS = (
    "questions", "correct", "answered", "executed", "wrong_answers", "execution_accuracy", "execution_success_rate",
    "wrong_answer_rate", "answer_rate", "accuracy_on_answered", "p50_latency_seconds", "p95_latency_seconds",
    "statuses", "error_types", "model_calls", "model_calls_with_usage", "benchmark_valid",
)
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
_QUESTION_FIELDS = ("question_id", "db_id", "difficulty", "ex", "status", "latency_seconds", "model_calls", "stop_reason")
_DENSE_FIELDS = ("version", "model", "revision", "source_sha256", "device", "store", "distance",
                 "max_sequence_length", "dense_rank_k")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite benchmark metric")
    return number


def parse_json(value):
    return json.loads(value, parse_float=finite_float, parse_constant=finite_float)


def read_json(path, default):
    try:
        value = parse_json(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def pick(values, fields):
    return {key: values[key] for key in fields if key in values}


def compatibility(run):
    """Compare the evaluation contract across prompt and product code changes."""
    if any(key not in run for key in _COMPATIBILITY_FIELDS):
        return None
    linking = run.get("schema_linking")
    if not isinstance(linking, dict) or "top_k" not in linking or "dense" not in linking:
        return None
    return {**pick(run, _COMPATIBILITY_FIELDS), "top_k": linking["top_k"], "dense": linking["dense"]}


def public_metrics(summary):
    metrics = pick(summary, _PUBLIC_METRICS)
    if isinstance(summary.get("token_totals"), dict):
        metrics["token_totals"] = pick(summary["token_totals"], _TOKEN_FIELDS)
    if isinstance(summary.get("semantic_checks"), dict):
        metrics["semantic_checks"] = pick(summary["semantic_checks"], ("pre_execution", "post_execution"))
    return metrics


def public_run(run):
    visible = pick(run, _PUBLIC_RUN_FIELDS)
    visible.setdefault("profile", "full")
    features = run.get("features", _FULL_FEATURES)
    if isinstance(features, dict):
        visible["features"] = pick(features, _FULL_FEATURES)
    linking = run.get("schema_linking")
    if isinstance(linking, dict):
        visible["schema_linking"] = pick(linking, ("version", "top_k", "dense"))
        if isinstance(linking.get("dense"), dict):
            visible["schema_linking"]["dense"] = pick(linking["dense"], _DENSE_FIELDS)
    return visible


def bird_summary(directory):
    run = read_json(directory / "run.json", {})
    summary = read_json(directory / "summary.json", {})
    try:
        rows = (directory / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        questions = [parse_json(row) for row in rows]
        if any(not isinstance(row, dict) for row in questions):
            questions = []
    except (OSError, ValueError):
        questions = []
    metrics = public_metrics(summary)
    for key in ("by_database", "by_difficulty"):
        if isinstance(summary.get(key), dict):
            metrics[key] = {name: public_metrics(group) for name, group in summary[key].items() if isinstance(group, dict)}
    return {"run": public_run(run), "summary": metrics, "questions": [pick(row, _QUESTION_FIELDS) for row in questions]}


def valid_latency(value):
    return type(value) in (int, float) and 0 <= value < math.inf


def valid_repetition(rep, fixed_ids):
    if not isinstance(rep, dict):
        return False
    run, summary, questions = (rep.get(key) for key in ("run", "summary", "questions"))
    if not isinstance(run, dict) or not isinstance(summary, dict) or not isinstance(questions, list):
        return False
    features = run.get("features")
    if (run.get("version") != "product" or run.get("split") != "dev" or run.get("full_split") is not True
            or run.get("uses_official_evidence") is not True or run.get("question_ids") != fixed_ids
            or compatibility(run) is None or run.get("profile") != "full"
            or not isinstance(features, dict) or any(features.get(key) is not True for key in _FULL_FEATURES)
            or type(run.get("workers")) is not int or run["workers"] < 1
            or ("run_id" in run and (not isinstance(run["run_id"], str) or not run["run_id"]))
            or summary.get("benchmark_valid") is not True or type(summary.get("questions")) is not int
            or summary["questions"] != _QUESTIONS
            or len(questions) != _QUESTIONS or len(fixed_ids) != _QUESTIONS):
        return False
    for question_id, item in zip(fixed_ids, questions):
        if (not isinstance(item, dict) or type(item.get("question_id")) is not int or item["question_id"] != question_id
                or type(item.get("ex")) is not int or item["ex"] not in (0, 1)
                or not isinstance(item.get("status"), str) or not item["status"]
                or (item["status"] == "correct") != bool(item["ex"])
                or not valid_latency(item.get("latency_seconds"))):
            return False
    statuses = Counter(item["status"] for item in questions)
    latencies = sorted(item["latency_seconds"] for item in questions)
    p95 = latencies[math.ceil(_QUESTIONS * .95) - 1]
    return (not (_INFRASTRUCTURE_ERRORS & statuses.keys()) and summary.get("statuses") == statuses
            and type(summary.get("correct")) is int and summary["correct"] == sum(item["ex"] for item in questions)
            and valid_latency(summary.get("p95_latency_seconds")) and summary["p95_latency_seconds"] == p95)


def repetition_config(rep):
    return {key: value for key, value in rep["run"].items() if key != "run_id"}


def evidence_fingerprint(rep):
    evidence = json.dumps(rep["questions"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(evidence.encode("utf-8")).hexdigest()


def aggregate(reps):
    report = {"format_version": _FORMAT_VERSION, "valid": False, "run": {}, "summary": {}, "runs": reps}
    if len(reps) != _REPETITIONS:
        report["error"] = f"Expected {_REPETITIONS} complete repetitions, received {len(reps)}."
        return report
    fixed_ids = read_json(_DEV_IDS, [])
    invalid = [str(i) for i, rep in enumerate(reps, 1) if not valid_repetition(rep, fixed_ids)]
    if invalid:
        report["error"] = f"Incomplete or invalid BIRD evidence in repetitions {', '.join(invalid)}."
        return report
    run_ids = [rep["run"]["run_id"] for rep in reps if "run_id" in rep["run"]]
    if len(set(run_ids)) != len(run_ids) or len({evidence_fingerprint(rep) for rep in reps}) != _REPETITIONS:
        report["error"] = "Duplicate BIRD run evidence."
        return report
    run = repetition_config(reps[0])
    if any(repetition_config(rep) != run for rep in reps[1:]):
        report["error"] = "BIRD repetitions use different run configurations."
        return report
    scores = [rep["summary"]["correct"] for rep in reps]
    p95 = [rep["summary"]["p95_latency_seconds"] for rep in reps]
    report.update(valid=True, run=run, summary={
        "repetitions": _REPETITIONS,
        "questions_per_run": _QUESTIONS,
        "correct_counts": scores,
        "correct_median": median(scores),
        "correct_range": [min(scores), max(scores)],
        "p95_latency_seconds_runs": p95,
        "p95_latency_seconds_median": median(p95),
        "p95_latency_seconds_range": [min(p95), max(p95)],
    })
    return report


def read_reference(path):
    report = read_json(path, {}) if path else {}
    if report.get("format_version") != _FORMAT_VERSION or report.get("valid") is not True:
        return None
    reps = report.get("runs")
    if not isinstance(reps, list):
        return None
    rebuilt = aggregate(reps)
    if rebuilt["valid"] and rebuilt["summary"] == report.get("summary") and rebuilt["run"] == report.get("run"):
        return rebuilt
    return None


def majority_correct(report):
    correct = Counter(row["question_id"] for rep in report["runs"] for row in rep["questions"] if row["ex"])
    return {key for key, count in correct.items() if count > _REPETITIONS // 2}


def compare_bird(current, reference):
    if not current["valid"] or reference is None or compatibility(current["run"]) != compatibility(reference["run"]):
        return None
    now = majority_correct(current)
    earlier = majority_correct(reference)
    current_p95 = current["summary"]["p95_latency_seconds_median"]
    reference_p95 = reference["summary"]["p95_latency_seconds_median"]
    limit = max(reference_p95 * (1 + _P95_RELATIVE_MARGIN), reference_p95 + _P95_MIN_MARGIN_SECONDS)
    return {
        "lost": sorted(earlier - now),
        "gained": sorted(now - earlier),
        "correct_change": current["summary"]["correct_median"] - reference["summary"]["correct_median"],
        "p95_latency_seconds_change": round(current_p95 - reference_p95, 6),
        "p95_latency_seconds_limit": limit,
        "p95_regression": current_p95 > limit and not math.isclose(current_p95, limit, abs_tol=1e-9),
    }


def compare_previous(current, candidates):
    for path in candidates:
        comparison = compare_bird(current, read_reference(path))
        if comparison is not None:
            return comparison
    return None


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def conversation_p95(cases):
    if len(cases) != 30:
        return None
    timings = []
    for case in cases:
        turns = case.get("turns", [])
        if not turns or any(not valid_latency(turn.get("latency_ms")) for turn in turns):
            return None
        timings.extend(turn["latency_ms"] for turn in turns)
    timings.sort()
    return timings[math.ceil(len(timings) * .95) - 1] / 1000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-dir", required=True, action="append", type=Path, help="Repeat once per BIRD run")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output-dir", type=Path)
    output.add_argument("--write-baseline", type=Path, help="Freeze three complete product dev runs")
    parser.add_argument("--previous", action="append", default=[], type=Path,
                        help="Previous successful report, newest first when repeated")
    parser.add_argument("--baseline", type=Path, help="Frozen, sanitized three-run product dev report")
    args = parser.parse_args()

    bird = aggregate([bird_summary(directory) for directory in args.bird_dir])
    if len({directory.resolve() for directory in args.bird_dir}) != len(args.bird_dir):
        bird.update(valid=False, error="Duplicate BIRD run directory.")
    if args.write_baseline:
        if not bird["valid"]:
            print(bird["error"])
            return 1
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.write_baseline, bird)
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    delta = compare_previous(bird, args.previous)
    baseline_delta = compare_bird(bird, read_reference(args.baseline))
    bird.update(comparison=delta, baseline_comparison=baseline_delta)
    dialogue = read_json(args.output_dir / "conversations.json", {})
    lines = ["## Nightly model checks", ""]
    failures = []
    if bird["valid"]:
        summary = bird["summary"]
        scores = ", ".join(str(score) for score in summary["correct_counts"])
        p95 = ", ".join(f"{seconds:.2f}" for seconds in summary["p95_latency_seconds_runs"])
        lines.append(f"- BIRD dev median EX **{summary['correct_median']}/50** · Correct counts {scores}")
        lines.append(f"- BIRD dev median P95 **{summary['p95_latency_seconds_median']:.2f} s** · Three runs {p95} s")
    else:
        failures.append(bird["error"])
        for i, rep in enumerate(bird["runs"], 1):
            unavailable = [f"{row['question_id']} ({row['status']})" for row in rep["questions"]
                           if type(row.get("question_id")) is int and isinstance(row.get("status"), str)
                           and row["status"] in _INFRASTRUCTURE_ERRORS]
            if unavailable:
                lines.append(f"- BIRD repetition {i} infrastructure failures · {', '.join(unavailable)}")
    for label, comparison, required in (("Previous successful nightly", delta, False),
                                        ("Frozen dev baseline", baseline_delta, args.baseline is not None)):
        if comparison is None:
            unavailable = f"{label} has no valid, comparable three-run evidence."
            if required:
                failures.append(unavailable)
            else:
                lines.append(f"- {unavailable}")
            continue
        change = comparison["correct_change"]
        limit = comparison["p95_latency_seconds_limit"]
        p95_change = comparison["p95_latency_seconds_change"]
        lines.append(f"- {label} · Median EX change **{change:+d}/50** · Median P95 change **{p95_change:+.2f} s**"
                     f" · Majority-case losses {comparison['lost']} · Gains {comparison['gained']}")
        if change < -_MAX_ACCURACY_DROP:
            failures.append(f"Median EX falls by more than {_MAX_ACCURACY_DROP} questions against {label.lower()}.")
        if comparison["p95_regression"]:
            failures.append(f"Median P95 exceeds the {limit:.2f} s limit against {label.lower()}.")
    if dialogue:
        lines.append(f"- Multi-turn scenarios **{dialogue.get('passed')}/{dialogue.get('scenarios')}**"
                     f" · Model calls {dialogue.get('model_calls')}")
        for case in dialogue.get("cases", []):
            if not case["passed"]:
                check = case["turns"][-1]
                actual = check.get("actual", check.get("error"))
                lines.extend((f"  - `{case['case']}`", f"    - Expected `{json.dumps(check['expected'], ensure_ascii=False)}`",
                              f"    - Observed `{json.dumps(actual, ensure_ascii=False)}`"))
    if not (dialogue.get("transport") == "live" and dialogue.get("scenarios") == dialogue.get("passed") == 30
            and type(dialogue.get("model_calls")) is int and dialogue["model_calls"] > 0):
        failures.append("Golden gate requires 30/30 multi-turn scenarios and live model calls.")
    else:
        dialogue_p95 = conversation_p95(dialogue.get("cases", []))
        bird["conversation_p95_latency_seconds"] = dialogue_p95
        if dialogue_p95 is None:
            failures.append("Missing turn latency in live multi-turn evidence.")
        else:
            lines.append(f"- Conversation turn P95 **{dialogue_p95:.2f} s**")
            if dialogue_p95 > _CONVERSATION_P95_TARGET_SECONDS:
                failures.append(f"Conversation turn P95 exceeds the {_CONVERSATION_P95_TARGET_SECONDS:g} s target.")
    lines.extend(f"- **Failed** · {failure}" for failure in failures)
    bird["gate_failures"] = failures
    write_json(args.output_dir / "bird.json", bird)
    report = "\n".join(lines) + "\n"
    (args.output_dir / "summary.md").write_text(report, encoding="utf-8")
    print(report)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
