from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
HELPER = PROJECT_ROOT / "scripts" / "verify_formal_artifacts_immutable.py"


def load_workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def run_commands(job: dict) -> list[str]:
    return [
        step.get("run", "")
        for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("run")
    ]


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = load_workflow()
        cls.jobs = cls.workflow["jobs"]

    def test_01_workflow_exists_and_is_named_ci(self) -> None:
        self.assertTrue(WORKFLOW.exists())
        self.assertEqual(self.workflow["name"], "CI")

    def test_02_triggers_include_push_pull_request_and_manual_dispatch(self) -> None:
        triggers = self.workflow["on"]
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertIn("pull_request", triggers)
        self.assertIn("workflow_dispatch", triggers)

    def test_03_permissions_are_read_only(self) -> None:
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertNotIn("write", self.workflow_text.lower())
        self.assertNotIn("secrets.", self.workflow_text)

    def test_04_concurrency_cancels_previous_branch_runs(self) -> None:
        self.assertEqual(
            self.workflow["concurrency"]["group"],
            "ci-${{ github.workflow }}-${{ github.ref }}",
        )
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "true")

    def test_05_python_matrix_uses_ubuntu_windows_and_python_312(self) -> None:
        python_job = self.jobs["python"]
        matrix = python_job["strategy"]["matrix"]
        self.assertEqual(python_job["strategy"]["fail-fast"], "false")
        self.assertEqual(set(matrix["os"]), {"ubuntu-latest", "windows-latest"})
        self.assertEqual(matrix["python-version"], ["3.12"])
        self.assertNotIn("continue-on-error", self.workflow_text)

    def test_06_python_job_runs_full_unittest_discovery(self) -> None:
        commands = "\n".join(run_commands(self.jobs["python"]))
        self.assertIn("python -m pip install -r requirements.txt", commands)
        self.assertIn("python -m unittest discover tests", commands)

    def test_07_python_job_sets_offline_and_deterministic_environment(self) -> None:
        env = self.jobs["python"]["env"]
        for key, value in {
            "PYTHONHASHSEED": "0",
            "CARVEOPS_OMIT_TIMESTAMP": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }.items():
            self.assertEqual(env[key], value)
        for forbidden in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN"]:
            self.assertNotIn(forbidden, self.workflow_text)

    def test_08_formal_artifact_immutability_check_wraps_python_tests(self) -> None:
        commands = run_commands(self.jobs["python"])
        snapshot_index = commands.index("python scripts/verify_formal_artifacts_immutable.py snapshot")
        test_index = commands.index("python -m unittest discover tests")
        verify_index = commands.index("python scripts/verify_formal_artifacts_immutable.py verify")
        self.assertLess(snapshot_index, test_index)
        self.assertLess(test_index, verify_index)

    def test_09_frontend_uses_npm_ci_lint_test_and_build(self) -> None:
        frontend = self.jobs["frontend"]
        self.assertEqual(frontend["runs-on"], "ubuntu-latest")
        commands = run_commands(frontend)
        self.assertIn("npm ci", commands)
        self.assertIn("npm run lint", commands)
        self.assertIn("npm run test", commands)
        self.assertIn("npm run build", commands)
        self.assertNotIn("npm install", "\n".join(commands))

    def test_10_frontend_uses_node_22_and_package_lock_cache(self) -> None:
        setup = next(step for step in self.jobs["frontend"]["steps"] if step.get("uses", "").startswith("actions/setup-node"))
        self.assertEqual(setup["with"]["node-version"], "22")
        self.assertEqual(setup["with"]["cache"], "npm")
        self.assertEqual(setup["with"]["cache-dependency-path"], "frontend/package-lock.json")

    def test_11_only_official_major_tag_actions_are_used(self) -> None:
        uses = re.findall(r"uses:\s*([^\s#]+)", self.workflow_text)
        self.assertEqual(
            set(uses),
            {"actions/checkout@v4", "actions/setup-python@v5", "actions/setup-node@v4"},
        )

    def test_12_workflow_does_not_rebuild_artifacts_commit_or_push(self) -> None:
        run_text = "\n".join(run_commands(self.jobs["python"]) + run_commands(self.jobs["frontend"])).lower()
        for forbidden in [
            "build_migration_cutover_findings.py",
            "build_cutover_plan.py",
            "build_cutover_status.py",
            "smoke_test_multitarget_package_generation.py",
            "git commit",
            "git push",
            "gh ",
        ]:
            self.assertNotIn(forbidden, run_text)

    def test_13_helper_uses_complete_43_file_inventory(self) -> None:
        module = ast.parse(HELPER.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in module.body
            if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "FORMAL_ARTIFACTS" for target in node.targets)
        )
        artifacts = ast.literal_eval(assignment.value)
        self.assertEqual(len(artifacts), 43)
        for required in [
            "data/generated/generic_customer/customer.csv",
            "data/generated/generic_customer/customer_bank.csv",
            "data/generated/sap_supplier_reference/supplier_general.csv",
            "data/generated/sap_supplier_reference/supplier_company.csv",
            "data/generated/erpnext_item_price_multitarget/item.csv",
            "data/generated/erpnext_item_price_multitarget/item_price.csv",
            "data/synthetic/cutover_status_updates.json",
            "data/synthetic/cutover_agent_trace.json",
        ]:
            self.assertIn(required, artifacts)
        self.assertEqual(
            len([artifact for artifact in artifacts if artifact.startswith("data/synthetic/cutover_agent_runs/")]),
            6,
        )
        for excluded in [
            "data/synthetic/cutover_constraints.json",
            "data/synthetic/gap_analysis_evaluation.json",
            "data/synthetic/gap_analysis_report.json",
            "data/synthetic/interview_notes.json",
            "data/synthetic/interview_notes_ground_truth.json",
        ]:
            self.assertNotIn(excluded, artifacts)

    def test_14_helper_uses_runner_temp_snapshot_path(self) -> None:
        helper_text = HELPER.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("RUNNER_TEMP")', helper_text)
        self.assertIn("tempfile.gettempdir()", helper_text)
        self.assertNotIn("write_reports", helper_text)
        self.assertNotIn("os.replace", helper_text)


if __name__ == "__main__":
    unittest.main()
