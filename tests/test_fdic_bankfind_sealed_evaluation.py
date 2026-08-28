from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import evaluate_fdic_bankfind_sealed_benchmark as runner


REAL_ATTEMPT = PROJECT_ROOT / "data/benchmarks/sealed/fdic_bankfind_locations_v1/first_evaluation_attempt.json"
REAL_RESULT = PROJECT_ROOT / "data/benchmarks/sealed/fdic_bankfind_locations_v1/first_evaluation.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _feature_values(value: float) -> dict[str, float]:
    return {
        "top1_v5_score": value,
        "v5_score_margin_top1_top2": value / 10,
        "top1_semantic_score": value,
        "semantic_score_margin_top1_top2": value / 10,
        "top1_baseline_score": value,
        "top1_lexical_overlap": value / 2,
        "top1_fuzzy_score": value,
        "top1_value_pattern_score": 0.0,
        "top1_resource_context_score": 0.0,
        "top1_identifier_adjusted_score": value,
        "top1_type_gate": 1.0,
        "top1_v5_top1_eligible": 1.0,
        "eligible_candidate_count": 1.0,
        "candidate_count": 3.0,
    }


class TempSealedFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fixture = root / "data/benchmarks/sealed/fdic_bankfind_locations_v1"
        self.protocol = self.fixture / "protocol.json"
        self.lock = self.fixture / "fixture_lock.json"
        self.source = self.fixture / "source_sample.csv"
        self.ground_truth = self.fixture / "ground_truth.json"
        self.metadata = self.fixture / "source_metadata.json"
        self.registry = root / "data/benchmarks/schema_matching_public_sealed_v1.json"
        self.model = root / "data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json"
        self.contract = root / "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml"
        self.target_root = root / "data/benchmarks/fixtures/bank_account/target"
        self.runner_path = root / "scripts/evaluate_fdic_bankfind_sealed_benchmark.py"
        self.attempt = self.fixture / "first_evaluation_attempt.json"
        self.result = self.fixture / "first_evaluation.json"
        self._write()
        self.paths = runner.SealedPaths(
            repo_root=root,
            registry_path=self.registry,
            fixture_dir=self.fixture,
            protocol_path=self.protocol,
            fixture_lock_path=self.lock,
            source_path=self.source,
            ground_truth_path=self.ground_truth,
            source_metadata_path=self.metadata,
            calibration_model_path=self.model,
            target_contract_path=self.contract,
            target_data_root_path=self.target_root,
            attempt_marker_path=self.attempt,
            result_path=self.result,
            runner_path=self.runner_path,
        )
        self.expectations = runner.FrozenExpectations(
            protocol_sha=_sha(self.protocol),
            fixture_lock_sha=_sha(self.lock),
            source_sample_sha=_sha(self.source),
            calibration_model_sha=_sha(self.model),
        )

    def _write(self) -> None:
        self.fixture.mkdir(parents=True)
        self.target_root.mkdir(parents=True)
        self.runner_path.parent.mkdir(parents=True)
        self.runner_path.write_text("# synthetic temp runner\n", encoding="utf-8", newline="\n")
        self.contract.parent.mkdir(parents=True)
        self.contract.write_text("name: bank-account-benchmark\n", encoding="utf-8", newline="\n")
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(runner.EXPECTED_SOURCE_COLUMNS), lineterminator="\n")
            writer.writeheader()
            for index in range(128):
                writer.writerow({field: f"{field}_{index}" for field in runner.EXPECTED_SOURCE_COLUMNS})
        mappings = []
        positives = {
            "UNINUM": ["bank_branch.bank_key"],
            "NAME": ["bank_branch.bank_name"],
            "OFFNAME": ["bank_branch.bank_name"],
        }
        for field in runner.EXPECTED_SOURCE_COLUMNS:
            targets = positives.get(field, [])
            mappings.append({"source_field": field, "expected_targets": targets, "expected_no_target": not targets})
        _write_json(
            self.ground_truth,
            {
                "benchmark_id": runner.BENCHMARK_ID,
                "sealed_holdout": True,
                "first_evaluation_status": "not_run",
                "counts": {
                    "row_count": 128,
                    "source_field_count": 14,
                    "case_count": 14,
                    "target_bearing_source_field_count": 3,
                    "single_target_case_count": 3,
                    "multi_target_case_count": 0,
                    "no_target_case_count": 11,
                    "expected_target_link_count": 3,
                    "excluded_source_field_count": 0,
                },
                "mappings": mappings,
            },
        )
        _write_json(self.metadata, {"source_sample": {"raw_sha256": _sha(self.source)}})
        _write_json(
            self.registry,
            {
                "_meta": {
                    "benchmark_id": "schema_matching_public_sealed_v1",
                    "first_evaluation_status": "not_run",
                    "result_artifact_path": None,
                    "comparison_artifact_path": None,
                    "failure_analysis_artifact_path": None,
                },
                "scenarios": [
                    {
                        "scenario_id": "fdic_bankfind_locations",
                        "split": "sealed_holdout",
                        "source_path": "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_sample.csv",
                        "contract_path": "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml",
                        "data_root_path": "data/benchmarks/fixtures/bank_account/target",
                    }
                ],
            },
        )
        _write_json(
            self.model,
            {
                "_meta": {"production_promoted": False, "development_only": True},
                "models": {
                    "score_only_calibrator": _model(["top1_v5_score", "v5_score_margin_top1_top2"], 0.6),
                    "multifeature_calibrator": _model(list(_feature_values(0.0)), 0.9),
                },
            },
        )
        protocol = {
            "protocol_id": "fdic_bankfind_locations_v1_protocol",
            "benchmark_id": runner.BENCHMARK_ID,
            "first_evaluation_status": "not_run",
            "result_artifact_path": None,
            "comparison_artifact_path": None,
            "failure_analysis_artifact_path": None,
            "target_contract": {
                "path": "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml",
                "data_root_path": "data/benchmarks/fixtures/bank_account/target",
                "raw_sha256": _sha(self.contract),
            },
            "evaluation_roles": {
                "primary_decision_comparison": list(runner.PRIMARY_SYSTEMS),
                "secondary_diagnostics": list(runner.SECONDARY_SYSTEMS),
                "secondary_diagnostic_limits": [
                    "precision_tiered_v4 is not the current promotion candidate.",
                    "multifeature_calibrator_target_precision_95 is not the current promotion candidate.",
                    "Secondary diagnostic results must not override the primary decision rule.",
                ],
            },
            "limitations": {
                "positive_bearing_case_count": 3,
                "no_target_focus": "11 no-target cases",
                "contract_family_novelty": "unseen-source holdout, not an unseen-contract-family holdout",
                "multi_target_metrics": "N/A because the frozen denominator is 0.",
            },
        }
        _write_json(self.protocol, protocol)
        lock = {
            "benchmark_id": runner.BENCHMARK_ID,
            "first_evaluation_status": "not_run",
            "raw_sha256": {
                "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_sample.csv": _sha(self.source),
                "data/benchmarks/sealed/fdic_bankfind_locations_v1/ground_truth.json": _sha(self.ground_truth),
                "data/benchmarks/sealed/fdic_bankfind_locations_v1/source_metadata.json": _sha(self.metadata),
                "data/benchmarks/sealed/fdic_bankfind_locations_v1/protocol.json": _sha(self.protocol),
                "data/experiments/schema_matching_v5_correctness_calibration_v1/development_model.json": _sha(self.model),
                "data/benchmarks/fixtures/bank_account/contract/datapackage.yaml": _sha(self.contract),
            },
        }
        _write_json(self.lock, lock)


def _model(feature_order: list[str], threshold: float) -> dict:
    return {
        "feature_order": feature_order,
        "scaler_mean": [0.0 for _ in feature_order],
        "scaler_scale": [1.0 for _ in feature_order],
        "linear_coefficients": [1.0 for _ in feature_order],
        "intercept": 0.0,
        "thresholds": {"target_precision_95": {"threshold": threshold}},
    }


def _clean_git(_paths: runner.SealedPaths) -> dict[str, str]:
    return {"branch": "main", "head": "abc123", "status_short": ""}


def _dirty_git(_paths: runner.SealedPaths) -> dict[str, str]:
    return {"branch": "main", "head": "abc123", "status_short": " M README.md"}


def _cases() -> dict[str, list[runner.EvaluationCase]]:
    fields = list(runner.EXPECTED_SOURCE_COLUMNS)
    expected = {
        "UNINUM": ("bank_branch.bank_key",),
        "NAME": ("bank_branch.bank_name",),
        "OFFNAME": ("bank_branch.bank_name",),
    }
    cases: list[runner.EvaluationCase] = []
    for field in fields:
        targets = expected.get(field, ())
        top1 = targets[0] if targets and field != "OFFNAME" else "bank_branch.bank_name" if not targets else "bank_branch.country_code"
        if not targets:
            top1 = "bank_branch.country_code"
        top_candidates = (
            {"rank": 1, "score": 0.9, "target": top1},
            {"rank": 2, "score": 0.4, "target": "bank_branch.bank_name"},
            {"rank": 3, "score": 0.2, "target": "bank_branch.bank_key"},
        )
        cases.append(
            runner.EvaluationCase(
                case_id=f"fdic_bankfind_locations__{field}",
                source_field=field,
                expected_targets=targets,
                case_type="single_target" if targets else "no_target",
                top_candidates=top_candidates,
                recommendation=None if field in {"CERT", "CITY"} else top1,
                v5_status="suggested" if field == "UNINUM" else "review",
                features=_feature_values(0.8 if targets else 0.1),
                existing_v5_accepted=field == "UNINUM",
            )
        )
    return {system: list(cases) for system in runner.RANKING_SYSTEMS}


def _successful_callback(paths: runner.SealedPaths, context: dict, marker: dict) -> dict:
    if not paths.attempt_marker_path.exists():
        raise AssertionError("attempt marker must exist before evaluation callback")
    return runner.build_result_artifact(
        paths=paths,
        context=context,
        marker=marker,
        ranking_cases_by_system=_cases(),
    )


class FdicBankFindSealedEvaluationRunnerTest(unittest.TestCase):
    def test_validate_only_cli_succeeds_and_writes_nothing(self) -> None:
        before = {path: path.exists() for path in (REAL_ATTEMPT, REAL_RESULT)}
        result = subprocess.run(
            [sys.executable, "-u", "scripts/evaluate_fdic_bankfind_sealed_benchmark.py", "--validate-only"],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("validated_without_model_or_predictions", result.stdout)
        self.assertNotIn("SentenceTransformer", result.stdout + result.stderr)
        self.assertEqual({path: path.exists() for path in (REAL_ATTEMPT, REAL_RESULT)}, before)

    def test_validate_only_cli_succeeds_from_external_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-u", str(PROJECT_ROOT / "scripts/evaluate_fdic_bankfind_sealed_benchmark.py"), "--validate-only"],
                cwd=temp_dir,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("validated_without_model_or_predictions", result.stdout)

    def test_validate_only_and_execute_are_mutually_exclusive(self) -> None:
        result = subprocess.run(
            [sys.executable, "-u", "scripts/evaluate_fdic_bankfind_sealed_benchmark.py", "--validate-only", "--execute"],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_sha_mismatch_fails_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            bad = runner.FrozenExpectations("bad", fixture.expectations.fixture_lock_sha, fixture.expectations.source_sample_sha, fixture.expectations.calibration_model_sha)
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    bad,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha="bad",
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.attempt.exists())
            self.assertFalse(fixture.result.exists())

    def test_missing_execute_confirmation_fails_before_callback_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=None,
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.attempt.exists())

    def test_wrong_confirm_benchmark_id_fails_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id="other",
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.attempt.exists())

    def test_wrong_confirm_protocol_sha_fails_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha="wrong",
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.attempt.exists())

    def test_existing_result_blocks_execute_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            fixture.result.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.attempt.exists())

    def test_existing_attempt_marker_blocks_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            fixture.attempt.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=_successful_callback,
                    git_status_provider=_clean_git,
                )
            self.assertFalse(fixture.result.exists())

    def test_dirty_worktree_blocks_execute_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            with self.assertRaises(runner.FdicSealedEvaluationError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=_successful_callback,
                    git_status_provider=_dirty_git,
                )
            self.assertFalse(fixture.attempt.exists())

    def test_attempt_marker_is_created_before_evaluation_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            runner.execute_once(
                fixture.paths,
                fixture.expectations,
                confirm_benchmark_id=runner.BENCHMARK_ID,
                confirm_protocol_sha=fixture.expectations.protocol_sha,
                evaluation_callback=_successful_callback,
                git_status_provider=_clean_git,
            )
            marker = json.loads(fixture.attempt.read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "started")
            self.assertEqual(marker["benchmark_id"], runner.BENCHMARK_ID)
            self.assertTrue(fixture.result.exists())

    def test_callback_failure_retains_marker_and_does_not_write_result(self) -> None:
        def failing_callback(_paths: runner.SealedPaths, _context: dict, _marker: dict) -> dict:
            raise RuntimeError("synthetic callback failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            with self.assertRaises(RuntimeError):
                runner.execute_once(
                    fixture.paths,
                    fixture.expectations,
                    confirm_benchmark_id=runner.BENCHMARK_ID,
                    confirm_protocol_sha=fixture.expectations.protocol_sha,
                    evaluation_callback=failing_callback,
                    git_status_provider=_clean_git,
                )
            self.assertTrue(fixture.attempt.exists())
            self.assertFalse(fixture.result.exists())

    def test_result_is_written_atomically_and_temp_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            runner.execute_once(
                fixture.paths,
                fixture.expectations,
                confirm_benchmark_id=runner.BENCHMARK_ID,
                confirm_protocol_sha=fixture.expectations.protocol_sha,
                evaluation_callback=_successful_callback,
                git_status_provider=_clean_git,
            )
            self.assertTrue(fixture.result.exists())
            self.assertFalse(fixture.result.with_name(f".{fixture.result.name}.tmp").exists())

    def test_frozen_development_model_is_loaded_for_inference_only(self) -> None:
        source = (PROJECT_ROOT / "scripts/evaluate_fdic_bankfind_sealed_benchmark.py").read_text(encoding="utf-8")
        self.assertNotIn("fit_calibrator", source)
        self.assertNotIn("select_threshold", source)
        self.assertNotIn("run_experiment", source)
        self.assertGreater(runner._predict_probability(_model(["top1_v5_score"], 0.5), {"top1_v5_score": 1.0}), 0.5)

    def test_primary_and_secondary_roles_are_frozen_from_protocol(self) -> None:
        summary = runner.validate_only(runner.default_paths(), runner.default_expectations())
        self.assertEqual(summary["candidate_roles"]["primary_decision_comparison"], list(runner.PRIMARY_SYSTEMS))
        self.assertEqual(summary["candidate_roles"]["secondary_diagnostics"], list(runner.SECONDARY_SYSTEMS))

    def test_no_target_label_and_rejection_semantics_are_distinct(self) -> None:
        case = runner.EvaluationCase(
            case_id="x",
            source_field="CERT",
            expected_targets=(),
            case_type="no_target",
            top_candidates=({"rank": 1, "score": 0.8, "target": "bank_branch.bank_key"},),
            recommendation=None,
            v5_status="review",
            features=_feature_values(0.1),
            existing_v5_accepted=False,
        )
        self.assertEqual(case.label, 0)
        metrics = runner.selective_metrics([{"case_id": "x", "source_field": "CERT", "label": 0, "target_bearing": False, "accepted": False}])
        self.assertEqual(metrics["review_count"], 1)
        self.assertEqual(metrics["rejection_semantics"], "human_review_not_no_target_prediction")

    def test_multi_target_metric_is_not_applicable_with_zero_denominator(self) -> None:
        metrics = runner.ranking_metrics(_cases()["precision_tiered_v5"])
        self.assertIsNone(metrics["multi_target_full_coverage_at_3"]["value"])
        self.assertEqual(metrics["multi_target_full_coverage_at_3"]["denominator"], 0)
        self.assertEqual(metrics["multi_target_full_coverage_at_3"]["status"], "not_applicable")

    def test_pooled_counts_reconcile(self) -> None:
        records = [
            {"case_id": "a", "source_field": "a", "label": 1, "target_bearing": True, "accepted": True},
            {"case_id": "b", "source_field": "b", "label": 0, "target_bearing": True, "accepted": True},
            {"case_id": "c", "source_field": "c", "label": 0, "target_bearing": False, "accepted": False},
        ]
        metrics = runner.selective_metrics(records)
        self.assertEqual(metrics["case_count"], 3)
        self.assertEqual(metrics["accepted_correct_count"] + metrics["accepted_incorrect_count"], metrics["accepted_count"])
        self.assertEqual(metrics["accepted_count"] + metrics["review_count"], metrics["case_count"])
        self.assertEqual(metrics["wrong_target_accepted_count"], 1)

    def test_artifact_builder_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = TempSealedFixture(Path(temp_dir))
            context = runner.validate_execute_preconditions(
                fixture.paths,
                fixture.expectations,
                confirm_benchmark_id=runner.BENCHMARK_ID,
                confirm_protocol_sha=fixture.expectations.protocol_sha,
                git_status_provider=_clean_git,
            )
            marker = {"status": "started", "git_head": "abc123"}
            fixture.attempt.write_text("{}\n", encoding="utf-8")
            first = runner.build_result_artifact(paths=fixture.paths, context=context, marker=marker, ranking_cases_by_system=_cases())
            second = runner.build_result_artifact(paths=fixture.paths, context=context, marker=marker, ranking_cases_by_system=_cases())
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_artifact_privacy_schema_rejects_source_row_values_and_local_paths(self) -> None:
        payload = {
            "artifact_type": "fdic_bankfind_locations_first_evaluation",
            "schema_version": "1.0",
            "benchmark_id": runner.BENCHMARK_ID,
            "protocol_id": "fdic_bankfind_locations_v1_protocol",
            "frozen_input_shas": {},
            "candidate_roles": {},
            "corpus_counts": {},
            "ranking_metrics": {},
            "calibration_metrics": {},
            "selective_policy_counts": {},
            "primary_decision_comparison": {},
            "per_case_audit_records": [],
            "failure_categories": {},
            "preregistered_limitations": {},
            "production_promoted": False,
            "post_unseal_tuning_performed": False,
        }
        runner.validate_result_schema(payload)
        with self.assertRaises(runner.FdicSealedEvaluationError):
            runner.validate_result_schema(
                {**payload, "note": "SENSITIVE_SOURCE_VALUE_SENTINEL"},
                forbidden_values={"SENSITIVE_SOURCE_VALUE_SENTINEL"},
            )
        with self.assertRaises(runner.FdicSealedEvaluationError):
            synthetic_local_path = "C:" + "\\" + "Users" + "\\example"
            runner.validate_result_schema({**payload, "note": synthetic_local_path})

    def test_real_sealed_fixture_still_has_no_attempt_or_result(self) -> None:
        self.assertFalse(REAL_ATTEMPT.exists())
        self.assertFalse(REAL_RESULT.exists())

    def test_runner_has_no_hardcoded_user_path_or_custom_output_option(self) -> None:
        source = (PROJECT_ROOT / "scripts/evaluate_fdic_bankfind_sealed_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("REPO_ROOT = Path(__file__).resolve().parents[1]", source)
        self.assertNotIn("C:" + "\\\\" + "Users", source)
        current_user = os.environ.get("USERNAME")
        if current_user:
            self.assertNotIn(current_user, source)
        self.assertNotIn("--output", source)


if __name__ == "__main__":
    unittest.main()
