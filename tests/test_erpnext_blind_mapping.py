from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.evaluate_blind_multitarget_mapping import evaluate_blind_multitarget_mapping


REFERENCE = PROJECT_ROOT / "references" / "erpnext_item_price" / "upstream_reference.json"
CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
DATA_ROOT = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = DATA_ROOT / "source_product_catalog.csv"
TRUTH = DATA_ROOT / "ground_truth.json"
LOCK = DATA_ROOT / "blind_protocol_lock.json"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _source_rows() -> list[dict[str, str]]:
    with SOURCE.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _contract_doc():
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _aliases_count() -> int:
    return CONTRACT.read_text(encoding="utf-8").count("aliases:")


def _per_source(name: str) -> dict:
    evaluation = _json(EVALUATION)
    return next(item for item in evaluation["per_source_results"] if item["source_field"] == name)


def _has_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_has_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


class ErpnextBlindMappingTests(unittest.TestCase):
    def test_01_upstream_reference_has_fixed_commit(self):
        reference = _json(REFERENCE)
        self.assertEqual(reference["repository"], "frappe/erpnext")
        self.assertEqual(reference["commit_sha"], "a051a12d9b6603bb838f8b8149436a3a7900d545")
        self.assertEqual(reference["license"], "GPL-3.0")

    def test_02_upstream_sources_have_sha(self):
        reference = _json(REFERENCE)
        self.assertEqual(len(reference["sources"]), 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in reference["sources"]))

    def test_03_contract_is_not_authoritative(self):
        doc = _contract_doc()
        self.assertFalse(doc["carveops"]["authoritative"])
        self.assertEqual(doc["carveops"]["contract_id"], "erpnext-item-price-reference-v1")

    def test_04_contract_has_zero_aliases(self):
        self.assertEqual(_aliases_count(), 0)

    def test_05_source_does_not_use_target_field_names(self):
        header = list(_source_rows()[0].keys())
        forbidden = {"item_code", "item_name", "item_group", "stock_uom", "disabled", "uom", "price_list", "price_list_rate", "valid_from", "valid_upto"}
        self.assertFalse(forbidden.intersection(header))

    def test_06_source_has_ten_fields_and_eight_rows(self):
        rows = _source_rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len(rows[0]), 10)

    def test_07_ground_truth_stats(self):
        truth = _json(TRUTH)
        links = sum(len(item["expected_targets"]) for item in truth["mappings"])
        multi = sum(1 for item in truth["mappings"] if len(item["expected_targets"]) > 1)
        no_target = sum(1 for item in truth["mappings"] if not item["expected_targets"])
        self.assertEqual(links, 11)
        self.assertEqual(multi, 2)
        self.assertEqual(no_target, 1)

    def test_08_lock_was_created_before_mapping(self):
        lock = _json(LOCK)
        self.assertTrue(lock["locked_before_first_mapping"])
        self.assertFalse(lock["aliases_present"])

    def test_09_lock_sha_matches_files(self):
        lock = _json(LOCK)
        self.assertEqual(lock["contract_sha256"], _sha(CONTRACT))
        self.assertEqual(lock["source_sha256"], _sha(SOURCE))
        self.assertEqual(lock["ground_truth_sha256"], _sha(TRUTH))
        self.assertEqual(lock["upstream_reference_sha256"], _sha(REFERENCE))

    def test_10_engine_commit_is_correct(self):
        self.assertEqual(_json(LOCK)["engine_commit"], "c908712daab4f4989988d41e869df693b5d8944a")

    def test_11_engine_file_sha_matches_lock(self):
        lock = _json(LOCK)
        self.assertTrue(all(_sha(PROJECT_ROOT / path) == value for path, value in lock["engine_files"].items()))

    def test_12_mapping_engine_does_not_read_truth_file(self):
        files = [
            PROJECT_ROOT / "src/core/mapping/profiler.py",
            PROJECT_ROOT / "src/core/mapping/target_index.py",
            PROJECT_ROOT / "src/core/mapping/scorer.py",
            PROJECT_ROOT / "src/core/mapping/engine.py",
            PROJECT_ROOT / "src/tools/suggest_contract_mappings.py",
        ]
        forbidden = ("ground_truth.json", "ground_truth_path", "ground-truth", "Ground Truth")
        self.assertFalse(any(pattern in path.read_text(encoding="utf-8") for path in files for pattern in forbidden))

    def test_13_evaluator_does_not_modify_mapping(self):
        before = _sha(MAPPING)
        evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK)
        self.assertEqual(before, _sha(MAPPING))

    def test_14_multitarget_metrics_are_correct(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["expected_target_links"], 11)
        self.assertEqual(summary["top3_target_links_found"], 11)
        self.assertEqual(summary["top3_target_link_recall"], 1.0)
        self.assertEqual(summary["multi_target_full_top3_coverage"], 1.0)

    def test_15_no_target_metrics_are_correct(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["no_target_source_fields"], 1)
        self.assertEqual(summary["no_target_correct"], 1)
        self.assertEqual(summary["false_positive_no_target"], 0)

    def test_16_zero_high_confidence_precision_is_undefined(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["high_confidence_predictions"], 0)
        self.assertEqual(summary["high_confidence_source_correct"], 0)
        self.assertIsNone(summary["high_confidence_source_precision"])
        self.assertFalse(summary["high_confidence_source_precision_defined"])

    def test_17_positive_high_confidence_precision_is_defined(self):
        mapping = _json(MAPPING)
        for item in mapping["mappings"]:
            if item["source_field"] == "inventory_measure":
                item["confidence"] = 0.9
                item["recommendation"] = "item.stock_uom"
                break
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            mapping_path = Path(tmp) / "mapping.json"
            mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
            report = evaluate_blind_multitarget_mapping(mapping_path, TRUTH, LOCK)
        summary = report["summary"]
        self.assertEqual(summary["high_confidence_predictions"], 1)
        self.assertEqual(summary["high_confidence_source_correct"], 1)
        self.assertEqual(summary["high_confidence_source_precision"], 1.0)
        self.assertTrue(summary["high_confidence_source_precision_defined"])

    def test_18_capability_observations_are_current_boundaries(self):
        observations = _json(EVALUATION)["capability_observations"]
        self.assertFalse(observations["mapping_report_supports_multiple_recommendations_per_source"])
        self.assertFalse(observations["decision_loader_supports_one_source_to_multiple_targets"])
        self.assertFalse(observations["package_builder_can_execute_one_to_many_source_mapping"])

    def test_19_reports_have_no_absolute_paths(self):
        self.assertFalse(_has_absolute_path(_json(MAPPING)))
        self.assertFalse(_has_absolute_path(_json(EVALUATION)))

    def test_20_evaluation_content_sha_is_stable(self):
        first = evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK)
        second = evaluate_blind_multitarget_mapping(MAPPING, TRUTH, LOCK)
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_21_article_number_result_is_preserved(self):
        result = _per_source("article_number")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["expected_targets_found_in_top3"], ["item.item_code", "item_price.item_code"])

    def test_22_inventory_measure_result_is_preserved(self):
        result = _per_source("inventory_measure")
        self.assertEqual(result["recommendation"], "item.stock_uom")
        self.assertEqual(result["expected_targets_found_in_top3"], ["item.stock_uom", "item_price.uom"])

    def test_23_data_steward_result_is_preserved(self):
        result = _per_source("data_steward")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["expected_targets"], [])

    def test_24_mapping_sha_is_preserved(self):
        self.assertEqual(
            _json(MAPPING)["_run_info"]["content_sha256"],
            "f2b1a3b578222694b845950165334b628a6e8285d54287457af10fb2fd836164",
        )

    def test_25_protocol_lock_file_sha_is_preserved(self):
        self.assertEqual(
            _sha(LOCK),
            "bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed",
        )

    def test_26_substantive_metrics_are_preserved(self):
        summary = _json(EVALUATION)["summary"]
        self.assertEqual(summary["source_top1_accuracy"], 0.2222)
        self.assertEqual(summary["top3_target_link_recall"], 1.0)
        self.assertEqual(summary["multi_target_full_top3_coverage"], 1.0)
        self.assertEqual(summary["no_target_accuracy"], 1.0)

    def test_27_smoke_passes(self):
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test_erpnext_blind_mapping.py"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Validation: valid", result.stdout)


if __name__ == "__main__":
    unittest.main()
