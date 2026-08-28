from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS


BENCHMARK_ID = "fdic_bankfind_locations_v1"
SEALED_ROOT = PROJECT_ROOT / "data/benchmarks/sealed" / BENCHMARK_ID
SOURCE_PATH = SEALED_ROOT / "source_sample.csv"
METADATA_PATH = SEALED_ROOT / "source_metadata.json"
GROUND_TRUTH_PATH = SEALED_ROOT / "ground_truth.json"
PROTOCOL_PATH = SEALED_ROOT / "protocol.json"
README_PATH = SEALED_ROOT / "README.md"
LOCK_PATH = SEALED_ROOT / "fixture_lock.json"
REGISTRY_PATH = PROJECT_ROOT / "data/benchmarks/schema_matching_public_sealed_v1.json"
ATTEMPT_PATH = SEALED_ROOT / "first_evaluation_attempt.json"
RESULT_PATH = SEALED_ROOT / "first_evaluation.json"

SOURCE_COLUMNS = [
    "CERT",
    "UNINUM",
    "NAME",
    "OFFNAME",
    "CITY",
    "STNAME",
    "STALP",
    "ZIP",
    "COUNTY",
    "SERVTYPE",
    "SERVTYPE_DESC",
    "RUNDATE",
    "OFFDOM",
    "MAINOFF",
]

EXPECTED_SHAS = {
    "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_sample.csv": "775dbdac1a7c02bad64b8e1f4af117e227b43bf85d3e6e15e26db10c3f915da1",
    "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml": "5a6552fc04358c7c25f03225ed55a00f2655a9d46a53e144ad005541b9ee0e08",
    "data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json": "73b6597a8ee1fb81555c189bfd01aa0a39115251824ce4b1ce7737d5b1bf70b2",
    "data/experiments/schema_matching_v5_correctness_calibration_v1/feature_schema.json": "b0dd0aa958e4cfd6462da1d2e7217d98c3c06dd8fb00faa85d5bd561e70e5d18",
    "data/benchmarks/external/companies_house_customer_v1/first_evaluation_baseline_v4_v5.json": "d08584b1e77e59ba5362586d851e225e9d746f52eb01c2b268ffe2b68dc7edd8",
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json": "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json": "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
    "scripts/evaluate_fdic_bankfind_sealed_benchmark.py": "cca0d6b184698b4a8c8441a33656ee6b7e90edc48b6a4c5eb50b840b15e06568",
    "data/benchmarks/sealed/fdic_bankfind_locations_v1/first_evaluation_attempt.json": "62d35cda6b50aab0bcf98863d0184f884b8be9aa5a72646166146378b9b1dadd",
    "data/benchmarks/sealed/fdic_bankfind_locations_v1/first_evaluation.json": "504c9206b31a5016ccc8631c474a5d85eb52e60663046e87f5d0cfcc7b2ecfed",
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicSealedProtocolTest(unittest.TestCase):
    def test_source_sample_shape_headers_and_hash_are_frozen(self):
        with SOURCE_PATH.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(SOURCE_COLUMNS, list(rows[0].keys()))
        self.assertEqual(len(rows), 128)
        self.assertEqual(len(rows[0]), 14)
        self.assertEqual(len({row["UNINUM"] for row in rows}), 128)
        self.assertEqual(_raw_sha(SOURCE_PATH), EXPECTED_SHAS[SOURCE_PATH.relative_to(PROJECT_ROOT).as_posix()])

    def test_source_metadata_records_selection_license_sampling_and_privacy(self):
        metadata = _load_json(METADATA_PATH)
        comparison = metadata["candidate_dataset_comparison"]

        self.assertEqual(metadata["benchmark_id"], BENCHMARK_ID)
        self.assertTrue(metadata["sealed_holdout"])
        self.assertFalse(metadata["development_benchmark"])
        self.assertEqual(metadata["first_evaluation_status"], "not_run")
        self.assertEqual(metadata["source_index_name"], "locations_20260825141534")
        self.assertEqual(metadata["source_total_after_filter"], 71935)
        self.assertEqual(metadata["source_sample"]["row_count"], 128)
        self.assertEqual(metadata["source_sample"]["field_count"], 14)
        self.assertEqual(metadata["source_sample"]["raw_sha256"], _raw_sha(SOURCE_PATH))
        self.assertEqual(metadata["license"]["name"], "U.S. Public Domain Mark 1.0")
        self.assertEqual(metadata["sampling"]["field_name_normalization"], "none; source headers are copied exactly from the selected FDIC API fields")
        self.assertEqual(metadata["sampling"]["source_value_normalization"], "none; values are written as returned by the FDIC API except UTF-8 CSV serialization and CSV escaping")
        self.assertIn("No street address", metadata["sampling"]["sensitive_column_handling"])
        self.assertEqual([item["name"] for item in comparison], [
            "FDIC BankFind Suite locations",
            "FDIC BankFind Suite institutions",
            "World Bank Documents and Reports API",
        ])
        self.assertEqual(comparison[0]["decision"], "selected")
        self.assertEqual(comparison[1]["decision"], "not_selected")
        self.assertEqual(comparison[2]["decision"], "not_selected")

    def test_ground_truth_counts_targets_and_exclusions_are_preregistered(self):
        ground_truth = _load_json(GROUND_TRUTH_PATH)
        mappings = {item["source_field"]: item for item in ground_truth["mappings"]}

        self.assertEqual(set(mappings), set(SOURCE_COLUMNS))
        self.assertEqual(ground_truth["counts"]["case_count"], 14)
        self.assertEqual(ground_truth["counts"]["single_target_case_count"], 3)
        self.assertEqual(ground_truth["counts"]["multi_target_case_count"], 0)
        self.assertEqual(ground_truth["counts"]["no_target_case_count"], 11)
        self.assertEqual(ground_truth["counts"]["expected_target_link_count"], 3)
        self.assertEqual(mappings["UNINUM"]["expected_targets"], ["bank_branch.bank_key"])
        self.assertEqual(mappings["NAME"]["expected_targets"], ["bank_branch.bank_name"])
        self.assertEqual(mappings["OFFNAME"]["expected_targets"], ["bank_branch.bank_name"])
        self.assertEqual(mappings["CERT"]["expected_targets"], [])
        self.assertEqual(mappings["STALP"]["expected_targets"], [])
        self.assertEqual(mappings["MAINOFF"]["expected_targets"], [])
        self.assertIn("CERT", {item["source_field"] for item in ground_truth["explicit_exclusions"]})
        self.assertIn("STALP", {item["source_field"] for item in ground_truth["explicit_exclusions"]})
        self.assertIn("MAINOFF", {item["source_field"] for item in ground_truth["explicit_exclusions"]})

    def test_protocol_freezes_candidate_systems_metrics_and_decision_rule_without_results(self):
        protocol = _load_json(PROTOCOL_PATH)
        systems = {item["system_id"]: item for item in protocol["candidate_systems"]}

        self.assertEqual(protocol["first_evaluation_status"], "not_run")
        self.assertIsNone(protocol["result_artifact_path"])
        self.assertIsNone(protocol["comparison_artifact_path"])
        self.assertIsNone(protocol["failure_analysis_artifact_path"])
        self.assertTrue(protocol["leakage_prevention"]["no_result_artifact_exists"])
        self.assertEqual(set(systems), {
            "baseline",
            "precision_tiered_v4",
            "precision_tiered_v5",
            "existing_v5_policy",
            "score_only_calibrator_target_precision_95",
            "multifeature_calibrator_target_precision_95",
        })
        self.assertEqual(systems["score_only_calibrator_target_precision_95"]["threshold"], 0.68142199)
        self.assertEqual(systems["score_only_calibrator_target_precision_95"]["model_raw_sha256"], EXPECTED_SHAS["data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json"])
        self.assertEqual(protocol["evaluation_roles"]["primary_decision_comparison"], [
            "existing_v5_policy",
            "score_only_calibrator_target_precision_95",
        ])
        self.assertIn("multifeature_calibrator_target_precision_95", protocol["evaluation_roles"]["secondary_diagnostics"])
        self.assertIn("Secondary diagnostic results must not override the primary decision rule.", protocol["evaluation_roles"]["secondary_diagnostic_limits"])
        self.assertEqual(protocol["limitations"]["positive_bearing_case_count"], 3)
        self.assertIn("unseen-source holdout", protocol["limitations"]["contract_family_novelty"])
        self.assertEqual(protocol["limitations"]["multi_target_metrics"], "N/A because the frozen denominator is 0.")
        self.assertTrue(protocol["primary_decision_rule"]["no_tuning_after_unseal"])
        self.assertIn("accepted_precision", protocol["metrics"]["selective_acceptance_metrics"])
        self.assertIn("target_link_recall_at_3", protocol["metrics"]["ranking_metrics"])
        self.assertIn("aurc", protocol["metrics"]["calibration_metrics"])

    def test_fixture_lock_matches_current_frozen_inputs_and_has_no_self_hash(self):
        lock = _load_json(LOCK_PATH)

        self.assertFalse(lock["self_hash_included"])
        self.assertEqual(lock["first_evaluation_status"], "not_run")
        self.assertEqual(lock["source_sample_shape"], {"rows": 128, "columns": 14})
        for relative_path, expected_sha in lock["raw_sha256"].items():
            if relative_path == README_PATH.relative_to(PROJECT_ROOT).as_posix():
                self.assertEqual(expected_sha, "8bd759add6c68619b43b7489984c476da9a5fbc481c1fa5a64b25a64b505550d")
                continue
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha, relative_path)
        self.assertNotIn(LOCK_PATH.relative_to(PROJECT_ROOT).as_posix(), lock["raw_sha256"])

    def test_independent_registry_is_not_wired_to_current_benchmark_runner_or_development_training(self):
        registry = _load_json(REGISTRY_PATH)
        benchmark_runner = (PROJECT_ROOT / "src/core/mapping/benchmark.py").read_text(encoding="utf-8")
        ltr_source = (PROJECT_ROOT / "src/core/mapping/learning_to_rank.py").read_text(encoding="utf-8")
        calibration_source = (PROJECT_ROOT / "src/core/mapping/v5_correctness_calibration.py").read_text(encoding="utf-8")

        self.assertEqual(registry["_meta"]["benchmark_id"], "schema_matching_public_sealed_v1")
        self.assertTrue(registry["_meta"]["sealed_holdout"])
        self.assertFalse(registry["_meta"]["development_benchmark"])
        self.assertEqual(registry["_meta"]["first_evaluation_status"], "not_run")
        self.assertNotIn("schema_matching_public_sealed_v1", benchmark_runner)
        self.assertNotIn("schema_matching_public_sealed_v1", ltr_source)
        self.assertNotIn("schema_matching_public_sealed_v1", calibration_source)
        self.assertNotIn("fdic_bankfind_locations_v1", ltr_source)
        self.assertNotIn("fdic_bankfind_locations_v1", calibration_source)

    def test_post_evaluation_artifacts_are_frozen_without_protocol_backwrite_or_source_value_leaks(self):
        forbidden_paths = [
            SEALED_ROOT / "results",
            SEALED_ROOT / "comparison.json",
            SEALED_ROOT / "evaluation.json",
            SEALED_ROOT / "failure_analysis.json",
            SEALED_ROOT / "development_model.json",
        ]
        for path in forbidden_paths:
            self.assertFalse(path.exists(), path)

        protocol = _load_json(PROTOCOL_PATH)
        self.assertEqual(protocol["first_evaluation_status"], "not_run")
        self.assertIsNone(protocol["result_artifact_path"])
        self.assertIsNone(protocol["comparison_artifact_path"])
        self.assertIsNone(protocol["failure_analysis_artifact_path"])

        self.assertTrue(ATTEMPT_PATH.exists())
        self.assertTrue(RESULT_PATH.exists())
        self.assertEqual(_raw_sha(ATTEMPT_PATH), EXPECTED_SHAS[ATTEMPT_PATH.relative_to(PROJECT_ROOT).as_posix()])
        self.assertEqual(_raw_sha(RESULT_PATH), EXPECTED_SHAS[RESULT_PATH.relative_to(PROJECT_ROOT).as_posix()])

        attempt = _load_json(ATTEMPT_PATH)
        result = _load_json(RESULT_PATH)
        self.assertEqual(attempt["protocol_sha"], _raw_sha(PROTOCOL_PATH))
        self.assertEqual(attempt["runner_sha"], EXPECTED_SHAS["scripts/evaluate_fdic_bankfind_sealed_benchmark.py"])
        self.assertEqual(attempt["git_head"], "620c49bbd22a6196b8b1eb7ee508513b1053d670")
        self.assertEqual(result["git_head"], "620c49bbd22a6196b8b1eb7ee508513b1053d670")
        self.assertEqual(result["frozen_input_shas"]["protocol"], _raw_sha(PROTOCOL_PATH))
        self.assertEqual(result["runner"]["raw_sha256"], EXPECTED_SHAS["scripts/evaluate_fdic_bankfind_sealed_benchmark.py"])
        self.assertFalse(result["production_promoted"])
        self.assertFalse(result["post_unseal_tuning_performed"])
        self.assertEqual(len(result["per_case_audit_records"]), 14)
        self.assertEqual(len({record["case_id"] for record in result["per_case_audit_records"]}), 14)
        self.assertEqual(result["corpus_counts"]["case_count"], len(result["per_case_audit_records"]))
        for metrics in result["selective_policy_counts"].values():
            self.assertEqual(metrics["accepted_count"] + metrics["review_count"], metrics["case_count"])
            self.assertEqual(metrics["accepted_correct_count"] + metrics["accepted_incorrect_count"], metrics["accepted_count"])

        fixture_text_paths = [METADATA_PATH, GROUND_TRUTH_PATH, PROTOCOL_PATH, README_PATH, LOCK_PATH, REGISTRY_PATH, ATTEMPT_PATH, RESULT_PATH]
        text_blob = "\n".join(path.read_text(encoding="utf-8") for path in fixture_text_paths)
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\", text_blob))
        self.assertIsNone(re.search(r"(^|[\"'\\s])/(Users|home|tmp|var|mnt)/", text_blob))

        with SOURCE_PATH.open(newline="", encoding="utf-8") as handle:
            source_values = {
                value
                for row in csv.DictReader(handle)
                for value in row.values()
                if len(value) >= 8 and not re.fullmatch(r"[0-9/]+", value)
            }
        for value in source_values:
            self.assertNotIn(value, text_blob)

    def test_frozen_external_and_formal_artifacts_remain_unchanged(self):
        self.assertEqual(len(FORMAL_ARTIFACTS), 45)
        self.assertFalse(any(BENCHMARK_ID in artifact for artifact in FORMAL_ARTIFACTS))
        for relative_path, expected_sha in EXPECTED_SHAS.items():
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha, relative_path)


if __name__ == "__main__":
    unittest.main()
