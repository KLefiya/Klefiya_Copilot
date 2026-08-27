from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.core.contracts.loader import PROJECT_ROOT, load_migration_contract
from src.core.hashing import canonical_json_content_sha256
from src.core.mapping.benchmark import benchmark_run_specs, load_benchmark
from src.core.mapping.resource_context import resource_context_for_index
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, EmbeddingBackend, load_embedding_backend
from src.core.mapping.scorer_v2 import score_all_candidates_v2
from src.core.mapping.scorer_v3 import score_all_candidates_v3
from src.core.mapping.scorer_v4 import score_all_candidates_v4
from src.core.mapping.scorer_v5 import score_all_candidates_v5
from src.core.mapping.target_index import build_target_field_index
from src.core.mapping.profiler import profile_source_csv


EXPERIMENT_ID = "schema_matching_pairwise_ltr_v1"
SCORER_ID = "learned_pairwise_linear_v1"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/experiments/schema_matching_pairwise_ltr_v1"
BENCHMARK_PATHS = (
    PROJECT_ROOT / "data/benchmarks/schema_matching_v1.json",
    PROJECT_ROOT / "data/benchmarks/schema_matching_public_dev_v1.json",
)
FEATURE_ORDER = (
    "semantic_score",
    "fuzzy_score",
    "lexical_overlap",
    "alias_hit",
    "type_gate",
    "baseline_score",
    "value_pattern_score",
    "resource_context_score",
    "resource_context_support",
    "v3_score",
    "diagnostic_bonus",
    "supportive_bonus",
    "top1_eligible",
    "v4_score",
    "identifier_bonus",
    "identifier_adjusted_score",
    "v5_top1_eligible",
)
FORBIDDEN_FEATURE_TOKENS = (
    "source_field",
    "target",
    "qualified",
    "scenario",
    "contract",
    "case",
    "expected",
    "rank",
)
LEARNING_RATE = 0.18
L2_PENALTY = 0.02
EPOCHS = 700


@dataclass(frozen=True)
class CaseRecord:
    scenario_id: str
    contract_family: str
    case_id: str
    source_field: str
    expected_targets: tuple[str, ...]
    expected_no_target: bool

    @property
    def target_bearing(self) -> bool:
        return bool(self.expected_targets)


@dataclass(frozen=True)
class CandidateFeatureRecord:
    scenario_id: str
    contract_family: str
    case_id: str
    source_field: str
    candidate_target: str
    expected_targets: tuple[str, ...]
    baseline_rank: int
    v5_rank: int
    features: tuple[float, ...]

    @property
    def is_positive(self) -> bool:
        return self.candidate_target in self.expected_targets


def feature_schema() -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "feature_order": list(FEATURE_ORDER),
        "feature_count": len(FEATURE_ORDER),
        "missing_numeric_default": 0.0,
        "boolean_encoding": {"false": 0.0, "true": 1.0},
        "forbidden_identity_inputs": list(FORBIDDEN_FEATURE_TOKENS),
        "ground_truth_derived_features": False,
        "notes": [
            "Features are existing scorer numeric signals generated before pair construction.",
            "Raw source field names, target qualified names, scenario ids, contract ids, case ids, candidate ranks, and labels are not model features.",
        ],
    }


def load_development_corpus() -> tuple[list[dict[str, Any]], list[CaseRecord]]:
    benchmarks = [load_benchmark(path) for path in BENCHMARK_PATHS]
    cases: list[CaseRecord] = []
    for benchmark in benchmarks:
        for scenario in benchmark["scenarios"]:
            family = contract_family(scenario["contract_path"])
            for case in scenario["cases"]:
                cases.append(
                    CaseRecord(
                        scenario_id=scenario["scenario_id"],
                        contract_family=family,
                        case_id=case["case_id"],
                        source_field=case["source_field"],
                        expected_targets=tuple(case["expected_targets"]),
                        expected_no_target=bool(case["expected_no_target"]),
                    )
                )
    return benchmarks, sorted(cases, key=lambda item: item.case_id)


def contract_family(contract_path: str) -> str:
    if "generic_customer" in contract_path:
        return "generic_customer"
    if "sap_supplier_reference" in contract_path:
        return "supplier_reference"
    if "erpnext_item_price" in contract_path:
        return "item_item_price"
    if "bank_account" in contract_path:
        return "bank_account"
    if "sales_order_fulfillment" in contract_path:
        return "sales_order_fulfillment"
    raise ValueError(f"Unknown contract family for {contract_path}")


def collect_candidate_features(
    benchmarks: list[dict[str, Any]],
    cases: list[CaseRecord],
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
) -> list[CandidateFeatureRecord]:
    run_specs: list[dict[str, str]] = []
    for benchmark in benchmarks:
        run_specs.extend(benchmark_run_specs(benchmark))
    case_by_source = {(case.scenario_id, case.source_field): case for case in cases}
    backend = embedding_backend or load_embedding_backend(model_name)
    records: list[CandidateFeatureRecord] = []
    for spec in sorted(run_specs, key=lambda item: item["scenario_id"]):
        contract = load_migration_contract(_project_path(spec["contract_path"]), _project_path(spec["data_root_path"]))
        source_path = _project_path(spec["source_path"])
        profiles, _source_meta = profile_source_csv(source_path)
        targets = build_target_field_index(contract)
        source_fields = [profile.name for profile in profiles]
        v2_by_source = {profile.name: score_all_candidates_v2(profile, targets, backend) for profile in profiles}
        v3_by_source = {
            profile.name: score_all_candidates_v3(
                profile,
                v2_by_source[profile.name],
                resource_context_for_index(source_fields, v2_by_source, index),
            )
            for index, profile in enumerate(profiles)
        }
        for profile in profiles:
            case = case_by_source.get((spec["scenario_id"], profile.name))
            if case is None:
                continue
            v4_candidates = score_all_candidates_v4(profile, v3_by_source[profile.name], targets)
            v5_candidates = score_all_candidates_v5(profile, v4_candidates, targets)
            baseline_rank_by_target = _rank_by_score(v5_candidates, "baseline_score")
            v5_rank_by_target = {str(candidate["target"]): index + 1 for index, candidate in enumerate(v5_candidates)}
            for candidate in v5_candidates:
                target = str(candidate["target"])
                records.append(
                    CandidateFeatureRecord(
                        scenario_id=case.scenario_id,
                        contract_family=case.contract_family,
                        case_id=case.case_id,
                        source_field=case.source_field,
                        candidate_target=target,
                        expected_targets=case.expected_targets,
                        baseline_rank=baseline_rank_by_target[target],
                        v5_rank=v5_rank_by_target[target],
                        features=tuple(_feature_vector(candidate)),
                    )
                )
    return sorted(records, key=lambda item: (item.case_id, item.candidate_target))


def build_pairwise_training_data(records: list[CandidateFeatureRecord]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    grouped = _group_by_case(records)
    vectors: list[np.ndarray] = []
    labels: list[int] = []
    positive_link_count = 0
    train_case_count = 0
    for case_id in sorted(grouped):
        items = grouped[case_id]
        positives = [item for item in items if item.is_positive]
        if not positives:
            continue
        train_case_count += 1
        positive_link_count += len(positives)
        positive_targets = {item.candidate_target for item in positives}
        negatives = [item for item in items if item.candidate_target not in positive_targets]
        for positive in positives:
            for negative in negatives:
                diff = np.asarray(positive.features, dtype=float) - np.asarray(negative.features, dtype=float)
                vectors.append(diff)
                labels.append(1)
                vectors.append(-diff)
                labels.append(0)
    if not vectors:
        raise ValueError("No pairwise training rows were built")
    return np.vstack(vectors), np.asarray(labels, dtype=float), {
        "pair_count": len(labels),
        "target_bearing_case_count": train_case_count,
        "positive_link_count": positive_link_count,
        "no_target_cases_excluded_from_pair_training": sum(1 for items in grouped.values() if not any(item.is_positive for item in items)),
    }


def fit_pairwise_ranker(records: list[CandidateFeatureRecord]) -> dict[str, Any]:
    x_raw, y, pair_summary = build_pairwise_training_data(records)
    mean, scale, x = _fit_transform(x_raw)
    weights = np.zeros(x.shape[1], dtype=float)
    intercept = 0.0
    for _ in range(EPOCHS):
        logits = x @ weights + intercept
        preds = 1.0 / (1.0 + np.exp(-np.clip(logits, -40.0, 40.0)))
        error = preds - y
        weights -= LEARNING_RATE * ((x.T @ error) / len(y) + L2_PENALTY * weights)
        intercept -= LEARNING_RATE * float(np.mean(error))
    return {
        "feature_order": list(FEATURE_ORDER),
        "scaler_mean": _round_list(mean),
        "scaler_scale": _round_list(scale),
        "coefficients": _round_list(weights),
        "intercept": round(float(intercept), 8),
        "training": {
            **pair_summary,
            "scaler_fit_scope": "train_fold_only",
            "ranker_fit_scope": "train_fold_only",
            "l2_penalty": L2_PENALTY,
            "epochs": EPOCHS,
            "learning_rate": LEARNING_RATE,
        },
    }


def evaluate_cross_validation(records: list[CandidateFeatureRecord], cases: list[CaseRecord], group_by: str) -> dict[str, Any]:
    if group_by not in {"scenario", "contract_family"}:
        raise ValueError(f"Unknown group_by: {group_by}")
    groups = sorted({case.scenario_id if group_by == "scenario" else case.contract_family for case in cases})
    folds: list[dict[str, Any]] = []
    pooled_case_predictions = {scorer: [] for scorer in ("baseline", "precision_tiered_v5", SCORER_ID)}
    for group in groups:
        heldout_cases = [
            case for case in cases if (case.scenario_id if group_by == "scenario" else case.contract_family) == group
        ]
        heldout_case_ids = {case.case_id for case in heldout_cases}
        train_records = [record for record in records if record.case_id not in heldout_case_ids]
        heldout_records = [record for record in records if record.case_id in heldout_case_ids]
        model = fit_pairwise_ranker(train_records)
        fold_predictions = _predict_for_methods(heldout_records, model)
        for scorer, predictions in fold_predictions.items():
            pooled_case_predictions[scorer].extend(predictions)
        folds.append(
            {
                "fold_id": f"leave_one_{group_by}__{group}",
                "held_out_group": group,
                "train_scenarios": sorted({record.scenario_id for record in train_records}),
                "held_out_scenarios": sorted({case.scenario_id for case in heldout_cases}),
                "train_contract_families": sorted({record.contract_family for record in train_records}),
                "held_out_contract_families": sorted({case.contract_family for case in heldout_cases}),
                "train_cases": len({record.case_id for record in train_records}),
                "held_out_cases": len(heldout_case_ids),
                "train_positive_links": sum(1 for record in train_records if record.is_positive),
                "held_out_positive_links": sum(len(case.expected_targets) for case in heldout_cases),
                "pair_count": model["training"]["pair_count"],
                "scaler_fit_scope": "train_fold_only",
                "ranker_fit_scope": "train_fold_only",
                "metrics": {scorer: metric_summary(predictions) for scorer, predictions in fold_predictions.items()},
            }
        )
    return {
        "experiment_id": EXPERIMENT_ID,
        "group_by": group_by,
        "fold_count": len(folds),
        "folds": folds,
        "pooled_metrics": {scorer: metric_summary(predictions) for scorer, predictions in pooled_case_predictions.items()},
        "pooled_case_results": pooled_case_predictions,
    }


def metric_summary(case_predictions: list[dict[str, Any]]) -> dict[str, Any]:
    positive_cases = [case for case in case_predictions if case["expected_targets"]]
    single = [case for case in positive_cases if len(case["expected_targets"]) == 1]
    multi = [case for case in positive_cases if len(case["expected_targets"]) > 1]
    expected_links = sum(len(case["expected_targets"]) for case in positive_cases)
    top1_correct = sum(1 for case in single if case["top_targets"][:1] == case["expected_targets"][:1])
    recall1 = 0
    recall3 = 0
    mrr = 0.0
    multi_full = 0
    missing_top3 = []
    for case in positive_cases:
        top_targets = case["top_targets"]
        found = [target for target in case["expected_targets"] if target in top_targets[:3]]
        recall1 += sum(1 for target in case["expected_targets"] if target in top_targets[:1])
        recall3 += len(found)
        for target in case["expected_targets"]:
            mrr += (1.0 / (top_targets.index(target) + 1)) if target in top_targets[:3] else 0.0
        if len(case["expected_targets"]) > 1 and len(found) == len(case["expected_targets"]):
            multi_full += 1
        if len(found) < len(case["expected_targets"]):
            missing_top3.append(
                {
                    "case_id": case["case_id"],
                    "scenario_id": case["scenario_id"],
                    "source_field": case["source_field"],
                    "missing_expected_targets": [target for target in case["expected_targets"] if target not in top_targets[:3]],
                }
            )
    return {
        "case_count": len(case_predictions),
        "target_bearing_case_count": len(positive_cases),
        "single_target_case_count": len(single),
        "multi_target_case_count": len(multi),
        "no_target_case_count": len(case_predictions) - len(positive_cases),
        "expected_target_link_count": expected_links,
        "single_target_top1_accuracy": _safe(top1_correct, len(single)),
        "target_link_recall_at_1": _safe(recall1, expected_links),
        "target_link_recall_at_3": _safe(recall3, expected_links),
        "target_link_mrr": _safe(mrr, expected_links),
        "multi_target_full_coverage_at_3": _safe(multi_full, len(multi)),
        "no_target_accuracy": "not_applicable",
        "abstention_learning": "not_implemented_in_ltr_v1",
        "expected_target_missing_from_top3_cases": missing_top3,
    }


def train_development_model(records: list[CandidateFeatureRecord], cases: list[CaseRecord]) -> dict[str, Any]:
    model = fit_pairwise_ranker(records)
    predictions = _predict_for_methods(records, model)[SCORER_ID]
    return _attach_ltr_run_info(
        {
            "_meta": {
                "experiment_id": EXPERIMENT_ID,
                "component": "schema_matching_development_pairwise_ltr_model",
                "production_model": False,
                "sealed_holdout_evaluated": False,
                "formal_evaluation": False,
                "development_corpus_only": True,
                "no_abstention_or_calibration": True,
            },
            "feature_order": model["feature_order"],
            "scaler_mean": model["scaler_mean"],
            "scaler_scale": model["scaler_scale"],
            "linear_coefficients": model["coefficients"],
            "intercept": model["intercept"],
            "training": {
                **model["training"],
                "scenario_count": len({case.scenario_id for case in cases}),
                "contract_family_count": len({case.contract_family for case in cases}),
                "case_count": len(cases),
                "expected_target_link_count": sum(len(case.expected_targets) for case in cases),
            },
            "development_metrics_on_training_corpus": metric_summary(predictions),
        }
    )


def run_experiment(output_dir: Path = DEFAULT_OUTPUT_DIR, *, model_name: str = DEFAULT_MODEL_NAME) -> dict[str, Any]:
    benchmarks, cases = load_development_corpus()
    records = collect_candidate_features(benchmarks, cases, model_name=model_name)
    scenario_cv = evaluate_cross_validation(records, cases, "scenario")
    contract_cv = evaluate_cross_validation(records, cases, "contract_family")
    model = train_development_model(records, cases)
    comparison = build_comparison(scenario_cv, contract_cv, model)
    manifest = build_fold_manifest(scenario_cv, contract_cv)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "feature_schema.json", feature_schema())
    _write_json(output_dir / "fold_manifest.json", manifest)
    _write_json(output_dir / "leave_one_scenario_out.json", scenario_cv)
    _write_json(output_dir / "leave_one_contract_out.json", contract_cv)
    _write_json(output_dir / "development_model.json", model)
    _write_json(output_dir / "comparison.json", comparison)
    (output_dir / "README.md").write_text(_readme(cases, records, scenario_cv, contract_cv), encoding="utf-8", newline="\n")
    return {
        "output_dir": output_dir.as_posix(),
        "case_count": len(cases),
        "expected_target_link_count": sum(len(case.expected_targets) for case in cases),
        "candidate_count": len(records),
        "scenario_fold_count": scenario_cv["fold_count"],
        "contract_family_fold_count": contract_cv["fold_count"],
        "scenario_pooled_metrics": scenario_cv["pooled_metrics"],
        "contract_pooled_metrics": contract_cv["pooled_metrics"],
    }


def build_fold_manifest(scenario_cv: dict[str, Any], contract_cv: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "leakage_controls": {
            "scenario_folds_are_grouped": True,
            "contract_family_folds_are_grouped": True,
            "scaler_fit_scope": "train_fold_only",
            "ranker_fit_scope": "train_fold_only",
            "held_out_labels_used_for_training": False,
            "field_level_random_split": False,
            "identity_features_used": False,
        },
        "leave_one_scenario_out": [_fold_manifest_entry(fold) for fold in scenario_cv["folds"]],
        "leave_one_contract_out": [_fold_manifest_entry(fold) for fold in contract_cv["folds"]],
    }


def build_comparison(scenario_cv: dict[str, Any], contract_cv: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "development_only": True,
        "production_model": False,
        "sealed_holdout_evaluated": False,
        "metrics": {
            "leave_one_scenario_out": scenario_cv["pooled_metrics"],
            "leave_one_contract_out": contract_cv["pooled_metrics"],
        },
        "case_deltas": {
            "leave_one_scenario_out": _case_deltas(scenario_cv["pooled_case_results"]),
            "leave_one_contract_out": _case_deltas(contract_cv["pooled_case_results"]),
        },
        "by_scenario": _group_metrics(scenario_cv["pooled_case_results"], "scenario_id"),
        "by_contract_family": _group_metrics(contract_cv["pooled_case_results"], "contract_family"),
        "coefficient_summary": coefficient_summary(model),
        "negative_result_summary": "LTR v1 is a deterministic prototype; no-target abstention is not learned.",
    }


def coefficient_summary(model: dict[str, Any]) -> dict[str, Any]:
    pairs = sorted(
        zip(model["feature_order"], model["linear_coefficients"], strict=True),
        key=lambda item: (-abs(float(item[1])), item[0]),
    )
    return {
        "top_absolute_coefficients": [{"feature": name, "coefficient": value} for name, value in pairs[:8]],
        "positive_coefficients": [{"feature": name, "coefficient": value} for name, value in pairs if float(value) > 0],
        "negative_coefficients": [{"feature": name, "coefficient": value} for name, value in pairs if float(value) < 0],
        "alias_or_heuristic_dependency_visible": any(
            name in {"alias_hit", "fuzzy_score", "baseline_score"} and abs(float(value)) >= 0.05
            for name, value in pairs
        ),
    }


def _feature_vector(candidate: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for name in FEATURE_ORDER:
        value = candidate.get(name, 0.0)
        if isinstance(value, bool):
            values.append(1.0 if value else 0.0)
        elif value is None:
            values.append(0.0)
        else:
            values.append(float(value))
    return values


def _predict_for_methods(records: list[CandidateFeatureRecord], model: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped = _group_by_case(records)
    out = {"baseline": [], "precision_tiered_v5": [], SCORER_ID: []}
    for case_id in sorted(grouped):
        items = grouped[case_id]
        out["baseline"].append(_case_prediction(items, sorted(items, key=lambda item: (item.baseline_rank, item.candidate_target))))
        out["precision_tiered_v5"].append(_case_prediction(items, sorted(items, key=lambda item: (item.v5_rank, item.candidate_target))))
        scored = sorted(
            ((model_score(model, item.features), item) for item in items),
            key=lambda item: (-item[0], item[1].candidate_target),
        )
        out[SCORER_ID].append(_case_prediction(items, [item for _score, item in scored]))
    return out


def model_score(model: dict[str, Any], features: tuple[float, ...] | list[float]) -> float:
    x = np.asarray(features, dtype=float)
    mean = np.asarray(model["scaler_mean"], dtype=float)
    scale = np.asarray(model["scaler_scale"], dtype=float)
    coefficient_key = "coefficients" if "coefficients" in model else "linear_coefficients"
    weights = np.asarray(model[coefficient_key], dtype=float)
    return float(((x - mean) / scale) @ weights + float(model["intercept"]))


def _case_prediction(items: list[CandidateFeatureRecord], ranked: list[CandidateFeatureRecord]) -> dict[str, Any]:
    first = items[0]
    return {
        "scenario_id": first.scenario_id,
        "contract_family": first.contract_family,
        "case_id": first.case_id,
        "source_field": first.source_field,
        "expected_targets": list(first.expected_targets),
        "top_targets": [item.candidate_target for item in ranked[:3]],
    }


def _case_deltas(pooled: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    baseline = {case["case_id"]: case for case in pooled["baseline"]}
    v5 = {case["case_id"]: case for case in pooled["precision_tiered_v5"]}
    ltr = {case["case_id"]: case for case in pooled[SCORER_ID]}
    positive_ids = sorted(case_id for case_id, case in ltr.items() if case["expected_targets"])
    return {
        "ltr_improved_vs_baseline": [case_id for case_id in positive_ids if _better(ltr[case_id], baseline[case_id])],
        "ltr_regressed_vs_baseline": [case_id for case_id in positive_ids if _better(baseline[case_id], ltr[case_id])],
        "ltr_improved_vs_v5": [case_id for case_id in positive_ids if _better(ltr[case_id], v5[case_id])],
        "ltr_regressed_vs_v5": [case_id for case_id in positive_ids if _better(v5[case_id], ltr[case_id])],
        "ltr_expected_target_missing_from_top3": [
            {
                "case_id": case_id,
                "scenario_id": ltr[case_id]["scenario_id"],
                "source_field": ltr[case_id]["source_field"],
                "missing_expected_targets": [
                    target for target in ltr[case_id]["expected_targets"] if target not in ltr[case_id]["top_targets"]
                ],
            }
            for case_id in positive_ids
            if any(target not in ltr[case_id]["top_targets"] for target in ltr[case_id]["expected_targets"])
        ],
    }


def _group_metrics(pooled: dict[str, list[dict[str, Any]]], group_key: str) -> dict[str, dict[str, Any]]:
    groups = sorted({case[group_key] for case in pooled["baseline"]})
    return {
        group: {
            scorer: metric_summary([case for case in cases if case[group_key] == group])
            for scorer, cases in pooled.items()
        }
        for group in groups
    }


def _better(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _case_quality(left) > _case_quality(right)


def _case_quality(case: dict[str, Any]) -> tuple[int, float]:
    expected = case["expected_targets"]
    if not expected:
        return (0, 0.0)
    found = [target for target in expected if target in case["top_targets"]]
    reciprocal = sum(1.0 / (case["top_targets"].index(target) + 1) for target in found)
    top1 = 1 if case["top_targets"][:1] == expected[:1] else 0
    return (len(found), reciprocal + top1)


def _group_by_case(records: list[CandidateFeatureRecord]) -> dict[str, list[CandidateFeatureRecord]]:
    grouped: dict[str, list[CandidateFeatureRecord]] = {}
    for record in records:
        grouped.setdefault(record.case_id, []).append(record)
    return grouped


def _rank_by_score(candidates: list[dict[str, Any]], score_key: str) -> dict[str, int]:
    ranked = sorted(candidates, key=lambda item: (-float(item.get(score_key, item.get("score", 0.0))), str(item["target"])))
    return {str(candidate["target"]): index + 1 for index, candidate in enumerate(ranked)}


def _fit_transform(x_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = x_raw.mean(axis=0)
    scale = x_raw.std(axis=0)
    scale = np.where(scale == 0.0, 1.0, scale)
    return mean, scale, (x_raw - mean) / scale


def _fold_manifest_entry(fold: dict[str, Any]) -> dict[str, Any]:
    return {
        "fold_id": fold["fold_id"],
        "train_scenarios": fold["train_scenarios"],
        "held_out_scenarios": fold["held_out_scenarios"],
        "train_contract_families": fold["train_contract_families"],
        "held_out_contract_families": fold["held_out_contract_families"],
        "train_cases": fold["train_cases"],
        "held_out_cases": fold["held_out_cases"],
        "train_positive_links": fold["train_positive_links"],
        "held_out_positive_links": fold["held_out_positive_links"],
        "pair_count": fold["pair_count"],
    }


def _safe(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / denominator, 4)


def _round_list(values: np.ndarray) -> list[float]:
    return [round(float(value), 8) for value in values.tolist()]


def _project_path(value: str) -> Path:
    return (PROJECT_ROOT / value).resolve()


def _write_json(path: Path, body: dict[str, Any]) -> None:
    path.write_text(json.dumps(_attach_ltr_run_info(body), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _attach_ltr_run_info(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "_run_info": {
            "content_sha256": canonical_json_content_sha256(body),
            "note": "_run_info is deterministic for LTR v1 artifacts; compare content_sha256 for report body reproducibility.",
        },
        **body,
    }


def _readme(
    cases: list[CaseRecord],
    records: list[CandidateFeatureRecord],
    scenario_cv: dict[str, Any],
    contract_cv: dict[str, Any],
) -> str:
    return f"""# Schema Matching Pairwise LTR v1

This is an interpretable development-only learning-to-rank prototype. It is not connected to runtime, backend, frontend, or any production scorer.

## Corpus

- Cases: {len(cases)}
- Expected target links: {sum(len(case.expected_targets) for case in cases)}
- Scenario groups: {len({case.scenario_id for case in cases})}
- Contract families: {len({case.contract_family for case in cases})}
- Candidate rows: {len(records)}

The corpus combines the five existing synthetic schema-matching scenarios with two public development scenarios. It is development evidence only, not a sealed holdout.

## Leakage Controls

- Candidate features are generated before pair construction.
- Ground truth is used only for pair construction and evaluation.
- Scaler and ranker are fit inside each train fold only.
- Folds are grouped by complete scenario or complete contract family.
- No raw source field names, target qualified names, scenario ids, contract ids, case ids, candidate ranks, or label-derived statistics are used as model features.
- No-target cases are retained in counts but LTR v1 does not learn abstention or calibration.

## Results

- Leave-one-scenario-out folds: {scenario_cv["fold_count"]}
- Leave-one-contract-family-out folds: {contract_cv["fold_count"]}

See `comparison.json`, `leave_one_scenario_out.json`, and `leave_one_contract_out.json` for pooled metrics and failure cases.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the development-only schema matching pairwise LTR experiment.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME)
    args = parser.parse_args(argv)
    summary = run_experiment(args.output_dir, model_name=args.model)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
