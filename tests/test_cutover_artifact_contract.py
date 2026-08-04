from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "src" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from data_profile import attach_run_info  # noqa: E402


def load_report(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "data" / "synthetic" / name).read_text(encoding="utf-8"))


def migration_raids(plan: dict) -> list[dict]:
    return [
        raid
        for raid in plan["raid_register"]
        if isinstance(raid.get("source"), dict)
        and raid["source"].get("type") == "migration_cutover_finding"
    ]


def migration_activities(plan: dict) -> list[dict]:
    return [activity for activity in plan["activities"] if activity["activity_id"].startswith("CUT-MIG-")]


class CutoverArtifactContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.findings = load_report("migration_cutover_findings.json")
        cls.plan = load_report("cutover_plan_report.json")
        cls.updates = load_report("cutover_status_updates.json")
        cls.status = load_report("cutover_status_report.json")
        cls.daily = load_report("cutover_daily_report.json")

    def test_formal_cutover_plan_contains_22_migration_raids(self) -> None:
        self.assertEqual(len(migration_raids(self.plan)), 22)
        self.assertEqual(len(self.plan["raid_register"]), 29)

    def test_formal_cutover_plan_contains_four_migration_activities(self) -> None:
        self.assertEqual(
            [activity["activity_id"] for activity in migration_activities(self.plan)],
            [
                "CUT-MIG-DUPLICATE-RESOLUTION",
                "CUT-MIG-MAPPING-REVIEW",
                "CUT-MIG-TARGET-DEPENDENCY",
                "CUT-MIG-VALIDATION-REMEDIATION",
            ],
        )
        self.assertEqual(len(self.plan["activities"]), 34)

    def test_formal_migration_activities_directly_link_findings(self) -> None:
        finding_ids = {finding["finding_id"] for finding in self.findings["findings"]}
        activity_memberships: dict[str, list[str]] = {}
        raids_by_id = {raid["raid_id"]: raid for raid in migration_raids(self.plan)}
        for activity in migration_activities(self.plan):
            self.assertTrue(activity["linked_finding_ids"])
            self.assertEqual(activity["linked_finding_ids"], sorted(activity["linked_finding_ids"]))
            self.assertTrue(set(activity["linked_finding_ids"]) <= finding_ids)
            self.assertEqual(
                activity["linked_raid_ids"],
                sorted(f"RAID-{finding_id}" for finding_id in activity["linked_finding_ids"]),
            )
            for finding_id in activity["linked_finding_ids"]:
                activity_memberships.setdefault(finding_id, []).append(activity["activity_id"])
            for raid_id in activity["linked_raid_ids"]:
                self.assertEqual(raids_by_id[raid_id]["linked_activity_ids"], [activity["activity_id"]])
        self.assertEqual(set(activity_memberships), finding_ids)
        self.assertTrue(all(len(activity_ids) == 1 for activity_ids in activity_memberships.values()))

    def test_formal_status_uses_current_plan_sha(self) -> None:
        plan_sha = self.plan["_run_info"]["content_sha256"]
        self.assertEqual(self.updates["_meta"]["source_plan_content_sha256"], plan_sha)
        self.assertEqual(self.status["_meta"]["source_plan_content_sha256"], plan_sha)
        self.assertEqual(self.status["source_plan_content_sha256"], plan_sha)

    def test_formal_daily_uses_current_status_sha(self) -> None:
        self.assertEqual(
            self.daily["_meta"]["source_status_report_content_sha256"],
            self.status["_run_info"]["content_sha256"],
        )
        self.assertEqual(
            self.daily["validation"]["source_status_report_sha"],
            self.status["_run_info"]["content_sha256"],
        )

    def test_formal_migration_blocker_is_traceable(self) -> None:
        self.assertEqual(len(self.status["migration_blockers"]), 1)
        blocker = self.status["migration_blockers"][0]
        self.assertEqual(blocker["raid_id"], "RAID-MIG-127FBCE66F53")
        self.assertEqual(blocker["finding_id"], "MIG-127FBCE66F53")
        self.assertEqual(blocker["source_type"], "migration_cutover_finding")
        self.assertEqual(blocker, self.daily["migration_blockers"][0])
        raid = next(raid for raid in migration_raids(self.plan) if raid["raid_id"] == blocker["raid_id"])
        finding = next(finding for finding in self.findings["findings"] if finding["finding_id"] == blocker["finding_id"])
        self.assertEqual(raid["linked_finding_ids"], [finding["finding_id"]])
        self.assertEqual(raid["source"]["source_pointers"], blocker["source_pointers"])
        self.assertTrue(finding["sources"])

    def test_formal_reports_exclude_benchmark_and_ground_truth(self) -> None:
        text = json.dumps(
            {
                "plan": self.plan,
                "status": self.status,
                "daily": self.daily,
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

    def test_report_content_hashes_recalculate(self) -> None:
        for report in (self.findings, self.plan, self.status, self.daily):
            rebuilt = attach_run_info(report)
            self.assertEqual(rebuilt["_run_info"]["content_sha256"], report["_run_info"]["content_sha256"])


if __name__ == "__main__":
    unittest.main()
