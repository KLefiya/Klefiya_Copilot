from __future__ import annotations

import csv
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import PROJECT_ROOT, load_migration_contract
from src.core.hashing import canonical_json_content_sha256, provenance_text_or_raw_sha256
from src.core.mapping.engine import suggest_contract_mappings
from src.core.mapping.resource_context import FEATURE_VERSION as RESOURCE_CONTEXT_FEATURE_VERSION
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, EmbeddingBackend
from src.core.mapping.scorer_v2 import SCORER_ID as VALUE_PATTERN_SCORER_ID
from src.core.mapping.scorer_v2 import VALUE_PATTERN_BONUS_WEIGHT, score_source_field_v2
from src.core.mapping.scorer_v3 import SCORER_ID as TARGET_CONTEXT_SCORER_ID
from src.core.mapping.scorer_v3 import metadata as target_context_metadata
from src.core.mapping.scorer_v3 import score_source_fields_v3
from src.core.mapping.scorer_v4 import SCORER_ID as PRECISION_TIERED_SCORER_ID
from src.core.mapping.scorer_v4 import metadata as precision_tiered_metadata
from src.core.mapping.scorer_v4 import suggest_contract_mappings_v4
from src.core.mapping.scorer_v5 import SCORER_ID as PRECISION_TIERED_V5_SCORER_ID
from src.core.mapping.scorer_v5 import metadata as precision_tiered_v5_metadata
from src.core.mapping.scorer_v5 import suggest_contract_mappings_v5
from src.core.mapping.target_index import build_target_field_index
from src.core.mapping.value_patterns import VALUE_PATTERN_FEATURE_VERSION
from src.tools.data_profile import attach_run_info


class SchemaMatchingBenchmarkError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


BASELINE_SCORER_ID = "baseline"
ALLOWED_BENCHMARK_IDS = {
    "schema_matching_v1",
    "schema_matching_public_dev_v1",
}
ALLOWED_SPLITS = {"train", "validation", "test", "development"}
ALLOWED_SCORERS = {
    BASELINE_SCORER_ID,
    VALUE_PATTERN_SCORER_ID,
    TARGET_CONTEXT_SCORER_ID,
    PRECISION_TIERED_SCORER_ID,
    PRECISION_TIERED_V5_SCORER_ID,
}


def _project_path(value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SchemaMatchingBenchmarkError("path_not_project_relative", f"{label} must be project-relative")
    resolved = (PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise SchemaMatchingBenchmarkError("path_outside_project", f"{label} must stay inside the project") from exc
    if not resolved.exists():
        raise SchemaMatchingBenchmarkError("path_missing", f"{label} does not exist: {value}")
    return resolved


def _benchmark_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise SchemaMatchingBenchmarkError("benchmark_missing", f"Benchmark fixture does not exist: {path}")
    return resolved


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _csv_header(path: Path) -> set[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        try:
            return set(next(csv.reader(handle)))
        except StopIteration as exc:
            raise SchemaMatchingBenchmarkError("empty_source", f"Source CSV is empty: {_project_relative(path)}") from exc


def _target_names(contract_path: Path, data_root_path: Path) -> set[str]:
    contract = load_migration_contract(contract_path, data_root_path)
    return {field.qualified_name for field in build_target_field_index(contract)}


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaMatchingBenchmarkError("invalid_benchmark_shape", f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaMatchingBenchmarkError("invalid_benchmark_shape", f"{label} must be a list")
    return value


def _validate_case(
    case: dict[str, Any],
    *,
    scenario_id: str,
    source_fields: set[str],
    target_fields: set[str],
    seen_case_ids: set[str],
) -> None:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise SchemaMatchingBenchmarkError("case_id_missing", f"{scenario_id} contains a case without case_id")
    if case_id in seen_case_ids:
        raise SchemaMatchingBenchmarkError("duplicate_case_id", f"Duplicate case_id: {case_id}")
    seen_case_ids.add(case_id)

    source_field = case.get("source_field")
    if source_field not in source_fields:
        raise SchemaMatchingBenchmarkError("unknown_source_field", f"Unknown source_field: {source_field}")

    expected_targets = case.get("expected_targets")
    if not isinstance(expected_targets, list) or not all(isinstance(item, str) for item in expected_targets):
        raise SchemaMatchingBenchmarkError("invalid_expected_targets", f"{case_id} expected_targets must be strings")
    unknown_targets = [target for target in expected_targets if target not in target_fields]
    if unknown_targets:
        raise SchemaMatchingBenchmarkError("unknown_target_field", f"{case_id} has unknown targets: {unknown_targets}")

    expected_no_target = case.get("expected_no_target")
    if not isinstance(expected_no_target, bool):
        raise SchemaMatchingBenchmarkError("invalid_expected_no_target", f"{case_id} expected_no_target must be boolean")
    if expected_no_target != (len(expected_targets) == 0):
        raise SchemaMatchingBenchmarkError("inconsistent_no_target_case", f"{case_id} no-target flag disagrees with targets")

    difficulty_tags = case.get("difficulty_tags")
    if not isinstance(difficulty_tags, list) or not all(isinstance(item, str) for item in difficulty_tags):
        raise SchemaMatchingBenchmarkError("invalid_difficulty_tags", f"{case_id} difficulty_tags must be strings")


def load_benchmark(path: Path) -> dict[str, Any]:
    resolved = _benchmark_path(path)
    try:
        benchmark = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaMatchingBenchmarkError("benchmark_json_error", "Benchmark fixture is not valid JSON") from exc
    benchmark = _require_mapping(benchmark, "benchmark")
    meta = _require_mapping(benchmark.get("_meta"), "_meta")
    benchmark_id = meta.get("benchmark_id")
    if benchmark_id not in ALLOWED_BENCHMARK_IDS:
        allowed = ", ".join(sorted(ALLOWED_BENCHMARK_IDS))
        raise SchemaMatchingBenchmarkError("unexpected_benchmark_id", f"Benchmark id must be one of: {allowed}")
    scenarios = _require_list(benchmark.get("scenarios"), "scenarios")
    seen_scenario_ids: set[str] = set()
    seen_case_ids: set[str] = set()
    for scenario_value in scenarios:
        scenario = _require_mapping(scenario_value, "scenario")
        scenario_id = scenario.get("scenario_id")
        split = scenario.get("split")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise SchemaMatchingBenchmarkError("scenario_id_missing", "Scenario is missing scenario_id")
        if scenario_id in seen_scenario_ids:
            raise SchemaMatchingBenchmarkError("duplicate_scenario_id", f"Duplicate scenario_id: {scenario_id}")
        seen_scenario_ids.add(scenario_id)
        if not isinstance(split, str) or not split:
            raise SchemaMatchingBenchmarkError("split_missing", f"{scenario_id} is missing split")
        if split not in ALLOWED_SPLITS:
            raise SchemaMatchingBenchmarkError("unknown_split", f"Unknown scenario split: {split}")
        source_path = _project_path(str(scenario.get("source_path", "")), "source_path")
        contract_path = _project_path(str(scenario.get("contract_path", "")), "contract_path")
        data_root_path = _project_path(str(scenario.get("data_root_path", "")), "data_root_path")
        _project_path(str(scenario.get("answer_source_path", "")), "answer_source_path")
        source_fields = _csv_header(source_path)
        target_fields = _target_names(contract_path, data_root_path)
        cases = _require_list(scenario.get("cases"), f"{scenario_id}.cases")
        for case_value in cases:
            _validate_case(
                _require_mapping(case_value, f"{scenario_id}.case"),
                scenario_id=scenario_id,
                source_fields=source_fields,
                target_fields=target_fields,
                seen_case_ids=seen_case_ids,
            )
    return benchmark


def benchmark_run_specs(benchmark: dict[str, Any]) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for scenario in sorted(benchmark["scenarios"], key=lambda item: item["scenario_id"]):
        specs.append(
            {
                "scenario_id": scenario["scenario_id"],
                "split": scenario["split"],
                "source_path": scenario["source_path"],
                "contract_path": scenario["contract_path"],
                "data_root_path": scenario["data_root_path"],
            }
        )
    return specs


def generate_candidate_reports(
    run_specs: list[dict[str, str]],
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
    scorer_variant: str = BASELINE_SCORER_ID,
) -> dict[str, dict[str, Any]]:
    if scorer_variant not in ALLOWED_SCORERS:
        raise SchemaMatchingBenchmarkError("unknown_scorer", f"Unknown scorer variant: {scorer_variant}")
    reports: dict[str, dict[str, Any]] = {}
    for spec in sorted(run_specs, key=lambda item: item["scenario_id"]):
        contract = load_migration_contract(
            _project_path(spec["contract_path"], "contract_path"),
            _project_path(spec["data_root_path"], "data_root_path"),
        )
        source_path = _project_path(spec["source_path"], "source_path")
        if scorer_variant == BASELINE_SCORER_ID:
            reports[spec["scenario_id"]] = suggest_contract_mappings(
                contract,
                source_path,
                model_name=model_name,
                embedding_backend=embedding_backend,
            )
        elif scorer_variant == VALUE_PATTERN_SCORER_ID:
            reports[spec["scenario_id"]] = _suggest_contract_mappings_v2(
                contract,
                source_path,
                model_name=model_name,
                embedding_backend=embedding_backend,
            )
        elif scorer_variant == TARGET_CONTEXT_SCORER_ID:
            reports[spec["scenario_id"]] = _suggest_contract_mappings_v3(
                contract,
                source_path,
                model_name=model_name,
                embedding_backend=embedding_backend,
            )
        elif scorer_variant == PRECISION_TIERED_SCORER_ID:
            reports[spec["scenario_id"]] = suggest_contract_mappings_v4(
                contract,
                source_path,
                model_name=model_name,
                embedding_backend=embedding_backend,
            )
        else:
            reports[spec["scenario_id"]] = suggest_contract_mappings_v5(
                contract,
                source_path,
                model_name=model_name,
                embedding_backend=embedding_backend,
            )
    return reports


def _safe_metric(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _rank_for(target: str, candidates: list[dict[str, Any]]) -> int | None:
    for index, candidate in enumerate(candidates[:3], start=1):
        if candidate.get("target") == target:
            return index
    return None


def _score_margin(candidates: list[dict[str, Any]]) -> float | None:
    if len(candidates) < 2:
        return None
    return round(float(candidates[0].get("score") or 0.0) - float(candidates[1].get("score") or 0.0), 4)


def _empty_counts() -> dict[str, int]:
    return {
        "scenario_count": 0,
        "case_count": 0,
        "single_target_case_count": 0,
        "multi_target_case_count": 0,
        "no_target_case_count": 0,
        "expected_target_link_count": 0,
        "single_target_top1_correct": 0,
        "target_links_found_at_1": 0,
        "target_links_found_at_3": 0,
        "target_link_mrr_points": 0.0,
        "no_target_correct": 0,
        "multi_target_full_coverage_at_3_count": 0,
    }


def _finalize_counts(counts: dict[str, int | float]) -> dict[str, Any]:
    expected_links = int(counts["expected_target_link_count"])
    return {
        "scenario_count": int(counts["scenario_count"]),
        "case_count": int(counts["case_count"]),
        "single_target_case_count": int(counts["single_target_case_count"]),
        "multi_target_case_count": int(counts["multi_target_case_count"]),
        "no_target_case_count": int(counts["no_target_case_count"]),
        "expected_target_link_count": expected_links,
        "single_target_top1_accuracy": _safe_metric(
            int(counts["single_target_top1_correct"]),
            int(counts["single_target_case_count"]),
        ),
        "target_link_recall_at_1": _safe_metric(int(counts["target_links_found_at_1"]), expected_links),
        "target_link_recall_at_3": _safe_metric(int(counts["target_links_found_at_3"]), expected_links),
        "target_link_mrr": _safe_metric(float(counts["target_link_mrr_points"]), expected_links),
        "no_target_accuracy": _safe_metric(int(counts["no_target_correct"]), int(counts["no_target_case_count"])),
        "multi_target_full_coverage_at_3": _safe_metric(
            int(counts["multi_target_full_coverage_at_3_count"]),
            int(counts["multi_target_case_count"]),
        ),
    }


def _classify_case(
    expected_targets: list[str],
    recommendation: str | None,
    candidates: list[dict[str, Any]],
) -> tuple[str, int | None, list[str]]:
    top1_target = candidates[0].get("target") if candidates else None
    ranks = [rank for target in expected_targets if (rank := _rank_for(target, candidates)) is not None]
    best_rank = min(ranks) if ranks else None
    found_targets = [target for target in expected_targets if _rank_for(target, candidates) is not None]
    if not expected_targets:
        return ("correct_no_target" if recommendation is None else "false_positive_for_no_target", None, [])
    if len(expected_targets) > 1:
        if len(found_targets) == len(expected_targets):
            return "multi_target_fully_covered", best_rank, found_targets
        if found_targets:
            return "multi_target_partially_covered", best_rank, found_targets
        return "multi_target_missing", None, found_targets
    if top1_target == expected_targets[0]:
        return "correct_top1", 1, found_targets
    if best_rank in {2, 3}:
        return "correct_but_ranked_2_or_3", best_rank, found_targets
    return "expected_target_missing_from_top3", None, found_targets


def evaluate_benchmark(
    benchmark: dict[str, Any],
    candidate_reports: dict[str, dict[str, Any]],
    *,
    scorer_variant: str = BASELINE_SCORER_ID,
) -> dict[str, Any]:
    if scorer_variant not in ALLOWED_SCORERS:
        raise SchemaMatchingBenchmarkError("unknown_scorer", f"Unknown scorer variant: {scorer_variant}")
    overall_counts: dict[str, int | float] = _empty_counts()
    scenario_results: list[dict[str, Any]] = []
    case_results: list[dict[str, Any]] = []
    count_by_type: Counter[str] = Counter()
    count_by_scenario: Counter[str] = Counter()
    count_by_difficulty: Counter[str] = Counter()

    for scenario in sorted(benchmark["scenarios"], key=lambda item: item["scenario_id"]):
        scenario_id = scenario["scenario_id"]
        report = candidate_reports.get(scenario_id)
        if report is None:
            raise SchemaMatchingBenchmarkError("missing_candidate_report", f"Missing report for {scenario_id}")
        suggestions = {item["source_field"]: item for item in report.get("mappings", [])}
        scenario_counts: dict[str, int | float] = _empty_counts()
        scenario_counts["scenario_count"] = 1
        overall_counts["scenario_count"] += 1
        for case in sorted(scenario["cases"], key=lambda item: item["case_id"]):
            source_field = case["source_field"]
            suggestion = suggestions.get(source_field)
            if suggestion is None:
                raise SchemaMatchingBenchmarkError("missing_source_field", f"Missing candidate for {source_field}")
            expected_targets = list(case["expected_targets"])
            candidates = list(suggestion.get("top_candidates", []))[:3]
            recommendation = suggestion.get("recommendation")
            error_type, best_rank, found_targets = _classify_case(expected_targets, recommendation, candidates)
            count_by_type[error_type] += 1
            count_by_scenario[scenario_id] += 1
            for tag in sorted(case["difficulty_tags"]):
                count_by_difficulty[tag] += 1

            expected_count = len(expected_targets)
            is_single = expected_count == 1
            is_multi = expected_count > 1
            is_no_target = expected_count == 0
            for counts in (overall_counts, scenario_counts):
                counts["case_count"] += 1
                counts["single_target_case_count"] += int(is_single)
                counts["multi_target_case_count"] += int(is_multi)
                counts["no_target_case_count"] += int(is_no_target)
                counts["expected_target_link_count"] += expected_count
                counts["single_target_top1_correct"] += int(is_single and candidates and candidates[0].get("target") == expected_targets[0])
                counts["no_target_correct"] += int(is_no_target and recommendation is None)
                counts["multi_target_full_coverage_at_3_count"] += int(is_multi and len(found_targets) == expected_count)
                for target in expected_targets:
                    rank = _rank_for(target, candidates)
                    counts["target_links_found_at_1"] += int(rank == 1)
                    counts["target_links_found_at_3"] += int(rank is not None and rank <= 3)
                    counts["target_link_mrr_points"] += (1 / rank) if rank else 0.0

            case_results.append(
                {
                    "scenario_id": scenario_id,
                    "case_id": case["case_id"],
                    "source_field": source_field,
                    "expected_targets": expected_targets,
                    "recommendation": recommendation,
                    "top_candidates": [
                        _candidate_result(
                            {
                            "target": candidate.get("target"),
                            "rank": candidate.get("rank"),
                            "score": candidate.get("score"),
                            "mapping_basis": suggestion.get("mapping_basis"),
                            },
                            candidate,
                        )
                        for candidate in candidates
                    ],
                    "best_expected_rank": best_rank,
                    "error_type": error_type,
                    "score_margin_top1_top2": _score_margin(candidates),
                    "difficulty_tags": list(case["difficulty_tags"]),
                }
            )
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "split": scenario["split"],
                "metrics": _finalize_counts(scenario_counts),
            }
        )

    meta = benchmark["_meta"]
    body = {
        "_meta": {
            "component": "schema_matching_benchmark_evaluation",
            "benchmark_id": meta["benchmark_id"],
            "synthetic_demo": bool(meta.get("synthetic_demo")),
            "ground_truth_runtime_boundary": meta.get("ground_truth_runtime_boundary"),
            "ground_truth_used_for_candidate_generation": False,
            "ground_truth_used_for_runtime": False,
            "ground_truth_used_for_evaluation": True,
            "scorer_variant": scorer_variant,
            "feature_version": _feature_version(scorer_variant),
            **_scorer_metadata_for_evaluation(scorer_variant),
            "source_reports": [
                {
                    "scenario_id": scenario["scenario_id"],
                    "source_path": scenario["source_path"],
                    "source_sha256": provenance_text_or_raw_sha256(_project_path(scenario["source_path"], "source_path")),
                    "contract_path": scenario["contract_path"],
                    "contract_sha256": provenance_text_or_raw_sha256(_project_path(scenario["contract_path"], "contract_path")),
                    "answer_source_path": scenario["answer_source_path"],
                    "answer_source_sha256": provenance_text_or_raw_sha256(_project_path(scenario["answer_source_path"], "answer_source_path")),
                }
                for scenario in sorted(benchmark["scenarios"], key=lambda item: item["scenario_id"])
            ],
        },
        "overall": _finalize_counts(overall_counts),
        "by_scenario": scenario_results,
        "case_results": case_results,
        "error_count_by_type": dict(sorted(count_by_type.items())),
        "error_count_by_scenario": dict(sorted(count_by_scenario.items())),
        "error_count_by_difficulty_tag": dict(sorted(count_by_difficulty.items())),
    }
    for flag in (
        "development_benchmark",
        "sealed_holdout",
        "repeated_evaluation_allowed",
        "not_evidence_of_unseen_generalization",
    ):
        if flag in meta:
            body["_meta"][flag] = bool(meta[flag])
    if "formal_evaluation" in meta:
        body["_meta"]["formal_evaluation"] = bool(meta["formal_evaluation"])
    return attach_content_sha(body)


def _candidate_result(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "base_score",
        "base_blended",
        "baseline_score",
        "pattern_adjusted_blended",
        "value_pattern_score",
        "value_pattern_support",
        "value_pattern_evidence",
        "v2_score",
        "resource_context_score",
        "resource_context_support",
        "resource_context_evidence",
        "v3_score",
        "interaction_adjusted_score",
        "activated_interactions",
        "interaction_evidence",
        "diagnostic_bonus",
        "supportive_bonus",
        "top1_eligible",
        "top1_selection_reason",
        "v4_score",
        "identifier_interaction_evidence",
        "identifier_bonus",
        "identifier_adjusted_score",
        "v5_top1_eligible",
        "v5_top1_selection_reason",
    ):
        if key in candidate:
            base[key] = candidate[key]
    return base


def _feature_version(scorer_variant: str) -> str | None:
    if scorer_variant == VALUE_PATTERN_SCORER_ID:
        return VALUE_PATTERN_FEATURE_VERSION
    if scorer_variant == TARGET_CONTEXT_SCORER_ID:
        return RESOURCE_CONTEXT_FEATURE_VERSION
    if scorer_variant == PRECISION_TIERED_SCORER_ID:
        return precision_tiered_metadata()["feature_version"]
    if scorer_variant == PRECISION_TIERED_V5_SCORER_ID:
        return precision_tiered_v5_metadata()["feature_version"]
    return None


def _scorer_metadata_for_evaluation(scorer_variant: str) -> dict[str, Any]:
    if scorer_variant == PRECISION_TIERED_SCORER_ID:
        metadata = precision_tiered_metadata()
        return {
            "scorer_id": scorer_variant,
            "interaction_configuration": metadata["interaction_configuration"],
            "ground_truth_used_for_concept_extraction": metadata["ground_truth_used_for_concept_extraction"],
            "ground_truth_used_for_interaction_activation": metadata["ground_truth_used_for_interaction_activation"],
            "ground_truth_used_for_tier_decision": metadata["ground_truth_used_for_tier_decision"],
            "ground_truth_used_for_scoring": metadata["ground_truth_used_for_scoring"],
        }
    if scorer_variant != PRECISION_TIERED_V5_SCORER_ID:
        return {}
    metadata = precision_tiered_v5_metadata()
    return {
        "scorer_id": scorer_variant,
        "formal_evaluation": True,
        "embedding_model": DEFAULT_MODEL_NAME,
        "parent_scorer": metadata["parent_scorer"],
        "production_scorer_modified": metadata["production_scorer_modified"],
        "synthetic_formal_scope": "Synthetic five-scenario schema matching benchmark; evaluation-only ground truth.",
        "algorithm_source_sha256": {
            "src/core/mapping/identifier_interactions.py": provenance_text_or_raw_sha256(PROJECT_ROOT / "src/core/mapping/identifier_interactions.py"),
            "src/core/mapping/scorer_v5.py": provenance_text_or_raw_sha256(PROJECT_ROOT / "src/core/mapping/scorer_v5.py"),
        },
        "interaction_configuration": metadata["interaction_configuration"],
        "ground_truth_used": metadata["ground_truth_used"],
        "ground_truth_used_for_candidate_generation": metadata["ground_truth_used_for_candidate_generation"],
        "ground_truth_used_for_concept_extraction": metadata["ground_truth_used_for_concept_extraction"],
        "ground_truth_used_for_interaction_activation": metadata["ground_truth_used_for_interaction_activation"],
        "ground_truth_used_for_scoring": metadata["ground_truth_used_for_scoring"],
    }


def _suggest_contract_mappings_v2(
    contract,
    source_path: Path,
    *,
    model_name: str,
    embedding_backend: EmbeddingBackend | None,
) -> dict[str, Any]:
    from src.core.mapping.profiler import profile_source_csv
    from src.core.mapping.scorer import load_embedding_backend

    profiles, source_meta = profile_source_csv(source_path)
    targets = build_target_field_index(contract)
    backend = embedding_backend or load_embedding_backend(model_name)
    mappings = [
        score_source_field_v2(profile, targets, backend)
        for profile in profiles
    ]
    body = {
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "adapter": contract.adapter,
            "domain": contract.domain,
            **source_meta,
            "target_field_count": len(targets),
            "embedding_model": model_name,
            "ground_truth_used": False,
            "scorer_variant": VALUE_PATTERN_SCORER_ID,
            "feature_version": VALUE_PATTERN_FEATURE_VERSION,
            "experimental": True,
            "production_scorer_modified": False,
            "historical_blind_protocol_claimed": False,
            "value_pattern_bonus_weight": VALUE_PATTERN_BONUS_WEIGHT,
        },
        "mappings": mappings,
    }
    return attach_run_info(body)


def _suggest_contract_mappings_v3(
    contract,
    source_path: Path,
    *,
    model_name: str,
    embedding_backend: EmbeddingBackend | None,
) -> dict[str, Any]:
    from src.core.mapping.profiler import profile_source_csv
    from src.core.mapping.scorer import load_embedding_backend

    profiles, source_meta = profile_source_csv(source_path)
    targets = build_target_field_index(contract)
    backend = embedding_backend or load_embedding_backend(model_name)
    mappings = score_source_fields_v3(profiles, targets, backend)
    body = {
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "adapter": contract.adapter,
            "domain": contract.domain,
            **source_meta,
            "target_field_count": len(targets),
            "embedding_model": model_name,
            **target_context_metadata(),
        },
        "mappings": mappings,
    }
    return attach_run_info(body)


def attach_content_sha(report: dict[str, Any]) -> dict[str, Any]:
    body = deepcopy(report)
    return {
        "_run_info": {
            "content_sha256": canonical_json_content_sha256(body),
            "note": "_run_info is excluded from semantic benchmark content comparisons.",
        },
        **body,
    }


def write_benchmark_report(report: dict[str, Any], output_path: Path) -> Path:
    output = Path(output_path).resolve()
    if output.exists() and output.is_dir():
        output = output / "schema_matching_benchmark_report.json"
    elif output.suffix == "":
        output.mkdir(parents=True, exist_ok=True)
        output = output / "schema_matching_benchmark_report.json"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return output
