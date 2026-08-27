from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS
from src.core.mapping.benchmark import benchmark_run_specs, evaluate_benchmark, generate_candidate_reports, load_benchmark


BENCHMARK_PATH = PROJECT_ROOT / "data/benchmarks/schema_matching_public_dev_v1.json"
FIXTURE_ROOT = PROJECT_ROOT / "data/benchmarks/development/open_food_facts_products_v1"
SOURCE_PATH = FIXTURE_ROOT / "source_open_food_facts_products.csv"
GROUND_TRUTH_PATH = FIXTURE_ROOT / "ground_truth.json"
METADATA_PATH = FIXTURE_ROOT / "source_metadata.json"
RESULTS_ROOT = FIXTURE_ROOT / "results"

SOURCE_COLUMNS = [
    "code",
    "product_name",
    "generic_name",
    "quantity",
    "categories",
    "brands",
    "countries",
    "nutrition_grades",
    "nova_group",
    "packaging",
    "stores",
    "ingredients_text",
]

EXPECTED_TARGET_FIELDS = {
    "item.item_code",
    "item.item_name",
    "item.item_group",
    "item.stock_uom",
    "item.disabled",
    "item_price.item_code",
    "item_price.uom",
    "item_price.price_list",
    "item_price.price_list_rate",
    "item_price.valid_from",
    "item_price.valid_upto",
}

COMPANIES_HOUSE_FROZEN_SHAS = {
    "data/benchmarks/external/companies_house_customer_v1/first_evaluation_baseline_v4_v5.json": "d08584b1e77e59ba5362586d851e225e9d746f52eb01c2b268ffe2b68dc7edd8",
    "scripts/evaluate_companies_house_external_benchmark.py": "11618569e69c1bd360f1b738e6d72388c146add504f257f056fc22d30302edb4",
}

FORMAL_RESULT_SHAS = {
    "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json": "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
    "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json": "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
}


class FakeEmbeddingBackend:
    def encode(self, sentences, normalize_embeddings=True, show_progress_bar=False):
        return [self._vector(str(text), normalize_embeddings) for text in sentences]

    def _vector(self, text: str, normalize_embeddings: bool) -> list[float]:
        lowered = text.lower()
        features = ["item", "product", "code", "name", "category", "uom", "price", "quantity"]
        vector = [1.0 if feature in lowered else 0.0 for feature in features]
        vector.append(0.0 if any(vector) else 1.0)
        if normalize_embeddings:
            norm = math.sqrt(sum(item * item for item in vector))
            vector = [item / norm for item in vector]
        return vector


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OpenFoodFactsDevelopmentBenchmarkTest(unittest.TestCase):
    def test_development_benchmark_loads_without_exposing_ground_truth_to_runtime_specs(self):
        benchmark = load_benchmark(BENCHMARK_PATH)

        self.assertEqual(benchmark["_meta"]["benchmark_id"], "schema_matching_public_dev_v1")
        self.assertTrue(benchmark["_meta"]["development_benchmark"])
        self.assertFalse(benchmark["_meta"]["formal_evaluation"])
        self.assertFalse(benchmark["_meta"]["sealed_holdout"])
        self.assertTrue(benchmark["_meta"]["repeated_evaluation_allowed"])
        self.assertTrue(benchmark["_meta"]["not_evidence_of_unseen_generalization"])
        self.assertEqual(benchmark["scenarios"][0]["split"], "development")

        specs = benchmark_run_specs(benchmark)
        spec = next(item for item in specs if item["scenario_id"] == "open_food_facts_products")
        self.assertNotIn("answer_source_path", spec)
        self.assertNotIn("cases", spec)

    def test_public_source_sample_and_metadata_are_development_scoped(self):
        with SOURCE_PATH.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        metadata = _load_json(METADATA_PATH)

        self.assertEqual(SOURCE_COLUMNS, list(rows[0].keys()))
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row["code"] for row in rows}), 24)
        for row in rows:
            self.assertTrue(row["code"])
            self.assertTrue(row["product_name"])
            self.assertTrue(row["quantity"])
            self.assertTrue(row["categories"])
        self.assertEqual(metadata["dataset_id"], "open_food_facts_products_v1")
        self.assertEqual(metadata["license"]["attribution"], "Open Food Facts contributors")
        self.assertEqual(metadata["sampling"]["source_value_normalization"], "none; API values are written as returned, except CSV escaping performed by Python csv module")
        self.assertTrue(metadata["benchmark_flags"]["development_benchmark"])
        self.assertFalse(metadata["benchmark_flags"]["formal_evaluation"])
        self.assertFalse(metadata["target_contract"]["authoritative"])
        self.assertEqual(
            [candidate["name"] for candidate in metadata["candidate_dataset_comparison"]],
            ["Open Food Facts", "USDA FoodData Central"],
        )

    def test_ground_truth_counts_targets_and_no_target_cases_are_explicit(self):
        ground_truth = _load_json(GROUND_TRUTH_PATH)
        mappings = ground_truth["mappings"]
        grouped = {item["source_field"]: item for item in mappings}

        self.assertEqual(ground_truth["counts"]["case_count"], 12)
        self.assertEqual(ground_truth["counts"]["single_target_case_count"], 2)
        self.assertEqual(ground_truth["counts"]["multi_target_case_count"], 2)
        self.assertEqual(ground_truth["counts"]["no_target_case_count"], 8)
        self.assertEqual(ground_truth["counts"]["expected_target_link_count"], 6)
        self.assertEqual(set(grouped), set(SOURCE_COLUMNS))
        self.assertEqual(grouped["code"]["expected_targets"], ["item.item_code", "item_price.item_code"])
        self.assertEqual(grouped["product_name"]["expected_targets"], ["item.item_name"])
        self.assertEqual(grouped["quantity"]["expected_targets"], ["item.stock_uom", "item_price.uom"])
        self.assertEqual(grouped["categories"]["expected_targets"], ["item.item_group"])

        all_targets = {target for item in mappings for target in item["expected_targets"]}
        self.assertLessEqual(all_targets, EXPECTED_TARGET_FIELDS)
        self.assertEqual(
            sorted(item["source_field"] for item in mappings if not item["expected_targets"]),
            [
                "brands",
                "countries",
                "generic_name",
                "ingredients_text",
                "nova_group",
                "nutrition_grades",
                "packaging",
                "stores",
            ],
        )

    def test_candidate_generation_uses_fake_backend_and_result_meta_marks_non_formal_development(self):
        benchmark = load_benchmark(BENCHMARK_PATH)
        benchmark["scenarios"] = [
            scenario for scenario in benchmark["scenarios"] if scenario["scenario_id"] == "open_food_facts_products"
        ]
        candidate_reports = generate_candidate_reports(
            benchmark_run_specs(benchmark),
            embedding_backend=FakeEmbeddingBackend(),
            scorer_variant="baseline",
        )
        report = evaluate_benchmark(benchmark, candidate_reports, scorer_variant="baseline")

        self.assertEqual(report["_meta"]["benchmark_id"], "schema_matching_public_dev_v1")
        self.assertTrue(report["_meta"]["development_benchmark"])
        self.assertFalse(report["_meta"]["formal_evaluation"])
        self.assertFalse(report["_meta"]["sealed_holdout"])
        self.assertTrue(report["_meta"]["repeated_evaluation_allowed"])
        self.assertTrue(report["_meta"]["not_evidence_of_unseen_generalization"])
        self.assertEqual(report["overall"]["case_count"], 12)
        self.assertEqual(report["overall"]["expected_target_link_count"], 6)
        self.assertEqual(len(report["case_results"]), 12)

    def test_development_benchmark_is_not_part_of_formal_inventory_or_companies_house_freeze(self):
        self.assertEqual(len(FORMAL_ARTIFACTS), 45)
        self.assertNotIn("data/benchmarks/schema_matching_public_dev_v1.json", FORMAL_ARTIFACTS)
        self.assertFalse(any("open_food_facts_products_v1" in artifact for artifact in FORMAL_ARTIFACTS))
        for relative_path, expected_sha in COMPANIES_HOUSE_FROZEN_SHAS.items():
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha)
        for relative_path, expected_sha in FORMAL_RESULT_SHAS.items():
            self.assertEqual(_raw_sha(PROJECT_ROOT / relative_path), expected_sha)

    def test_saved_development_results_are_isolated_from_formal_artifacts(self):
        expected_results = {
            "baseline": RESULTS_ROOT / "baseline.json",
            "precision_tiered_v4": RESULTS_ROOT / "precision_tiered_v4.json",
            "precision_tiered_v5": RESULTS_ROOT / "precision_tiered_v5.json",
        }
        for scorer, path in expected_results.items():
            self.assertTrue(path.exists(), f"missing saved result for {scorer}")
            report = _load_json(path)
            self.assertEqual(report["_meta"]["benchmark_id"], "schema_matching_public_dev_v1")
            self.assertTrue(report["_meta"]["development_benchmark"])
            self.assertFalse(report["_meta"]["formal_evaluation"])
            self.assertFalse(report["_meta"]["sealed_holdout"])
            self.assertTrue(report["_meta"]["repeated_evaluation_allowed"])
            self.assertTrue(report["_meta"]["not_evidence_of_unseen_generalization"])
            self.assertEqual(report["_meta"]["scorer_variant"], scorer)
            self.assertEqual(report["overall"]["case_count"], 12)
            self.assertEqual(report["overall"]["expected_target_link_count"], 6)
            self.assertNotIn(path.as_posix(), FORMAL_ARTIFACTS)


if __name__ == "__main__":
    unittest.main()
