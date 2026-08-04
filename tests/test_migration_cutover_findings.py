from __future__ import annotations

import copy
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

from src.tools.migration_cutover_findings import (  # noqa: E402
    RECORD_SAMPLE_LIMIT,
    MigrationFindingError,
    build_migration_cutover_findings,
    finding_id,
    load_default_reports,
    write_report,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_reports() -> dict:
    return {
        "vendor_validation": {
            "_run_info": {"content_sha256": "v" * 64},
            "summary": {},
            "field_view": [
                {
                    "legacy_field": "country",
                    "target": "A_BusinessPartnerAddress.Country",
                    "field_issues": [
                        {
                            "issue_type": "normalization_required",
                            "severity": "medium",
                            "field": "country",
                            "detail_zh": "Country values require ISO normalization.",
                        }
                    ],
                }
            ],
            "record_view": [
                {
                    "record_id": "V003",
                    "issues": [
                        {
                            "field": "country",
                            "target": "A_BusinessPartnerAddress.Country",
                            "issue_type": "max_length_overflow",
                            "severity": "high",
                            "based_on_unverified": False,
                        }
                    ],
                },
                {
                    "record_id": "V001",
                    "issues": [
                        {
                            "field": "country",
                            "target": "A_BusinessPartnerAddress.Country",
                            "issue_type": "max_length_overflow",
                            "severity": "high",
                            "based_on_unverified": False,
                        }
                    ],
                },
                {
                    "record_id": "V002",
                    "issues": [
                        {
                            "field": "country",
                            "target": "A_BusinessPartnerAddress.Country",
                            "issue_type": "max_length_overflow",
                            "severity": "high",
                            "based_on_unverified": False,
                        }
                    ],
                },
            ],
            "deferred_checks": [],
        },
        "vendor_duplicate": {
            "_run_info": {"content_sha256": "d" * 64},
            "summary": {},
            "duplicate_groups": [
                {
                    "group_id": "G2",
                    "needs_review": False,
                    "records": [
                        {"legacy_vendor_id": "V200"},
                        {"legacy_vendor_id": "V201"},
                    ],
                },
                {
                    "group_id": "G1",
                    "needs_review": True,
                    "review_reasons": ["weak edge"],
                    "confidence": {"min_match_probability": 0.1},
                    "records": [
                        {"legacy_vendor_id": "V100"},
                        {"legacy_vendor_id": "V101"},
                    ],
                },
            ],
            "borderline_pairs": {
                "threshold_used_for_clustering": 0.95,
                "pairs": [
                    {
                        "record_ids": ["V300", "V301"],
                        "match_probability": 0.94,
                    }
                ],
            },
        },
        "vendor_field_mapping": {
            "_run_info": {"content_sha256": "m" * 64},
            "mappings": [
                {
                    "legacy_field": "legacy_vendor_id",
                    "status": "needs_review",
                    "needs_review": True,
                    "confidence": 0.56,
                    "recommendation": "A_BusinessPartner.BusinessPartnerIDByExtSystem",
                    "band": "medium",
                },
                {
                    "legacy_field": "email",
                    "status": "no_confident_target",
                    "needs_review": True,
                    "confidence": 0.33,
                    "recommendation": None,
                    "band": "low",
                },
            ],
            "gaps": [
                {
                    "legacy_field": "email",
                    "status": "no_confident_target",
                    "best_candidate": "A_AddressEmail.EmailAddress",
                    "best_confidence": 0.33,
                    "message": "No confident target.",
                }
            ],
        },
        "generated_validation": {
            "_run_info": {"content_sha256": "g" * 64},
            "summary": {"valid": True, "finding_count": 0},
            "resources": [],
            "findings": [],
            "validation": {"valid": True},
        },
    }


def _finding(report: dict, dedupe_key: str) -> dict:
    return next(item for item in report["findings"] if item["dedupe_key"] == dedupe_key)


class MigrationCutoverFindingTests(unittest.TestCase):
    def test_same_inputs_produce_same_content_sha(self) -> None:
        reports = load_default_reports()
        first = build_migration_cutover_findings(reports)
        second = build_migration_cutover_findings(reports)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_reordered_input_arrays_do_not_change_output(self) -> None:
        reports = _fixture_reports()
        first = build_migration_cutover_findings(reports)
        reordered = copy.deepcopy(reports)
        reordered["vendor_validation"]["field_view"].reverse()
        reordered["vendor_validation"]["record_view"].reverse()
        reordered["vendor_duplicate"]["duplicate_groups"].reverse()
        reordered["vendor_duplicate"]["borderline_pairs"]["pairs"].reverse()
        reordered["vendor_field_mapping"]["mappings"].reverse()
        reordered["vendor_field_mapping"]["gaps"].reverse()
        second = build_migration_cutover_findings(reordered)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_finding_ids_follow_rule_and_dedupe_key(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = report["findings"][0]
        self.assertEqual(item["finding_id"], finding_id(item["rule_id"], item["dedupe_key"]))
        self.assertRegex(item["finding_id"], r"^MIG-[0-9A-F]{12}$")

    def test_country_overflow_and_normalization_are_aggregated(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        key = "validation|country|a_businesspartneraddress.country|normalization_required"
        matches = [item for item in report["findings"] if item["dedupe_key"] == key]
        self.assertEqual(len(matches), 1)
        item = matches[0]
        self.assertEqual(item["severity"], "High")
        self.assertEqual(item["severity_origin"], "source")
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(item["evidence"]["root_cause"], "normalization_required")

    def test_record_level_issues_are_grouped_by_root_cause(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = _finding(report, "validation|country|a_businesspartneraddress.country|normalization_required")
        record_source = next(source for source in item["sources"] if source["json_pointer"].startswith("/record_view/"))
        self.assertEqual(record_source["matched_record_count"], 3)
        self.assertEqual(record_source["record_ids_sample"], ["V001", "V002", "V003"])

    def test_duplicate_groups_always_require_human_merge_approval(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        duplicate_findings = [item for item in report["findings"] if item["category"] == "duplicate_supplier"]
        self.assertTrue(duplicate_findings)
        self.assertTrue(all(item["review_required"] for item in duplicate_findings))
        self.assertTrue(all(not item["gate_impact"]["blocker"] for item in duplicate_findings))

    def test_low_duplicate_confidence_does_not_create_high_severity(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = _finding(report, "duplicate|group|g1|needs_review")
        self.assertEqual(item["confidence"], 0.1)
        self.assertEqual(item["severity"], "Medium")
        self.assertEqual(item["severity_origin"], "rule")

    def test_borderline_pairs_are_non_blocking_review_risks(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = _finding(report, "duplicate|borderline_pairs|review_required")
        self.assertEqual(item["suggested_raid_type"], "Risk")
        self.assertTrue(item["review_required"])
        self.assertFalse(item["gate_impact"]["blocker"])

    def test_mapping_review_is_non_blocking(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = _finding(report, "mapping|legacy_vendor_id|needs_review")
        self.assertEqual(item["suggested_raid_type"], "Risk")
        self.assertEqual(item["confidence"], 0.56)
        self.assertTrue(item["review_required"])
        self.assertFalse(item["gate_impact"]["blocker"])

    def test_no_confident_target_is_reviewed_dependency(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        item = _finding(report, "mapping|email|no_confident_target")
        self.assertEqual(item["suggested_raid_type"], "Dependency")
        self.assertEqual(item["severity"], "Medium")
        self.assertEqual(len(item["sources"]), 2)
        self.assertTrue(item["review_required"])

    def test_benchmark_metrics_are_not_formal_sources(self) -> None:
        report = build_migration_cutover_findings(load_default_reports())
        paths = [source["path"] for source in report["source_reports"]]
        self.assertNotIn("erpnext_item_price_blind_evaluation.json", paths)
        self.assertTrue(
            all(
                "ground_truth" not in str(source)
                and "source_top1_accuracy" not in str(source)
                and "precision" not in str(source)
                and "recall" not in str(source)
                for source in report["findings"]
            )
        )

    def test_valid_generated_package_with_no_findings_creates_nothing(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        self.assertEqual(report["source_reports"][0]["report_id"], "generated_validation")
        self.assertEqual(report["source_reports"][0]["finding_count"], 0)
        self.assertFalse([item for item in report["findings"] if item["category"] == "generated_package_validation"])

    def test_explicit_high_non_review_validation_failure_can_recommend_blocker(self) -> None:
        reports = _fixture_reports()
        reports["generated_validation"] = {
            "_run_info": {"content_sha256": "h" * 64},
            "summary": {"valid": False, "finding_count": 1},
            "findings": [
                {
                    "resource": "item",
                    "row_number": 1,
                    "field": "item_code",
                    "category": "required",
                    "raw_code": "constraint-error",
                    "severity": "High",
                    "message": "Required value is missing.",
                }
            ],
            "validation": {"valid": False},
        }
        report = build_migration_cutover_findings(reports)
        generated = [item for item in report["findings"] if item["category"] == "generated_package_validation"]
        self.assertEqual(len(generated), 1)
        self.assertTrue(generated[0]["gate_impact"]["blocker"])

    def test_missing_severity_is_not_invented(self) -> None:
        reports = _fixture_reports()
        reports["generated_validation"] = {
            "_run_info": {"content_sha256": "n" * 64},
            "summary": {"valid": False, "finding_count": 1},
            "findings": [
                {
                    "resource": "item",
                    "row_number": 1,
                    "field": "item_code",
                    "category": "required",
                    "raw_code": "constraint-error",
                    "message": "Required value is missing.",
                }
            ],
            "validation": {"valid": False},
        }
        report = build_migration_cutover_findings(reports)
        item = next(item for item in report["findings"] if item["category"] == "generated_package_validation")
        self.assertIsNone(item["severity"])
        self.assertEqual(item["severity_origin"], "none")
        self.assertTrue(item["review_required"])
        self.assertFalse(item["gate_impact"]["blocker"])

    def test_malformed_required_report_shape_raises_value_error(self) -> None:
        reports = _fixture_reports()
        reports["vendor_validation"].pop("record_view")
        with self.assertRaisesRegex(ValueError, "vendor_validation.*record_view"):
            build_migration_cutover_findings(reports)

    def test_aggregated_record_samples_are_sorted_and_capped(self) -> None:
        reports = _fixture_reports()
        records = []
        for index in range(20, -1, -1):
            records.append(
                {
                    "record_id": f"V{index:03d}",
                    "issues": [
                        {
                            "field": "country",
                            "target": "A_BusinessPartnerAddress.Country",
                            "issue_type": "max_length_overflow",
                            "severity": "high",
                            "based_on_unverified": False,
                        }
                    ],
                }
            )
        reports["vendor_validation"]["record_view"] = records
        report = build_migration_cutover_findings(reports)
        item = _finding(report, "validation|country|a_businesspartneraddress.country|normalization_required")
        record_source = next(source for source in item["sources"] if source["json_pointer"].startswith("/record_view/"))
        self.assertEqual(record_source["matched_record_count"], 21)
        self.assertEqual(len(record_source["record_ids_sample"]), RECORD_SAMPLE_LIMIT)
        self.assertEqual(record_source["record_ids_sample"], [f"V{index:03d}" for index in range(10)])

    def test_output_contains_only_synthetic_classification(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        self.assertEqual(report["_meta"]["data_classification"], "synthetic_demo")
        self.assertNotIn("production", json.dumps(report, ensure_ascii=False).lower())

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "migration_cutover_findings.json"
            env = {**os.environ, "CARVEOPS_OMIT_TIMESTAMP": "1"}
            result = subprocess.run(
                [
                    sys.executable,
                    "src/tools/build_migration_cutover_findings.py",
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("Findings", result.stdout)

    def test_write_report_preserves_bytes_for_same_content(self) -> None:
        report = build_migration_cutover_findings(_fixture_reports())
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
            output = Path(temp_dir) / "report.json"
            write_report(report, output)
            first_sha = _sha(output)
            write_report(build_migration_cutover_findings(_fixture_reports()), output)
            second_sha = _sha(output)
        self.assertEqual(first_sha, second_sha)


if __name__ == "__main__":
    unittest.main()
