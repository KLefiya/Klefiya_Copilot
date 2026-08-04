from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import build_cutover_plan as cutover  # noqa: E402
from data_profile import attach_run_info  # noqa: E402


def load_inputs() -> tuple[dict, dict, dict]:
    synthetic = PROJECT_ROOT / "data" / "synthetic"
    gap = json.loads((synthetic / "gap_analysis_report.json").read_text(encoding="utf-8"))
    constraints = json.loads((synthetic / "cutover_constraints.json").read_text(encoding="utf-8"))
    findings = json.loads((synthetic / "migration_cutover_findings.json").read_text(encoding="utf-8"))
    return gap, constraints, findings


def migration_raids(plan: dict) -> list[dict]:
    return [
        item
        for item in plan["raid_register"]
        if isinstance(item.get("source"), dict)
        and item["source"].get("type") == "migration_cutover_finding"
    ]


def migration_activities(plan: dict) -> list[dict]:
    return [item for item in plan["activities"] if item["activity_id"].startswith("CUT-MIG-")]


def by_id(items: list[dict], key: str) -> dict[str, dict]:
    return {item[key]: item for item in items}


def one_finding_report(base: dict, finding: dict) -> dict:
    report = copy.deepcopy(base)
    report["findings"] = [finding]
    report["summary"] = {
        "finding_count": 1,
        "by_category": {finding["category"]: 1},
        "by_severity": {str(finding["severity"]): 1},
        "by_suggested_raid_type": {finding["suggested_raid_type"]: 1},
        "review_required_count": 1 if finding["review_required"] else 0,
        "gate_blocker_count": 1 if finding["gate_impact"]["blocker"] else 0,
    }
    return report


def sample_source() -> list[dict]:
    return [
        {
            "report_id": "vendor_validation",
            "content_sha256": "source-sha",
            "json_pointer": "/field_view/0/field_issues/0",
            "matched_record_count": 1,
            "record_ids_sample": ["V001"],
        }
    ]


def sample_finding(**overrides: object) -> dict:
    finding = {
        "finding_id": "MIG-TEST000000",
        "rule_id": "MIG-VAL-001",
        "dedupe_key": "validation|sample|target|root",
        "category": "master_data_validation",
        "title": "Sample migration validation finding",
        "description": "Sample deterministic migration finding.",
        "affected_workstream": "master_data",
        "suggested_raid_type": "Issue",
        "severity": "High",
        "severity_origin": "source",
        "confidence": None,
        "confidence_origin": "none",
        "status": "Open",
        "review_required": False,
        "gate_impact": {"blocker": True, "reason": "Explicit high validation failure."},
        "sources": sample_source(),
        "evidence": {"issue_type": "sample_validation_failure"},
    }
    finding.update(overrides)
    return finding


class CutoverPlanMigrationFindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gap, cls.constraints, cls.findings = load_inputs()
        cls.legacy_plan = cutover.build_cutover_plan(cls.gap, cls.constraints)
        cls.plan = cutover.build_cutover_plan(cls.gap, cls.constraints, cls.findings)

    def test_none_migration_findings_preserves_legacy_plan_and_sha(self) -> None:
        built = attach_run_info(cutover.build_cutover_plan(self.gap, self.constraints))
        self.assertEqual(
            built["_run_info"]["content_sha256"],
            "c4a88a3cb0923d2ed28356f72c037ace313ee73bee961ae8212265e4de2a0a8d",
        )
        self.assertNotIn("migration_findings", built["_meta"])
        self.assertEqual(len(built["raid_register"]), 7)
        self.assertEqual(len(built["activities"]), 30)

    def test_empty_migration_findings_adds_only_provenance(self) -> None:
        empty = copy.deepcopy(self.findings)
        empty["findings"] = []
        empty["summary"]["finding_count"] = 0
        plan = cutover.build_cutover_plan(self.gap, self.constraints, empty)
        self.assertEqual(len(plan["raid_register"]), len(self.legacy_plan["raid_register"]))
        self.assertEqual(len(plan["activities"]), len(self.legacy_plan["activities"]))
        self.assertEqual(plan["_meta"]["migration_findings"]["finding_count"], 0)
        self.assertEqual(plan["_meta"]["migration_findings"]["content_sha256"], empty["_run_info"]["content_sha256"])

    def test_each_finding_creates_one_traceable_raid(self) -> None:
        raids = migration_raids(self.plan)
        self.assertEqual(len(raids), len(self.findings["findings"]))
        self.assertEqual(
            sorted(raid["linked_finding_ids"][0] for raid in raids),
            sorted(finding["finding_id"] for finding in self.findings["findings"]),
        )

    def test_migration_raid_preserves_type_severity_review_and_blocker(self) -> None:
        finding = next(item for item in self.findings["findings"] if item["gate_impact"]["blocker"])
        raid = by_id(self.plan["raid_register"], "raid_id")[f"RAID-{finding['finding_id']}"]
        self.assertEqual(raid["type"], finding["suggested_raid_type"])
        self.assertEqual(raid["severity"], finding["severity"])
        self.assertEqual(raid["impact"], finding["severity"])
        self.assertEqual(raid["severity_origin"], finding["severity_origin"])
        self.assertEqual(raid["review_required"], finding["review_required"])
        self.assertEqual(raid["gate_blocker"], finding["gate_impact"]["blocker"])

    def test_null_severity_is_not_invented(self) -> None:
        finding = sample_finding(
            finding_id="MIG-NULL000000",
            dedupe_key="mapping|null|severity",
            category="field_mapping",
            suggested_raid_type="Risk",
            severity=None,
            severity_origin="none",
            review_required=True,
            gate_impact={"blocker": False, "reason": None},
        )
        plan = cutover.build_cutover_plan(self.gap, self.constraints, one_finding_report(self.findings, finding))
        raid = by_id(plan["raid_register"], "raid_id")["RAID-MIG-NULL000000"]
        self.assertIsNone(raid["severity"])
        self.assertIsNone(raid["impact"])
        self.assertIsNone(raid["probability"])

    def test_review_required_high_finding_is_not_gate_blocker(self) -> None:
        finding = sample_finding(
            finding_id="MIG-REVIEWHIGH",
            dedupe_key="validation|review|high",
            review_required=True,
            gate_impact={"blocker": False, "reason": None},
        )
        plan = cutover.build_cutover_plan(self.gap, self.constraints, one_finding_report(self.findings, finding))
        raid = by_id(plan["raid_register"], "raid_id")["RAID-MIG-REVIEWHIGH"]
        self.assertTrue(raid["review_required"])
        self.assertFalse(raid["gate_blocker"])

    def test_invalid_review_required_blocker_is_rejected(self) -> None:
        finding = sample_finding(review_required=True)
        with self.assertRaisesRegex(ValueError, "cannot be both gate blocker and review_required"):
            cutover.build_cutover_plan(self.gap, self.constraints, one_finding_report(self.findings, finding))

    def test_duplicate_finding_ids_are_rejected(self) -> None:
        report = copy.deepcopy(self.findings)
        report["findings"] = [sample_finding(), sample_finding(dedupe_key="validation|sample|other")]
        with self.assertRaisesRegex(ValueError, "Duplicate migration finding_id"):
            cutover.build_cutover_plan(self.gap, self.constraints, report)

    def test_duplicate_dedupe_keys_are_rejected(self) -> None:
        report = copy.deepcopy(self.findings)
        report["findings"] = [sample_finding(), sample_finding(finding_id="MIG-OTHER00000")]
        with self.assertRaisesRegex(ValueError, "Duplicate migration dedupe_key"):
            cutover.build_cutover_plan(self.gap, self.constraints, report)

    def test_invalid_raid_type_is_rejected(self) -> None:
        finding = sample_finding(suggested_raid_type="Assumption")
        with self.assertRaisesRegex(ValueError, "invalid suggested_raid_type"):
            cutover.build_cutover_plan(self.gap, self.constraints, one_finding_report(self.findings, finding))

    def test_findings_are_grouped_into_expected_activities(self) -> None:
        self.assertEqual(
            [item["activity_id"] for item in migration_activities(self.plan)],
            [
                "CUT-MIG-DUPLICATE-RESOLUTION",
                "CUT-MIG-MAPPING-REVIEW",
                "CUT-MIG-TARGET-DEPENDENCY",
                "CUT-MIG-VALIDATION-REMEDIATION",
            ],
        )

    def test_activities_link_back_to_findings_and_raids(self) -> None:
        raids_by_finding = {
            raid["linked_finding_ids"][0]: raid["raid_id"]
            for raid in migration_raids(self.plan)
        }
        for activity in migration_activities(self.plan):
            self.assertTrue(activity["source_finding_ids"])
            self.assertEqual(activity["linked_finding_ids"], activity["source_finding_ids"])
            self.assertEqual(
                activity["linked_raid_ids"],
                sorted(raids_by_finding[finding_id] for finding_id in activity["source_finding_ids"]),
            )

    def test_migration_activities_directly_link_findings(self) -> None:
        finding_ids = {finding["finding_id"] for finding in self.findings["findings"]}
        for activity in migration_activities(self.plan):
            self.assertTrue(activity["linked_finding_ids"])
            self.assertTrue(set(activity["linked_finding_ids"]) <= finding_ids)

    def test_activity_finding_and_raid_links_are_bidirectional(self) -> None:
        raids_by_id = by_id(migration_raids(self.plan), "raid_id")
        for activity in migration_activities(self.plan):
            self.assertEqual(
                activity["linked_raid_ids"],
                sorted(f"RAID-{finding_id}" for finding_id in activity["linked_finding_ids"]),
            )
            for raid_id in activity["linked_raid_ids"]:
                raid = raids_by_id[raid_id]
                self.assertEqual(raid["linked_activity_ids"], [activity["activity_id"]])
                self.assertEqual(raid["linked_finding_ids"], [raid_id.removeprefix("RAID-")])

    def test_each_finding_belongs_to_exactly_one_migration_activity(self) -> None:
        memberships: dict[str, list[str]] = {}
        for activity in migration_activities(self.plan):
            for finding_id in activity["linked_finding_ids"]:
                memberships.setdefault(finding_id, []).append(activity["activity_id"])
        self.assertEqual(set(memberships), {finding["finding_id"] for finding in self.findings["findings"]})
        self.assertTrue(all(len(activity_ids) == 1 for activity_ids in memberships.values()))

    def test_legacy_activities_are_unchanged_without_migration_input(self) -> None:
        legacy = cutover.build_cutover_plan(self.gap, self.constraints)
        self.assertEqual(legacy["activities"], self.legacy_plan["activities"])
        self.assertFalse(any("linked_finding_ids" in activity for activity in legacy["activities"]))

    def test_raids_link_to_aggregate_activity(self) -> None:
        activity_ids = {item["activity_id"] for item in migration_activities(self.plan)}
        for raid in migration_raids(self.plan):
            self.assertEqual(len(raid["linked_activity_ids"]), 1)
            self.assertIn(raid["linked_activity_ids"][0], activity_ids)

    def test_mapping_dependency_validation_activity_order(self) -> None:
        activities = by_id(self.plan["activities"], "activity_id")
        self.assertEqual(activities["CUT-MIG-MAPPING-REVIEW"]["depends_on"], [])
        self.assertEqual(activities["CUT-MIG-TARGET-DEPENDENCY"]["depends_on"], ["CUT-MIG-MAPPING-REVIEW"])
        self.assertIn("CUT-MIG-TARGET-DEPENDENCY", activities["CUT-MIG-VALIDATION-REMEDIATION"]["depends_on"])

    def test_data_freeze_depends_on_generated_migration_activities(self) -> None:
        activities = by_id(self.plan["activities"], "activity_id")
        data_freeze_dependencies = set(activities["CUT-DATA-FREEZE"]["depends_on"])
        self.assertTrue({activity["activity_id"] for activity in migration_activities(self.plan)} <= data_freeze_dependencies)

    def test_no_orphan_activity_dependencies(self) -> None:
        activity_ids = {item["activity_id"] for item in self.plan["activities"]}
        for activity in self.plan["activities"]:
            self.assertTrue(set(activity["depends_on"]) <= activity_ids)
        self.assertEqual(self.plan["validation"]["missing_dependency_references"], [])

    def test_reordered_findings_produce_same_plan_sha(self) -> None:
        first = attach_run_info(cutover.build_cutover_plan(self.gap, self.constraints, self.findings))
        reordered = copy.deepcopy(self.findings)
        reordered["findings"].reverse()
        for finding in reordered["findings"]:
            finding["sources"].reverse()
        second = attach_run_info(cutover.build_cutover_plan(self.gap, self.constraints, reordered))
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_reordered_findings_keep_activity_links_and_plan_sha_stable(self) -> None:
        first = attach_run_info(cutover.build_cutover_plan(self.gap, self.constraints, self.findings))
        reordered = copy.deepcopy(self.findings)
        reordered["findings"] = list(reversed(reordered["findings"]))
        second = attach_run_info(cutover.build_cutover_plan(self.gap, self.constraints, reordered))
        first_links = {
            activity["activity_id"]: (activity["linked_finding_ids"], activity["linked_raid_ids"])
            for activity in migration_activities(first)
        }
        second_links = {
            activity["activity_id"]: (activity["linked_finding_ids"], activity["linked_raid_ids"])
            for activity in migration_activities(second)
        }
        self.assertEqual(first_links, second_links)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_migration_provenance_uses_finding_report_sha(self) -> None:
        provenance = self.plan["_meta"]["migration_findings"]
        self.assertEqual(provenance["report_type"], "migration_cutover_findings")
        self.assertEqual(provenance["content_sha256"], self.findings["_run_info"]["content_sha256"])
        self.assertEqual(provenance["finding_count"], len(self.findings["findings"]))
        self.assertEqual(
            provenance["source_report_ids"],
            sorted(source["report_id"] for source in self.findings["source_reports"]),
        )

    def test_formal_report_produces_22_raids_and_four_activities(self) -> None:
        self.assertEqual(len(migration_raids(self.plan)), 22)
        self.assertEqual(len(migration_activities(self.plan)), 4)
        self.assertEqual(len(self.plan["raid_register"]), 29)
        self.assertEqual(len(self.plan["activities"]), 34)

    def test_benchmark_data_is_not_copied_into_plan(self) -> None:
        text = json.dumps(
            {
                "migration_raids": migration_raids(self.plan),
                "migration_activities": migration_activities(self.plan),
                "migration_provenance": self.plan["_meta"]["migration_findings"],
            },
            ensure_ascii=False,
        ).lower()
        for forbidden in (
            "erpnext_item_price_blind_evaluation.json",
            "source_top1_accuracy",
            "precision",
            "recall",
            "ground_truth",
            "protocol_lock",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
