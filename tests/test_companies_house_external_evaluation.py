from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "data" / "benchmarks" / "external" / "companies_house_customer_v1"
ARTIFACT_PATH = FIXTURE_DIR / "first_evaluation_baseline_v4_v5.json"
SOURCE_PATH = FIXTURE_DIR / "source_companies_house_customer.csv"
GROUND_TRUTH_PATH = FIXTURE_DIR / "ground_truth.json"
PROTOCOL_PATH = FIXTURE_DIR / "protocol_lock.json"
RUNNER_PATH = PROJECT_ROOT / "scripts" / "evaluate_companies_house_external_benchmark.py"

EXPECTED_SCORERS = {"baseline", "precision_tiered_v4", "precision_tiered_v5"}
EXPECTED_COUNTS = {
    "single_target_cases": 3,
    "multi_target_cases": 0,
    "no_target_cases": 9,
    "target_links": 3,
}
EXPECTED_FORMAL_COUNT = 45
EXPECTED_V4_SHA = "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b"
EXPECTED_V5_SHA = "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57"
EXPECTED_ARTIFACT_SHA = "d08584b1e77e59ba5362586d851e225e9d746f52eb01c2b268ffe2b68dc7edd8"


class CompaniesHouseExternalEvaluationBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.final_artifact = ARTIFACT_PATH
        self.temp_artifact = ARTIFACT_PATH.with_name(f".{ARTIFACT_PATH.name}.tmp")
        self.assertTrue(self.final_artifact.exists())
        self.initial_artifact_sha = raw_sha(self.final_artifact)
        self.assertEqual(self.initial_artifact_sha, EXPECTED_ARTIFACT_SHA)
        self.assertFalse(self.temp_artifact.exists())

    def assert_validate_only_succeeds(self, *, external_cwd: bool) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "validate-only-worktree"
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
                cwd=PROJECT_ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            try:
                copied_runner = worktree / "scripts" / "evaluate_companies_house_external_benchmark.py"
                shutil.copy2(RUNNER_PATH, copied_runner)
                for rel_path in (
                    "src/core/mapping/scorer.py",
                    "src/core/mapping/scorer_v4.py",
                    "src/core/mapping/scorer_v5.py",
                ):
                    shutil.copy2(PROJECT_ROOT / rel_path, worktree / rel_path)
                isolated_artifact = worktree / ARTIFACT_PATH.relative_to(PROJECT_ROOT)
                isolated_temp_artifact = isolated_artifact.with_name(f".{isolated_artifact.name}.tmp")
                self.assertFalse(isolated_artifact.exists())
                self.assertFalse(isolated_temp_artifact.exists())

                if external_cwd:
                    cwd = Path(temp_dir) / "external-cwd"
                    cwd.mkdir()
                    script: Path | str = copied_runner
                else:
                    cwd = worktree
                    script = Path("scripts") / "evaluate_companies_house_external_benchmark.py"

                result = subprocess.run(
                    [sys.executable, "-u", str(script), "--validate-only"],
                    cwd=cwd,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
                self.assertIn("validated_imports_without_model_or_predictions", result.stdout)
                self.assertNotIn("SentenceTransformer", result.stdout + result.stderr)
                self.assertNotIn("candidate_generation_started", result.stdout + result.stderr)
                self.assertFalse(isolated_artifact.exists())
                self.assertFalse(isolated_temp_artifact.exists())
                self.assertTrue(self.final_artifact.exists())
                self.assertEqual(raw_sha(self.final_artifact), self.initial_artifact_sha)
                self.assertFalse(self.temp_artifact.exists())
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=PROJECT_ROOT,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

    def test_validate_only_bootstraps_repo_root_from_file_path_at_repo_root(self) -> None:
        self.assert_validate_only_succeeds(external_cwd=False)

    def test_validate_only_bootstraps_repo_root_from_external_cwd(self) -> None:
        self.assert_validate_only_succeeds(external_cwd=True)

    def test_runner_uses_file_based_repo_root_without_hardcoded_local_path(self) -> None:
        text = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("REPO_ROOT = Path(__file__).resolve().parents[1]", text)
        self.assertIn("sys.path.insert(0, str(REPO_ROOT))", text)
        self.assertNotIn("C:\\\\Users", text)
        self.assertNotIn("klxs", text)
        self.assertNotIn("os.chdir", text)
        self.assertNotIn("PYTHONPATH", text)


def raw_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def git_diff_names(path: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--", path],
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line for line in result.stdout.splitlines() if line]


def metric(numerator: int | float, denominator: int):
    return None if denominator == 0 else round(float(numerator) / denominator, 4)


def recompute_metrics(ground_truth: dict, predictions: list[dict]) -> dict:
    case_by_field = {case["source_field"]: case for case in ground_truth["cases"]}
    prediction_by_field = {prediction["source_field"]: prediction for prediction in predictions}
    single_top1 = 0
    links_at_1 = 0
    links_at_3 = 0
    mrr_points = 0.0
    no_target_correct = 0
    for source_field in ground_truth["source_fields"]:
        case = case_by_field[source_field]
        prediction = prediction_by_field[source_field]
        top_candidates = prediction["top_candidates"]
        if case["case_type"] == "single_target":
            expected = set(case["expected_targets"])
            rank = next(
                (
                    int(candidate["rank"])
                    for candidate in top_candidates
                    if candidate["target"] in expected
                ),
                None,
            )
            single_top1 += int(rank == 1)
            links_at_1 += int(rank == 1)
            links_at_3 += int(rank is not None and rank <= 3)
            mrr_points += (1 / rank) if rank else 0.0
        elif case["case_type"] == "no_target":
            no_target_correct += int(prediction["recommendation"] is None)
    return {
        "single_target_top1_accuracy": metric(single_top1, 3),
        "target_link_recall_at_1": metric(links_at_1, 3),
        "target_link_recall_at_3": metric(links_at_3, 3),
        "mean_reciprocal_rank": metric(round(mrr_points, 4), 3),
        "no_target_accuracy": metric(no_target_correct, 9),
        "multi_target_full_recall_at_3": None,
    }


def source_values_to_exclude() -> set[str]:
    values: set[str] = set()
    with SOURCE_PATH.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            for value in row.values():
                if value and len(value) >= 8:
                    values.add(value)
    return values


class CompaniesHouseExternalEvaluationArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not ARTIFACT_PATH.exists():
            raise AssertionError("first evaluation artifact must exist")
        if raw_sha(ARTIFACT_PATH) != EXPECTED_ARTIFACT_SHA:
            raise AssertionError("first evaluation artifact SHA changed")
        cls.artifact = read_json(ARTIFACT_PATH)
        cls.ground_truth = read_json(GROUND_TRUTH_PATH)
        cls.protocol = read_json(PROTOCOL_PATH)
        cls.artifact_text = ARTIFACT_PATH.read_text(encoding="utf-8")

    def test_artifact_records_first_evaluation_without_mutating_protocol(self) -> None:
        self.assertEqual(self.artifact["evaluation_ordinal"], 1)
        self.assertEqual(self.artifact["evaluation_status"], "completed")
        self.assertEqual(self.artifact["execution_history"]["substantive_evaluation_ordinal"], 1)
        aborts = self.artifact["execution_history"]["pre_prediction_infrastructure_aborts"]
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0]["stage"], "canonical_benchmark_import")
        self.assertEqual(aborts[0]["error_code"], "module_not_found_src")
        self.assertFalse(aborts[0]["model_loaded"])
        self.assertFalse(aborts[0]["candidate_generation_started"])
        self.assertFalse(aborts[0]["scorer_execution_started"])
        self.assertFalse(aborts[0]["predictions_computed"])
        self.assertFalse(aborts[0]["artifact_created"])
        self.assertEqual(self.artifact["registration_commit"], "66ae6fe8f55bd9efc782820622eb27c0c6e1f4ef")
        self.assertEqual(self.artifact["algorithm_baseline_commit"], "23add9d90fe93c590f32e946f471fb929cb88ac3")
        self.assertEqual(self.protocol["first_evaluation_status"], "not_run")
        self.assertIsNone(self.protocol["evaluation_artifact"])
        self.assertIsNone(self.protocol["evaluation_output"])
        self.assertEqual(git_diff_names("data/benchmarks/external/companies_house_customer_v1/protocol_lock.json"), [])

    def test_artifact_contains_expected_frozen_hashes_and_runner_hash(self) -> None:
        self.assertEqual(self.artifact["frozen_inputs"]["source_fixture"]["raw_sha256"], raw_sha(SOURCE_PATH))
        self.assertEqual(self.artifact["frozen_inputs"]["ground_truth"]["raw_sha256"], raw_sha(GROUND_TRUTH_PATH))
        self.assertEqual(self.artifact["frozen_inputs"]["protocol"]["raw_sha256"], raw_sha(PROTOCOL_PATH))
        self.assertEqual(self.artifact["evaluation_runner"]["raw_sha256"], raw_sha(RUNNER_PATH))
        self.assertEqual(
            self.artifact["frozen_inputs"]["target_contract"]["target_contract_git_blob_content_sha256"],
            "8fe32d08f23a2c97dedea8d43d37d96925003766acbd4f69326b7646b90da792",
        )

    def test_scorer_set_and_metadata_are_complete(self) -> None:
        self.assertEqual(set(self.artifact["scorers"]), EXPECTED_SCORERS)
        self.assertEqual(self.artifact["planned_scorers"], ["baseline", "precision_tiered_v4", "precision_tiered_v5"])
        for scorer_id, result in self.artifact["scorers"].items():
            self.assertEqual(result["scorer_metadata"]["scorer_id"], scorer_id)
            self.assertIn("model_runtime", result)
            self.assertEqual(result["source_fields_evaluated"], 12)

    def test_predictions_are_privacy_minimal_and_do_not_embed_ground_truth(self) -> None:
        allowlist = set(self.ground_truth["target_allowlist"])
        for result in self.artifact["scorers"].values():
            fields = [prediction["source_field"] for prediction in result["predictions"]]
            self.assertEqual(fields, sorted(self.ground_truth["source_fields"]))
            for prediction in result["predictions"]:
                self.assertNotIn("expected_targets", prediction)
                self.assertNotIn("ground_truth", prediction)
                self.assertNotIn("case_type", prediction)
                self.assertEqual(
                    set(prediction),
                    {"confidence", "recommendation", "source_field", "status", "top_candidates"},
                )
                self.assertEqual(len(prediction["top_candidates"]), 3)
                for candidate in prediction["top_candidates"]:
                    self.assertEqual(set(candidate), {"rank", "score", "target"})
                    self.assertIn(candidate["target"], allowlist)

    def test_metrics_recompute_from_artifact_predictions(self) -> None:
        for result in self.artifact["scorers"].values():
            expected = recompute_metrics(self.ground_truth, result["predictions"])
            metrics = result["metrics"]
            for metric_name, value in expected.items():
                self.assertEqual(metrics[metric_name]["value"], value)
            self.assertEqual(metrics["multi_target_full_recall_at_3"]["denominator"], 0)
            self.assertEqual(metrics["multi_target_full_recall_at_3"]["status"], "not_applicable")

    def test_model_execution_is_offline_and_single_load(self) -> None:
        model_execution = self.artifact["model_execution"]
        self.assertEqual(model_execution["model_load_count"], 1)
        self.assertTrue(model_execution["local_files_only"])
        self.assertTrue(model_execution["hf_hub_offline_enabled"])
        self.assertTrue(model_execution["transformers_offline_enabled"])
        self.assertGreater(model_execution["encode_call_count"], 0)

    def test_artifact_does_not_contain_row_source_values_or_local_paths(self) -> None:
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\", self.artifact_text))
        self.assertIsNone(re.search(r"(?<!https:)//", self.artifact_text))
        for value in source_values_to_exclude():
            self.assertNotIn(value, self.artifact_text)

    def test_counts_and_header_normalization_contract_remain_recorded(self) -> None:
        self.assertEqual(self.artifact["case_counts"], EXPECTED_COUNTS)
        header_normalization = self.artifact["header_normalization"]
        self.assertEqual(
            header_normalization["rule"],
            "strip ASCII space (0x20) from header boundaries only",
        )
        self.assertTrue(header_normalization["frozen_before_first_evaluation"])
        self.assertFalse(header_normalization["reads_target_aliases_ground_truth_or_predictions"])
        self.assertFalse(header_normalization["source_values_normalized"])

    def test_formal_artifacts_are_unchanged(self) -> None:
        self.assertEqual(len(FORMAL_ARTIFACTS), EXPECTED_FORMAL_COUNT)
        for artifact in FORMAL_ARTIFACTS:
            self.assertTrue((PROJECT_ROOT / artifact).exists(), msg=artifact)
        self.assertEqual(
            raw_sha(PROJECT_ROOT / "data" / "synthetic" / "schema_matching_precision_tiered_v4_5scenario_evaluation.json"),
            EXPECTED_V4_SHA,
        )
        self.assertEqual(
            raw_sha(PROJECT_ROOT / "data" / "synthetic" / "schema_matching_precision_tiered_v5_5scenario_evaluation.json"),
            EXPECTED_V5_SHA,
        )
        self.assertEqual(git_diff_names("data/synthetic"), [])
        self.assertEqual(git_diff_names("data/generated"), [])


if __name__ == "__main__":
    unittest.main()
