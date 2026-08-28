from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BENCHMARK_ID = "fdic_bankfind_locations_v1"
PROTOCOL_SHA = "9ee72d856ee331a6dae25328e2d117c645ec46e17497beccb1fb4acf8a59df5f"
FIXTURE_LOCK_SHA = "27ce0cde37ff8347211d8386a6d78fb212ddd81d6b5847496e40287ae3d2e2f1"
SOURCE_SAMPLE_SHA = "775dbdac1a7c02bad64b8e1f4af117e227b43bf85d3e6e15e26db10c3f915da1"
CALIBRATION_MODEL_SHA = "73b6597a8ee1fb81555c189bfd01aa0a39115251824ce4b1ce7737d5b1bf70b2"

EXPECTED_HEAD = "a41b2821a84df4f0b3524541b3a517323a8a4db7"
EXPECTED_SOURCE_COLUMNS = (
    "CERT",
    "UNINUM",
    "NAME",
    "OFFNAME",
    "CITY",
    "STNAME",
    "STALP",
    "ZIP",
    "COUNTY",
    "SERVTYPE",
    "SERVTYPE_DESC",
    "RUNDATE",
    "OFFDOM",
    "MAINOFF",
)
RANKING_SYSTEMS = ("baseline", "precision_tiered_v4", "precision_tiered_v5")
PRIMARY_SYSTEMS = ("existing_v5_policy", "score_only_calibrator_target_precision_95")
SECONDARY_SYSTEMS = (
    "baseline",
    "precision_tiered_v4",
    "precision_tiered_v5",
    "multifeature_calibrator_target_precision_95",
)
CALIBRATOR_SYSTEMS = (
    "score_only_calibrator_target_precision_95",
    "multifeature_calibrator_target_precision_95",
)


class FdicSealedEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SealedPaths:
    repo_root: Path
    registry_path: Path
    fixture_dir: Path
    protocol_path: Path
    fixture_lock_path: Path
    source_path: Path
    ground_truth_path: Path
    source_metadata_path: Path
    calibration_model_path: Path
    target_contract_path: Path
    target_data_root_path: Path
    attempt_marker_path: Path
    result_path: Path
    runner_path: Path


@dataclass(frozen=True)
class FrozenExpectations:
    protocol_sha: str
    fixture_lock_sha: str
    source_sample_sha: str
    calibration_model_sha: str
    branch: str = "main"


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    source_field: str
    expected_targets: tuple[str, ...]
    case_type: str
    top_candidates: tuple[dict[str, Any], ...]
    recommendation: str | None
    v5_status: str
    features: dict[str, float]
    existing_v5_accepted: bool

    @property
    def target_bearing(self) -> bool:
        return bool(self.expected_targets)

    @property
    def label(self) -> int:
        if not self.expected_targets:
            return 0
        if not self.top_candidates:
            return 0
        return int(str(self.top_candidates[0]["target"]) in self.expected_targets)


EvaluationCallback = Callable[[SealedPaths, dict[str, Any], dict[str, Any]], dict[str, Any]]
GitStatusProvider = Callable[[SealedPaths], dict[str, str]]


def default_paths() -> SealedPaths:
    fixture_dir = REPO_ROOT / "data/benchmarks/sealed/fdic_bankfind_locations_v1"
    return SealedPaths(
        repo_root=REPO_ROOT,
        registry_path=REPO_ROOT / "data/benchmarks/schema_matching_public_sealed_v1.json",
        fixture_dir=fixture_dir,
        protocol_path=fixture_dir / "protocol.json",
        fixture_lock_path=fixture_dir / "fixture_lock.json",
        source_path=fixture_dir / "source_sample.csv",
        ground_truth_path=fixture_dir / "ground_truth.json",
        source_metadata_path=fixture_dir / "source_metadata.json",
        calibration_model_path=REPO_ROOT / "data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json",
        target_contract_path=REPO_ROOT / "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml",
        target_data_root_path=REPO_ROOT / "data/benchmarks/fixtures/bank_account/target",
        attempt_marker_path=fixture_dir / "first_evaluation_attempt.json",
        result_path=fixture_dir / "first_evaluation.json",
        runner_path=Path(__file__).resolve(),
    )


def default_expectations() -> FrozenExpectations:
    return FrozenExpectations(
        protocol_sha=PROTOCOL_SHA,
        fixture_lock_sha=FIXTURE_LOCK_SHA,
        source_sample_sha=SOURCE_SAMPLE_SHA,
        calibration_model_sha=CALIBRATION_MODEL_SHA,
    )


def raw_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def repo_relative(paths: SealedPaths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise FdicSealedEvaluationError(f"path is outside repo root: {path}") from exc


def _require_sha(path: Path, expected: str, label: str) -> str:
    actual = raw_sha256(path)
    if actual != expected:
        raise FdicSealedEvaluationError(f"{label} SHA mismatch: expected {expected}, got {actual}")
    return actual


def _assert_no_output_artifacts(paths: SealedPaths) -> None:
    for label, path in {
        "result artifact": paths.result_path,
        "attempt marker": paths.attempt_marker_path,
        "temp result artifact": paths.result_path.with_name(f".{paths.result_path.name}.tmp"),
        "temp attempt marker": paths.attempt_marker_path.with_name(f".{paths.attempt_marker_path.name}.tmp"),
    }.items():
        if path.exists():
            raise FdicSealedEvaluationError(f"{label} already exists: {repo_relative(paths, path)}")


def validate_frozen_inputs(paths: SealedPaths, expectations: FrozenExpectations) -> dict[str, Any]:
    _assert_no_output_artifacts(paths)
    protocol_sha = _require_sha(paths.protocol_path, expectations.protocol_sha, "protocol")
    lock_sha = _require_sha(paths.fixture_lock_path, expectations.fixture_lock_sha, "fixture lock")
    source_sha = _require_sha(paths.source_path, expectations.source_sample_sha, "source sample")
    model_sha = _require_sha(paths.calibration_model_path, expectations.calibration_model_sha, "calibration development model")

    protocol = read_json(paths.protocol_path)
    fixture_lock = read_json(paths.fixture_lock_path)
    registry = read_json(paths.registry_path)
    ground_truth = read_json(paths.ground_truth_path)
    source_metadata = read_json(paths.source_metadata_path)
    model = read_json(paths.calibration_model_path)

    if protocol["benchmark_id"] != BENCHMARK_ID or fixture_lock["benchmark_id"] != BENCHMARK_ID:
        raise FdicSealedEvaluationError("benchmark_id mismatch in protocol or fixture lock")
    if registry["_meta"]["benchmark_id"] != "schema_matching_public_sealed_v1":
        raise FdicSealedEvaluationError("unexpected sealed registry id")
    scenario = registry["scenarios"][0]
    if scenario["split"] != "sealed_holdout":
        raise FdicSealedEvaluationError("sealed registry split must be sealed_holdout")
    if registry["_meta"]["first_evaluation_status"] != "not_run" or protocol["first_evaluation_status"] != "not_run":
        raise FdicSealedEvaluationError("first evaluation status must remain not_run")
    for key in ("result_artifact_path", "comparison_artifact_path", "failure_analysis_artifact_path"):
        if protocol[key] is not None or registry["_meta"][key] is not None:
            raise FdicSealedEvaluationError(f"{key} must remain null")

    locked = fixture_lock["raw_sha256"]
    lock_checks = {
        "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_sample.csv": source_sha,
        "data/benchmarks/sealed/fdic_bankfind_locations_v1/ground_truth.json": raw_sha256(paths.ground_truth_path),
        "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_metadata.json": raw_sha256(paths.source_metadata_path),
        "data/benchmarks/sealed/fdic_bankfind_locations_v1/protocol.json": protocol_sha,
        "data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json": model_sha,
        "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml": raw_sha256(paths.target_contract_path),
    }
    for relative_path, actual in lock_checks.items():
        if locked.get(relative_path) != actual:
            raise FdicSealedEvaluationError(f"fixture lock mismatch for {relative_path}")
    if protocol["target_contract"]["raw_sha256"] != raw_sha256(paths.target_contract_path):
        raise FdicSealedEvaluationError("target contract SHA mismatch in protocol")

    with paths.source_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if list(rows[0].keys()) != list(EXPECTED_SOURCE_COLUMNS):
        raise FdicSealedEvaluationError("source header changed")
    if len(rows) != 128 or any(len(row) != 14 for row in rows):
        raise FdicSealedEvaluationError("source sample shape must be 128 x 14")

    counts = ground_truth["counts"]
    if counts["case_count"] != 14:
        raise FdicSealedEvaluationError("case count must be 14")
    if counts["single_target_case_count"] != 3:
        raise FdicSealedEvaluationError("single-target case count must be 3")
    if counts["multi_target_case_count"] != 0:
        raise FdicSealedEvaluationError("multi-target case count must be 0")
    if counts["no_target_case_count"] != 11:
        raise FdicSealedEvaluationError("no-target case count must be 11")
    if counts["expected_target_link_count"] != 3:
        raise FdicSealedEvaluationError("expected target link count must be 3")
    if source_metadata["source_sample"]["raw_sha256"] != source_sha:
        raise FdicSealedEvaluationError("source metadata sample SHA mismatch")
    if model["_meta"]["production_promoted"] or not model["_meta"]["development_only"]:
        raise FdicSealedEvaluationError("calibration model must remain development-only")

    return {
        "protocol": protocol,
        "fixture_lock": fixture_lock,
        "registry": registry,
        "ground_truth": ground_truth,
        "source_metadata": source_metadata,
        "calibration_model": model,
        "validated_shas": {
            "protocol": protocol_sha,
            "fixture_lock": lock_sha,
            "source_sample": source_sha,
            "calibration_development_model": model_sha,
            "ground_truth": raw_sha256(paths.ground_truth_path),
            "source_metadata": raw_sha256(paths.source_metadata_path),
            "target_contract": raw_sha256(paths.target_contract_path),
        },
    }


def validate_imports_without_model_or_predictions() -> None:
    from src.core.mapping.benchmark import generate_candidate_reports

    if not callable(generate_candidate_reports):
        raise FdicSealedEvaluationError("canonical generate_candidate_reports import is not callable")


def validate_only(paths: SealedPaths, expectations: FrozenExpectations) -> dict[str, Any]:
    context = validate_frozen_inputs(paths, expectations)
    validate_imports_without_model_or_predictions()
    return {
        "status": "validated_without_model_or_predictions",
        "benchmark_id": BENCHMARK_ID,
        "first_evaluation_status": context["protocol"]["first_evaluation_status"],
        "source_case_count": context["ground_truth"]["counts"]["case_count"],
        "source_shape": {"rows": 128, "columns": 14},
        "candidate_roles": {
            "primary_decision_comparison": list(context["protocol"]["evaluation_roles"]["primary_decision_comparison"]),
            "secondary_diagnostics": list(context["protocol"]["evaluation_roles"]["secondary_diagnostics"]),
        },
        "result_absent": True,
        "attempt_marker_absent": True,
        "validated_shas": context["validated_shas"],
    }


def git_status(paths: SealedPaths) -> dict[str, str]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=paths.repo_root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    return {
        "branch": run("branch", "--show-current"),
        "head": run("rev-parse", "HEAD"),
        "status_short": run("status", "--short", "-uall"),
    }


def validate_execute_preconditions(
    paths: SealedPaths,
    expectations: FrozenExpectations,
    *,
    confirm_benchmark_id: str | None,
    confirm_protocol_sha: str | None,
    git_status_provider: GitStatusProvider = git_status,
) -> dict[str, Any]:
    if confirm_benchmark_id != BENCHMARK_ID:
        raise FdicSealedEvaluationError("execute requires --confirm-benchmark-id fdic_bankfind_locations_v1")
    if confirm_protocol_sha != expectations.protocol_sha:
        raise FdicSealedEvaluationError("execute requires the frozen protocol SHA confirmation")
    context = validate_frozen_inputs(paths, expectations)
    status = git_status_provider(paths)
    if status["branch"] != expectations.branch:
        raise FdicSealedEvaluationError("execute requires branch main")
    if status["status_short"]:
        raise FdicSealedEvaluationError("execute requires a clean git worktree")
    context["git"] = status
    return context


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd != -1:
            os.close(fd)


def write_json_atomic(path: Path, payload: dict[str, Any], *, forbidden_values: set[str] | None = None) -> None:
    if path.exists():
        raise FdicSealedEvaluationError("refusing to overwrite existing result artifact")
    temp_path = path.with_name(f".{path.name}.tmp")
    if temp_path.exists():
        raise FdicSealedEvaluationError("refusing to reuse existing temp result artifact")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    validate_result_schema(payload, artifact_text=text, forbidden_values=forbidden_values)
    try:
        temp_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def create_attempt_marker(paths: SealedPaths, context: dict[str, Any]) -> dict[str, Any]:
    marker = {
        "artifact_type": "fdic_bankfind_locations_first_evaluation_attempt",
        "benchmark_id": BENCHMARK_ID,
        "protocol_sha": context["validated_shas"]["protocol"],
        "source_sha": context["validated_shas"]["source_sample"],
        "ground_truth_sha": context["validated_shas"]["ground_truth"],
        "calibration_model_sha": context["validated_shas"]["calibration_development_model"],
        "runner_sha": raw_sha256(paths.runner_path),
        "git_head": context["git"]["head"],
        "status": "started",
    }
    _write_json_exclusive(paths.attempt_marker_path, marker)
    return marker


def execute_once(
    paths: SealedPaths,
    expectations: FrozenExpectations,
    *,
    confirm_benchmark_id: str | None,
    confirm_protocol_sha: str | None,
    evaluation_callback: EvaluationCallback,
    git_status_provider: GitStatusProvider = git_status,
) -> dict[str, Any]:
    context = validate_execute_preconditions(
        paths,
        expectations,
        confirm_benchmark_id=confirm_benchmark_id,
        confirm_protocol_sha=confirm_protocol_sha,
        git_status_provider=git_status_provider,
    )
    marker = create_attempt_marker(paths, context)
    artifact = evaluation_callback(paths, context, marker)
    write_json_atomic(paths.result_path, artifact, forbidden_values=source_values_to_exclude(paths.source_path))
    return artifact


def _safe(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / denominator, 8)


def _rank_of_expected(case: EvaluationCase) -> int | None:
    for candidate in case.top_candidates[:3]:
        if str(candidate["target"]) in case.expected_targets:
            return int(candidate["rank"])
    return None


def ranking_metrics(cases: list[EvaluationCase]) -> dict[str, Any]:
    single = [case for case in cases if case.case_type == "single_target"]
    multi = [case for case in cases if case.case_type == "multi_target"]
    no_target = [case for case in cases if case.case_type == "no_target"]
    target_links = sum(len(case.expected_targets) for case in cases)
    top1 = sum(1 for case in single if _rank_of_expected(case) == 1)
    links_at_1 = top1
    links_at_3 = sum(1 for case in cases for target in case.expected_targets if _candidate_rank(case, target) in {1, 2, 3})
    mrr_points = sum((1.0 / rank) for case in cases for target in case.expected_targets if (rank := _candidate_rank(case, target)))
    no_target_correct = sum(1 for case in no_target if case.recommendation is None)
    return {
        "single_target_top1_accuracy": _metric(top1, len(single)),
        "target_link_recall_at_1": _metric(links_at_1, target_links),
        "target_link_recall_at_3": _metric(links_at_3, target_links),
        "target_link_mrr": _metric(round(mrr_points, 8), target_links),
        "no_target_accuracy": _metric(no_target_correct, len(no_target)),
        "multi_target_full_coverage_at_3": _metric(0, len(multi)),
    }


def _candidate_rank(case: EvaluationCase, target: str) -> int | None:
    for candidate in case.top_candidates[:3]:
        if str(candidate["target"]) == target:
            return int(candidate["rank"])
    return None


def _metric(numerator: int | float, denominator: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "numerator": numerator,
        "denominator": denominator,
        "value": _safe(numerator, denominator),
    }
    if denominator == 0:
        body["status"] = "not_applicable"
    return body


def probability_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(item["label"]) for item in records]
    probabilities = [float(item["probability"]) for item in records]
    return {
        "brier_score": round(sum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / len(labels), 8),
        "log_loss": round(_log_loss(labels, probabilities), 8),
        "roc_auc": _roc_auc(labels, probabilities),
        "average_precision": _average_precision(labels, probabilities),
        "ece_5_bin": _ece(labels, probabilities),
        "aurc": _aurc(labels, probabilities),
    }


def selective_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [item for item in records if item["accepted"]]
    correct = sum(int(item["label"]) for item in accepted)
    target_bearing = [item for item in records if item["target_bearing"]]
    no_target = [item for item in records if not item["target_bearing"]]
    no_target_accepted = [item for item in accepted if not item["target_bearing"]]
    wrong_target_accepted = [item for item in accepted if item["target_bearing"] and not item["label"]]
    return {
        "case_count": len(records),
        "accepted_count": len(accepted),
        "review_count": len(records) - len(accepted),
        "coverage": _safe(len(accepted), len(records)),
        "review_rate": _safe(len(records) - len(accepted), len(records)),
        "accepted_precision": _safe(correct, len(accepted)),
        "accepted_correct_count": correct,
        "accepted_incorrect_count": len(accepted) - correct,
        "target_bearing_auto_map_recall": _safe(sum(1 for item in accepted if item["target_bearing"] and item["label"]), len(target_bearing)),
        "no_target_accepted_count": len(no_target_accepted),
        "no_target_rejection_rate": _safe(sum(1 for item in no_target if not item["accepted"]), len(no_target)),
        "wrong_target_accepted_count": len(wrong_target_accepted),
        "rejection_semantics": "human_review_not_no_target_prediction",
    }


def primary_decision_comparison(existing: dict[str, Any], score_only: dict[str, Any], limitations: dict[str, Any]) -> dict[str, Any]:
    supports = (
        (score_only["coverage"] or 0.0) > (existing["coverage"] or 0.0)
        and score_only["accepted_incorrect_count"] <= existing["accepted_incorrect_count"]
        and (score_only["accepted_precision"] or 0.0) >= (existing["accepted_precision"] or 0.0)
    )
    return {
        "primary_candidate": "score_only_calibrator_target_precision_95",
        "baseline_comparator": "existing_v5_policy",
        "coverage_delta": None if score_only["coverage"] is None or existing["coverage"] is None else round(score_only["coverage"] - existing["coverage"], 8),
        "accepted_error_delta": score_only["accepted_incorrect_count"] - existing["accepted_incorrect_count"],
        "no_target_false_acceptance_delta": score_only["no_target_accepted_count"] - existing["no_target_accepted_count"],
        "observed_result_supports_future_opt_in_integration_consideration": supports,
        "limitations": limitations,
        "post_unseal_tuning_performed": False,
        "production_promoted": False,
    }


def build_result_artifact(
    *,
    paths: SealedPaths,
    context: dict[str, Any],
    marker: dict[str, Any],
    ranking_cases_by_system: dict[str, list[EvaluationCase]],
) -> dict[str, Any]:
    protocol = context["protocol"]
    model = context["calibration_model"]
    v5_cases = ranking_cases_by_system["precision_tiered_v5"]
    score_only_records = _calibrator_records(v5_cases, model["models"]["score_only_calibrator"], "target_precision_95")
    multifeature_records = _calibrator_records(v5_cases, model["models"]["multifeature_calibrator"], "target_precision_95")
    existing_records = [_existing_policy_record(case) for case in v5_cases]
    existing_metrics = selective_metrics(existing_records)
    score_only_metrics = selective_metrics(score_only_records)
    return {
        "artifact_type": "fdic_bankfind_locations_first_evaluation",
        "schema_version": "1.0",
        "benchmark_id": BENCHMARK_ID,
        "protocol_id": protocol["protocol_id"],
        "first_evaluation_attempt_marker": repo_relative(paths, paths.attempt_marker_path),
        "attempt_marker_sha256": raw_sha256(paths.attempt_marker_path),
        "attempt_marker_status": marker["status"],
        "git_head": marker["git_head"],
        "frozen_input_shas": context["validated_shas"],
        "runner": {
            "path": repo_relative(paths, paths.runner_path),
            "raw_sha256": raw_sha256(paths.runner_path),
        },
        "candidate_roles": protocol["evaluation_roles"],
        "corpus_counts": {
            "case_count": 14,
            "target_bearing_case_count": 3,
            "single_target_case_count": 3,
            "multi_target_case_count": 0,
            "no_target_case_count": 11,
            "expected_target_link_count": 3,
        },
        "ranking_metrics": {
            system_id: ranking_metrics(cases)
            for system_id, cases in sorted(ranking_cases_by_system.items())
        },
        "calibration_metrics": {
            "score_only_calibrator_target_precision_95": probability_metrics(score_only_records),
            "multifeature_calibrator_target_precision_95": probability_metrics(multifeature_records),
        },
        "selective_policy_counts": {
            "existing_v5_policy": existing_metrics,
            "score_only_calibrator_target_precision_95": score_only_metrics,
            "multifeature_calibrator_target_precision_95": selective_metrics(multifeature_records),
        },
        "primary_decision_comparison": primary_decision_comparison(existing_metrics, score_only_metrics, protocol["limitations"]),
        "per_case_audit_records": _case_audit_records(v5_cases, score_only_records, multifeature_records),
        "failure_categories": _failure_categories(score_only_records),
        "preregistered_limitations": protocol["limitations"],
        "production_promoted": False,
        "post_unseal_tuning_performed": False,
    }


def _existing_policy_record(case: EvaluationCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "source_field": case.source_field,
        "label": case.label,
        "target_bearing": case.target_bearing,
        "accepted": case.existing_v5_accepted,
    }


def _calibrator_records(cases: list[EvaluationCase], model: dict[str, Any], threshold_key: str) -> list[dict[str, Any]]:
    threshold = model["thresholds"][threshold_key]["threshold"]
    return [
        {
            "case_id": case.case_id,
            "source_field": case.source_field,
            "label": case.label,
            "target_bearing": case.target_bearing,
            "probability": _predict_probability(model, case.features),
            "threshold": threshold,
            "accepted": _predict_probability(model, case.features) >= threshold,
        }
        for case in cases
    ]


def _case_audit_records(
    cases: list[EvaluationCase],
    score_only_records: list[dict[str, Any]],
    multifeature_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    score_by_case = {item["case_id"]: item for item in score_only_records}
    multi_by_case = {item["case_id"]: item for item in multifeature_records}
    rows = []
    for case in cases:
        score = score_by_case[case.case_id]
        multi = multi_by_case[case.case_id]
        rows.append(
            {
                "case_id": case.case_id,
                "source_field": case.source_field,
                "case_type": case.case_type,
                "expected_target_count": len(case.expected_targets),
                "top1_target": case.top_candidates[0]["target"] if case.top_candidates else None,
                "expected_rank": _rank_of_expected(case),
                "correctness_label": case.label,
                "existing_v5_accepted": case.existing_v5_accepted,
                "score_only_probability": score["probability"],
                "score_only_threshold": score["threshold"],
                "score_only_accepted": score["accepted"],
                "multifeature_probability": multi["probability"],
                "multifeature_threshold": multi["threshold"],
                "multifeature_accepted": multi["accepted"],
                "failure_category": _failure_category(case, score["accepted"]),
            }
        )
    return rows


def _failure_categories(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "confident_wrong_target_acceptance": [
            item["case_id"] for item in records if item["accepted"] and item["target_bearing"] and not item["label"]
        ],
        "no_target_false_acceptance": [
            item["case_id"] for item in records if item["accepted"] and not item["target_bearing"]
        ],
        "correct_prediction_unnecessarily_rejected": [
            item["case_id"] for item in records if not item["accepted"] and item["label"]
        ],
    }


def _failure_category(case: EvaluationCase, accepted: bool) -> str | None:
    if accepted and case.target_bearing and not case.label:
        return "confident_wrong_target_acceptance"
    if accepted and not case.target_bearing:
        return "no_target_false_acceptance"
    if not accepted and case.label:
        return "correct_prediction_unnecessarily_rejected"
    return None


def _predict_probability(model: dict[str, Any], features: dict[str, float]) -> float:
    total = float(model["intercept"])
    for name, mean, scale, coefficient in zip(
        model["feature_order"],
        model["scaler_mean"],
        model["scaler_scale"],
        model["linear_coefficients"],
        strict=True,
    ):
        total += ((float(features[name]) - float(mean)) / float(scale)) * float(coefficient)
    return round(_sigmoid(total), 8)


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, value))))


def _log_loss(labels: list[int], probabilities: list[float]) -> float:
    eps = 1e-12
    total = 0.0
    for label, probability in zip(labels, probabilities, strict=True):
        p = min(1.0 - eps, max(eps, probability))
        total += -(label * math.log(p) + (1 - label) * math.log(1 - p))
    return total / len(labels)


def _roc_auc(labels: list[int], probabilities: list[float]) -> float | None:
    positives = [p for label, p in zip(labels, probabilities, strict=True) if label == 1]
    negatives = [p for label, p in zip(labels, probabilities, strict=True) if label == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    return round(wins / (len(positives) * len(negatives)), 8)


def _average_precision(labels: list[int], probabilities: list[float]) -> float | None:
    positive_count = sum(labels)
    if positive_count == 0:
        return None
    hits = 0
    total = 0.0
    for index, (_probability, label) in enumerate(sorted(zip(probabilities, labels, strict=True), reverse=True), start=1):
        if label:
            hits += 1
            total += hits / index
    return round(total / positive_count, 8)


def _ece(labels: list[int], probabilities: list[float]) -> dict[str, Any]:
    bins = []
    total = len(labels)
    value = 0.0
    for index in range(5):
        low = index / 5
        high = (index + 1) / 5
        members = [
            (label, probability)
            for label, probability in zip(labels, probabilities, strict=True)
            if (low <= probability < high) or (index == 4 and low <= probability <= high)
        ]
        if members:
            confidence = sum(probability for _label, probability in members) / len(members)
            accuracy = sum(label for label, _probability in members) / len(members)
            value += (len(members) / total) * abs(confidence - accuracy)
        else:
            confidence = None
            accuracy = None
        bins.append(
            {
                "bin": index + 1,
                "lower_inclusive": round(low, 1),
                "upper_exclusive": None if index == 4 else round(high, 1),
                "upper_inclusive": round(high, 1) if index == 4 else None,
                "count": len(members),
                "average_confidence": None if confidence is None else round(confidence, 8),
                "accuracy": None if accuracy is None else round(accuracy, 8),
            }
        )
    return {"scheme": "five_fixed_probability_bins_width_0.2", "value": round(value, 8), "bins": bins}


def _aurc(labels: list[int], probabilities: list[float]) -> float:
    wrong = 0
    risks = []
    for index, (_probability, label) in enumerate(sorted(zip(probabilities, labels, strict=True), reverse=True), start=1):
        wrong += int(label == 0)
        risks.append(wrong / index)
    return round(sum(risks) / len(risks), 8)


def source_values_to_exclude(source_path: Path) -> set[str]:
    with source_path.open(newline="", encoding="utf-8") as handle:
        return {
            value
            for row in csv.DictReader(handle)
            for value in row.values()
            if value and len(value) >= 8 and not value.replace("/", "").isdigit()
        }


def validate_result_schema(
    payload: dict[str, Any],
    *,
    artifact_text: str | None = None,
    forbidden_values: set[str] | None = None,
) -> None:
    required = {
        "artifact_type",
        "schema_version",
        "benchmark_id",
        "protocol_id",
        "frozen_input_shas",
        "candidate_roles",
        "corpus_counts",
        "ranking_metrics",
        "calibration_metrics",
        "selective_policy_counts",
        "primary_decision_comparison",
        "per_case_audit_records",
        "failure_categories",
        "preregistered_limitations",
        "production_promoted",
        "post_unseal_tuning_performed",
    }
    if not required.issubset(payload):
        raise FdicSealedEvaluationError("result artifact is missing required schema keys")
    if payload["benchmark_id"] != BENCHMARK_ID:
        raise FdicSealedEvaluationError("result benchmark_id mismatch")
    if payload["production_promoted"] or payload["post_unseal_tuning_performed"]:
        raise FdicSealedEvaluationError("result must not promote production or perform post-unseal tuning")
    text = artifact_text if artifact_text is not None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if "\\" in text and any(marker in text for marker in ("C:\\", "\\Users\\")):
        raise FdicSealedEvaluationError("result contains local absolute path material")
    for value in forbidden_values or set():
        if value in text:
            raise FdicSealedEvaluationError("result contains source row values")


def real_evaluation_callback(paths: SealedPaths, context: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    from src.core.mapping.benchmark import generate_candidate_reports
    from src.core.mapping.scorer import load_embedding_backend

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise FdicSealedEvaluationError("HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE must both be set to 1")
    backend = load_embedding_backend(local_files_only_model_name())
    scenario = context["registry"]["scenarios"][0]
    specs = [
        {
            "scenario_id": scenario["scenario_id"],
            "source_path": scenario["source_path"],
            "contract_path": scenario["contract_path"],
            "data_root_path": scenario["data_root_path"],
        }
    ]
    ranking_cases_by_system = {}
    reports = {}
    for system_id in RANKING_SYSTEMS:
        reports[system_id] = generate_candidate_reports(
            specs,
            model_name=local_files_only_model_name(),
            embedding_backend=backend,
            scorer_variant=system_id,
        )[scenario["scenario_id"]]
    ground_truth = context["ground_truth"]
    for system_id, report in reports.items():
        ranking_cases_by_system[system_id] = cases_from_report(ground_truth, report, is_v5=(system_id == "precision_tiered_v5"))
    return build_result_artifact(paths=paths, context=context, marker=marker, ranking_cases_by_system=ranking_cases_by_system)


def local_files_only_model_name() -> str:
    return "sentence-transformers/all-MiniLM-L6-v2"


def cases_from_report(ground_truth: dict[str, Any], report: dict[str, Any], *, is_v5: bool) -> list[EvaluationCase]:
    gt_by_field = {item["source_field"]: item for item in ground_truth["mappings"]}
    mappings_by_field = {item["source_field"]: item for item in report["mappings"]}
    cases = []
    for source_field in sorted(gt_by_field):
        gt = gt_by_field[source_field]
        mapping = mappings_by_field[source_field]
        top_candidates = tuple(_minimal_candidate(candidate) for candidate in mapping["top_candidates"][:3])
        case_type = "no_target" if not gt["expected_targets"] else "multi_target" if len(gt["expected_targets"]) > 1 else "single_target"
        features = _features_from_candidates(mapping["top_candidates"]) if is_v5 else _zero_features()
        cases.append(
            EvaluationCase(
                case_id=f"fdic_bankfind_locations__{source_field}",
                source_field=source_field,
                expected_targets=tuple(gt["expected_targets"]),
                case_type=case_type,
                top_candidates=top_candidates,
                recommendation=mapping.get("recommendation"),
                v5_status=mapping.get("status", "review"),
                features=features,
                existing_v5_accepted=is_v5 and mapping.get("status") == "suggested" and mapping.get("recommendation") is not None,
            )
        )
    return cases


def _minimal_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": int(candidate["rank"]),
        "score": round(float(candidate["score"]), 8),
        "target": str(candidate["target"]),
    }


def _features_from_candidates(candidates: list[dict[str, Any]]) -> dict[str, float]:
    top1 = candidates[0]
    by_semantic = sorted(candidates, key=lambda item: (-float(item.get("semantic_score", 0.0)), str(item["target"])))
    semantic_second = by_semantic[1] if len(by_semantic) > 1 else by_semantic[0]
    values = {
        "top1_v5_score": _number(top1.get("score")),
        "v5_score_margin_top1_top2": _number(candidates[0].get("score")) - _number(candidates[1].get("score")) if len(candidates) > 1 else 0.0,
        "top1_semantic_score": _number(top1.get("semantic_score")),
        "semantic_score_margin_top1_top2": _number(by_semantic[0].get("semantic_score")) - _number(semantic_second.get("semantic_score")),
        "top1_baseline_score": _number(top1.get("baseline_score")),
        "top1_lexical_overlap": _number(top1.get("lexical_overlap")),
        "top1_fuzzy_score": _number(top1.get("fuzzy_score")),
        "top1_value_pattern_score": _number(top1.get("value_pattern_score")),
        "top1_resource_context_score": _number(top1.get("resource_context_score")),
        "top1_identifier_adjusted_score": _number(top1.get("identifier_adjusted_score")),
        "top1_type_gate": _number(top1.get("type_gate")),
        "top1_v5_top1_eligible": 1.0 if top1.get("v5_top1_eligible") else 0.0,
        "eligible_candidate_count": float(sum(1 for candidate in candidates if candidate.get("v5_top1_eligible"))),
        "candidate_count": float(len(candidates)),
    }
    return {name: round(float(value), 8) for name, value in values.items()}


def _zero_features() -> dict[str, float]:
    return {
        "top1_v5_score": 0.0,
        "v5_score_margin_top1_top2": 0.0,
        "top1_semantic_score": 0.0,
        "semantic_score_margin_top1_top2": 0.0,
        "top1_baseline_score": 0.0,
        "top1_lexical_overlap": 0.0,
        "top1_fuzzy_score": 0.0,
        "top1_value_pattern_score": 0.0,
        "top1_resource_context_score": 0.0,
        "top1_identifier_adjusted_score": 0.0,
        "top1_type_gate": 0.0,
        "top1_v5_top1_eligible": 0.0,
        "eligible_candidate_count": 0.0,
        "candidate_count": 0.0,
    }


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate or execute the FDIC BankFind sealed benchmark once.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-benchmark-id")
    parser.add_argument("--confirm-protocol-sha")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = default_paths()
    expectations = default_expectations()
    try:
        if args.validate_only:
            if args.confirm_benchmark_id or args.confirm_protocol_sha:
                raise FdicSealedEvaluationError("confirm arguments are only accepted with --execute")
            summary = validate_only(paths, expectations)
        else:
            artifact = execute_once(
                paths,
                expectations,
                confirm_benchmark_id=args.confirm_benchmark_id,
                confirm_protocol_sha=args.confirm_protocol_sha,
                evaluation_callback=real_evaluation_callback,
            )
            summary = {
                "status": "completed",
                "result_path": repo_relative(paths, paths.result_path),
                "artifact_sha256": raw_sha256(paths.result_path),
                "primary_decision_comparison": artifact["primary_decision_comparison"],
            }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except FdicSealedEvaluationError as exc:
        print(json.dumps({"status": "failed_before_or_during_execution", "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
