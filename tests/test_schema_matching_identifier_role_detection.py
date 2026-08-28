from __future__ import annotations

import json
import math
import unittest

from src.core.mapping import identifier_role_detection as detector


class IdentifierRoleFeatureExtractionTest(unittest.TestCase):
    def test_feature_extraction_is_deterministic(self) -> None:
        values = ["A001", "A002", "A003", "A004"]
        self.assertEqual(detector.extract_value_profile_features(values), detector.extract_value_profile_features(values))

    def test_empty_and_null_column_is_safe(self) -> None:
        features = detector.extract_value_profile_features(["", None, ""])
        self.assertEqual(features["row_count"], 3.0)
        self.assertEqual(features["non_null_count"], 0.0)
        self.assertEqual(features["null_ratio"], 1.0)
        self.assertEqual(features["uniqueness_ratio"], 0.0)
        self.assertTrue(all(math.isfinite(value) for value in features.values()))

    def test_numeric_identifier_signals(self) -> None:
        features = detector.extract_value_profile_features(["0001", "0002", "0003", "0004"])
        self.assertEqual(features["numeric_only_value_ratio"], 1.0)
        self.assertEqual(features["leading_zero_ratio"], 1.0)
        self.assertEqual(features["fixed_width_ratio"], 1.0)
        self.assertGreaterEqual(features["integer_monotonicity_signal"], 0.9)
        self.assertEqual(detector.heuristic_predict(features), 1)

    def test_alphanumeric_identifier_signals(self) -> None:
        features = detector.extract_value_profile_features(["AB10", "AB11", "AB12", "AB13"])
        self.assertEqual(features["alphanumeric_mixed_ratio"], 1.0)
        self.assertEqual(features["dominant_normalized_pattern_ratio"], 1.0)
        self.assertEqual(detector.heuristic_predict(features), 1)

    def test_uuid_like_identifier_signals(self) -> None:
        values = [
            "123e4567-e89b-12d3-a456-426614174000",
            "123e4567-e89b-12d3-a456-426614174001",
            "123e4567-e89b-12d3-a456-426614174002",
        ]
        features = detector.extract_value_profile_features(values)
        self.assertEqual(features["uuid_like_ratio"], 1.0)
        self.assertEqual(features["fixed_width_ratio"], 1.0)
        self.assertEqual(detector.heuristic_predict(features), 1)

    def test_sequential_integer_signal(self) -> None:
        features = detector.extract_value_profile_features(["10", "11", "12", "13", "14"])
        self.assertEqual(features["integer_sequentiality_signal"], 1.0)

    def test_ordinary_name_text_column_is_not_identifier_by_heuristic(self) -> None:
        features = detector.extract_value_profile_features(["Alice Smith", "Bob Jones", "Carol White", "Dan Brown"])
        self.assertGreater(features["alphabetic_character_fraction"], 0.7)
        self.assertEqual(detector.heuristic_predict(features), 0)

    def test_low_cardinality_categorical_column_is_not_identifier_by_heuristic(self) -> None:
        features = detector.extract_value_profile_features(["open", "closed", "open", "closed", "open"])
        self.assertLess(features["uniqueness_ratio"], 0.5)
        self.assertEqual(detector.heuristic_predict(features), 0)

    def test_pattern_normalization_does_not_preserve_raw_value(self) -> None:
        self.assertEqual(detector.normalize_value_pattern("AB-001"), "Ax9")
        self.assertEqual(detector.normalize_value_pattern("abc 123", compress=False), "aaax999")


class IdentifierRoleDataBoundaryTest(unittest.TestCase):
    def test_annotations_cover_development_corpus_and_include_no_target_identifier_semantics(self) -> None:
        samples, excluded = detector.build_samples()
        annotation_body = detector.annotation_artifact()
        self.assertEqual(annotation_body["counts"]["total_annotations"], 107)
        self.assertEqual(len(samples) + len(excluded), 107)
        self.assertEqual(len(excluded), 0)
        no_target_identifiers = {
            sample.source_field
            for sample in samples
            if sample.source_field in {"legacy_created_by", "legacy_operator", "header_audit_marker", "migration_batch_label", "ocid"}
            and sample.label == 1
        }
        self.assertEqual(no_target_identifiers, {"legacy_created_by", "legacy_operator", "header_audit_marker", "migration_batch_label", "ocid"})

    def test_loader_rejects_external_and_sealed_paths(self) -> None:
        with self.assertRaises(ValueError):
            detector.load_development_source_cases([detector.PROJECT_ROOT / "data/benchmarks/schema_matching_public_sealed_v1.json"])
        with self.assertRaises(ValueError):
            detector.load_development_source_cases([detector.PROJECT_ROOT / "data/benchmarks/external/companies_house_customer_v1/protocol_lock.json"])

    def test_feature_schema_and_matrix_have_no_identity_or_ground_truth_inputs(self) -> None:
        schema = detector.feature_schema()
        feature_names = schema["feature_order"]
        forbidden = detector.FORBIDDEN_FEATURE_TOKENS
        for name in feature_names:
            for token in forbidden:
                self.assertNotIn(token, name)
        samples, _excluded = detector.build_samples()
        matrix, labels = detector._matrix(samples[:5], detector.COMBINED_FEATURE_ORDER)
        self.assertEqual(matrix.shape, (5, len(detector.COMBINED_FEATURE_ORDER)))
        self.assertEqual(labels.shape, (5,))
        self.assertFalse(any(sample.scenario_id in detector.COMBINED_FEATURE_ORDER for sample in samples[:5]))
        self.assertFalse(any(sample.source_field in detector.COMBINED_FEATURE_ORDER for sample in samples[:5]))

    def test_raw_values_do_not_enter_artifact_payloads(self) -> None:
        payload = {
            "schema": detector.feature_schema(),
            "annotations": detector.annotation_artifact(),
        }
        text = json.dumps(payload, sort_keys=True)
        self.assertNotIn("AB10", text)
        self.assertNotIn("Alice Smith", text)


class IdentifierRoleEvaluationTest(unittest.TestCase):
    def _tiny_samples(self) -> list[detector.DetectorSample]:
        return [
            detector.DetectorSample("s1", "f1", "s1__id", "id_col", detector.LABEL_IDENTIFIER, 1, detector.extract_value_profile_features(["001", "002", "003", "004"])),
            detector.DetectorSample("s1", "f1", "s1__name", "name_col", detector.LABEL_NON_IDENTIFIER, 0, detector.extract_value_profile_features(["red", "blue", "red", "green"])),
            detector.DetectorSample("s2", "f2", "s2__code", "code_col", detector.LABEL_IDENTIFIER, 1, detector.extract_value_profile_features(["A1", "A2", "A3", "A4"])),
            detector.DetectorSample("s2", "f2", "s2__text", "text_col", detector.LABEL_NON_IDENTIFIER, 0, detector.extract_value_profile_features(["long text", "other text", "more words", "plain words"])),
        ]

    def test_scaler_fits_training_fold_only(self) -> None:
        samples = self._tiny_samples()
        results = detector.grouped_evaluation(samples, "scenario_id", ("s1", "s2"))
        for fold in results["folds"]:
            self.assertFalse(fold["held_out_used_for_scaler_or_model_fit"])
            self.assertEqual(fold["train_case_count"], 2)
            self.assertEqual(fold["held_out_case_count"], 2)

    def test_pooled_metrics_reconcile(self) -> None:
        predictions = [
            {"label": 1, "predicted_label": 1, "probability": 0.9},
            {"label": 0, "predicted_label": 1, "probability": 0.8},
            {"label": 0, "predicted_label": 0, "probability": 0.1},
            {"label": 1, "predicted_label": 0, "probability": 0.2},
        ]
        metrics = detector.metrics(predictions)
        cm = metrics["confusion_matrix"]
        self.assertEqual(cm, {"tp": 1, "fp": 1, "tn": 1, "fn": 1})
        self.assertEqual(cm["tp"] + cm["fp"] + cm["tn"] + cm["fn"], metrics["case_count"])
        self.assertEqual(metrics["identifier_precision"], 0.5)
        self.assertEqual(metrics["identifier_recall"], 0.5)

    def test_json_model_rebuilds_prediction_without_pickle(self) -> None:
        samples = self._tiny_samples()
        model = detector.fit_logistic(samples, detector.COMBINED_FEATURE_ORDER)
        probability_before = detector.predict_probability(model, samples[0].features)
        reloaded = json.loads(json.dumps(model, sort_keys=True))
        probability_after = detector.predict_probability(reloaded, samples[0].features)
        self.assertAlmostEqual(probability_before, probability_after, places=10)
        self.assertNotIn("pickle", json.dumps(reloaded).lower())

    def test_ablation_feature_sets_are_fixed(self) -> None:
        self.assertEqual(set(detector.LOGISTIC_FEATURE_SETS), {"distribution_only", "pattern_only", "combined"})
        self.assertEqual(detector.LOGISTIC_FEATURE_SETS["combined"], detector.COMBINED_FEATURE_ORDER)

    def test_development_model_flags_are_not_promoted(self) -> None:
        samples = self._tiny_samples()
        model = detector.development_model(samples)
        self.assertTrue(model["_meta"]["development_only"])
        self.assertFalse(model["_meta"]["production_promoted"])
        self.assertFalse(model["_meta"]["ranking_integrated"])
        self.assertFalse(model["_meta"]["sealed_holdout_validated"])

    def test_tiny_grouped_evaluation_is_deterministic(self) -> None:
        samples = self._tiny_samples()
        first = detector.grouped_evaluation(samples, "scenario_id", ("s1", "s2"))
        second = detector.grouped_evaluation(samples, "scenario_id", ("s1", "s2"))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
