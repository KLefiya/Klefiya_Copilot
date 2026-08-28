from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.core.contracts.loader import PROJECT_ROOT
from src.core.mapping import identifier_role_detection as detector


EXPERIMENT_ID = "identifier_role_reranker_v1"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/experiments/schema_matching_identifier_role_ranking_ablation_v1"
V5_RESULT_PATHS = (
    PROJECT_ROOT / "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json",
    PROJECT_ROOT / "data/benchmarks/development/combined_public_dev_v1/results/precision_tiered_v5.json",
)
ALPHA_GRID = (0.0, 0.01, 0.02, 0.05)
SYSTEMS = (
    "v5_reference",
    "v5_plus_heuristic_identifier_role",
    "v5_plus_learned_identifier_role",
    "v5_plus_oracle_identifier_role",
)
POSITIVE_TARGET_TOKENS = ("id", "identifier", "number", "num", "code", "key", "reference", "ref", "uuid", "guid", "iban", "bic")
NEGATIVE_TARGET_TOKENS = (
    "amount",
    "category",
    "country",
    "currency",
    "date",
    "description",
    "disabled",
    "flag",
    "group",
    "holder",
    "language",
    "method",
    "name",
    "price",
    "quantity",
    "rate",
    "status",
    "terms",
    "uom",
    "unit",
    "valid",
)
TARGET_ROLE_LABELS: dict[str, str] = {
    "bank_account.account_holder": "non_identifier",
    "bank_account.account_id": "identifier",
    "bank_account.account_number": "identifier",
    "bank_account.bank_key": "identifier",
    "bank_account.country_code": "non_identifier",
    "bank_account.currency_code": "non_identifier",
    "bank_account.iban": "identifier",
    "bank_account.primary_flag": "non_identifier",
    "bank_account.valid_from": "non_identifier",
    "bank_account.valid_to": "non_identifier",
    "bank_branch.bank_key": "identifier",
    "bank_branch.bank_name": "non_identifier",
    "bank_branch.bic": "identifier",
    "bank_branch.country_code": "non_identifier",
    "bank_branch.routing_number": "identifier",
    "customer.country": "non_identifier",
    "customer.customer_id": "identifier",
    "customer.customer_name": "non_identifier",
    "customer.email": "non_identifier",
    "customer.payment_terms": "ambiguous_excluded",
    "customer.phone": "non_identifier",
    "customer.tax_number": "identifier",
    "customer_bank.bank_id": "identifier",
    "customer_bank.currency": "non_identifier",
    "customer_bank.customer_id": "identifier",
    "customer_bank.iban": "identifier",
    "delivery_schedule.confirmed_quantity": "non_identifier",
    "delivery_schedule.confirmed_ship_date": "non_identifier",
    "delivery_schedule.fulfillment_status": "non_identifier",
    "delivery_schedule.line_number": "identifier",
    "delivery_schedule.requested_ship_date": "non_identifier",
    "delivery_schedule.sales_order_id": "identifier",
    "delivery_schedule.schedule_number": "identifier",
    "item.disabled": "non_identifier",
    "item.item_code": "identifier",
    "item.item_group": "non_identifier",
    "item.item_name": "non_identifier",
    "item.stock_uom": "non_identifier",
    "item_price.item_code": "identifier",
    "item_price.price_list": "ambiguous_excluded",
    "item_price.price_list_rate": "non_identifier",
    "item_price.uom": "non_identifier",
    "item_price.valid_from": "non_identifier",
    "item_price.valid_upto": "non_identifier",
    "sales_order_header.currency_code": "non_identifier",
    "sales_order_header.customer_id": "identifier",
    "sales_order_header.customer_purchase_order": "identifier",
    "sales_order_header.distribution_channel": "non_identifier",
    "sales_order_header.order_date": "non_identifier",
    "sales_order_header.order_status": "non_identifier",
    "sales_order_header.sales_order_id": "identifier",
    "sales_order_line.item_description": "non_identifier",
    "sales_order_line.line_amount": "non_identifier",
    "sales_order_line.line_number": "identifier",
    "sales_order_line.order_quantity": "non_identifier",
    "sales_order_line.product_id": "identifier",
    "sales_order_line.quantity_uom": "non_identifier",
    "sales_order_line.sales_order_id": "identifier",
    "sales_order_line.unit_price": "non_identifier",
    "supplier_company.assignment_id": "identifier",
    "supplier_company.company_code": "identifier",
    "supplier_company.payment_terms": "ambiguous_excluded",
    "supplier_company.reconciliation_account": "identifier",
    "supplier_company.supplier_id": "identifier",
    "supplier_general.business_partner_category": "non_identifier",
    "supplier_general.country_code": "non_identifier",
    "supplier_general.language_code": "non_identifier",
    "supplier_general.organization_name": "non_identifier",
    "supplier_general.supplier_id": "identifier",
    "supplier_general.tax_number": "identifier",
}


@dataclass(frozen=True)
class CandidateRow:
    target: str
    original_rank: int
    v5_score: float


@dataclass(frozen=True)
class RankingCase:
    scenario_id: str
    contract_family: str
    case_id: str
    source_field: str
    expected_targets: tuple[str, ...]
    original_recommendation: str | None
    source_identifier_label: int
    source_features: dict[str, float]
    candidates: tuple[CandidateRow, ...]

    @property
    def target_bearing(self) -> bool:
        return bool(self.expected_targets)

    @property
    def multi_target(self) -> bool:
        return len(self.expected_targets) > 1


def load_development_v5_cases(paths: Iterable[Path] = V5_RESULT_PATHS) -> list[RankingCase]:
    source_samples, _excluded = detector.build_samples()
    sample_by_case = {sample.case_id: sample for sample in source_samples}
    cases: list[RankingCase] = []
    seen: set[str] = set()
    for path in paths:
        normalized = path.as_posix().replace("\\", "/")
        if "/external/" in normalized or "/sealed/" in normalized or "public_sealed" in normalized:
            raise ValueError(f"Only development V5 result paths are allowed: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("_meta", {})
        if meta.get("sealed_holdout") or "sealed" in str(meta.get("benchmark_id", "")):
            raise ValueError(f"Sealed result paths are rejected: {path}")
        for item in payload["case_results"]:
            case_id = str(item["case_id"])
            if case_id in seen:
                continue
            sample = sample_by_case.get(case_id)
            if sample is None:
                raise ValueError(f"V5 case has no detector sample: {case_id}")
            candidates = tuple(
                CandidateRow(target=str(candidate["target"]), original_rank=int(candidate["rank"]), v5_score=float(candidate["score"]))
                for candidate in item["top_candidates"]
            )
            cases.append(
                RankingCase(
                    scenario_id=str(item["scenario_id"]),
                    contract_family=sample.contract_family,
                    case_id=case_id,
                    source_field=sample.source_field,
                    expected_targets=tuple(item.get("expected_targets") or ()),
                    original_recommendation=item.get("recommendation"),
                    source_identifier_label=sample.label,
                    source_features=sample.features,
                    candidates=candidates,
                )
            )
            seen.add(case_id)
    return sorted(cases, key=lambda item: item.case_id)


def infer_target_identifier_probability(target_path: str) -> float:
    tokens = tokenize_target_schema_text(target_path)
    positive = any(token in POSITIVE_TARGET_TOKENS for token in tokens)
    negative = any(token in NEGATIVE_TARGET_TOKENS for token in tokens)
    strong_positive = bool({"id", "key", "reference", "ref", "uuid", "guid", "iban", "bic"} & set(tokens))
    if strong_positive:
        return 0.9
    if positive and not negative:
        return 0.8
    if positive and negative:
        return 0.55
    if negative:
        return 0.1
    return 0.35


def tokenize_target_schema_text(text: str) -> tuple[str, ...]:
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return tuple(token for token in re.split(r"[^A-Za-z0-9]+", expanded.lower()) if token)


def target_role_label_artifact(cases: list[RankingCase]) -> dict[str, Any]:
    targets = sorted({candidate.target for case in cases for candidate in case.candidates})
    missing = sorted(set(targets) - set(TARGET_ROLE_LABELS))
    if missing:
        raise ValueError(f"Missing target role labels: {missing}")
    counts = Counter(TARGET_ROLE_LABELS[target] for target in targets)
    return {
        "experiment_id": EXPERIMENT_ID,
        "label_scope": "development target fields present in stored V5 candidate diagnostics",
        "actual_reranker_uses_labels": False,
        "oracle_diagnostic_uses_labels": True,
        "generic_inference_tokens": {
            "positive": list(POSITIVE_TARGET_TOKENS),
            "negative": list(NEGATIVE_TARGET_TOKENS),
        },
        "counts": {
            "target_field_count": len(targets),
            "identifier": counts["identifier"],
            "non_identifier": counts["non_identifier"],
            "ambiguous_excluded": counts["ambiguous_excluded"],
        },
        "labels": [
            {
                "target_path": target,
                "role_label": TARGET_ROLE_LABELS[target],
                "inferred_probability": infer_target_identifier_probability(target),
                "ambiguous_exclusion_reason": "schema role is both coded/reference-like and operationally categorical" if TARGET_ROLE_LABELS[target] == "ambiguous_excluded" else None,
            }
            for target in targets
        ],
    }


def target_role_inference_evaluation(cases: list[RankingCase]) -> dict[str, Any]:
    labels = target_role_label_artifact(cases)["labels"]
    predictions = []
    ambiguous = []
    for item in labels:
        if item["role_label"] == "ambiguous_excluded":
            ambiguous.append(item["target_path"])
            continue
        probability = float(item["inferred_probability"])
        predictions.append(
            {
                "target_path": item["target_path"],
                "label": 1 if item["role_label"] == "identifier" else 0,
                "predicted_label": int(probability >= 0.5),
                "probability": round(probability, 8),
            }
        )
    return {
        "experiment_id": EXPERIMENT_ID,
        "inference_method": "generic_target_schema_token_rules",
        "ambiguous_excluded": ambiguous,
        "metrics": binary_metrics(predictions),
        "confusion_matrix": binary_metrics(predictions)["confusion_matrix"],
    }


def nested_contract_family_ablation() -> dict[str, Any]:
    cases = load_development_v5_cases()
    if len(cases) != 107:
        raise ValueError(f"Expected 107 development cases, got {len(cases)}")
    if tuple(sorted({case.contract_family for case in cases})) != detector.CONTRACT_FAMILY_ORDER:
        raise ValueError("Unexpected development contract families")
    folds: list[dict[str, Any]] = []
    pooled: dict[str, list[dict[str, Any]]] = {system: [] for system in SYSTEMS}
    for held_out in detector.CONTRACT_FAMILY_ORDER:
        outer_train = [case for case in cases if case.contract_family != held_out]
        outer_test = [case for case in cases if case.contract_family == held_out]
        learned_alpha, inner_learned = select_alpha(outer_train, "learned")
        heuristic_alpha, inner_heuristic = select_alpha(outer_train, "heuristic")
        source_model = fit_source_detector(outer_train)
        fold_predictions = {
            "v5_reference": evaluate_cases(outer_test, source_mode="heuristic", alpha=0.0, source_model=None, system="v5_reference"),
            "v5_plus_heuristic_identifier_role": evaluate_cases(outer_test, source_mode="heuristic", alpha=heuristic_alpha, source_model=None, system="v5_plus_heuristic_identifier_role"),
            "v5_plus_learned_identifier_role": evaluate_cases(outer_test, source_mode="learned", alpha=learned_alpha, source_model=source_model, system="v5_plus_learned_identifier_role"),
            "v5_plus_oracle_identifier_role": evaluate_cases(outer_test, source_mode="oracle", alpha=learned_alpha, source_model=None, system="v5_plus_oracle_identifier_role"),
        }
        for system, predictions in fold_predictions.items():
            pooled[system].extend(predictions)
        folds.append(
            {
                "fold_id": f"leave_one_contract_family__{held_out}",
                "held_out_contract_family": held_out,
                "train_contract_families": sorted({case.contract_family for case in outer_train}),
                "train_case_count": len(outer_train),
                "held_out_case_count": len(outer_test),
                "train_source_detector_support": source_detector_support(outer_train),
                "held_out_source_detector_support": source_detector_support(outer_test),
                "held_out_used_for_detector_fit": False,
                "inner_selection": {
                    "objective": "maximize pooled MRR subject to no-target accuracy >= V5 and multi-target full coverage@3 >= V5; ties choose smaller alpha",
                    "alpha_grid": list(ALPHA_GRID),
                    "learned_selected_alpha": learned_alpha,
                    "heuristic_selected_alpha": heuristic_alpha,
                    "oracle_applied_alpha": learned_alpha,
                    "learned_inner_summary": inner_learned,
                    "heuristic_inner_summary": inner_heuristic,
                },
                "outer_metrics": {system: ranking_metrics(predictions) for system, predictions in fold_predictions.items()},
                "source_detector_held_out_metrics": source_detector_held_out_metrics(outer_test, source_model),
            }
        )
    pooled_metrics = {system: ranking_metrics(predictions) for system, predictions in pooled.items()}
    return {
        "experiment_id": EXPERIMENT_ID,
        "experimental_only": True,
        "production_promoted": False,
        "runtime_integrated": False,
        "sealed_holdout_validated": False,
        "case_count": len(cases),
        "outer_cv": "leave_one_contract_family_out",
        "alpha_selection": {
            "alpha_grid": list(ALPHA_GRID),
            "objective": "MRR",
            "constraints": {
                "no_target_accuracy": "must be >= v5_reference on inner pooled predictions",
                "multi_target_full_coverage_at_3": "must be >= v5_reference on inner pooled predictions",
            },
            "tie_break": "choose smaller alpha; this includes 0.0 when tied",
            "outer_held_out_used_for_alpha_selection": False,
        },
        "folds": folds,
        "pooled": {
            system: {
                "metrics": pooled_metrics[system],
                "predictions": sorted(predictions, key=lambda item: item["case_id"]),
            }
            for system, predictions in pooled.items()
        },
    }


def select_alpha(train_cases: list[RankingCase], source_mode: str) -> tuple[float, list[dict[str, Any]]]:
    inner_predictions_by_alpha: dict[float, list[dict[str, Any]]] = {alpha: [] for alpha in ALPHA_GRID}
    for held_out in sorted({case.contract_family for case in train_cases}):
        inner_train = [case for case in train_cases if case.contract_family != held_out]
        inner_test = [case for case in train_cases if case.contract_family == held_out]
        source_model = fit_source_detector(inner_train) if source_mode == "learned" else None
        for alpha in ALPHA_GRID:
            inner_predictions_by_alpha[alpha].extend(
                evaluate_cases(inner_test, source_mode=source_mode, alpha=alpha, source_model=source_model, system=f"inner_{source_mode}")
            )
    v5_metrics = ranking_metrics(inner_predictions_by_alpha[0.0])
    summaries = []
    for alpha in ALPHA_GRID:
        metrics = ranking_metrics(inner_predictions_by_alpha[alpha])
        constraints_met = (
            _metric_value(metrics["no_target_accuracy"]) >= _metric_value(v5_metrics["no_target_accuracy"])
            and _metric_value(metrics["multi_target_full_coverage_at_3"]) >= _metric_value(v5_metrics["multi_target_full_coverage_at_3"])
        )
        summaries.append(
            {
                "alpha": alpha,
                "constraints_met": constraints_met,
                "metrics": metrics,
            }
        )
    viable = [item for item in summaries if item["constraints_met"]]
    best = max(viable, key=lambda item: (_metric_value(item["metrics"]["target_link_mrr"]), -float(item["alpha"])))
    return float(best["alpha"]), summaries


def fit_source_detector(cases: list[RankingCase]) -> dict[str, Any]:
    samples = [
        detector.DetectorSample(
            scenario_id=case.scenario_id,
            contract_family=case.contract_family,
            case_id=case.case_id,
            source_field=case.source_field,
            role_label=detector.LABEL_IDENTIFIER if case.source_identifier_label else detector.LABEL_NON_IDENTIFIER,
            label=case.source_identifier_label,
            features=case.source_features,
        )
        for case in cases
    ]
    return detector.fit_logistic(samples, detector.COMBINED_FEATURE_ORDER)


def source_identifier_probability(case: RankingCase, source_mode: str, source_model: dict[str, Any] | None) -> float:
    if source_mode == "heuristic":
        return float(detector.heuristic_predict(case.source_features))
    if source_mode == "learned":
        if source_model is None:
            raise ValueError("learned source mode requires a source detector model")
        return detector.predict_probability(source_model, case.source_features)
    if source_mode == "oracle":
        return float(case.source_identifier_label)
    raise ValueError(f"Unknown source mode: {source_mode}")


def evaluate_cases(
    cases: list[RankingCase],
    *,
    source_mode: str,
    alpha: float,
    source_model: dict[str, Any] | None,
    system: str,
) -> list[dict[str, Any]]:
    return [evaluate_case(case, source_mode=source_mode, alpha=alpha, source_model=source_model, system=system) for case in cases]


def evaluate_case(
    case: RankingCase,
    *,
    source_mode: str,
    alpha: float,
    source_model: dict[str, Any] | None,
    system: str,
) -> dict[str, Any]:
    p_source = source_identifier_probability(case, source_mode, source_model)
    reranked = []
    for candidate in case.candidates:
        p_target = target_identifier_probability(candidate.target, source_mode)
        compatibility = role_compatibility(p_source, p_target)
        experimental_score = candidate.v5_score + alpha * signed_role_compatibility(p_source, p_target)
        reranked.append(
            {
                "target": candidate.target,
                "original_rank": candidate.original_rank,
                "v5_score": round(candidate.v5_score, 8),
                "p_source_identifier": round(p_source, 8),
                "p_target_identifier": round(p_target, 8),
                "role_compatibility": round(compatibility, 8),
                "role_mismatch": round(1.0 - compatibility, 8),
                "experimental_score": round(experimental_score, 8),
            }
        )
    reranked.sort(key=lambda item: (-float(item["experimental_score"]), int(item["original_rank"]), str(item["target"])))
    expected = set(case.expected_targets)
    rank = next((index for index, item in enumerate(reranked, start=1) if item["target"] in expected), None)
    v5_rank = next((candidate.original_rank for candidate in case.candidates if candidate.target in expected), None)
    top_targets = [item["target"] for item in reranked[:3]]
    original_top3 = [candidate.target for candidate in sorted(case.candidates, key=lambda item: item.original_rank)[:3]]
    expected_in_top3 = sorted(expected & set(top_targets))
    v5_expected_in_top3 = sorted(expected & set(original_top3))
    return {
        "system": system,
        "case_id": case.case_id,
        "scenario_id": case.scenario_id,
        "contract_family": case.contract_family,
        "source_field": case.source_field,
        "source_identifier_label": case.source_identifier_label,
        "source_identifier_probability": round(p_source, 8),
        "expected_target_count": len(case.expected_targets),
        "target_bearing": case.target_bearing,
        "multi_target": case.multi_target,
        "alpha": alpha,
        "v5_best_expected_rank": v5_rank,
        "best_expected_rank": rank,
        "top1_correct": bool(rank == 1),
        "recall_at_3": bool(rank is not None and rank <= 3),
        "expected_targets_in_top3_count": len(expected_in_top3),
        "multi_target_full_coverage_at_3": bool(case.multi_target and len(expected_in_top3) == len(expected)),
        "v5_expected_targets_in_top3_count": len(v5_expected_in_top3),
        "v5_multi_target_full_coverage_at_3": bool(case.multi_target and len(v5_expected_in_top3) == len(expected)),
        "no_target_correct": bool((not case.expected_targets) and case.original_recommendation is None),
        "candidate_count": len(case.candidates),
        "correct_target_in_candidate_set": bool(rank is not None),
        "top_targets": top_targets,
    }


def target_identifier_probability(target_path: str, source_mode: str) -> float:
    if source_mode == "oracle":
        role = TARGET_ROLE_LABELS.get(target_path)
        if role == "identifier":
            return 1.0
        if role == "non_identifier":
            return 0.0
    return infer_target_identifier_probability(target_path)


def role_compatibility(p_source_identifier: float, p_target_identifier: float) -> float:
    return p_source_identifier * p_target_identifier + (1.0 - p_source_identifier) * (1.0 - p_target_identifier)


def signed_role_compatibility(p_source_identifier: float, p_target_identifier: float) -> float:
    return 2.0 * role_compatibility(p_source_identifier, p_target_identifier) - 1.0


def ranking_metrics(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    target_cases = [item for item in predictions if item["target_bearing"]]
    no_target_cases = [item for item in predictions if not item["target_bearing"]]
    multi_cases = [item for item in predictions if item["multi_target"]]
    top1 = sum(1 for item in target_cases if item["top1_correct"])
    recall3 = sum(1 for item in target_cases if item["recall_at_3"])
    reciprocal = sum(0.0 if item["best_expected_rank"] is None else 1.0 / float(item["best_expected_rank"]) for item in target_cases)
    no_target_correct = sum(1 for item in no_target_cases if item["no_target_correct"])
    full_multi = sum(1 for item in multi_cases if item["multi_target_full_coverage_at_3"])
    comparison = movement_counts(predictions)
    return {
        "case_count": len(predictions),
        "single_target_top1_accuracy": _fraction(top1, len(target_cases)),
        "target_link_recall_at_1": _fraction(top1, len(target_cases)),
        "target_link_recall_at_3": _fraction(recall3, len(target_cases)),
        "target_link_mrr": _fraction(reciprocal, len(target_cases)),
        "no_target_accuracy": _fraction(no_target_correct, len(no_target_cases)),
        "multi_target_full_coverage_at_3": _fraction(full_multi, len(multi_cases), none_when_zero=True),
        **comparison,
    }


def movement_counts(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    improved = regressed = unchanged = into_top3 = out_of_top3 = top1_gain = top1_loss = 0
    multi_gain = multi_loss = 0
    for item in predictions:
        if not item["target_bearing"]:
            continue
        old_rank = _rank_value(item["v5_best_expected_rank"])
        new_rank = _rank_value(item["best_expected_rank"])
        if new_rank < old_rank:
            improved += 1
        elif new_rank > old_rank:
            regressed += 1
        else:
            unchanged += 1
        if old_rank > 3 and new_rank <= 3:
            into_top3 += 1
        if old_rank <= 3 and new_rank > 3:
            out_of_top3 += 1
        if old_rank != 1 and new_rank == 1:
            top1_gain += 1
        if old_rank == 1 and new_rank != 1:
            top1_loss += 1
        if item["multi_target"]:
            if item["multi_target_full_coverage_at_3"] and not item["v5_multi_target_full_coverage_at_3"]:
                multi_gain += 1
            if item["v5_multi_target_full_coverage_at_3"] and not item["multi_target_full_coverage_at_3"]:
                multi_loss += 1
    return {
        "improved_cases": improved,
        "regressed_cases": regressed,
        "unchanged_cases": unchanged,
        "correct_target_entered_top3": into_top3,
        "correct_target_left_top3": out_of_top3,
        "top1_gain": top1_gain,
        "top1_loss": top1_loss,
        "multi_target_gain": multi_gain,
        "multi_target_loss": multi_loss,
    }


def binary_metrics(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(item["label"]) for item in predictions]
    predicted = [int(item["predicted_label"]) for item in predictions]
    probabilities = [float(item["probability"]) for item in predictions]
    tp = sum(1 for y, yhat in zip(labels, predicted, strict=True) if y == 1 and yhat == 1)
    tn = sum(1 for y, yhat in zip(labels, predicted, strict=True) if y == 0 and yhat == 0)
    fp = sum(1 for y, yhat in zip(labels, predicted, strict=True) if y == 0 and yhat == 1)
    fn = sum(1 for y, yhat in zip(labels, predicted, strict=True) if y == 1 and yhat == 0)
    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    specificity = _safe_divide(tn, tn + fp)
    return {
        "case_count": len(predictions),
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "accuracy": _safe_divide(tp + tn, len(predictions)),
        "balanced_accuracy": None if recall is None or specificity is None else round((recall + specificity) / 2.0, 8),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "specificity": specificity,
        "roc_auc": _roc_auc(labels, probabilities),
        "average_precision": _average_precision(labels, probabilities),
        "brier_score": _brier(labels, probabilities),
    }


def source_detector_held_out_metrics(cases: list[RankingCase], source_model: dict[str, Any]) -> dict[str, Any]:
    predictions = []
    for case in cases:
        probability = detector.predict_probability(source_model, case.source_features)
        predictions.append({"label": case.source_identifier_label, "predicted_label": int(probability >= 0.5), "probability": probability})
    return binary_metrics(predictions)


def source_detector_support(cases: list[RankingCase]) -> dict[str, int]:
    positives = sum(case.source_identifier_label for case in cases)
    return {"identifier": positives, "non_identifier": len(cases) - positives}


def comparison(results: dict[str, Any]) -> dict[str, Any]:
    pooled = {system: body["metrics"] for system, body in results["pooled"].items()}
    learned = pooled["v5_plus_learned_identifier_role"]
    v5 = pooled["v5_reference"]
    oracle = pooled["v5_plus_oracle_identifier_role"]
    learned_predictions = results["pooled"]["v5_plus_learned_identifier_role"]["predictions"]
    improvement_families = sorted(
        {
            item["contract_family"]
            for item in learned_predictions
            if item["target_bearing"] and _rank_value(item["best_expected_rank"]) < _rank_value(item["v5_best_expected_rank"])
        }
    )
    improvement_scenarios = sorted(
        {
            item["scenario_id"]
            for item in learned_predictions
            if item["target_bearing"] and _rank_value(item["best_expected_rank"]) < _rank_value(item["v5_best_expected_rank"])
        }
    )
    improves = (
        _metric_value(learned["target_link_mrr"]) > _metric_value(v5["target_link_mrr"])
        and learned["improved_cases"] > learned["regressed_cases"]
        and _metric_value(learned["no_target_accuracy"]) >= _metric_value(v5["no_target_accuracy"])
        and _metric_value(learned["multi_target_full_coverage_at_3"]) >= _metric_value(v5["multi_target_full_coverage_at_3"])
        and len(improvement_families) > 1
        and len(improvement_scenarios) > 1
        and _metric_value(oracle["target_link_mrr"]) > _metric_value(v5["target_link_mrr"])
    )
    return {
        "experiment_id": EXPERIMENT_ID,
        "experimental_only": True,
        "production_promoted": False,
        "runtime_integrated": False,
        "sealed_holdout_validated": False,
        "case_count": results["case_count"],
        "pooled_metrics": pooled,
        "decision_rule": {
            "worth_future_sealed_holdout": bool(improves),
            "requires_separate_future_authorization": True,
            "learned_improvement_contract_families": improvement_families,
            "learned_improvement_scenarios": improvement_scenarios,
            "criteria": [
                "learned reranker improves pooled ranking over V5",
                "improved cases exceed regressed cases",
                "no-target accuracy does not decline",
                "multi-target full coverage@3 does not decline",
                "improvement is not confined to a single source/scenario",
                "oracle diagnostic supports identifier role value",
            ],
        },
    }


def case_level_analysis(results: dict[str, Any]) -> dict[str, Any]:
    by_system = {system: {item["case_id"]: item for item in body["predictions"]} for system, body in results["pooled"].items()}
    learned = by_system["v5_plus_learned_identifier_role"]
    heuristic = by_system["v5_plus_heuristic_identifier_role"]
    oracle = by_system["v5_plus_oracle_identifier_role"]
    categories: dict[str, list[dict[str, Any]]] = {
        "identifier_source_ranked_to_non_identifier_target": [],
        "non_identifier_source_promoted_to_identifier_target": [],
        "detector_correct_but_ranking_not_improved": [],
        "detector_error_caused_ranking_regression": [],
        "correct_target_candidate_present_but_boost_insufficient": [],
        "correct_target_absent_from_candidate_set": [],
        "heuristic_improved_learned_regressed": [],
        "learned_improved_heuristic_regressed": [],
        "oracle_improved_learned_not_improved": [],
        "multi_target_partial_coverage_change": [],
        "no_target_false_acceptance_change": [],
    }
    for case_id, item in sorted(learned.items()):
        record = {
            "case_id": item["case_id"],
            "scenario_id": item["scenario_id"],
            "contract_family": item["contract_family"],
            "source_field": item["source_field"],
            "source_identifier_label": item["source_identifier_label"],
            "v5_best_expected_rank": item["v5_best_expected_rank"],
            "learned_best_expected_rank": item["best_expected_rank"],
            "source_identifier_probability": item["source_identifier_probability"],
        }
        old_rank = _rank_value(item["v5_best_expected_rank"])
        new_rank = _rank_value(item["best_expected_rank"])
        heuristic_delta = _rank_value(heuristic[case_id]["best_expected_rank"]) - old_rank
        learned_delta = new_rank - old_rank
        oracle_delta = _rank_value(oracle[case_id]["best_expected_rank"]) - old_rank
        learned_source_prediction = int(float(item["source_identifier_probability"]) >= 0.5)
        detector_is_correct = learned_source_prediction == int(item["source_identifier_label"])
        if item["target_bearing"] and old_rank == new_rank and detector_is_correct:
            categories["detector_correct_but_ranking_not_improved"].append(record)
        if item["target_bearing"] and new_rank > old_rank and not detector_is_correct:
            categories["detector_error_caused_ranking_regression"].append(record)
        if item["target_bearing"] and item["correct_target_in_candidate_set"] and new_rank > 1:
            categories["correct_target_candidate_present_but_boost_insufficient"].append(record)
        if item["target_bearing"] and not item["correct_target_in_candidate_set"]:
            categories["correct_target_absent_from_candidate_set"].append(record)
        if heuristic_delta < 0 and learned_delta > 0:
            categories["heuristic_improved_learned_regressed"].append(record)
        if learned_delta < 0 and heuristic_delta > 0:
            categories["learned_improved_heuristic_regressed"].append(record)
        if oracle_delta < 0 and learned_delta >= 0:
            categories["oracle_improved_learned_not_improved"].append(record)
        if item["multi_target"] and item["multi_target_full_coverage_at_3"] != item["v5_multi_target_full_coverage_at_3"]:
            categories["multi_target_partial_coverage_change"].append(record)
        if not item["target_bearing"] and not item["no_target_correct"]:
            categories["no_target_false_acceptance_change"].append(record)
    return {
        "experiment_id": EXPERIMENT_ID,
        "privacy": "Includes development case ids and source field identifiers only; raw source values are excluded.",
        "categories": categories,
    }


def feature_schema() -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "training_matrix_excludes": [
            "source field name/token/text",
            "target path/name/text",
            "scenario id",
            "benchmark id",
            "contract family id",
            "case id",
            "ground-truth target count",
            "expected target information",
            "label-derived features",
        ],
        "source_identifier_detector_features": [
            {
                "name": name,
                "source": "header-blind aggregate source-column value profile",
                "meaning": detector._feature_formula(name),
                "requires_scaling": True,
                "leakage_rationale": "Computed from column values only as aggregate numeric statistics; names, ids, target paths, and mapping ground truth are excluded.",
            }
            for name in detector.COMBINED_FEATURE_ORDER
        ],
        "reranker_features": [
            {
                "name": "v5_score",
                "source": "stored V5 candidate diagnostic score",
                "meaning": "Pre-existing V5 candidate ranking score used without changing candidate generation.",
                "requires_scaling": False,
                "leakage_rationale": "Comes from the pre-existing V5 development artifact and does not include labels or expected targets.",
            },
            {
                "name": "p_source_identifier",
                "source": "heuristic or learned source value-profile identifier detector",
                "meaning": "Probability or binary diagnostic estimate that the source column carries an identifier-like role.",
                "requires_scaling": False,
                "leakage_rationale": "Learned folds fit only on training source-role annotations and aggregate value-profile features; held-out family labels are excluded.",
            },
            {
                "name": "p_target_identifier",
                "source": "generic target-schema token inference; oracle only in separate diagnostic system",
                "meaning": "Estimated target-field identifier role from generic schema/path tokens.",
                "requires_scaling": False,
                "leakage_rationale": "Actual heuristic/learned rerankers do not use per-path label lookup or mapping ground truth.",
            },
            {
                "name": "signed_role_compatibility",
                "source": "2 * (p_source_identifier * p_target_identifier + (1 - p_source_identifier) * (1 - p_target_identifier)) - 1",
                "meaning": "Positive when source and target roles appear compatible and negative when they appear mismatched.",
                "requires_scaling": False,
                "leakage_rationale": "Derived only from source role probability and generic target role probability.",
            },
        ],
    }


def development_model(cases: list[RankingCase]) -> dict[str, Any]:
    learned_alpha, learned_inner_summary = select_alpha(cases, "learned")
    heuristic_alpha, heuristic_inner_summary = select_alpha(cases, "heuristic")
    return {
        "_meta": {
            "experiment_id": EXPERIMENT_ID,
            "development_only": True,
            "experimental_only": True,
            "production_promoted": False,
            "runtime_integrated": False,
            "sealed_holdout_validated": False,
            "model_format": "json",
            "pickle_used": False,
        },
        "source_identifier_detector": fit_source_detector(cases),
        "target_identifier_inference": {
            "method": "generic_target_schema_token_rules",
            "positive_tokens": list(POSITIVE_TARGET_TOKENS),
            "negative_tokens": list(NEGATIVE_TARGET_TOKENS),
            "per_target_label_lookup_used": False,
        },
        "reranker": {
            "score_formula": "experimental_score = v5_score + alpha * (2 * role_compatibility - 1)",
            "role_compatibility_formula": "p_source_identifier * p_target_identifier + (1 - p_source_identifier) * (1 - p_target_identifier)",
            "alpha_grid": list(ALPHA_GRID),
            "learned_selected_alpha": learned_alpha,
            "heuristic_selected_alpha": heuristic_alpha,
            "threshold_selection_scope": "development corpus contract-family OOF predictions only",
            "learned_inner_summary": learned_inner_summary,
            "heuristic_inner_summary": heuristic_inner_summary,
        },
    }


def experiment_config() -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "experimental_only": True,
        "production_promoted": False,
        "runtime_integrated": False,
        "sealed_holdout_validated": False,
        "data_boundary": {
            "allowed": [
                "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json",
                "data/benchmarks/development/combined_public_dev_v1/results/precision_tiered_v5.json",
                "data/benchmarks/schema_matching_v1.json",
                "data/benchmarks/schema_matching_public_dev_v1.json",
                "data/experiments/schema_matching_identifier_role_v1/source_field_role_labels.json",
            ],
            "forbidden": ["Companies House", "FDIC", "external benchmarks", "sealed benchmarks"],
        },
        "alpha_grid": list(ALPHA_GRID),
        "alpha_grid_basis": "Small fixed grid on the observed V5 score scale, where stored V5 candidate scores are normalized decimals.",
        "alpha_selection_objective": "maximize inner pooled MRR with no-target and multi-target non-regression constraints; ties choose smaller alpha",
        "score_formula": "experimental_score = v5_score + alpha * (2 * role_compatibility - 1)",
        "role_compatibility_formula": "p_source_identifier * p_target_identifier + (1 - p_source_identifier) * (1 - p_target_identifier)",
    }


def fold_manifest(results: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "outer_cv": results["outer_cv"],
        "alpha_selection": results["alpha_selection"],
        "folds": [
            {
                "fold_id": fold["fold_id"],
                "held_out_contract_family": fold["held_out_contract_family"],
                "train_contract_families": fold["train_contract_families"],
                "train_case_count": fold["train_case_count"],
                "held_out_case_count": fold["held_out_case_count"],
                "train_source_detector_support": fold["train_source_detector_support"],
                "held_out_source_detector_support": fold["held_out_source_detector_support"],
                "held_out_used_for_detector_fit": fold["held_out_used_for_detector_fit"],
                "learned_selected_alpha": fold["inner_selection"]["learned_selected_alpha"],
                "heuristic_selected_alpha": fold["inner_selection"]["heuristic_selected_alpha"],
            }
            for fold in results["folds"]
        ],
    }


def readme_text(comparison_body: dict[str, Any]) -> str:
    v5 = comparison_body["pooled_metrics"]["v5_reference"]
    learned = comparison_body["pooled_metrics"]["v5_plus_learned_identifier_role"]
    oracle = comparison_body["pooled_metrics"]["v5_plus_oracle_identifier_role"]
    return (
        "# Identifier Role Ranking Ablation V1\n\n"
        "This development-only ablation tests whether header-blind source identifier probability can improve stored V5 candidate ranking without changing V5 itself. It does not modify the scorer, backend, frontend, runtime, aliases, features, thresholds, Companies House, or FDIC artifacts.\n\n"
        "The fixed alpha grid is `0.0`, `0.01`, `0.02`, and `0.05`. Alpha is selected inside each outer training set by inner leave-one-contract-family-out MRR, constrained so no-target accuracy and multi-target full coverage@3 do not fall below V5; ties choose the smaller alpha.\n\n"
        "Compared systems are `v5_reference`, `v5_plus_heuristic_identifier_role`, `v5_plus_learned_identifier_role`, and the non-deployable `v5_plus_oracle_identifier_role` diagnostic.\n\n"
        "## Development Result\n\n"
        f"V5 reference MRR is {_format_metric(v5['target_link_mrr']['value'])}; learned reranker MRR is {_format_metric(learned['target_link_mrr']['value'])}; oracle MRR is {_format_metric(oracle['target_link_mrr']['value'])}. Learned improved {learned['improved_cases']} cases, regressed {learned['regressed_cases']} cases, and left {learned['unchanged_cases']} unchanged.\n\n"
        f"No-target accuracy changed from {_format_metric(v5['no_target_accuracy']['value'])} to {_format_metric(learned['no_target_accuracy']['value'])}. Multi-target full coverage@3 changed from {_format_metric(v5['multi_target_full_coverage_at_3']['value'])} to {_format_metric(learned['multi_target_full_coverage_at_3']['value'])}.\n\n"
        f"Future sealed holdout gate: {comparison_body['decision_rule']['worth_future_sealed_holdout']}. Even if positive, this would only justify a future separately authorized ablation, not V5 or runtime integration.\n"
    )


def write_ablation_artifacts(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results = nested_contract_family_ablation()
    cases = load_development_v5_cases()
    target_eval = target_role_inference_evaluation(cases)
    comparison_body = comparison(results)
    artifacts = {
        "README.md": readme_text(comparison_body),
        "experiment_config.json": experiment_config(),
        "feature_schema.json": feature_schema(),
        "target_field_role_labels.json": target_role_label_artifact(cases),
        "target_role_inference_evaluation.json": target_eval,
        "fold_manifest.json": fold_manifest(results),
        "nested_contract_family_results.json": results,
        "comparison.json": comparison_body,
        "case_level_analysis.json": case_level_analysis(results),
        "development_model.json": development_model(cases),
    }
    written: dict[str, str] = {}
    for filename, payload in artifacts.items():
        path = output_dir / filename
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8", newline="\n")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        written[path.relative_to(PROJECT_ROOT).as_posix()] = raw_sha256(path)
    return written


def _fraction(numerator: float, denominator: int, *, none_when_zero: bool = False) -> dict[str, Any]:
    if denominator == 0:
        return {"numerator": numerator, "denominator": denominator, "value": None if none_when_zero else 0.0}
    return {"numerator": numerator, "denominator": denominator, "value": round(float(numerator) / float(denominator), 8)}


def _rank_value(rank: Any) -> int:
    return 999 if rank is None else int(rank)


def _metric_value(metric: dict[str, Any]) -> float:
    return 0.0 if metric["value"] is None else float(metric["value"])


def _safe_divide(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else round(float(numerator) / float(denominator), 8)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return round(2.0 * precision * recall / (precision + recall), 8)


def _roc_auc(labels: list[int], probabilities: list[float]) -> float | None:
    positives = [probability for label, probability in zip(labels, probabilities, strict=True) if label == 1]
    negatives = [probability for label, probability in zip(labels, probabilities, strict=True) if label == 0]
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
    for index, (_probability, label) in enumerate(sorted(zip(probabilities, labels, strict=True), reverse=True), start=1):
        if label:
            hits += 1
            total += hits / index
    return round(total / positive_count, 8)


def _brier(labels: list[int], probabilities: list[float]) -> float | None:
    if not labels:
        return None
    return round(sum((probability - label) ** 2 for label, probability in zip(labels, probabilities, strict=True)) / len(labels), 8)


def _format_metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run identifier-role ranking ablation on development V5 artifacts.")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    if not args.write_artifacts:
        parser.error("--write-artifacts is required")
    written = write_ablation_artifacts(args.output_dir)
    print(json.dumps({"experiment_id": EXPERIMENT_ID, "written": written}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
