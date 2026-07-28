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

import build_cutover_status as status_tool  # noqa: E402


def load_inputs() -> tuple[dict, dict, dict]:
    plan = json.loads((PROJECT_ROOT / "data" / "synthetic" / "cutover_plan_report.json").read_text(encoding="utf-8"))
    constraints = json.loads((PROJECT_ROOT / "data" / "synthetic" / "cutover_constraints.json").read_text(encoding="utf-8"))
    updates = json.loads((PROJECT_ROOT / "data" / "synthetic" / "cutover_status_updates.json").read_text(encoding="utf-8"))
    return plan, constraints, updates


class CutoverStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan, cls.constraints, cls.updates = load_inputs()
        cls.status_report = status_tool.build_status_report(cls.plan, cls.constraints, cls.updates)
        cls.daily_report = status_tool.build_daily_report(cls.status_report)

    def build_with_updates(self, updates: dict) -> dict:
        return status_tool.build_status_report(self.plan, self.constraints, updates)

    def event_by_id(self, updates: dict, event_id: str) -> dict:
        return next(event for event in updates["events"] if event["event_id"] == event_id)

    def test_source_plan_shape_is_expected(self) -> None:
        self.assertEqual(len(self.plan["activities"]), 30)
        self.assertEqual(len(self.plan["raid_register"]), 7)
        self.assertEqual(len(self.plan["approval_gates"]), 4)

    def test_source_plan_sha_matches_expected_snapshot(self) -> None:
        self.assertEqual(self.plan["_run_info"]["content_sha256"], status_tool.EXPECTED_SOURCE_PLAN_SHA)
        self.assertEqual(self.status_report["source_plan_content_sha256"], status_tool.EXPECTED_SOURCE_PLAN_SHA)

    def test_event_log_has_expected_event_count(self) -> None:
        self.assertEqual(self.status_report["events_applied_count"], 28)

    def test_event_ids_are_unique(self) -> None:
        event_ids = [event["event_id"] for event in self.updates["events"]]
        self.assertEqual(len(event_ids), len(set(event_ids)))

    def test_sequences_are_unique(self) -> None:
        sequences = [event["sequence"] for event in self.updates["events"]]
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_activity_status_counts_are_expected(self) -> None:
        self.assertEqual(
            self.status_report["activity_status_counts"],
            {"Not Started": 11, "In Progress": 0, "Blocked": 2, "Completed": 17, "Cancelled": 0},
        )

    def test_work_package_status_counts_are_expected(self) -> None:
        self.assertEqual(
            self.status_report["work_package_status_counts"],
            {"Not Started": 0, "In Progress": 4, "Blocked": 1, "Completed": 0, "Cancelled": 0},
        )

    def test_raid_dependencies_are_mitigating(self) -> None:
        dependencies = [item for item in self.status_report["raid_register"] if item["type"] == "Dependency"]
        self.assertEqual(len(dependencies), 5)
        self.assertEqual({item["current_status"] for item in dependencies}, {"Mitigating"})

    def test_risk_statuses_are_deterministic(self) -> None:
        risks = sorted(
            (item for item in self.status_report["raid_register"] if item["type"] == "Risk"),
            key=lambda x: x["raid_id"],
        )
        self.assertEqual(risks[0]["raid_id"], "RAID-RISK-EX-016")
        self.assertEqual(risks[0]["current_status"], "Resolved")
        self.assertEqual(risks[1]["raid_id"], "RAID-RISK-EX-022")
        self.assertEqual(risks[1]["current_status"], "Open")

    def test_approval_gate_status_counts_are_expected(self) -> None:
        self.assertEqual(
            self.status_report["approval_gate_status_counts"],
            {"Pending": 2, "Ready": 0, "Approved": 1, "Rejected": 0, "Blocked": 1},
        )

    def test_design_gate_is_approved_and_ready(self) -> None:
        gates = {gate["gate_id"]: gate for gate in self.status_report["approval_gates"]}
        self.assertEqual(gates["GATE-DESIGN-SIGNOFF"]["current_status"], "Approved")
        self.assertTrue(gates["GATE-DESIGN-SIGNOFF"]["readiness"])
        self.assertEqual(gates["GATE-DESIGN-SIGNOFF"]["missing_readiness_criteria"], [])

    def test_cutover_readiness_gate_is_blocked_and_not_ready(self) -> None:
        gates = {gate["gate_id"]: gate for gate in self.status_report["approval_gates"]}
        gate = gates["GATE-CUTOVER-READINESS"]
        self.assertEqual(gate["current_status"], "Blocked")
        self.assertFalse(gate["readiness"])
        self.assertIn("ACT-EX-024-TEST", " ".join(gate["missing_readiness_criteria"]))

    def test_day1_critical_path_flags_are_derived(self) -> None:
        activities = {activity["activity_id"]: activity for activity in self.status_report["activities"]}
        self.assertTrue(activities["ACT-EX-024-TEST"]["is_critical_to_day1"])
        self.assertTrue(activities["CUT-CUTOVER-READINESS"]["is_critical_to_day1"])
        self.assertTrue(activities["CUT-DAY1-VALIDATION"]["is_critical_to_day1"])
        self.assertFalse(activities["CUT-RECONCILIATION"]["is_critical_to_day1"])

    def test_critical_blockers_are_expected(self) -> None:
        blocker_ids = [item["activity_id"] for item in self.status_report["critical_blockers"]]
        self.assertEqual(blocker_ids, ["ACT-EX-024-TEST", "CUT-CUTOVER-READINESS"])

    def test_daily_due_buckets_are_expected(self) -> None:
        self.assertEqual(len(self.daily_report["due_now"]), 2)
        self.assertEqual(len(self.daily_report["overdue"]), 0)
        self.assertEqual(len(self.daily_report["due_next"]), 9)

    def test_daily_next_gate_is_cutover_readiness(self) -> None:
        next_gate = self.daily_report["headline"]["next_gate"]
        self.assertEqual(next_gate["gate_id"], "GATE-CUTOVER-READINESS")
        self.assertEqual(next_gate["current_status"], "Blocked")

    def test_daily_overall_rag_is_red(self) -> None:
        self.assertEqual(self.daily_report["headline"]["overall_rag"], "Red")
        self.assertTrue(self.daily_report["rag_reasons"])

    def test_management_actions_are_prioritized(self) -> None:
        actions = self.daily_report["management_actions"]
        self.assertEqual(len(actions), 4)
        self.assertEqual([action["priority"] for action in actions], sorted(action["priority"] for action in actions))
        self.assertEqual(actions[0]["source_id"], "ACT-EX-024-TEST")

    def test_same_input_status_sha_is_stable(self) -> None:
        first = status_tool.build_status_report(self.plan, self.constraints, self.updates)
        second = status_tool.build_status_report(self.plan, self.constraints, self.updates)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_same_input_daily_sha_is_stable(self) -> None:
        first_status = status_tool.build_status_report(self.plan, self.constraints, self.updates)
        second_status = status_tool.build_status_report(self.plan, self.constraints, self.updates)
        first_daily = status_tool.build_daily_report(first_status)
        second_daily = status_tool.build_daily_report(second_status)
        self.assertEqual(first_daily["_run_info"]["content_sha256"], second_daily["_run_info"]["content_sha256"])

    def test_shuffled_events_produce_same_status_sha(self) -> None:
        shuffled = copy.deepcopy(self.updates)
        shuffled["events"] = list(reversed(shuffled["events"]))
        rebuilt = self.build_with_updates(shuffled)
        self.assertEqual(self.status_report["_run_info"]["content_sha256"], rebuilt["_run_info"]["content_sha256"])

    def test_changing_real_status_content_changes_sha(self) -> None:
        changed = copy.deepcopy(self.updates)
        event = self.event_by_id(changed, "EVT-ACT-EX-024-TEST-BLOCKED")
        event["progress_percent"] = 70
        rebuilt = self.build_with_updates(changed)
        self.assertNotEqual(self.status_report["_run_info"]["content_sha256"], rebuilt["_run_info"]["content_sha256"])

    def test_unknown_entity_fails(self) -> None:
        changed = copy.deepcopy(self.updates)
        self.event_by_id(changed, "EVT-ACT-EX-004-DESIGN-COMPLETED")["entity_id"] = "NO-SUCH-ACTIVITY"
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "unknown activity"):
            self.build_with_updates(changed)

    def test_unknown_owner_role_fails(self) -> None:
        changed = copy.deepcopy(self.updates)
        self.event_by_id(changed, "EVT-ACT-EX-004-DESIGN-COMPLETED")["updated_by_role"] = "Unlisted Role"
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "unknown owner role"):
            self.build_with_updates(changed)

    def test_future_event_fails(self) -> None:
        changed = copy.deepcopy(self.updates)
        self.event_by_id(changed, "EVT-ACT-EX-004-DESIGN-COMPLETED")["effective_offset"] = "T+1"
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "later than as_of_offset"):
            self.build_with_updates(changed)

    def test_invalid_statuses_fail(self) -> None:
        cases = [
            ("EVT-ACT-EX-004-DESIGN-COMPLETED", "invalid activity status"),
            ("EVT-RAID-DEP-EX-004-MITIGATING", "invalid RAID status"),
            ("EVT-GATE-DESIGN-SIGNOFF-READY", "invalid gate status"),
        ]
        for event_id, expected_error in cases:
            with self.subTest(event_id=event_id):
                changed = copy.deepcopy(self.updates)
                self.event_by_id(changed, event_id)["new_status"] = "Invalid"
                with self.assertRaisesRegex(status_tool.CutoverStatusError, expected_error):
                    self.build_with_updates(changed)

    def test_blocker_rules_fail(self) -> None:
        cases = [
            ("EVT-ACT-EX-024-TEST-BLOCKED", "", "must include a blocker"),
            ("EVT-ACT-EX-004-DESIGN-COMPLETED", "not actually blocked", "must not include a blocker"),
        ]
        for event_id, blocker, expected_error in cases:
            with self.subTest(event_id=event_id):
                changed = copy.deepcopy(self.updates)
                self.event_by_id(changed, event_id)["blocker"] = blocker
                with self.assertRaisesRegex(status_tool.CutoverStatusError, expected_error):
                    self.build_with_updates(changed)

    def test_progress_rules_fail(self) -> None:
        cases = [
            ("EVT-ACT-EX-004-DESIGN-COMPLETED", 99, "Completed progress"),
            ("EVT-ACT-EX-024-TEST-BLOCKED", 100, "Blocked progress"),
        ]
        for event_id, progress, expected_error in cases:
            with self.subTest(event_id=event_id):
                changed = copy.deepcopy(self.updates)
                self.event_by_id(changed, event_id)["progress_percent"] = progress
                with self.assertRaisesRegex(status_tool.CutoverStatusError, expected_error):
                    self.build_with_updates(changed)

    def test_completed_terminal_status_cannot_roll_back(self) -> None:
        changed = copy.deepcopy(self.updates)
        changed["events"].append({
            "event_id": "EVT-ACT-EX-004-DESIGN-ROLLBACK",
            "sequence": 75,
            "effective_offset": "T-24",
            "entity_type": "Activity",
            "entity_id": "ACT-EX-004-DESIGN",
            "new_status": "In Progress",
            "progress_percent": 50,
            "updated_by_role": "Business Process Owner",
            "note": "Attempt to reopen a completed design.",
            "blocker": "",
            "evidence": ["Rollback attempt."],
        })
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "illegal activity transition"):
            self.build_with_updates(changed)

    def test_completed_activity_requires_completed_dependencies(self) -> None:
        changed = copy.deepcopy(self.updates)
        changed["events"] = [
            event
            for event in changed["events"]
            if event["event_id"] != "EVT-ACT-EX-004-DESIGN-COMPLETED"
        ]
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "dependencies incomplete"):
            self.build_with_updates(changed)

    def test_gate_approval_requires_readiness(self) -> None:
        changed = copy.deepcopy(self.updates)
        blocked_gate = self.event_by_id(changed, "EVT-GATE-CUTOVER-READINESS-BLOCKED")
        blocked_gate["new_status"] = "Ready"
        blocked_gate["blocker"] = ""
        changed["events"].append({
            "event_id": "EVT-GATE-CUTOVER-READINESS-APPROVED",
            "sequence": 280,
            "effective_offset": "T-7",
            "entity_type": "ApprovalGate",
            "entity_id": "GATE-CUTOVER-READINESS",
            "new_status": "Approved",
            "progress_percent": None,
            "updated_by_role": "Cutover Manager",
            "note": "Attempt to approve cutover readiness while test evidence is incomplete.",
            "blocker": "",
            "evidence": ["Approval attempt."],
        })
        with self.assertRaisesRegex(status_tool.CutoverStatusError, "missing readiness"):
            self.build_with_updates(changed)

    def test_tool_does_not_reference_forbidden_inputs_or_credentials(self) -> None:
        source = (TOOLS_DIR / "build_cutover_status.py").read_text(encoding="utf-8")
        for forbidden in (
            "interview_notes_ground_truth",
            "gap_analysis_evaluation",
            "llm_cache",
            "data/legacy",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "reasoning_content",
        ):
            self.assertNotIn(forbidden, source)

    def test_missing_and_invalid_json_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            invalid = Path(tmp) / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(status_tool.CutoverStatusError, "Required input is missing"):
                status_tool.load_json(missing)
            with self.assertRaisesRegex(status_tool.CutoverStatusError, "Invalid JSON"):
                status_tool.load_json(invalid)


if __name__ == "__main__":
    unittest.main()
