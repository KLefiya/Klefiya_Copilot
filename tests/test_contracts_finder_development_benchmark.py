from __future__ import annotations

import csv
import hashlib
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS
from src.core.mapping.benchmark import benchmark_run_specs, load_benchmark


DATASET_ID = "contracts_finder_procurement_2026_v1"
SCENARIO_ID = "contracts_finder_procurement_2026"
REGISTRY_PATH = PROJECT_ROOT / "data/benchmarks/schema_matching_public_dev_v1.json"
FIXTURE_ROOT = PROJECT_ROOT / "data/benchmarks/development" / DATASET_ID
SOURCE_PATH = FIXTURE_ROOT / f"source_{DATASET_ID}.csv"
GROUND_TRUTH_PATH = FIXTURE_ROOT / "ground_truth.json"
METADATA_PATH = FIXTURE_ROOT / "source_metadata.json"
RESULTS_ROOT = FIXTURE_ROOT / "results"
COMBINED_ROOT = PROJECT_ROOT / "data/benchmarks/development/combined_public_dev_v1"
COMBINED_RESULTS_ROOT = COMBINED_ROOT / "results"
COMPARISON_PATH = COMBINED_ROOT / "comparison.json"

SOURCE_COLUMNS = [
    "id",
    "date",
    "ocid",
    "language",
    "initiationType",
    "buyer_id",
    "buyer_name",
    "tender_id",
    "tender_title",
    "tender_mainProcurementCategory",
    "tender_status",
    "tender_description",
    "tender_procurementMethod",
    "tender_value_amount",
    "tender_value_currency",
    "tender_tenderPeriod_endDate",
    "tender_classification_id",
    "tender_classification_scheme",
    "tender_classification_description",
    "tender_contractPeriod_startDate",
    "tender_contractPeriod_endDate",
    "tender_datePublished",
    "title",
]

EXPECTED_TARGET_FIELDS = {
    "sales_order_header.sales_order_id",
    "sales_order_header.customer_purchase_order",
    "sales_order_header.customer_id",
    "sales_order_header.order_date",
    "sales_order_header.currency_code",
    "sales_order_header.distribution_channel",
    "sales_order_header.order_status",
    "sales_order_line.sales_order_id",
    "sales_order_line.line_number",
    "sales_order_line.product_id",
    "sales_order_line.order_quantity",
    "sales_order_line.quantity_uom",
    "sales_order_line.unit_price",
    "sales_order_line.line_amount",
    "sales_order_line.item_description",
    "delivery_schedule.sales_order_id",
    "delivery_schedule.line_number",
    "delivery_schedule.schedule_number",
    "delivery_schedule.requested_ship_date",
    "delivery_schedule.confirmed_ship_date",
    "delivery_schedule.confirmed_quantity",
    "delivery_schedule.fulfillment_status",
}

FORMAL_RESULT_SHAS = {
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json": "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json": "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContractsFinderDevelopmentBenchmarkTest(unittest.TestCase):
    def test_source_fields_ground_truth_and_counts_meet_public_development_contract(self):
        with SOURCE_PATH.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        ground_truth = _load_json(GROUND_TRUTH_PATH)
        mappings = ground_truth["mappings"]
        by_source = {mapping["source_field"]: mapping for mapping in mappings}

        self.assertEqual(SOURCE_COLUMNS, list(rows[0].keys()))
        self.assertEqual(len(rows), 24)
        self.assertEqual(set(SOURCE_COLUMNS), set(by_source))
        self.assertEqual(ground_truth["counts"]["case_count"], 23)
        self.assertGreaterEqual(ground_truth["counts"]["target_bearing_source_field_count"], 8)
        self.assertGreaterEqual(ground_truth["counts"]["expected_target_link_count"], 10)
        self.assertGreaterEqual(ground_truth["counts"]["no_target_case_count"], 2)
        self.assertEqual(ground_truth["counts"]["multi_target_case_count"], 2)
        for mapping in mappings:
            self.assertIn("label_rationale", mapping)
            self.assertTrue(mapping["label_rationale"])
            self.assertLessEqual(set(mapping["expected_targets"]), EXPECTED_TARGET_FIELDS)

    def test_metadata_records_candidate_comparison_license_sampling_and_sensitive_column_exclusion(self):
        metadata = _load_json(METADATA_PATH)

        self.assertEqual(metadata["dataset_id"], DATASET_ID)
        self.assertEqual(metadata["source_domain"], "public_procurement")
        self.assertEqual(metadata["license"]["name"], "Open Government Licence v3.0")
        self.assertEqual(metadata["sampling"]["field_count"], 23)
        self.assertEqual(metadata["sampling"]["row_count"], 24)
        self.assertIn("physical line-end trailing spaces", metadata["sampling"]["source_value_normalization"])
        self.assertEqual(metadata["sampling"]["field_name_normalization"], "none; selected source column names are copied from the public CSV header")
        self.assertIn("not copied", metadata["sampling"]["sensitive_column_handling"])
        self.assertEqual(
            [candidate["name"] for candidate in metadata["candidate_dataset_comparison"]],
            [
                "United Kingdom Contracts Finder OCDS CSV",
                "IDB Project procurement bidding notices and contract awards dataset",
            ],
        )
        self.assertTrue(metadata["benchmark_flags"]["development_benchmark"])
        self.assertFalse(metadata["benchmark_flags"]["formal_evaluation"])
        self.assertFalse(metadata["benchmark_flags"]["sealed_holdout"])
        self.assertTrue(metadata["benchmark_flags"]["repeated_evaluation_allowed"])

    def test_combined_registry_contains_two_domains_and_keeps_ground_truth_out_of_run_specs(self):
        benchmark = load_benchmark(REGISTRY_PATH)
        scenario_ids = [scenario["scenario_id"] for scenario in benchmark["scenarios"]]
        specs = benchmark_run_specs(benchmark)

        self.assertEqual(scenario_ids, ["open_food_facts_products", SCENARIO_ID])
        self.assertEqual(benchmark["_meta"]["source_datasets"], ["open_food_facts_products_v1", DATASET_ID])
        self.assertEqual(len(specs), 2)
        for spec in specs:
            self.assertNotIn("answer_source_path", spec)
            self.assertNotIn("cases", spec)
        contracts_finder = next(spec for spec in specs if spec["scenario_id"] == SCENARIO_ID)
        self.assertEqual(
            contracts_finder["contract_path"],
            "data/benchmarks/fixtures/sales_order_fulfillment/contract/datapackage.yaml",
        )

    def test_single_scenario_results_recompute_major_metrics_and_dev_metadata(self):
        for scorer in ("baseline", "precision_tiered_v4", "precision_tiered_v5"):
            report = _load_json(RESULTS_ROOT / f"{scorer}.json")
            overall = report["overall"]
            self.assertEqual(report["_meta"]["benchmark_id"], "schema_matching_public_dev_v1")
            self.assertTrue(report["_meta"]["development_benchmark"])
            self.assertFalse(report["_meta"]["formal_evaluation"])
            self.assertFalse(report["_meta"]["sealed_holdout"])
            self.assertTrue(report["_meta"]["repeated_evaluation_allowed"])
            self.assertEqual(report["_meta"]["scorer_variant"], scorer)
            self.assertEqual(overall["scenario_count"], 1)
            self.assertEqual(overall["case_count"], 23)
            self.assertEqual(overall["expected_target_link_count"], 19)
            self.assertEqual(overall["no_target_case_count"], 7)
            self.assertEqual(len(report["case_results"]), 23)

    def test_combined_results_and_comparison_cover_both_public_scenarios(self):
        comparison = _load_json(COMPARISON_PATH)
        self.assertEqual(comparison["benchmark_id"], "schema_matching_public_dev_v1")
        self.assertEqual(set(comparison["scenarios"]), {"open_food_facts_products", SCENARIO_ID})
        self.assertIn("top1_different_cases", comparison)
        self.assertIn("top3_order_different_cases", comparison)
        self.assertIn("v4_improved_cases", comparison)
        self.assertIn("v5_regressed_cases", comparison)
        for scorer in ("baseline", "precision_tiered_v4", "precision_tiered_v5"):
            report = _load_json(COMBINED_RESULTS_ROOT / f"{scorer}.json")
            self.assertEqual(report["_meta"]["benchmark_id"], "schema_matching_public_dev_v1")
            self.assertTrue(report["_meta"]["development_benchmark"])
            self.assertFalse(report["_meta"]["formal_evaluation"])
            self.assertEqual(report["overall"]["scenario_count"], 2)
            self.assertEqual(report["overall"]["case_count"], 35)
            self.assertEqual(report["overall"]["expected_target_link_count"], 25)
            self.assertEqual({item["scenario_id"] for item in report["by_scenario"]}, {"open_food_facts_products", SCENARIO_ID})
            self.assertEqual(report["overall"], comparison["scorer_metrics"][scorer]["overall"])

    def test_public_development_artifacts_do_not_enter_formal_inventory(self):
        self.assertEqual(len(FORMAL_ARTIFACTS), 45)
        forbidden_fragments = ("open_food_facts_products_v1", DATASET_ID, "combined_public_dev_v1", "schema_matching_public_dev_v1")
        self.assertFalse(any(any(fragment in artifact for fragment in forbidden_fragments) for artifact in FORMAL_ARTIFACTS))
        for relative_path, expected_sha in FORMAL_RESULT_SHAS.items():
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha)
        self.assertTrue((PROJECT_ROOT / "data/benchmarks/development/open_food_facts_products_v1/source_open_food_facts_products.csv").exists())


if __name__ == "__main__":
    unittest.main()
