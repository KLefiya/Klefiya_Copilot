from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import build_cutover_plan as cutover  # noqa: E402
from data_profile import attach_run_info  # noqa: E402


def load_inputs() -> tuple[dict, dict]:
    report = json.loads((PROJECT_ROOT / "data" / "synthetic" / "gap_analysis_report.json").read_text(encoding="utf-8"))
    constraints = json.loads((PROJECT_ROOT / "data" / "synthetic" / "cutover_constraints.json").read_text(encoding="utf-8"))
    return report, constraints


class CutoverPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_report, cls.constraints = load_inputs()
        cls.report = cutover.build_cutover_plan(cls.source_report, cls.constraints)

    def test_formal_module_two_report_has_five_development_backlog_items(self) -> None:
        self.assertEqual(len(self.source_report["dev_backlog"]), 5)

    def test_builds_five_work_packages(self) -> None:
        self.assertEqual(len(self.report["work_packages"]), 5)

    def test_builds_thirty_activities(self) -> None:
        self.assertEqual(len(self.report["activities"]), 30)

    def test_each_work_package_has_four_phases(self) -> None:
        phases = {"DESIGN", "BUILD", "TEST", "DEPLOY"}
        for package in self.report["work_packages"]:
            suffixes = {
                activity["activity_id"].rsplit("-", 1)[1]
                for activity in self.report["activities"]
                if activity["work_package_id"] == package["work_package_id"]
            }
            self.assertEqual(suffixes, phases)

    def test_activity_ids_do_not_depend_on_input_order(self) -> None:
        reordered = copy.deepcopy(self.source_report)
        reordered["dev_backlog"] = list(reversed(reordered["dev_backlog"]))
        reordered["requirements"] = list(reversed(reordered["requirements"]))
        rebuilt = cutover.build_cutover_plan(reordered, self.constraints)
        self.assertEqual(
            sorted(activity["activity_id"] for activity in self.report["activities"]),
            sorted(activity["activity_id"] for activity in rebuilt["activities"]),
        )

    def test_all_deploy_activities_have_rollback(self) -> None:
        deploys = [activity for activity in self.report["activities"] if activity["activity_id"].endswith("-DEPLOY")]
        self.assertEqual(len(deploys), 5)
        for activity in deploys:
            self.assertTrue(activity["rollback_required"])
            self.assertTrue(activity["rollback_action"].strip())

    def test_dependency_graph_is_acyclic(self) -> None:
        self.assertTrue(self.report["validation"]["dependency_graph_acyclic"])

    def test_no_dangling_dependencies_exist(self) -> None:
        self.assertEqual(self.report["validation"]["missing_dependency_references"], [])

    def test_generates_five_dependency_raid_items(self) -> None:
        counts = self.raid_counts()
        self.assertEqual(counts["Dependency"], 5)

    def test_generates_two_needs_review_risks(self) -> None:
        risks = [item for item in self.report["raid_register"] if item["type"] == "Risk"]
        self.assertEqual(len(risks), 2)
        for risk in risks:
            self.assertEqual(risk["source"], "needs_review")
            self.assertIn("confidence=", risk["description"])

    def test_raid_total_is_seven(self) -> None:
        self.assertEqual(len(self.report["raid_register"]), 7)

    def test_owner_role_mapping_is_deterministic(self) -> None:
        activities = {activity["activity_id"]: activity for activity in self.report["activities"]}
        self.assertEqual(activities["ACT-EX-019-DESIGN"]["owner_role"], "Finance Lead")
        self.assertEqual(activities["ACT-EX-008-DESIGN"]["owner_role"], "Data Migration Lead")
        self.assertEqual(activities["ACT-EX-012-BUILD"]["owner_role"], "Integration Lead")
        self.assertEqual(activities["ACT-EX-004-DESIGN"]["owner_role"], "Business Process Owner")

    def test_source_report_sha_is_recorded(self) -> None:
        self.assertEqual(
            self.report["_meta"]["source_report_content_sha256"],
            self.source_report["_run_info"]["content_sha256"],
        )

    def test_same_input_produces_same_content_sha(self) -> None:
        first = attach_run_info(cutover.build_cutover_plan(self.source_report, self.constraints))
        second = attach_run_info(cutover.build_cutover_plan(self.source_report, self.constraints))
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_changing_real_plan_content_changes_content_sha(self) -> None:
        original = attach_run_info(cutover.build_cutover_plan(self.source_report, self.constraints))
        changed_report = cutover.build_cutover_plan(self.source_report, self.constraints)
        changed_report["work_packages"][0]["description"] += " Additional deterministic scope note."
        changed = attach_run_info(changed_report)
        self.assertNotEqual(original["_run_info"]["content_sha256"], changed["_run_info"]["content_sha256"])

    def test_tool_does_not_reference_ground_truth_files(self) -> None:
        source = (TOOLS_DIR / "build_cutover_plan.py").read_text(encoding="utf-8")
        self.assertNotIn("interview_notes_ground_truth", source)
        self.assertNotIn("gap_analysis_evaluation", source)

    def test_missing_module_two_report_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing_gap_analysis_report.json"
            with self.assertRaisesRegex(cutover.CutoverBuildError, "Required input is missing"):
                cutover.load_json(missing)

    def test_invalid_module_two_report_shape_fails_clearly(self) -> None:
        with self.assertRaisesRegex(cutover.CutoverBuildError, "missing `_run_info`"):
            cutover.require_gap_report_shape({"requirements": [], "dev_backlog": []})

    def test_validation_is_valid(self) -> None:
        self.assertTrue(self.report["validation"]["valid"])
        self.assertEqual(self.report["validation"]["errors"], [])

    def raid_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.report["raid_register"]:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
        return counts


if __name__ == "__main__":
    unittest.main()
