from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS
from src.core.mapping.benchmark import benchmark_run_specs, load_benchmark
from src.core.mapping.learning_to_rank import (
    FEATURE_ORDER,
    CandidateFeatureRecord,
    build_pairwise_training_data,
    feature_schema,
    load_development_corpus,
    metric_summary,
    model_score,
)


EXPERIMENT_ROOT = PROJECT_ROOT / "data/experiments/schema_matching_pairwise_ltr_v1"
FORMAL_RESULT_SHAS = {
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json": "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json": "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
}
COMPANIES_HOUSE_FROZEN_SHAS = {
    "data/benchmarks/external/companies_house_customer_v1/first_evaluation_baseline_v4_v5.json": "d08584b1e77e59ba5362586d851e225e9d746f52eb01c2b268ffe2b68dc7edd8",
    "scripts/evaluate_companies_house_external_benchmark.py": "11618569e69c1bd360f1b738e6d72388c146add504f257f056fc22d30302edb4",
}


def _load_json(relative_path: str) -> dict:
    return json.loads((EXPERIMENT_ROOT / relative_path).read_text(encoding="utf-8"))


def _raw_sha(relative_path: str) -> str:
    import hashlib

    return hashlib.sha256((PROJECT_ROOT / relative_path).read_bytes()).hexdigest()


def _record(case_id: str, target: str, expected: tuple[str, ...]) -> CandidateFeatureRecord:
    return CandidateFeatureRecord(
        scenario_id="scenario",
        contract_family="family",
        case_id=case_id,
        source_field="source",
        candidate_target=target,
        expected_targets=expected,
        baseline_rank=1,
        v5_rank=1,
        features=tuple(0.1 for _ in FEATURE_ORDER),
    )


class SchemaMatchingPairwiseLtrTest(unittest.TestCase):
    def test_feature_order_is_fixed_and_has_no_identity_or_label_features(self):
        schema = feature_schema()

        self.assertEqual(tuple(schema["feature_order"]), FEATURE_ORDER)
        self.assertEqual(len(FEATURE_ORDER), 17)
        for feature in FEATURE_ORDER:
            lowered = feature.lower()
            for forbidden in ("source_field", "qualified", "scenario", "contract", "case", "expected", "rank"):
                self.assertNotIn(forbidden, lowered)
        self.assertFalse(schema["ground_truth_derived_features"])

    def test_corpus_counts_match_current_development_scope(self):
        benchmarks, cases = load_development_corpus()

        self.assertEqual(len(cases), 107)
        self.assertEqual(sum(len(case.expected_targets) for case in cases), 95)
        self.assertEqual(len({case.scenario_id for case in cases}), 7)
        self.assertEqual(len({case.contract_family for case in cases}), 5)
        specs = [spec for benchmark in benchmarks for spec in benchmark_run_specs(benchmark)]
        self.assertEqual(len(specs), 7)
        for spec in specs:
            self.assertNotIn("cases", spec)
            self.assertNotIn("answer_source_path", spec)

    def test_pairwise_rows_do_not_pair_expected_targets_against_each_other_or_use_no_target_cases(self):
        records = [
            _record("multi", "target_a", ("target_a", "target_b")),
            _record("multi", "target_b", ("target_a", "target_b")),
            _record("multi", "target_c", ("target_a", "target_b")),
            _record("no_target", "target_a", ()),
            _record("no_target", "target_b", ()),
        ]

        x, y, summary = build_pairwise_training_data(records)

        self.assertEqual(summary["pair_count"], 4)
        self.assertEqual(summary["target_bearing_case_count"], 1)
        self.assertEqual(summary["positive_link_count"], 2)
        self.assertEqual(summary["no_target_cases_excluded_from_pair_training"], 1)
        self.assertEqual(x.shape, (4, len(FEATURE_ORDER)))
        self.assertEqual(y.tolist(), [1.0, 0.0, 1.0, 0.0])

    def test_fold_manifests_keep_grouped_train_and_test_sets_disjoint(self):
        manifest = _load_json("fold_manifest.json")

        self.assertTrue(manifest["leakage_controls"]["scenario_folds_are_grouped"])
        self.assertTrue(manifest["leakage_controls"]["contract_family_folds_are_grouped"])
        self.assertEqual(manifest["leakage_controls"]["scaler_fit_scope"], "train_fold_only")
        self.assertEqual(manifest["leakage_controls"]["ranker_fit_scope"], "train_fold_only")
        self.assertFalse(manifest["leakage_controls"]["field_level_random_split"])
        for fold in manifest["leave_one_scenario_out"]:
            self.assertFalse(set(fold["train_scenarios"]) & set(fold["held_out_scenarios"]))
        for fold in manifest["leave_one_contract_out"]:
            self.assertFalse(set(fold["train_contract_families"]) & set(fold["held_out_contract_families"]))

    def test_pooled_metrics_are_recomputed_from_case_predictions(self):
        loso = _load_json("leave_one_scenario_out.json")
        contract = _load_json("leave_one_contract_out.json")

        for body in (loso, contract):
            for scorer, cases in body["pooled_case_results"].items():
                self.assertEqual(metric_summary(cases), body["pooled_metrics"][scorer])
                self.assertEqual(body["pooled_metrics"][scorer]["no_target_accuracy"], "not_applicable")
                self.assertEqual(body["pooled_metrics"][scorer]["abstention_learning"], "not_implemented_in_ltr_v1")

    def test_development_model_json_can_recompute_candidate_scores_deterministically(self):
        model = _load_json("development_model.json")
        vector = tuple(0.25 for _ in model["feature_order"])

        self.assertFalse(model["_meta"]["production_model"])
        self.assertFalse(model["_meta"]["sealed_holdout_evaluated"])
        self.assertEqual(model["feature_order"], list(FEATURE_ORDER))
        self.assertEqual(len(model["linear_coefficients"]), len(FEATURE_ORDER))
        self.assertEqual(len(model["scaler_mean"]), len(FEATURE_ORDER))
        self.assertEqual(len(model["scaler_scale"]), len(FEATURE_ORDER))
        self.assertEqual(model_score(model, vector), model_score(model, vector))
        self.assertTrue(math.isfinite(model_score(model, vector)))

    def test_comparison_records_ltr_deltas_coefficients_and_unresolved_failures(self):
        comparison = _load_json("comparison.json")

        self.assertIn("leave_one_scenario_out", comparison["metrics"])
        self.assertIn("leave_one_contract_out", comparison["metrics"])
        self.assertIn("ltr_improved_vs_baseline", comparison["case_deltas"]["leave_one_scenario_out"])
        self.assertIn("ltr_regressed_vs_v5", comparison["case_deltas"]["leave_one_contract_out"])
        self.assertGreater(len(comparison["coefficient_summary"]["top_absolute_coefficients"]), 0)
        self.assertIn("alias_or_heuristic_dependency_visible", comparison["coefficient_summary"])
        self.assertIn("expected_target_missing_from_top3_cases", comparison["metrics"]["leave_one_scenario_out"]["learned_pairwise_linear_v1"])

    def test_formal_and_companies_house_frozen_artifacts_are_unchanged(self):
        self.assertEqual(len(FORMAL_ARTIFACTS), 45)
        self.assertFalse(any("schema_matching_pairwise_ltr_v1" in artifact for artifact in FORMAL_ARTIFACTS))
        for relative_path, expected_sha in FORMAL_RESULT_SHAS.items():
            self.assertEqual(_raw_sha(relative_path), expected_sha)
        for relative_path, expected_sha in COMPANIES_HOUSE_FROZEN_SHAS.items():
            self.assertEqual(_raw_sha(relative_path), expected_sha)


if __name__ == "__main__":
    unittest.main()
