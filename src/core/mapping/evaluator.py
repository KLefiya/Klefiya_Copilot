from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT
from src.tools.data_profile import attach_run_info


class MappingEvaluationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _load_json(path: Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    resolved.relative_to(PROJECT_ROOT)
    return json.loads(resolved.read_text(encoding="utf-8"))


def _project_relative(path: Path) -> str:
    return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()


def _metric(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _group_template() -> dict[str, Any]:
    return {
        "fields": 0,
        "top1_correct": 0,
        "top3_correct": 0,
        "no_target_correct": 0,
    }


def evaluate_mapping_report(
    mapping_report_path: Path,
    ground_truth_path: Path,
) -> dict[str, Any]:
    mapping_report = _load_json(mapping_report_path)
    ground_truth = _load_json(ground_truth_path)
    if mapping_report.get("_meta", {}).get("ground_truth_used") is not False:
        raise MappingEvaluationError(
            "mapping_report_boundary_error",
            "Mapping report must declare that ground truth was not used",
        )
    suggestions = {
        item["source_field"]: item
        for item in mapping_report.get("mappings", [])
    }
    truth_rows = ground_truth.get("mappings", [])
    evaluated_fields = len(truth_rows)
    mapped_truth = [item for item in truth_rows if item.get("expected_target") is not None]
    no_target_truth = [item for item in truth_rows if item.get("expected_target") is None]
    top1_correct = 0
    top3_correct = 0
    high_confidence_predictions = 0
    high_confidence_correct = 0
    no_target_correct = 0
    false_positive_no_target = 0
    groups = {
        "alias_backed": _group_template(),
        "semantic_only": _group_template(),
        "no_target": _group_template(),
    }
    details = []
    for truth in truth_rows:
        source = truth["source_field"]
        expected = truth.get("expected_target")
        group = truth["evaluation_group"]
        suggestion = suggestions.get(source)
        if suggestion is None:
            raise MappingEvaluationError("missing_source_field", f"Missing mapping for {source}")
        recommendation = suggestion.get("recommendation")
        top_targets = [candidate["target"] for candidate in suggestion.get("top_candidates", [])]
        is_top1 = expected is not None and recommendation == expected
        is_top3 = expected is not None and expected in top_targets[:3]
        is_no_target_ok = expected is None and suggestion.get("status") != "suggested"
        if is_top1:
            top1_correct += 1
        if is_top3:
            top3_correct += 1
        if suggestion.get("status") == "suggested":
            high_confidence_predictions += 1
            if recommendation == expected:
                high_confidence_correct += 1
        if is_no_target_ok:
            no_target_correct += 1
        if expected is None and suggestion.get("status") == "suggested":
            false_positive_no_target += 1
        groups[group]["fields"] += 1
        if is_top1:
            groups[group]["top1_correct"] += 1
        if is_top3:
            groups[group]["top3_correct"] += 1
        if is_no_target_ok:
            groups[group]["no_target_correct"] += 1
        details.append(
            {
                "source_field": source,
                "expected_target": expected,
                "recommendation": recommendation,
                "status": suggestion.get("status"),
                "evaluation_group": group,
                "top1_correct": is_top1,
                "top3_correct": is_top3,
                "no_target_correct": is_no_target_ok,
            }
        )
    for group in groups.values():
        group["top1_accuracy"] = _metric(group["top1_correct"], group["fields"])
        group["top3_recall"] = _metric(group["top3_correct"], group["fields"])
        group["no_target_accuracy"] = _metric(group["no_target_correct"], group["fields"])
    body = {
        "_meta": {
            "component": "contract_field_mapping_evaluation",
            "mapping_report": _project_relative(mapping_report_path),
            "mapping_report_sha256": mapping_report["_run_info"]["content_sha256"],
            "ground_truth": _project_relative(ground_truth_path),
            "ground_truth_used_for_evaluation_only": True,
            "mapping_report_ground_truth_used": mapping_report["_meta"]["ground_truth_used"],
            "synthetic": bool(ground_truth.get("_meta", {}).get("synthetic", False)),
        },
        "summary": {
            "evaluated_fields": evaluated_fields,
            "mapped_ground_truth_fields": len(mapped_truth),
            "no_target_ground_truth_fields": len(no_target_truth),
            "top1_correct": top1_correct,
            "top1_accuracy": _metric(top1_correct, len(mapped_truth)),
            "top3_correct": top3_correct,
            "top3_recall": _metric(top3_correct, len(mapped_truth)),
            "high_confidence_predictions": high_confidence_predictions,
            "high_confidence_correct": high_confidence_correct,
            "high_confidence_precision": _metric(high_confidence_correct, high_confidence_predictions),
            "no_target_correct": no_target_correct,
            "no_target_accuracy": _metric(no_target_correct, len(no_target_truth)),
            "false_positive_no_target": false_positive_no_target,
        },
        "by_evaluation_group": groups,
        "details": details,
    }
    return attach_run_info(body)


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
            previous_run.get("content_sha256") == next_run.get("content_sha256")
            and previous_run.get("generated_at")
        ):
            next_report["_run_info"]["generated_at"] = previous_run["generated_at"]
    if os.environ.get("CARVEOPS_OMIT_TIMESTAMP") == "1":
        next_report.get("_run_info", {}).pop("generated_at", None)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(next_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
