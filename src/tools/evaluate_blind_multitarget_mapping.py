from __future__ import annotations

import argparse
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.hashing import canonical_json_content_sha256, provenance_text_or_raw_sha256
from src.core.mapping.protocol_lock import (
    ProtocolLockError,
    validate_effective_protocol_lock,
    validate_historical_protocol_lock,
)
from src.tools.data_profile import attach_run_info


HIGH_CONFIDENCE = 0.7


class BlindEvaluationError(Exception):
    pass


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _safe_project_path(path: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise BlindEvaluationError(f"{label} must be inside the project") from exc
    if not resolved.exists():
        raise BlindEvaluationError(f"{label} does not exist: {path}")
    return resolved


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BlindEvaluationError(f"{label} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise BlindEvaluationError(f"{label} must be a JSON object")
    return data


def _top_targets(mapping: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for candidate in mapping.get("top_candidates", []):
        if isinstance(candidate, dict) and candidate.get("target") is not None:
            targets.append(str(candidate["target"]))
    return targets[:3]


def _truth_index(ground_truth: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mappings = ground_truth.get("mappings")
    if not isinstance(mappings, list):
        raise BlindEvaluationError("Ground truth must contain mappings")
    index: dict[str, dict[str, Any]] = {}
    for item in mappings:
        if not isinstance(item, dict):
            raise BlindEvaluationError("Ground truth mapping entries must be objects")
        source_field = str(item.get("source_field", ""))
        expected = item.get("expected_targets")
        if not source_field or not isinstance(expected, list):
            raise BlindEvaluationError("Ground truth entries require source_field and expected_targets")
        index[source_field] = {
            "expected_targets": [str(target) for target in expected],
            "evaluation_group": str(item.get("evaluation_group", "")),
        }
    return index


def _mapping_index(mapping_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mappings = mapping_report.get("mappings")
    if not isinstance(mappings, list):
        raise BlindEvaluationError("Mapping report must contain mappings")
    return {str(item["source_field"]): item for item in mappings if isinstance(item, dict) and "source_field" in item}


def _safe_accuracy(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _safe_precision(numerator: int, denominator: int) -> tuple[float | None, bool]:
    if not denominator:
        return None, False
    return round(numerator / denominator, 4), True


def _has_absolute_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def evaluate_blind_multitarget_mapping(
    mapping_report_path: Path,
    ground_truth_path: Path,
    protocol_lock_path: Path,
    protocol_amendment_path: Path | None = None,
) -> dict[str, Any]:
    mapping_path = _safe_project_path(mapping_report_path, "mapping_report")
    truth_path = _safe_project_path(ground_truth_path, "ground_truth")
    lock_path = _safe_project_path(protocol_lock_path, "protocol_lock")
    amendment_path = (
        _safe_project_path(protocol_amendment_path, "protocol_amendment")
        if protocol_amendment_path is not None
        else None
    )
    mapping_report = _load_json(mapping_path, "mapping_report")
    protocol_lock = _load_json(lock_path, "protocol_lock")
    try:
        protocol_validation = (
            validate_effective_protocol_lock(lock_path, amendment_path)
            if amendment_path is not None
            else validate_historical_protocol_lock(lock_path)
        )
    except ProtocolLockError as exc:
        raise BlindEvaluationError(f"protocol_lock_{exc.code}: {exc.message}") from exc
    ground_truth = _load_json(truth_path, "ground_truth")
    mapping_run_info = mapping_report.get("_run_info", {})
    mapping_content_sha256 = mapping_run_info.get("content_sha256") if isinstance(mapping_run_info, dict) else None
    if not isinstance(mapping_content_sha256, str) or not mapping_content_sha256:
        raise BlindEvaluationError("Mapping report is missing _run_info.content_sha256")
    if mapping_content_sha256 != canonical_json_content_sha256(mapping_report):
        raise BlindEvaluationError("Mapping report _run_info.content_sha256 does not match canonical JSON content")
    truth = _truth_index(ground_truth)
    mappings = _mapping_index(mapping_report)

    per_source: list[dict[str, Any]] = []
    source_top1_correct = 0
    target_bearing = 0
    expected_links = 0
    links_found = 0
    multi_fields = 0
    multi_full = 0
    no_target_fields = 0
    no_target_correct = 0
    false_positive_no_target = 0
    high_confidence = 0
    high_confidence_correct = 0

    for source_field in sorted(truth):
        expected_targets = truth[source_field]["expected_targets"]
        mapping = mappings.get(source_field, {})
        recommendation = mapping.get("recommendation")
        top_targets = _top_targets(mapping)
        found = [target for target in expected_targets if target in top_targets]
        source_correct = recommendation in expected_targets if expected_targets else recommendation is None
        confidence = float(mapping.get("confidence") or 0.0)
        if expected_targets:
            target_bearing += 1
            expected_links += len(expected_targets)
            links_found += len(found)
            if recommendation in expected_targets:
                source_top1_correct += 1
        else:
            no_target_fields += 1
            if recommendation is None:
                no_target_correct += 1
            else:
                false_positive_no_target += 1
        if len(expected_targets) > 1:
            multi_fields += 1
            if len(found) == len(expected_targets):
                multi_full += 1
        if confidence >= HIGH_CONFIDENCE:
            high_confidence += 1
            if source_correct:
                high_confidence_correct += 1
        per_source.append(
            {
                "source_field": source_field,
                "status": mapping.get("status"),
                "recommendation": recommendation,
                "confidence": confidence,
                "mapping_basis": mapping.get("mapping_basis"),
                "top_1": top_targets[0] if len(top_targets) > 0 else None,
                "top_2": top_targets[1] if len(top_targets) > 1 else None,
                "top_3": top_targets[2] if len(top_targets) > 2 else None,
                "expected_targets": expected_targets,
                "expected_targets_found_in_top3": found,
                "source_top1_correct": bool(source_correct) if expected_targets else None,
                "evaluation_group": truth[source_field]["evaluation_group"],
            }
        )

    status_counts: dict[str, int] = {}
    for item in mappings.values():
        status = str(item.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    high_confidence_precision, high_confidence_precision_defined = _safe_precision(
        high_confidence_correct,
        high_confidence,
    )

    body = {
        "_meta": {
            "component": "erpnext_blind_multitarget_mapping_evaluation",
            "mapping_report_path": _project_relative(mapping_path),
            "mapping_report_content_sha256": mapping_content_sha256,
            "mapping_report_hash_mode": "content_sha256",
            # Backward-compatible field name; value is mapping report _run_info.content_sha256.
            "mapping_report_sha256": mapping_content_sha256,
            "ground_truth_path": _project_relative(truth_path),
            "ground_truth_sha256": provenance_text_or_raw_sha256(truth_path),
            "protocol_lock_path": _project_relative(lock_path),
            "protocol_lock_sha256": provenance_text_or_raw_sha256(lock_path),
            "protocol_lock_hash_mode": "normalized_text_sha256_v1",
            "protocol_amendment_path": _project_relative(amendment_path) if amendment_path is not None else None,
            "protocol_amendment_content_sha256": (
                protocol_validation.get("protocol_amendment_content_sha256")
                if amendment_path is not None
                else None
            ),
            "effective_protocol_validation": protocol_validation["mode"],
            "ground_truth_used_for_evaluation_only": True,
        },
        "summary": {
            "source_fields": len(truth),
            "target_bearing_source_fields": target_bearing,
            "source_top1_correct": source_top1_correct,
            "source_top1_accuracy": _safe_accuracy(source_top1_correct, target_bearing),
            "expected_target_links": expected_links,
            "top3_target_links_found": links_found,
            "top3_target_link_recall": _safe_accuracy(links_found, expected_links),
            "multi_target_source_fields": multi_fields,
            "multi_target_full_top3_coverage_count": multi_full,
            "multi_target_full_top3_coverage": _safe_accuracy(multi_full, multi_fields),
            "no_target_source_fields": no_target_fields,
            "no_target_correct": no_target_correct,
            "no_target_accuracy": _safe_accuracy(no_target_correct, no_target_fields),
            "false_positive_no_target": false_positive_no_target,
            "high_confidence_predictions": high_confidence,
            "high_confidence_source_correct": high_confidence_correct,
            "high_confidence_source_precision": high_confidence_precision,
            "high_confidence_source_precision_defined": high_confidence_precision_defined,
        },
        "mapping_status_distribution": {key: status_counts[key] for key in sorted(status_counts)},
        "per_source_results": per_source,
        "capability_observations": {
            "mapping_report_supports_multiple_recommendations_per_source": False,
            "decision_loader_supports_one_source_to_multiple_targets": False,
            "package_builder_can_execute_one_to_many_source_mapping": False,
        },
        "blind_protocol_lock": {
            "engine_commit": protocol_lock.get("engine_commit"),
            "aliases_present": protocol_lock.get("aliases_present"),
            "locked_before_first_mapping": protocol_lock.get("locked_before_first_mapping"),
            "compatibility_amendment_applied": amendment_path is not None,
            "current_engine_claim": (
                "historical_lock"
                if amendment_path is None
                else "historical_lock_with_provenance_only_profiler_amendment"
            ),
        },
    }
    report = attach_run_info(body)
    if _has_absolute_path(report):
        raise BlindEvaluationError("Evaluation report contains an absolute path")
    return report


def write_evaluation_report(report: dict[str, Any], output_path: Path) -> None:
    output = Path(output_path).resolve()
    output.relative_to(PROJECT_ROOT)
    next_report = deepcopy(report)
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        previous_run = previous.get("_run_info", {})
        next_run = next_report.get("_run_info", {})
        if (
            previous_run.get("content_sha256")
            and previous_run.get("content_sha256") == next_run.get("content_sha256")
            and previous_run.get("generated_at")
        ):
            next_report["_run_info"]["generated_at"] = previous_run["generated_at"]
    if os.environ.get("CARVEOPS_OMIT_TIMESTAMP") == "1":
        next_report.get("_run_info", {}).pop("generated_at", None)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(next_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate blind multi-target contract mapping results.")
    parser.add_argument("--mapping-report", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--protocol-lock", required=True, type=Path)
    parser.add_argument("--protocol-amendment", type=Path, default=None)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_blind_multitarget_mapping(
            args.mapping_report,
            args.ground_truth,
            args.protocol_lock,
            args.protocol_amendment,
        )
        write_evaluation_report(report, args.output)
    except BlindEvaluationError as exc:
        print(f"Blind evaluation error: {exc}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"Source fields              : {summary['source_fields']}")
    print(f"Source top1 accuracy       : {summary['source_top1_accuracy']:.4f}")
    print(f"Top3 target-link recall    : {summary['top3_target_link_recall']:.4f}")
    print(f"Multi-target full coverage : {summary['multi_target_full_top3_coverage']:.4f}")
    print(f"No-target accuracy         : {summary['no_target_accuracy']:.4f}")
    precision = summary["high_confidence_source_precision"]
    precision_text = f"{precision:.4f}" if precision is not None else "undefined"
    print(f"High-confidence precision  : {precision_text}")
    print(f"Content SHA                : {report['_run_info']['content_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
