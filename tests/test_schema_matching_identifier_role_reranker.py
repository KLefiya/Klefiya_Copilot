import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core.mapping import identifier_role_reranker as reranker


def _case(
    case_id="scenario__field",
    family="bank_account",
    expected=("target.id",),
    source_label=1,
    candidates=None,
):
    if candidates is None:
        candidates = (
            reranker.CandidateRow("target.id", 1, 0.80),
            reranker.CandidateRow("target.name", 2, 0.79),
        )
    return reranker.RankingCase(
        scenario_id=case_id.split("__")[0],
        contract_family=family,
        case_id=case_id,
        source_field=case_id.split("__", 1)[-1],
        expected_targets=tuple(expected),
        original_recommendation=candidates[0].target if expected else None,
        source_identifier_label=source_label,
        source_features={name: 0.0 for name in reranker.detector.COMBINED_FEATURE_ORDER},
        candidates=tuple(candidates),
    )


class IdentifierRoleRerankerTest(unittest.TestCase):
    def test_target_token_inference_is_generic_and_handles_ambiguous_roles(self):
        self.assertGreaterEqual(reranker.infer_target_identifier_probability("customer.customer_id"), 0.8)
        self.assertLessEqual(reranker.infer_target_identifier_probability("sales_order_line.line_amount"), 0.2)
        self.assertEqual(reranker.TARGET_ROLE_LABELS["customer.payment_terms"], "ambiguous_excluded")
        self.assertNotIn("CompanyNumber", Path(reranker.__file__).read_text(encoding="utf-8"))
        self.assertGreater(reranker.infer_target_identifier_probability("external.CompanyNumber"), 0.5)

    def test_feature_schema_excludes_identity_and_ground_truth_inputs(self):
        schema = reranker.feature_schema()
        serialized = json.dumps(schema, sort_keys=True)
        forbidden = ("case_id", "scenario_id", "contract_family", "expected_targets", "ground_truth")
        feature_names = [item["name"] for item in schema["source_identifier_detector_features"]]
        feature_names.extend(item["name"] for item in schema["reranker_features"])
        for name in feature_names:
            self.assertFalse(any(token in name for token in forbidden), name)
        self.assertIn("expected target information", serialized)
        self.assertIn("target path/name/text", serialized)

    def test_compatibility_formula_and_signed_transform(self):
        compatibility = reranker.role_compatibility(0.8, 0.7)
        self.assertAlmostEqual(compatibility, 0.8 * 0.7 + 0.2 * 0.3)
        self.assertAlmostEqual(reranker.signed_role_compatibility(0.8, 0.7), 2 * compatibility - 1)

    def test_alpha_zero_preserves_v5_order_and_tie_breaking(self):
        case = _case(
            candidates=(
                reranker.CandidateRow("target.name", 1, 0.7),
                reranker.CandidateRow("target.id", 2, 0.7),
            )
        )
        prediction = reranker.evaluate_case(case, source_mode="heuristic", alpha=0.0, source_model=None, system="test")
        self.assertEqual(prediction["top_targets"][:2], ["target.name", "target.id"])
        self.assertEqual(prediction["best_expected_rank"], 2)

    def test_multi_target_full_coverage_uses_all_expected_targets(self):
        partial = _case(
            expected=("target.id", "target.code"),
            candidates=(
                reranker.CandidateRow("target.id", 1, 0.9),
                reranker.CandidateRow("target.name", 2, 0.8),
                reranker.CandidateRow("target.date", 3, 0.7),
            ),
        )
        full = _case(
            case_id="scenario__other",
            expected=("target.id", "target.code"),
            candidates=(
                reranker.CandidateRow("target.id", 1, 0.9),
                reranker.CandidateRow("target.code", 2, 0.8),
                reranker.CandidateRow("target.date", 3, 0.7),
            ),
        )
        partial_prediction = reranker.evaluate_case(partial, source_mode="heuristic", alpha=0.0, source_model=None, system="test")
        full_prediction = reranker.evaluate_case(full, source_mode="heuristic", alpha=0.0, source_model=None, system="test")
        metrics = reranker.ranking_metrics([partial_prediction, full_prediction])
        self.assertFalse(partial_prediction["multi_target_full_coverage_at_3"])
        self.assertTrue(full_prediction["multi_target_full_coverage_at_3"])
        self.assertEqual(metrics["multi_target_full_coverage_at_3"]["numerator"], 1)
        self.assertEqual(metrics["multi_target_full_coverage_at_3"]["denominator"], 2)

    def test_no_target_metric_is_preserved_as_rejection_semantics(self):
        no_target = _case(expected=(), candidates=(reranker.CandidateRow("target.id", 1, 0.9),))
        no_target = reranker.RankingCase(
            **{**no_target.__dict__, "original_recommendation": None}
        )
        prediction = reranker.evaluate_case(no_target, source_mode="heuristic", alpha=0.05, source_model=None, system="test")
        metrics = reranker.ranking_metrics([prediction])
        self.assertTrue(prediction["no_target_correct"])
        self.assertEqual(metrics["no_target_accuracy"]["value"], 1.0)

    def test_select_alpha_uses_inner_predictions_and_smaller_alpha_tie(self):
        cases = [_case(case_id=f"{family}__field", family=family) for family in ("a", "b", "c")]

        def fake_evaluate(inner_cases, *, source_mode, alpha, source_model, system):
            return [
                {
                    "case_id": case.case_id,
                    "contract_family": case.contract_family,
                    "target_bearing": True,
                    "multi_target": False,
                    "top1_correct": True,
                    "recall_at_3": True,
                    "v5_best_expected_rank": 2,
                    "best_expected_rank": 1 if alpha in (0.01, 0.02) else 2,
                    "no_target_correct": False,
                    "multi_target_full_coverage_at_3": False,
                    "v5_multi_target_full_coverage_at_3": False,
                }
                for case in inner_cases
            ]

        with mock.patch.object(reranker, "evaluate_cases", side_effect=fake_evaluate), mock.patch.object(
            reranker, "fit_source_detector", return_value={"fit": "inner"}
        ) as fit_mock:
            alpha, summary = reranker.select_alpha(cases, "learned")
        self.assertEqual(alpha, 0.01)
        self.assertEqual(sorted(item["alpha"] for item in summary), list(reranker.ALPHA_GRID))
        fitted_families = [{case.contract_family for case in call.args[0]} for call in fit_mock.call_args_list]
        self.assertTrue(all(len(families) == 2 for families in fitted_families))
        self.assertTrue(all(families != {"a", "b", "c"} for families in fitted_families))

    def test_select_alpha_respects_no_target_and_multi_target_constraints(self):
        cases = [_case(case_id=f"{family}__field", family=family) for family in ("a", "b", "c")]

        def fake_ranking_metrics(predictions):
            alpha = predictions[0]["alpha"]
            return {
                "target_link_mrr": {"value": 1.0 if alpha == 0.05 else 0.5},
                "no_target_accuracy": {"value": 0.0 if alpha == 0.05 else 1.0},
                "multi_target_full_coverage_at_3": {"value": 1.0},
            }

        def fake_evaluate(inner_cases, *, source_mode, alpha, source_model, system):
            return [{"alpha": alpha, "case_id": case.case_id} for case in inner_cases]

        with mock.patch.object(reranker, "evaluate_cases", side_effect=fake_evaluate), mock.patch.object(
            reranker, "ranking_metrics", side_effect=fake_ranking_metrics
        ):
            alpha, _summary = reranker.select_alpha(cases, "heuristic")
        self.assertEqual(alpha, 0.0)

    def test_development_loader_rejects_external_and_sealed_paths(self):
        with self.assertRaises(ValueError):
            reranker.load_development_v5_cases([Path("data/benchmarks/external/example.json")])
        with self.assertRaises(ValueError):
            reranker.load_development_v5_cases([Path("data/benchmarks/sealed/example.json")])
        with self.assertRaises(ValueError):
            reranker.load_development_v5_cases([Path("data/benchmarks/schema_matching_public_sealed_v1.json")])

    def test_json_model_can_reconstruct_probability_without_pickle(self):
        cases = reranker.load_development_v5_cases()
        model = reranker.fit_source_detector(cases)
        sample = cases[0].source_features
        probability = reranker.detector.predict_probability(model, sample)
        reloaded = json.loads(json.dumps(model, sort_keys=True))
        self.assertAlmostEqual(probability, reranker.detector.predict_probability(reloaded, sample), places=12)
        self.assertEqual(reloaded["model_type"], "standard_scaler_l2_logistic_regression_json_v1")

    def test_artifact_payloads_are_deterministic_and_privacy_scoped(self):
        cases = reranker.load_development_v5_cases()
        first = json.dumps(reranker.target_role_label_artifact(cases), sort_keys=True)
        second = json.dumps(reranker.target_role_label_artifact(cases), sort_keys=True)
        self.assertEqual(first, second)
        self.assertNotRegex(first, r"[A-Za-z]:\\\\|/home/|/Users/")
        self.assertNotIn("source_values", first)

    def test_write_artifacts_can_be_exercised_with_stubbed_experiment(self):
        stub_results = {
            "experiment_id": reranker.EXPERIMENT_ID,
            "experimental_only": True,
            "production_promoted": False,
            "runtime_integrated": False,
            "sealed_holdout_validated": False,
            "case_count": 1,
            "outer_cv": "stub",
            "alpha_selection": {},
            "folds": [],
            "pooled": {
                system: {
                    "metrics": {
                        "target_link_mrr": {"value": 1.0},
                        "no_target_accuracy": {"value": 1.0},
                        "multi_target_full_coverage_at_3": {"value": None},
                        "improved_cases": 0,
                        "regressed_cases": 0,
                        "unchanged_cases": 1,
                    },
                    "predictions": [],
                }
                for system in reranker.SYSTEMS
            },
        }
        case = _case(candidates=(reranker.CandidateRow("customer.customer_id", 1, 0.9),))
        temp_parent = reranker.PROJECT_ROOT / "data/experiments"
        with tempfile.TemporaryDirectory(dir=temp_parent) as tmpdir, mock.patch.object(
            reranker, "nested_contract_family_ablation", return_value=stub_results
        ), mock.patch.object(reranker, "load_development_v5_cases", return_value=[case]), mock.patch.object(
            reranker, "development_model", return_value={"_meta": {"development_only": True}}
        ):
            written = reranker.write_ablation_artifacts(Path(tmpdir))
            self.assertEqual(
                sorted(Path(path).name for path in written),
                [
                    "README.md",
                    "case_level_analysis.json",
                    "comparison.json",
                    "development_model.json",
                    "experiment_config.json",
                    "feature_schema.json",
                    "fold_manifest.json",
                    "nested_contract_family_results.json",
                    "target_field_role_labels.json",
                    "target_role_inference_evaluation.json",
                ],
            )


if __name__ == "__main__":
    unittest.main()
