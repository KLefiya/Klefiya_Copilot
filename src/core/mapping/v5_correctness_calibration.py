from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from src.core.contracts.loader import PROJECT_ROOT, load_migration_contract
from src.core.hashing import canonical_json_content_sha256
from src.core.mapping.benchmark import benchmark_run_specs, load_benchmark
from src.core.mapping.models import MappingCandidate
from src.core.mapping.profiler import profile_source_csv
from src.core.mapping.resource_context import resource_context_for_index
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, EmbeddingBackend, _basis, _status, load_embedding_backend
from src.core.mapping.scorer_v2 import score_all_candidates_v2
from src.core.mapping.scorer_v3 import score_all_candidates_v3
from src.core.mapping.scorer_v4 import score_all_candidates_v4
from src.core.mapping.scorer_v5 import score_all_candidates_v5
from src.core.mapping.target_index import build_target_field_index


EXPERIMENT_ID = "schema_matching_v5_correctness_calibration_v1"
EXISTING_POLICY_ID = "existing_v5_policy"
SCORE_ONLY_ID = "score_only_calibrator"
MULTIFEATURE_ID = "multifeature_calibrator"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/experiments/schema_matching_v5_correctness_calibration_v1"
BENCHMARK_PATHS = (
    PROJECT_ROOT / "data/benchmarks/schema_matching_v1.json",
    PROJECT_ROOT / "data/benchmarks/schema_matching_public_dev_v1.json",
)
CONTRACT_FAMILY_ORDER = (
    "bank_account",
    "generic_customer",
    "item_item_price",
    "sales_order_fulfillment",
    "supplier_reference",
)
TARGET_PRECISIONS = (0.90, 0.95)
SCORE_ONLY_FEATURE_ORDER = (
    "top1_v5_score",
    "v5_score_margin_top1_top2",
)
MULTIFEATURE_ORDER = (
    "top1_v5_score",
    "v5_score_margin_top1_top2",
    "top1_semantic_score",
    "semantic_score_margin_top1_top2",
    "top1_baseline_score",
    "top1_lexical_overlap",
    "top1_fuzzy_score",
    "top1_value_pattern_score",
    "top1_resource_context_score",
    "top1_identifier_adjusted_score",
    "top1_type_gate",
    "top1_v5_top1_eligible",
    "eligible_candidate_count",
    "candidate_count",
)
FORBIDDEN_FEATURE_TOKENS = (
    "source",
    "target",
    "path",
    "name",
    "text",
    "scenario",
    "benchmark",
    "contract",
    "family",
    "case",
    "ground",
    "truth",
    "expected",
    "label",
    "id",
)
LEARNING_RATE = 0.12
L2_PENALTY = 0.05
EPOCHS = 900


@dataclass(frozen=True)
class CalibrationCase:
    scenario_id: str
    contract_family: str
    case_id: str
    source_field: str
    expected_targets: tuple[str, ...]
    expected_no_target: bool
    top1_target: str
    top3_targets: tuple[str, ...]
    top1_status: str
    existing_v5_accept: bool
    label: int
    features: dict[str, float]

    @property
    def target_bearing(self) -> bool:
        return bool(self.expected_targets)

    def public_record(self, *, probability: float | None = None, accepted: bool | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "scenario_id": self.scenario_id,
            "contract_family": self.contract_family,
            "case_id": self.case_id,
            "target_bearing": self.target_bearing,
            "expected_target_count": len(self.expected_targets),
            "top1_correct": bool(self.label),
            "existing_v5_status": self.top1_status,
            "existing_v5_accept": self.existing_v5_accept,
        }
        if probability is not None:
            body["probability"] = round(float(probability), 8)
        if accepted is not None:
            body["accepted"] = bool(accepted)
        return body


def feature_schema() -> dict[str, Any]:
    features = {
        "top1_v5_score": (
            "score on the V5-ranked top candidate",
            "Existing V5 candidate diagnostic `score` after precision-tiered identifier adjustment.",
            True,
        ),
        "v5_score_margin_top1_top2": (
            "V5 score gap between the V5-ranked first and second candidates",
            "Difference between the first two V5 candidate `score` values.",
            True,
        ),
        "top1_semantic_score": (
            "semantic score on the V5-ranked top candidate",
            "Existing base candidate `semantic_score` for the V5 Top-1 candidate.",
            True,
        ),
        "semantic_score_margin_top1_top2": (
            "semantic-score gap between the two strongest semantic candidates",
            "Difference between the largest and second-largest candidate `semantic_score` values.",
            True,
        ),
        "top1_baseline_score": (
            "baseline score on the V5-ranked top candidate",
            "Existing V2/V3/V4/V5 diagnostic `baseline_score` carried forward before calibration.",
            True,
        ),
        "top1_lexical_overlap": (
            "lexical overlap on the V5-ranked top candidate",
            "Existing numeric `lexical_overlap` diagnostic.",
            True,
        ),
        "top1_fuzzy_score": (
            "fuzzy score on the V5-ranked top candidate",
            "Existing numeric `fuzzy_score` diagnostic.",
            True,
        ),
        "top1_value_pattern_score": (
            "value-pattern score on the V5-ranked top candidate",
            "Existing V2 `value_pattern_score` diagnostic.",
            True,
        ),
        "top1_resource_context_score": (
            "resource-context score on the V5-ranked top candidate",
            "Existing V3 `resource_context_score` diagnostic.",
            True,
        ),
        "top1_identifier_adjusted_score": (
            "identifier-adjusted score on the V5-ranked top candidate",
            "Existing V5 `identifier_adjusted_score` diagnostic.",
            True,
        ),
        "top1_type_gate": (
            "type compatibility gate on the V5-ranked top candidate",
            "Existing numeric `type_gate` diagnostic.",
            True,
        ),
        "top1_v5_top1_eligible": (
            "whether the V5-ranked top candidate had identifier evidence eligibility",
            "Existing boolean `v5_top1_eligible` encoded as 0/1.",
            False,
        ),
        "eligible_candidate_count": (
            "count of candidates with V5 top-1 eligibility evidence",
            "Count of existing boolean `v5_top1_eligible` diagnostics across candidates.",
            True,
        ),
        "candidate_count": (
            "number of target candidates available for the source field",
            "Length of the V5 candidate list emitted for the case.",
            True,
        ),
    }
    return {
        "experiment_id": EXPERIMENT_ID,
        "score_only_feature_order": list(SCORE_ONLY_FEATURE_ORDER),
        "multifeature_feature_order": list(MULTIFEATURE_ORDER),
        "feature_count": len(MULTIFEATURE_ORDER),
        "features": [
            {
                "name": name,
                "source": source,
                "meaning": meaning,
                "requires_scaling": requires_scaling,
                "leakage_rationale": (
                    "Numeric or boolean scorer diagnostic available before labels are read; it does not include "
                    "raw source field text, target path text, scenario/family/case identifiers, expected targets, "
                    "ground truth counts, or label-derived values."
                ),
            }
            for name, (meaning, source, requires_scaling) in features.items()
        ],
        "forbidden_identity_or_ground_truth_inputs": list(FORBIDDEN_FEATURE_TOKENS),
        "ground_truth_derived_features": False,
    }


def load_development_cases() -> list[CalibrationCase]:
    benchmarks = [load_benchmark(path) for path in BENCHMARK_PATHS]
    return collect_calibration_cases(benchmarks)


def collect_calibration_cases(
    benchmarks: list[dict[str, Any]],
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
) -> list[CalibrationCase]:
    specs = [spec for benchmark in benchmarks for spec in benchmark_run_specs(benchmark)]
    case_lookup = _case_lookup(benchmarks)
    backend = embedding_backend or load_embedding_backend(model_name)
    cases: list[CalibrationCase] = []
    for spec in sorted(specs, key=lambda item: item["scenario_id"]):
        contract = load_migration_contract(_project_path(spec["contract_path"]), _project_path(spec["data_root_path"]))
        profiles, _source_meta = profile_source_csv(_project_path(spec["source_path"]))
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
            raw_case = case_lookup.get((spec["scenario_id"], profile.name))
            if raw_case is None:
                continue
            v4_candidates = score_all_candidates_v4(profile, v3_by_source[profile.name], targets)
            v5_candidates = score_all_candidates_v5(profile, v4_candidates, targets)
            cases.append(_calibration_case(spec["scenario_id"], spec["contract_path"], raw_case, v5_candidates))
    return sorted(cases, key=lambda item: item.case_id)


def fit_calibrator(samples: list[CalibrationCase], feature_order: tuple[str, ...]) -> dict[str, Any]:
    x_raw, y = _matrix(samples, feature_order)
    mean, scale, x = _fit_transform(x_raw)
    weights = np.zeros(x.shape[1], dtype=float)
    intercept = _initial_intercept(y)
    for _ in range(EPOCHS):
        logits = np.clip(x @ weights + intercept, -40.0, 40.0)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        error = probabilities - y
        weights -= LEARNING_RATE * ((x.T @ error) / len(y) + L2_PENALTY * weights)
        intercept -= LEARNING_RATE * float(np.mean(error))
    return {
        "model_type": "standard_scaler_l2_logistic_regression_json_v1",
        "feature_order": list(feature_order),
        "scaler_mean": _round_list(mean),
        "scaler_scale": _round_list(scale),
        "linear_coefficients": _round_list(weights),
        "intercept": round(float(intercept), 8),
        "training": {
            "case_count": len(samples),
            "positive_label_count": int(np.sum(y)),
            "negative_label_count": int(len(y) - np.sum(y)),
            "scaler_fit_scope": "provided_training_cases_only",
            "l2_penalty": L2_PENALTY,
            "epochs": EPOCHS,
            "learning_rate": LEARNING_RATE,
            "random_state": 0,
            "solver": "deterministic_batch_gradient_descent",
        },
    }


def predict_probability(model: dict[str, Any], sample: CalibrationCase | dict[str, float]) -> float:
    features = sample.features if isinstance(sample, CalibrationCase) else sample
    x = np.asarray([float(features[name]) for name in model["feature_order"]], dtype=float)
    mean = np.asarray(model["scaler_mean"], dtype=float)
    scale = np.asarray(model["scaler_scale"], dtype=float)
    weights = np.asarray(model["linear_coefficients"], dtype=float)
    logit = float(((x - mean) / scale) @ weights + float(model["intercept"]))
    return _sigmoid(logit)


def inner_oof_predictions(samples: list[CalibrationCase], feature_order: tuple[str, ...]) -> list[dict[str, Any]]:
    predictions: list[dict[str, Any]] = []
    families = sorted({sample.contract_family for sample in samples})
    if len(families) < 2:
        raise ValueError("At least two inner contract families are required")
    for held_out_family in families:
        fit_samples = [sample for sample in samples if sample.contract_family != held_out_family]
        held_out_samples = [sample for sample in samples if sample.contract_family == held_out_family]
        model = fit_calibrator(fit_samples, feature_order)
        for sample in held_out_samples:
            predictions.append(_prediction_record(sample, predict_probability(model, sample)))
    return sorted(predictions, key=lambda item: item["case_id"])


def select_threshold(predictions: list[dict[str, Any]], target_precision: float) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for threshold in sorted({float(item["probability"]) for item in predictions}, reverse=True):
        accepted = [item for item in predictions if float(item["probability"]) >= threshold]
        correct = sum(int(item["label"]) for item in accepted)
        precision = correct / len(accepted) if accepted else 0.0
        if accepted and precision >= target_precision:
            candidates.append(
                {
                    "threshold": round(threshold, 8),
                    "accepted_count": len(accepted),
                    "accepted_precision": round(precision, 8),
                }
            )
    if not candidates:
        return {
            "target_precision": target_precision,
            "threshold": None,
            "policy": "abstain_all",
            "accepted_count": 0,
            "accepted_precision": None,
            "selection_source": "inner_contract_family_oof_predictions",
        }
    best = max(candidates, key=lambda item: (item["accepted_count"], item["accepted_precision"], item["threshold"]))
    return {
        "target_precision": target_precision,
        "threshold": best["threshold"],
        "policy": "accept_probability_at_or_above_threshold",
        "accepted_count": best["accepted_count"],
        "accepted_precision": best["accepted_precision"],
        "selection_source": "inner_contract_family_oof_predictions",
    }


def nested_contract_family_evaluation(samples: list[CalibrationCase]) -> dict[str, Any]:
    actual_families = tuple(sorted({sample.contract_family for sample in samples}))
    if actual_families != CONTRACT_FAMILY_ORDER:
        raise ValueError(f"Unexpected contract families: {actual_families}")
    strategies = {
        SCORE_ONLY_ID: SCORE_ONLY_FEATURE_ORDER,
        MULTIFEATURE_ID: MULTIFEATURE_ORDER,
    }
    folds: list[dict[str, Any]] = []
    pooled_predictions = {strategy: [] for strategy in strategies}
    pooled_policy_predictions: dict[str, dict[str, list[dict[str, Any]]]] = {
        strategy: {_threshold_key(target): [] for target in TARGET_PRECISIONS}
        for strategy in strategies
    }
    for held_out_family in CONTRACT_FAMILY_ORDER:
        outer_train = [sample for sample in samples if sample.contract_family != held_out_family]
        outer_test = [sample for sample in samples if sample.contract_family == held_out_family]
        fold: dict[str, Any] = {
            "fold_id": f"leave_one_contract_family__{held_out_family}",
            "held_out_contract_family": held_out_family,
            "train_contract_families": sorted({sample.contract_family for sample in outer_train}),
            "held_out_case_count": len(outer_test),
            "train_case_count": len(outer_train),
            "outer_held_out_used_for_threshold_selection": False,
            "strategies": {},
        }
        for strategy, feature_order in strategies.items():
            inner_predictions = inner_oof_predictions(outer_train, feature_order)
            thresholds = {
                _threshold_key(target): select_threshold(inner_predictions, target)
                for target in TARGET_PRECISIONS
            }
            model = fit_calibrator(outer_train, feature_order)
            predictions = [_prediction_record(sample, predict_probability(model, sample)) for sample in outer_test]
            pooled_predictions[strategy].extend(predictions)
            for key, threshold in thresholds.items():
                pooled_policy_predictions[strategy][key].extend(
                    [
                        {**prediction, "accepted": _accepted_by_threshold(prediction, threshold["threshold"])}
                        for prediction in predictions
                    ]
                )
            fold["strategies"][strategy] = {
                "feature_order": list(feature_order),
                "inner_oof_case_count": len(inner_predictions),
                "inner_thresholds": thresholds,
                "held_out_probability_metrics": probability_metrics(predictions),
                "held_out_policy_metrics": {
                    key: selective_metrics(predictions, spec)
                    for key, spec in thresholds.items()
                },
            }
        folds.append(fold)
    existing_predictions = [_existing_policy_record(sample) for sample in samples]
    return {
        "experiment_id": EXPERIMENT_ID,
        "outer_cv": "leave_one_contract_family_out",
        "inner_cv": "leave_one_contract_family_out_within_outer_training_families",
        "outer_fold_count": len(folds),
        "target_precisions": list(TARGET_PRECISIONS),
        "leakage_controls": {
            "outer_held_out_used_for_training": False,
            "outer_held_out_used_for_scaling": False,
            "outer_held_out_used_for_threshold_selection": False,
            "threshold_source": "inner_oof_predictions_only",
            "field_level_random_split": False,
            "identity_features_used": False,
        },
        "folds": folds,
        "pooled": {
            EXISTING_POLICY_ID: {
                "probability_metrics": "not_applicable_existing_rule_policy",
                "policy_metrics": {
                    "current_suggested_auto_accept": selective_metrics(existing_predictions, {"policy": "existing_v5_suggested_status"})
                },
                "predictions": existing_predictions,
            },
            **{
                strategy: {
                    "probability_metrics": probability_metrics(predictions),
                    "policy_metrics": {
                        key: selective_metrics(policy_predictions, {"policy": "precomputed_fold_thresholds"})
                        for key, policy_predictions in pooled_policy_predictions[strategy].items()
                    },
                    "predictions": predictions,
                }
                for strategy in strategies
                for predictions in [pooled_predictions[strategy]]
            },
        },
    }


def development_model(samples: list[CalibrationCase]) -> dict[str, Any]:
    score_inner = inner_oof_predictions(samples, SCORE_ONLY_FEATURE_ORDER)
    multi_inner = inner_oof_predictions(samples, MULTIFEATURE_ORDER)
    score_model = fit_calibrator(samples, SCORE_ONLY_FEATURE_ORDER)
    multi_model = fit_calibrator(samples, MULTIFEATURE_ORDER)
    body = {
        "_meta": {
            "experiment_id": EXPERIMENT_ID,
            "production_promoted": False,
            "sealed_holdout_validated": False,
            "development_only": True,
            "model_format": "json",
            "pickle_used": False,
        },
        "models": {
            SCORE_ONLY_ID: {
                **score_model,
                "thresholds": {
                    _threshold_key(target): select_threshold(score_inner, target)
                    for target in TARGET_PRECISIONS
                },
            },
            MULTIFEATURE_ID: {
                **multi_model,
                "thresholds": {
                    _threshold_key(target): select_threshold(multi_inner, target)
                    for target in TARGET_PRECISIONS
                },
            },
        },
    }
    return _attach_run_info(body)


def comparison(results: dict[str, Any], samples: list[CalibrationCase]) -> dict[str, Any]:
    pooled = results["pooled"]
    labels = [sample.label for sample in samples]
    return _attach_run_info(
        {
            "experiment_id": EXPERIMENT_ID,
            "development_only": True,
            "production_promoted": False,
            "sealed_holdout_validated": False,
            "case_count": len(samples),
            "positive_label_count": sum(labels),
            "negative_label_count": len(labels) - sum(labels),
            "strategy_summary": {
                EXISTING_POLICY_ID: {
                    "probability_metrics": "not_applicable_existing_rule_policy",
                    "policy_metrics": pooled[EXISTING_POLICY_ID]["policy_metrics"],
                },
                SCORE_ONLY_ID: {
                    "probability_metrics": pooled[SCORE_ONLY_ID]["probability_metrics"],
                    "policy_metrics": pooled[SCORE_ONLY_ID]["policy_metrics"],
                },
                MULTIFEATURE_ID: {
                    "probability_metrics": pooled[MULTIFEATURE_ID]["probability_metrics"],
                    "policy_metrics": pooled[MULTIFEATURE_ID]["policy_metrics"],
                },
            },
            "conclusion": (
                "This experiment evaluates whether V5 Top-1 recommendations can be gated for auto-accept. "
                "Abstention means human review, not a no-target decision. Results are development-only."
            ),
        }
    )


def fold_manifest(results: dict[str, Any]) -> dict[str, Any]:
    return _attach_run_info(
        {
            "experiment_id": EXPERIMENT_ID,
            "contract_families": list(CONTRACT_FAMILY_ORDER),
            "outer_cv": results["outer_cv"],
            "inner_cv": results["inner_cv"],
            "leakage_controls": results["leakage_controls"],
            "folds": [
                {
                    "fold_id": fold["fold_id"],
                    "held_out_contract_family": fold["held_out_contract_family"],
                    "train_contract_families": fold["train_contract_families"],
                    "train_case_count": fold["train_case_count"],
                    "held_out_case_count": fold["held_out_case_count"],
                    "strategy_thresholds": {
                        strategy: strategy_body["inner_thresholds"]
                        for strategy, strategy_body in fold["strategies"].items()
                    },
                }
                for fold in results["folds"]
            ],
        }
    )


def outer_oof_audit_records(results: dict[str, Any]) -> list[dict[str, Any]]:
    score_predictions = _prediction_by_case(results, SCORE_ONLY_ID)
    multi_predictions = _prediction_by_case(results, MULTIFEATURE_ID)
    existing_predictions = _prediction_by_case(results, EXISTING_POLICY_ID)
    threshold_by_family = {
        fold["held_out_contract_family"]: {
            strategy: fold["strategies"][strategy]["inner_thresholds"]
            for strategy in (SCORE_ONLY_ID, MULTIFEATURE_ID)
        }
        for fold in results["folds"]
    }
    records: list[dict[str, Any]] = []
    for case_id in sorted(multi_predictions):
        multi = multi_predictions[case_id]
        score = score_predictions[case_id]
        existing = existing_predictions[case_id]
        family = str(multi["contract_family"])
        thresholds = threshold_by_family[family]
        record = {
            "case_id": case_id,
            "scenario_id": multi["scenario_id"],
            "contract_family": family,
            "outer_fold": f"leave_one_contract_family__{family}",
            "has_expected_target": bool(multi["target_bearing"]),
            "top1_correct": bool(multi["label"]),
            "existing_v5_accepted": bool(existing["accepted"]),
            "score_only_probability": round(float(score["probability"]), 8),
            "multifeature_probability": round(float(multi["probability"]), 8),
            "score_only_target_precision_90_threshold": thresholds[SCORE_ONLY_ID]["target_precision_90"]["threshold"],
            "score_only_target_precision_95_threshold": thresholds[SCORE_ONLY_ID]["target_precision_95"]["threshold"],
            "multifeature_target_precision_90_threshold": thresholds[MULTIFEATURE_ID]["target_precision_90"]["threshold"],
            "multifeature_target_precision_95_threshold": thresholds[MULTIFEATURE_ID]["target_precision_95"]["threshold"],
        }
        record.update(_accepted_decisions(record))
        records.append(record)
    return records


def build_failure_analysis_from_oof_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: str(item["case_id"]))
    return _attach_run_info(
        {
            "experiment_id": EXPERIMENT_ID,
            "repair_type": "deterministic post-processing repair",
            "privacy": (
                "Case ids are included for traceability; raw source values, source field names, "
                "target paths, target names, and expected target identities are not included."
            ),
            "case_count": len(ordered),
            "positive_label_count": sum(1 for item in ordered if item["top1_correct"]),
            "by_strategy_policy": {
                "existing_v5_policy": _failure_categories_for_policy(
                    ordered,
                    "existing_v5_accepted",
                    include_disagreement=False,
                ),
                "score_only_target_precision_90": _failure_categories_for_policy(
                    ordered,
                    "score_only_target_precision_90_accepted",
                    disagreement_left="score_only_target_precision_90_accepted",
                    disagreement_right="multifeature_target_precision_90_accepted",
                ),
                "score_only_target_precision_95": _failure_categories_for_policy(
                    ordered,
                    "score_only_target_precision_95_accepted",
                    disagreement_left="score_only_target_precision_95_accepted",
                    disagreement_right="multifeature_target_precision_95_accepted",
                ),
                "multifeature_target_precision_90": _failure_categories_for_policy(
                    ordered,
                    "multifeature_target_precision_90_accepted",
                    disagreement_left="score_only_target_precision_90_accepted",
                    disagreement_right="multifeature_target_precision_90_accepted",
                ),
                "multifeature_target_precision_95": _failure_categories_for_policy(
                    ordered,
                    "multifeature_target_precision_95_accepted",
                    disagreement_left="score_only_target_precision_95_accepted",
                    disagreement_right="multifeature_target_precision_95_accepted",
                ),
            },
            "contract_family_calibration_shift": _audit_calibration_shift(ordered),
        }
    )


def failure_analysis(results: dict[str, Any]) -> dict[str, Any]:
    return build_failure_analysis_from_oof_records(outer_oof_audit_records(results))


def reconciliation_from_oof_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: str(item["case_id"]))
    policies = {
        "existing_v5_policy": "existing_v5_accepted",
        "score_only_target_precision_90": "score_only_target_precision_90_accepted",
        "score_only_target_precision_95": "score_only_target_precision_95_accepted",
        "multifeature_target_precision_90": "multifeature_target_precision_90_accepted",
        "multifeature_target_precision_95": "multifeature_target_precision_95_accepted",
    }
    return {name: _reconciliation_counts(ordered, field) for name, field in policies.items()}


def run_experiment(output_dir: Path = DEFAULT_OUTPUT_DIR, *, model_name: str = DEFAULT_MODEL_NAME) -> dict[str, Any]:
    samples = load_development_cases_with_model(model_name)
    results = nested_contract_family_evaluation(samples)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "feature_schema.json", feature_schema())
    _write_json(output_dir / "fold_manifest.json", fold_manifest(results))
    _write_json(output_dir / "contract_family_out_results.json", _attach_run_info(_strip_predictions_for_results(results)))
    _write_json(output_dir / "comparison.json", comparison(results, samples))
    _write_json(output_dir / "outer_oof_predictions.json", _attach_run_info({"experiment_id": EXPERIMENT_ID, "records": outer_oof_audit_records(results)}))
    _write_json(output_dir / "failure_analysis.json", failure_analysis(results))
    _write_json(output_dir / "development_model.json", development_model(samples))
    (output_dir / "README.md").write_text(_readme(samples, results), encoding="utf-8", newline="\n")
    return {
        "experiment_id": EXPERIMENT_ID,
        "case_count": len(samples),
        "positive_label_count": sum(sample.label for sample in samples),
        "negative_label_count": sum(1 - sample.label for sample in samples),
        "feature_count": len(MULTIFEATURE_ORDER),
        "output_dir": output_dir.as_posix(),
        "pooled_policy_metrics": comparison(results, samples)["strategy_summary"],
    }


def load_development_cases_with_model(model_name: str = DEFAULT_MODEL_NAME) -> list[CalibrationCase]:
    benchmarks = [load_benchmark(path) for path in BENCHMARK_PATHS]
    return collect_calibration_cases(benchmarks, model_name=model_name)


def probability_metrics(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(item["label"]) for item in predictions]
    probabilities = [float(item["probability"]) for item in predictions]
    return {
        "brier_score": round(sum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / len(labels), 8),
        "log_loss": round(_log_loss(labels, probabilities), 8),
        "roc_auc": _roc_auc(labels, probabilities),
        "average_precision": _average_precision(labels, probabilities),
        "ece_5_bin": _ece(labels, probabilities),
        "aurc": _aurc(labels, probabilities),
    }


def selective_metrics(predictions: list[dict[str, Any]], threshold: dict[str, Any]) -> dict[str, Any]:
    accepted = [item for item in predictions if item.get("accepted", False)]
    if threshold.get("policy") not in {"existing_v5_suggested_status", "precomputed_fold_thresholds"}:
        accepted = [item for item in predictions if _accepted_by_threshold(item, threshold.get("threshold"))]
    accepted_ids = {item["case_id"] for item in accepted}
    correct = sum(int(item["label"]) for item in accepted)
    incorrect = len(accepted) - correct
    no_target_cases = [item for item in predictions if not item["target_bearing"]]
    target_bearing_cases = [item for item in predictions if item["target_bearing"]]
    no_target_accepted = [item for item in accepted if not item["target_bearing"]]
    wrong_target_accepted = [item for item in accepted if item["target_bearing"] and item["label"] == 0]
    return {
        "threshold": threshold.get("threshold"),
        "threshold_policy": threshold.get("policy"),
        "case_count": len(predictions),
        "accepted_count": len(accepted),
        "review_count": len(predictions) - len(accepted),
        "coverage": _safe(len(accepted), len(predictions)),
        "review_rate": _safe(len(predictions) - len(accepted), len(predictions)),
        "accepted_precision": _safe(correct, len(accepted)),
        "accepted_correct_count": correct,
        "accepted_incorrect_count": incorrect,
        "target_bearing_auto_map_recall": _safe(
            sum(1 for item in target_bearing_cases if item["case_id"] in accepted_ids and item["label"] == 1),
            len(target_bearing_cases),
        ),
        "no_target_accepted_count": len(no_target_accepted),
        "no_target_rejection_rate": _safe(
            sum(1 for item in no_target_cases if item["case_id"] not in accepted_ids),
            len(no_target_cases),
        ),
        "wrong_target_accepted_count": len(wrong_target_accepted),
    }


def _calibration_case(
    scenario_id: str,
    contract_path: str,
    raw_case: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> CalibrationCase:
    top1 = candidates[0]
    top1_target = str(top1["target"])
    expected_targets = tuple(str(target) for target in raw_case["expected_targets"])
    label = int(bool(expected_targets) and top1_target in expected_targets)
    status, _band, _reasons = _status(_candidate_from_dict(top1), _basis(_candidate_from_dict(top1)))
    return CalibrationCase(
        scenario_id=scenario_id,
        contract_family=contract_family(contract_path),
        case_id=str(raw_case["case_id"]),
        source_field=str(raw_case["source_field"]),
        expected_targets=expected_targets,
        expected_no_target=bool(raw_case["expected_no_target"]),
        top1_target=top1_target,
        top3_targets=tuple(str(candidate["target"]) for candidate in candidates[:3]),
        top1_status=status,
        existing_v5_accept=status == "suggested",
        label=label,
        features=_case_features(candidates),
    )


def _case_features(candidates: list[dict[str, Any]]) -> dict[str, float]:
    top1 = candidates[0]
    by_semantic = sorted(candidates, key=lambda item: (-float(item.get("semantic_score", 0.0)), str(item["target"])))
    semantic_second = by_semantic[1] if len(by_semantic) > 1 else by_semantic[0]
    values = {
        "top1_v5_score": _numeric(top1.get("score")),
        "v5_score_margin_top1_top2": _margin(candidates, "score"),
        "top1_semantic_score": _numeric(top1.get("semantic_score")),
        "semantic_score_margin_top1_top2": _numeric(by_semantic[0].get("semantic_score")) - _numeric(semantic_second.get("semantic_score")),
        "top1_baseline_score": _numeric(top1.get("baseline_score")),
        "top1_lexical_overlap": _numeric(top1.get("lexical_overlap")),
        "top1_fuzzy_score": _numeric(top1.get("fuzzy_score")),
        "top1_value_pattern_score": _numeric(top1.get("value_pattern_score")),
        "top1_resource_context_score": _numeric(top1.get("resource_context_score")),
        "top1_identifier_adjusted_score": _numeric(top1.get("identifier_adjusted_score")),
        "top1_type_gate": _numeric(top1.get("type_gate")),
        "top1_v5_top1_eligible": 1.0 if top1.get("v5_top1_eligible") else 0.0,
        "eligible_candidate_count": float(sum(1 for candidate in candidates if candidate.get("v5_top1_eligible"))),
        "candidate_count": float(len(candidates)),
    }
    return {name: round(float(values[name]), 8) for name in MULTIFEATURE_ORDER}


def _prediction_record(sample: CalibrationCase, probability: float) -> dict[str, Any]:
    return {
        **sample.public_record(probability=probability),
        "label": sample.label,
        "target_bearing": sample.target_bearing,
    }


def _existing_policy_record(sample: CalibrationCase) -> dict[str, Any]:
    return {
        **sample.public_record(accepted=sample.existing_v5_accept),
        "label": sample.label,
        "target_bearing": sample.target_bearing,
    }


def _strip_predictions_for_results(results: dict[str, Any]) -> dict[str, Any]:
    body = dict(results)
    body["pooled"] = {
        strategy: {key: value for key, value in strategy_body.items() if key != "predictions"}
        for strategy, strategy_body in results["pooled"].items()
    }
    return body


def _prediction_by_case(results: dict[str, Any], strategy: str) -> dict[str, dict[str, Any]]:
    return {str(item["case_id"]): item for item in results["pooled"][strategy]["predictions"]}


def _accepted_decisions(record: dict[str, Any]) -> dict[str, bool]:
    return {
        "score_only_target_precision_90_accepted": _probability_accepts(
            record["score_only_probability"],
            record["score_only_target_precision_90_threshold"],
        ),
        "score_only_target_precision_95_accepted": _probability_accepts(
            record["score_only_probability"],
            record["score_only_target_precision_95_threshold"],
        ),
        "multifeature_target_precision_90_accepted": _probability_accepts(
            record["multifeature_probability"],
            record["multifeature_target_precision_90_threshold"],
        ),
        "multifeature_target_precision_95_accepted": _probability_accepts(
            record["multifeature_probability"],
            record["multifeature_target_precision_95_threshold"],
        ),
    }


def _failure_categories_for_policy(
    records: list[dict[str, Any]],
    accepted_field: str,
    *,
    include_disagreement: bool = True,
    disagreement_left: str | None = None,
    disagreement_right: str | None = None,
) -> dict[str, Any]:
    wrong_target = [
        _audit_failure_record(item)
        for item in records
        if item["has_expected_target"] and not item["top1_correct"] and item[accepted_field]
    ]
    no_target = [
        _audit_failure_record(item)
        for item in records
        if not item["has_expected_target"] and item[accepted_field]
    ]
    correct_rejected = [
        _audit_failure_record(item)
        for item in records
        if item["top1_correct"] and not item[accepted_field]
    ]
    disagreement = []
    if include_disagreement and disagreement_left and disagreement_right:
        disagreement = [
            {
                "case_id": item["case_id"],
                "scenario_id": item["scenario_id"],
                "contract_family": item["contract_family"],
                "score_only_accept": bool(item[disagreement_left]),
                "multifeature_accept": bool(item[disagreement_right]),
                "top1_correct": bool(item["top1_correct"]),
                "has_expected_target": bool(item["has_expected_target"]),
            }
            for item in records
            if bool(item[disagreement_left]) != bool(item[disagreement_right])
        ]
    return {
        "confident_wrong_target_acceptance": wrong_target,
        "no_target_false_acceptance": no_target,
        "correct_prediction_unnecessarily_rejected": correct_rejected,
        "score_only_multifeature_disagreement": disagreement,
        "reconciliation": _reconciliation_counts(records, accepted_field),
    }


def _reconciliation_counts(records: list[dict[str, Any]], accepted_field: str) -> dict[str, Any]:
    total = len(records)
    positive = sum(1 for item in records if item["top1_correct"])
    accepted = [item for item in records if item[accepted_field]]
    correct_accepted = sum(1 for item in accepted if item["top1_correct"])
    incorrect_accepted = len(accepted) - correct_accepted
    no_target_accepted = sum(1 for item in accepted if not item["has_expected_target"])
    wrong_target_accepted = sum(1 for item in accepted if item["has_expected_target"] and not item["top1_correct"])
    correct_rejected = sum(1 for item in records if item["top1_correct"] and not item[accepted_field])
    rejected = total - len(accepted)
    return {
        "case_count": total,
        "positive_label_count": positive,
        "accepted_count": len(accepted),
        "rejected_count": rejected,
        "accepted_correct_count": correct_accepted,
        "accepted_incorrect_count": incorrect_accepted,
        "correct_prediction_accepted_count": correct_accepted,
        "correct_prediction_unnecessarily_rejected_count": correct_rejected,
        "no_target_accepted_count": no_target_accepted,
        "wrong_target_accepted_count": wrong_target_accepted,
        "accepted_balance_ok": correct_accepted + incorrect_accepted == len(accepted),
        "coverage_balance_ok": len(accepted) + rejected == total,
        "incorrect_breakdown_ok": no_target_accepted + wrong_target_accepted == incorrect_accepted,
        "correct_breakdown_ok": correct_accepted + correct_rejected == positive,
    }


def _audit_calibration_shift(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        SCORE_ONLY_ID: _probability_shift(records, "score_only_probability"),
        MULTIFEATURE_ID: _probability_shift(records, "multifeature_probability"),
    }


def _probability_shift(records: list[dict[str, Any]], probability_field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family in sorted({item["contract_family"] for item in records}):
        family_records = [item for item in records if item["contract_family"] == family]
        avg_probability = sum(float(item[probability_field]) for item in family_records) / len(family_records)
        observed_accuracy = sum(1 for item in family_records if item["top1_correct"]) / len(family_records)
        rows.append(
            {
                "contract_family": family,
                "case_count": len(family_records),
                "average_probability": round(avg_probability, 8),
                "observed_top1_accuracy": round(observed_accuracy, 8),
                "probability_minus_accuracy": round(avg_probability - observed_accuracy, 8),
            }
        )
    return rows


def _audit_failure_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": item["case_id"],
        "scenario_id": item["scenario_id"],
        "contract_family": item["contract_family"],
        "outer_fold": item["outer_fold"],
        "top1_correct": bool(item["top1_correct"]),
        "has_expected_target": bool(item["has_expected_target"]),
    }


def _probability_accepts(probability: float, threshold: float | None) -> bool:
    return threshold is not None and float(probability) >= float(threshold)


def _calibration_shift(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family in sorted({item["contract_family"] for item in predictions}):
        family_predictions = [item for item in predictions if item["contract_family"] == family]
        avg_probability = sum(float(item["probability"]) for item in family_predictions) / len(family_predictions)
        observed_accuracy = sum(int(item["label"]) for item in family_predictions) / len(family_predictions)
        rows.append(
            {
                "contract_family": family,
                "case_count": len(family_predictions),
                "average_probability": round(avg_probability, 8),
                "observed_top1_accuracy": round(observed_accuracy, 8),
                "probability_minus_accuracy": round(avg_probability - observed_accuracy, 8),
            }
        )
    return rows


def _failure_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": item["case_id"],
        "scenario_id": item["scenario_id"],
        "contract_family": item["contract_family"],
        "probability": item.get("probability"),
        "top1_correct": bool(item["label"]),
        "target_bearing": bool(item["target_bearing"]),
    }


def _matrix(samples: list[CalibrationCase], feature_order: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray([[float(sample.features[name]) for name in feature_order] for sample in samples], dtype=float)
    y = np.asarray([sample.label for sample in samples], dtype=float)
    if len(set(y.tolist())) < 2:
        raise ValueError("Calibration training needs positive and negative labels")
    return x, y


def _fit_transform(x_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = x_raw.mean(axis=0)
    scale = x_raw.std(axis=0)
    scale = np.where(scale == 0.0, 1.0, scale)
    return mean, scale, (x_raw - mean) / scale


def _initial_intercept(labels: np.ndarray) -> float:
    rate = min(0.999, max(0.001, float(labels.mean())))
    return math.log(rate / (1.0 - rate))


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
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return round(wins / (len(positives) * len(negatives)), 8)


def _average_precision(labels: list[int], probabilities: list[float]) -> float | None:
    positive_count = sum(labels)
    if positive_count == 0:
        return None
    hits = 0
    total = 0.0
    ranked = sorted(zip(probabilities, labels, strict=True), key=lambda item: (-item[0], -item[1]))
    for index, (_probability, label) in enumerate(ranked, start=1):
        if label:
            hits += 1
            total += hits / index
    return round(total / positive_count, 8)


def _ece(labels: list[int], probabilities: list[float]) -> dict[str, Any]:
    bins: list[dict[str, Any]] = []
    total = len(labels)
    ece = 0.0
    for index in range(5):
        low = index / 5
        high = (index + 1) / 5
        members = [
            (label, probability)
            for label, probability in zip(labels, probabilities, strict=True)
            if (low <= probability < high) or (index == 4 and low <= probability <= high)
        ]
        if members:
            avg_confidence = sum(probability for _label, probability in members) / len(members)
            accuracy = sum(label for label, _probability in members) / len(members)
            contribution = (len(members) / total) * abs(accuracy - avg_confidence)
        else:
            avg_confidence = None
            accuracy = None
            contribution = 0.0
        ece += contribution
        bins.append(
            {
                "bin": index + 1,
                "lower_inclusive": round(low, 1),
                "upper_exclusive": None if index == 4 else round(high, 1),
                "upper_inclusive": round(high, 1) if index == 4 else None,
                "count": len(members),
                "average_confidence": None if avg_confidence is None else round(avg_confidence, 8),
                "accuracy": None if accuracy is None else round(accuracy, 8),
            }
        )
    return {"scheme": "five_fixed_probability_bins_width_0.2", "value": round(ece, 8), "bins": bins}


def _aurc(labels: list[int], probabilities: list[float]) -> float:
    ranked = sorted(zip(probabilities, labels, strict=True), key=lambda item: -item[0])
    wrong = 0
    risks: list[float] = []
    for index, (_probability, label) in enumerate(ranked, start=1):
        wrong += int(label == 0)
        risks.append(wrong / index)
    return round(sum(risks) / len(risks), 8)


def _accepted_by_threshold(item: dict[str, Any], threshold: float | None) -> bool:
    return threshold is not None and float(item["probability"]) >= float(threshold)


def _margin(candidates: list[dict[str, Any]], key: str) -> float:
    if len(candidates) < 2:
        return 0.0
    return _numeric(candidates[0].get(key)) - _numeric(candidates[1].get(key))


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None:
        return 0.0
    return float(value)


def _candidate_from_dict(candidate: dict[str, Any]) -> MappingCandidate:
    return MappingCandidate(
        target=str(candidate["target"]),
        rank=int(candidate["rank"]),
        score=float(candidate["score"]),
        semantic_score=float(candidate["semantic_score"]),
        fuzzy_score=float(candidate["fuzzy_score"]),
        alias_hit=bool(candidate["alias_hit"]),
        alias_source=candidate["alias_source"],
        lexical_overlap=float(candidate["lexical_overlap"]),
        type_gate=float(candidate["type_gate"]),
        warnings=tuple(candidate["warnings"]),
    )


def _case_lookup(benchmarks: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (scenario["scenario_id"], case["source_field"]): case
        for benchmark in benchmarks
        for scenario in benchmark["scenarios"]
        for case in scenario["cases"]
    }


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


def _project_path(value: str) -> Path:
    return (PROJECT_ROOT / value).resolve()


def _round_list(values: np.ndarray) -> list[float]:
    return [round(float(value), 8) for value in values.tolist()]


def _safe(numerator: int | float, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / denominator, 8)


def _threshold_key(target_precision: float) -> str:
    return f"target_precision_{int(round(target_precision * 100))}"


def _attach_run_info(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "_run_info": {
            "content_sha256": canonical_json_content_sha256(body),
            "note": "_run_info is deterministic for V5 correctness calibration artifacts.",
        },
        **body,
    }


def _write_json(path: Path, body: dict[str, Any]) -> None:
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _readme(samples: list[CalibrationCase], results: dict[str, Any]) -> str:
    comparison_body = comparison(results, samples)
    multi_metrics = comparison_body["strategy_summary"][MULTIFEATURE_ID]["probability_metrics"]
    score_metrics = comparison_body["strategy_summary"][SCORE_ONLY_ID]["probability_metrics"]
    existing_policy = comparison_body["strategy_summary"][EXISTING_POLICY_ID]["policy_metrics"]["current_suggested_auto_accept"]
    multi_90 = comparison_body["strategy_summary"][MULTIFEATURE_ID]["policy_metrics"]["target_precision_90"]
    multi_95 = comparison_body["strategy_summary"][MULTIFEATURE_ID]["policy_metrics"]["target_precision_95"]
    return f"""# V5 Correctness Calibration v1

This experiment asks whether the current strongest ranker, `precision_tiered_v5`, can be wrapped with an interpretable Top-1 correctness gate. The gate predicts whether the V5 Top-1 recommendation is suitable for automatic acceptance. Rejected cases go to human review; rejection is not an automatic no-target decision. Multi-target full coverage is outside this Top-1 gate and remains a separate ranking/evaluation problem.

## Why Calibration

Pairwise LTR v1 did not beat V5 on the development corpus, so this run does not tune another ranker. Correctness calibration and ranking improvement are different questions: V5 keeps producing the ranking, while this model estimates whether the already-selected Top-1 should be accepted without review.

## Corpus

- Cases: {len(samples)}
- Positive Top-1 labels: {sum(sample.label for sample in samples)}
- Negative Top-1 labels: {sum(1 - sample.label for sample in samples)}
- Contract families: {", ".join(CONTRACT_FAMILY_ORDER)}
- Development-only corpus: synthetic five-scenario benchmark plus two public development benchmarks

The development benchmarks have already been used for iteration. These results cannot be treated as sealed final evidence, production generalization, or statistically significant improvement.

## Leakage Controls

- Features are numeric/boolean V5 ranking diagnostics only.
- No source field names, source text, target paths, target text, scenario id, benchmark id, contract family id, case id, expected target count, expected target list, or label-derived feature enters the training matrix.
- Outer folds leave out an entire contract family.
- Thresholds are selected only from inner contract-family out-of-fold probabilities.
- The held-out family is not used for training, scaling, threshold selection, regularization choice, probability binning, or failure handling.

## Results

- Score-only Brier: {score_metrics["brier_score"]}
- Multifeature Brier: {multi_metrics["brier_score"]}
- Existing V5 policy coverage: {existing_policy["coverage"]}, accepted precision: {existing_policy["accepted_precision"]}, accepted incorrect: {existing_policy["accepted_incorrect_count"]}
- Multifeature 90% policy coverage: {multi_90["coverage"]}, accepted precision: {multi_90["accepted_precision"]}, accepted incorrect: {multi_90["accepted_incorrect_count"]}
- Multifeature 95% policy coverage: {multi_95["coverage"]}, accepted precision: {multi_95["accepted_precision"]}, accepted incorrect: {multi_95["accepted_incorrect_count"]}

See `comparison.json`, `contract_family_out_results.json`, and `failure_analysis.json` for pooled counts, family-level shifts, and disagreements. `development_model.json` is JSON-only and explicitly marked `production_promoted: false`, `sealed_holdout_validated: false`, and `development_only: true`.

## Limitations

The sample has only five contract families and limited negative Top-1 cases. The estimates are useful for deciding whether future sealed holdout work is worth doing, but only an independent sealed holdout can support stronger generalization claims.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the development-only V5 correctness calibration experiment.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME)
    args = parser.parse_args(argv)
    summary = run_experiment(args.output_dir, model_name=args.model)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
