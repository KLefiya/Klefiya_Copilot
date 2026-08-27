from __future__ import annotations

import hashlib
import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS
from src.core.mapping.v5_correctness_calibration import (
    MULTIFEATURE_ID,
    MULTIFEATURE_ORDER,
    SCORE_ONLY_FEATURE_ORDER,
    CalibrationCase,
    build_failure_analysis_from_oof_records,
    development_model,
    feature_schema,
    fit_calibrator,
    inner_oof_predictions,
    predict_probability,
    selective_metrics,
    select_threshold,
    reconciliation_from_oof_records,
    _sigmoid,
)


EXPERIMENT_ROOT = PROJECT_ROOT / "data/experiments/schema_matching_v5_correctness_calibration_v1"
FORMAL_RESULT_SHAS = {
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json": "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json": "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
}
COMPANIES_HOUSE_ARTIFACT = PROJECT_ROOT / "data/benchmarks/external/companies_house_customer_v1/first_evaluation_baseline_v4_v5.json"
COMPANIES_HOUSE_ARTIFACT_SHA = "d08584b1e77e59ba5362586d851e225e9d746f52eb01c2b268ffe2b68dc7edd8"


def _load_json(relative_path: str) -> dict:
    return json.loads((EXPERIMENT_ROOT / relative_path).read_text(encoding="utf-8"))


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_record(
    case_id: str,
    family: str,
    *,
    has_expected_target: bool,
    top1_correct: bool,
    existing_accepted: bool,
    score_probability: float,
    multi_probability: float,
    score_threshold_90: float,
    score_threshold_95: float,
    multi_threshold_90: float,
    multi_threshold_95: float,
) -> dict:
    return {
        "case_id": case_id,
        "scenario_id": f"scenario_{family}",
        "contract_family": family,
        "outer_fold": f"leave_one_contract_family__{family}",
        "has_expected_target": has_expected_target,
        "top1_correct": top1_correct,
        "existing_v5_accepted": existing_accepted,
        "score_only_probability": score_probability,
        "multifeature_probability": multi_probability,
        "score_only_target_precision_90_threshold": score_threshold_90,
        "score_only_target_precision_95_threshold": score_threshold_95,
        "multifeature_target_precision_90_threshold": multi_threshold_90,
        "multifeature_target_precision_95_threshold": multi_threshold_95,
        "score_only_target_precision_90_accepted": score_probability >= score_threshold_90,
        "score_only_target_precision_95_accepted": score_probability >= score_threshold_95,
        "multifeature_target_precision_90_accepted": multi_probability >= multi_threshold_90,
        "multifeature_target_precision_95_accepted": multi_probability >= multi_threshold_95,
    }


def _audit_fixture() -> list[dict]:
    return [
        _audit_record(
            "fold_a_correct_accept",
            "fold_a",
            has_expected_target=True,
            top1_correct=True,
            existing_accepted=True,
            score_probability=0.91,
            multi_probability=0.92,
            score_threshold_90=0.70,
            score_threshold_95=0.95,
            multi_threshold_90=0.80,
            multi_threshold_95=0.95,
        ),
        _audit_record(
            "fold_a_wrong_accept",
            "fold_a",
            has_expected_target=True,
            top1_correct=False,
            existing_accepted=False,
            score_probability=0.75,
            multi_probability=0.90,
            score_threshold_90=0.70,
            score_threshold_95=0.95,
            multi_threshold_90=0.80,
            multi_threshold_95=0.95,
        ),
        _audit_record(
            "fold_b_no_target_accept",
            "fold_b",
            has_expected_target=False,
            top1_correct=False,
            existing_accepted=False,
            score_probability=0.59,
            multi_probability=0.83,
            score_threshold_90=0.60,
            score_threshold_95=0.90,
            multi_threshold_90=0.82,
            multi_threshold_95=0.93,
        ),
        _audit_record(
            "fold_b_correct_reject",
            "fold_b",
            has_expected_target=True,
            top1_correct=True,
            existing_accepted=False,
            score_probability=0.59,
            multi_probability=0.81,
            score_threshold_90=0.60,
            score_threshold_95=0.90,
            multi_threshold_90=0.82,
            multi_threshold_95=0.93,
        ),
    ]


def _case(
    case_id: str,
    family: str,
    label: int,
    *,
    target_bearing: bool = True,
    score: float = 0.7,
    margin: float = 0.1,
    accepted: bool = False,
) -> CalibrationCase:
    features = {name: 0.0 for name in MULTIFEATURE_ORDER}
    features.update(
        {
            "top1_v5_score": score,
            "v5_score_margin_top1_top2": margin,
            "top1_semantic_score": score,
            "semantic_score_margin_top1_top2": margin,
            "top1_baseline_score": score,
            "top1_lexical_overlap": margin,
            "top1_fuzzy_score": score,
            "top1_value_pattern_score": score,
            "top1_resource_context_score": score,
            "top1_identifier_adjusted_score": score,
            "top1_type_gate": 1.0,
            "top1_v5_top1_eligible": 1.0,
            "eligible_candidate_count": 1.0,
            "candidate_count": 3.0,
        }
    )
    expected = ("table.field",) if target_bearing else ()
    return CalibrationCase(
        scenario_id=f"scenario_{family}",
        contract_family=family,
        case_id=case_id,
        source_field="not_a_feature",
        expected_targets=expected,
        expected_no_target=not target_bearing,
        top1_target="table.field" if label else "table.other",
        top3_targets=("table.field", "table.other"),
        top1_status="suggested" if accepted else "needs_review",
        existing_v5_accept=accepted,
        label=label,
        features=features,
    )


class SchemaMatchingV5CorrectnessCalibrationTest(unittest.TestCase):
    def test_feature_schema_has_no_identity_or_ground_truth_inputs(self):
        schema = feature_schema()
        forbidden_exact = {
            "source_field",
            "source_text",
            "target",
            "target_path",
            "target_name",
            "target_text",
            "scenario_id",
            "benchmark_id",
            "contract_family_id",
            "case_id",
            "ground_truth_target_count",
            "expected_targets",
            "label",
        }

        self.assertEqual(tuple(schema["multifeature_feature_order"]), MULTIFEATURE_ORDER)
        self.assertEqual(tuple(schema["score_only_feature_order"]), SCORE_ONLY_FEATURE_ORDER)
        self.assertFalse(schema["ground_truth_derived_features"])
        for feature in schema["multifeature_feature_order"]:
            self.assertNotIn(feature, forbidden_exact)

    def test_no_target_label_is_negative_and_rejection_means_review(self):
        no_target = _case("no_target", "a", 0, target_bearing=False)
        correct_target = _case("correct", "a", 1)
        predictions = [
            {"case_id": no_target.case_id, "target_bearing": no_target.target_bearing, "label": no_target.label, "probability": 0.2},
            {
                "case_id": correct_target.case_id,
                "target_bearing": correct_target.target_bearing,
                "label": correct_target.label,
                "probability": 0.8,
            },
        ]

        metrics = selective_metrics(predictions, {"policy": "accept_probability_at_or_above_threshold", "threshold": 0.5})

        self.assertEqual(no_target.label, 0)
        self.assertEqual(metrics["accepted_count"], 1)
        self.assertEqual(metrics["review_count"], 1)
        self.assertEqual(metrics["no_target_rejection_rate"], 1.0)
        self.assertEqual(metrics["no_target_accepted_count"], 0)

    def test_threshold_selection_uses_inner_oof_probabilities_only(self):
        predictions = [
            {"case_id": "a", "label": 1, "probability": 0.91},
            {"case_id": "b", "label": 0, "probability": 0.81},
            {"case_id": "c", "label": 1, "probability": 0.80},
            {"case_id": "d", "label": 0, "probability": 0.10},
        ]

        threshold = select_threshold(predictions, 0.90)

        self.assertEqual(threshold["selection_source"], "inner_contract_family_oof_predictions")
        self.assertEqual(threshold["threshold"], 0.91)
        self.assertEqual(threshold["accepted_count"], 1)

    def test_inner_oof_holds_out_complete_contract_family_from_fit(self):
        samples = [
            _case("a1", "a", 1, score=0.9),
            _case("a0", "a", 0, score=0.2),
            _case("b1", "b", 1, score=0.85),
            _case("b0", "b", 0, score=0.25),
            _case("c1", "c", 1, score=0.8),
            _case("c0", "c", 0, score=0.3),
        ]

        predictions = inner_oof_predictions(samples, SCORE_ONLY_FEATURE_ORDER)

        self.assertEqual({item["case_id"] for item in predictions}, {sample.case_id for sample in samples})
        self.assertEqual({item["contract_family"] for item in predictions}, {"a", "b", "c"})
        self.assertTrue(all(0.0 <= item["probability"] <= 1.0 for item in predictions))

    def test_pooled_counts_are_computed_from_case_predictions(self):
        predictions = [
            {"case_id": "a", "target_bearing": True, "label": 1, "probability": 0.9},
            {"case_id": "b", "target_bearing": True, "label": 0, "probability": 0.8},
            {"case_id": "c", "target_bearing": False, "label": 0, "probability": 0.7},
            {"case_id": "d", "target_bearing": False, "label": 0, "probability": 0.1},
        ]

        metrics = selective_metrics(predictions, {"policy": "accept_probability_at_or_above_threshold", "threshold": 0.75})

        self.assertEqual(metrics["case_count"], 4)
        self.assertEqual(metrics["accepted_count"], 2)
        self.assertEqual(metrics["accepted_correct_count"], 1)
        self.assertEqual(metrics["accepted_incorrect_count"], 1)
        self.assertEqual(metrics["wrong_target_accepted_count"], 1)
        self.assertEqual(metrics["no_target_accepted_count"], 0)

    def test_training_and_json_prediction_are_deterministic_without_pickle(self):
        samples = [
            _case("a1", "a", 1, score=0.9),
            _case("a0", "a", 0, score=0.1),
            _case("b1", "b", 1, score=0.8),
            _case("b0", "b", 0, score=0.2),
        ]

        left = fit_calibrator(samples, SCORE_ONLY_FEATURE_ORDER)
        right = fit_calibrator(samples, SCORE_ONLY_FEATURE_ORDER)
        probability = predict_probability(json.loads(json.dumps(left)), samples[0])

        self.assertEqual(left, right)
        self.assertNotIn("pickle", json.dumps(left).lower())
        self.assertTrue(math.isfinite(probability))

    def test_sigmoid_is_stable_and_prediction_path_executes(self):
        samples = [
            _case("a1", "a", 1, score=0.9),
            _case("a0", "a", 0, score=0.1),
            _case("b1", "b", 1, score=0.8),
            _case("b0", "b", 0, score=0.2),
        ]
        model = fit_calibrator(samples, SCORE_ONLY_FEATURE_ORDER)

        self.assertAlmostEqual(_sigmoid(0.0), 0.5)
        self.assertGreater(_sigmoid(1_000_000.0), 0.999999)
        self.assertLess(_sigmoid(-1_000_000.0), 0.000001)
        self.assertTrue(0.0 <= predict_probability(model, samples[0]) <= 1.0)

    def test_failure_analysis_uses_fold_thresholds_not_pooled_null_threshold(self):
        records = _audit_fixture()
        analysis = build_failure_analysis_from_oof_records(records)

        self.assertEqual(len({item["score_only_target_precision_90_threshold"] for item in records}), 2)
        self.assertEqual(len(analysis["by_strategy_policy"]["multifeature_target_precision_90"]["no_target_false_acceptance"]), 1)
        self.assertEqual(
            len(analysis["by_strategy_policy"]["multifeature_target_precision_90"]["confident_wrong_target_acceptance"]),
            1,
        )
        self.assertEqual(
            len(analysis["by_strategy_policy"]["multifeature_target_precision_90"]["correct_prediction_unnecessarily_rejected"]),
            1,
        )
        self.assertEqual(
            len(analysis["by_strategy_policy"]["score_only_target_precision_90"]["score_only_multifeature_disagreement"]),
            1,
        )

    def test_failure_analysis_reconciliation_and_deterministic_output(self):
        records = _audit_fixture()
        left = build_failure_analysis_from_oof_records(records)
        right = build_failure_analysis_from_oof_records(list(reversed(records)))
        reconciliation = reconciliation_from_oof_records(records)

        self.assertEqual(left, right)
        self.assertEqual(reconciliation["multifeature_target_precision_90"]["accepted_count"], 3)
        self.assertEqual(reconciliation["multifeature_target_precision_90"]["accepted_correct_count"], 1)
        self.assertEqual(reconciliation["multifeature_target_precision_90"]["accepted_incorrect_count"], 2)
        self.assertEqual(reconciliation["multifeature_target_precision_90"]["no_target_accepted_count"], 1)
        self.assertEqual(reconciliation["multifeature_target_precision_90"]["wrong_target_accepted_count"], 1)
        self.assertTrue(all(value["accepted_balance_ok"] for value in reconciliation.values()))
        self.assertTrue(all(value["coverage_balance_ok"] for value in reconciliation.values()))
        self.assertTrue(all(value["incorrect_breakdown_ok"] for value in reconciliation.values()))
        self.assertTrue(all(value["correct_breakdown_ok"] for value in reconciliation.values()))

    def test_failure_reporting_repair_is_pure_and_audit_schema_is_private(self):
        records = _audit_fixture()
        forbidden_fragments = ("source_field", "source_value", "target_path", "target_name", "expected_targets")

        with patch("src.core.mapping.v5_correctness_calibration.fit_calibrator", side_effect=AssertionError("fit called")):
            body = build_failure_analysis_from_oof_records(records)

        audit_text = json.dumps(records, sort_keys=True)
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, audit_text)
        for feature in MULTIFEATURE_ORDER:
            self.assertNotIn("case_id", feature)
            self.assertNotIn("scenario", feature)
            self.assertNotIn("expected", feature)
        self.assertEqual(body["repair_type"], "deterministic post-processing repair")

    def test_development_model_records_non_production_flags(self):
        samples = [
            _case("a1", "a", 1, score=0.9),
            _case("a0", "a", 0, score=0.1),
            _case("b1", "b", 1, score=0.8),
            _case("b0", "b", 0, score=0.2),
            _case("c1", "c", 1, score=0.85),
            _case("c0", "c", 0, score=0.15),
        ]

        model = development_model(samples)

        self.assertFalse(model["_meta"]["production_promoted"])
        self.assertFalse(model["_meta"]["sealed_holdout_validated"])
        self.assertTrue(model["_meta"]["development_only"])
        self.assertIn(MULTIFEATURE_ID, model["models"])

    def test_saved_artifacts_record_nested_cv_and_development_scope(self):
        if not EXPERIMENT_ROOT.exists():
            self.skipTest("calibration artifacts are created only by the single formal experiment run")
        comparison = _load_json("comparison.json")
        manifest = _load_json("fold_manifest.json")
        model = _load_json("development_model.json")
        oof = _load_json("outer_oof_predictions.json")
        failure = _load_json("failure_analysis.json")

        self.assertEqual(comparison["case_count"], 107)
        self.assertEqual(comparison["positive_label_count"], 72)
        self.assertEqual(comparison["negative_label_count"], 35)
        self.assertEqual(manifest["outer_cv"], "leave_one_contract_family_out")
        self.assertEqual(manifest["inner_cv"], "leave_one_contract_family_out_within_outer_training_families")
        self.assertFalse(manifest["leakage_controls"]["outer_held_out_used_for_threshold_selection"])
        self.assertFalse(model["_meta"]["production_promoted"])
        self.assertFalse(model["_meta"]["sealed_holdout_validated"])
        self.assertTrue(model["_meta"]["development_only"])
        self.assertEqual(len(oof["records"]), comparison["case_count"])
        self.assertEqual(failure["repair_type"], "deterministic post-processing repair")

    def test_saved_oof_records_are_private_and_reconcile_with_comparison(self):
        if not (EXPERIMENT_ROOT / "outer_oof_predictions.json").exists():
            self.skipTest("OOF audit records are created only after deterministic forensic replay")
        comparison = _load_json("comparison.json")
        oof = _load_json("outer_oof_predictions.json")
        failure = _load_json("failure_analysis.json")
        records = oof["records"]
        forbidden_keys = {"source_field", "source_value", "target_path", "target_name", "expected_targets"}

        self.assertEqual(len(records), comparison["case_count"])
        self.assertEqual(sum(1 for item in records if item["top1_correct"]), comparison["positive_label_count"])
        for record in records:
            self.assertFalse(forbidden_keys & set(record))
            self.assertIn("outer_fold", record)
            self.assertIn("score_only_target_precision_90_threshold", record)
            self.assertIn("multifeature_target_precision_95_accepted", record)

        expected_counts = {
            "existing_v5_policy": comparison["strategy_summary"]["existing_v5_policy"]["policy_metrics"]["current_suggested_auto_accept"],
            "score_only_target_precision_90": comparison["strategy_summary"]["score_only_calibrator"]["policy_metrics"]["target_precision_90"],
            "score_only_target_precision_95": comparison["strategy_summary"]["score_only_calibrator"]["policy_metrics"]["target_precision_95"],
            "multifeature_target_precision_90": comparison["strategy_summary"]["multifeature_calibrator"]["policy_metrics"]["target_precision_90"],
            "multifeature_target_precision_95": comparison["strategy_summary"]["multifeature_calibrator"]["policy_metrics"]["target_precision_95"],
        }
        for policy, metrics in expected_counts.items():
            reconciliation = failure["by_strategy_policy"][policy]["reconciliation"]
            self.assertEqual(reconciliation["accepted_count"], metrics["accepted_count"])
            self.assertEqual(reconciliation["accepted_correct_count"], metrics["accepted_correct_count"])
            self.assertEqual(reconciliation["accepted_incorrect_count"], metrics["accepted_incorrect_count"])
            self.assertEqual(reconciliation["no_target_accepted_count"], metrics["no_target_accepted_count"])
            self.assertEqual(reconciliation["wrong_target_accepted_count"], metrics["wrong_target_accepted_count"])
            self.assertTrue(reconciliation["accepted_balance_ok"])
            self.assertTrue(reconciliation["coverage_balance_ok"])
            self.assertTrue(reconciliation["incorrect_breakdown_ok"])
            self.assertTrue(reconciliation["correct_breakdown_ok"])
        self.assertEqual(
            len(failure["by_strategy_policy"]["multifeature_target_precision_90"]["no_target_false_acceptance"]),
            6,
        )
        self.assertEqual(
            len(failure["by_strategy_policy"]["multifeature_target_precision_90"]["confident_wrong_target_acceptance"]),
            7,
        )

    def test_frozen_formal_and_companies_house_artifacts_are_unchanged(self):
        self.assertEqual(len(FORMAL_ARTIFACTS), 45)
        self.assertEqual(_raw_sha(COMPANIES_HOUSE_ARTIFACT), COMPANIES_HOUSE_ARTIFACT_SHA)
        for relative_path, expected_sha in FORMAL_RESULT_SHAS.items():
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha)


if __name__ == "__main__":
    unittest.main()
