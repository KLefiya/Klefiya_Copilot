from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Iterator

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.benchmark import (
    ALLOWED_SCORERS,
    BASELINE_SCORER_ID,
    SchemaMatchingBenchmarkError,
    benchmark_run_specs,
    evaluate_benchmark,
    generate_candidate_reports,
    load_benchmark,
)
from src.core.hashing import canonical_json_content_sha256
from scripts.verify_formal_artifacts_immutable import FORMAL_ARTIFACTS


BENCHMARK = PROJECT_ROOT / "data" / "benchmarks" / "schema_matching_v1.json"
BANK_SOURCE = PROJECT_ROOT / "data" / "benchmarks" / "fixtures" / "bank_account" / "source_bank_accounts.csv"
BANK_TRUTH = PROJECT_ROOT / "data" / "benchmarks" / "fixtures" / "bank_account" / "ground_truth.json"
BANK_CONTRACT = PROJECT_ROOT / "data" / "benchmarks" / "fixtures" / "bank_account" / "contract" / "datapackage.yaml"
BANK_TARGET = PROJECT_ROOT / "data" / "benchmarks" / "fixtures" / "bank_account" / "target"
SALES_ORDER_ROOT = PROJECT_ROOT / "data" / "benchmarks" / "fixtures" / "sales_order_fulfillment"
SALES_ORDER_SOURCE = SALES_ORDER_ROOT / "source_sales_order_fulfillment.csv"
SALES_ORDER_TRUTH = SALES_ORDER_ROOT / "ground_truth.json"
SALES_ORDER_CONTRACT = SALES_ORDER_ROOT / "contract" / "datapackage.yaml"
SALES_ORDER_LOCK = SALES_ORDER_ROOT / "fixture_lock.json"
SALES_ORDER_TARGET = SALES_ORDER_ROOT / "target"
FORMAL_V4_EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "schema_matching_precision_tiered_v4_5scenario_evaluation.json"
SALES_ORDER_SOURCE_HEADER = [
    "legacy_sales_order_id",
    "customer_po_ref",
    "sold_to_ref",
    "order_created_on",
    "document_currency",
    "distribution_channel",
    "order_state",
    "header_audit_marker",
    "legacy_line_item_no",
    "material_ref",
    "ordered_qty",
    "sales_uom",
    "net_unit_amount",
    "line_total_amount",
    "item_description",
    "line_analyst_note",
    "schedule_seq",
    "requested_ship_on",
    "confirmed_ship_on",
    "confirmed_qty",
    "fulfillment_state",
    "migration_batch_label",
]
SALES_ORDER_TARGET_FIELDS = {
    "sales_order_header": {
        "sales_order_id",
        "customer_purchase_order",
        "customer_id",
        "order_date",
        "currency_code",
        "distribution_channel",
        "order_status",
    },
    "sales_order_line": {
        "sales_order_id",
        "line_number",
        "product_id",
        "order_quantity",
        "quantity_uom",
        "unit_price",
        "line_amount",
        "item_description",
    },
    "delivery_schedule": {
        "sales_order_id",
        "line_number",
        "schedule_number",
        "requested_ship_date",
        "confirmed_ship_date",
        "confirmed_quantity",
        "fulfillment_status",
    },
}


class FakeEmbeddingBackend:
    def encode(self, sentences, normalize_embeddings=True, show_progress_bar=False):
        return [self._vector(text, normalize_embeddings) for text in sentences]

    def _vector(self, text: str, normalize_embeddings: bool):
        lowered = text.lower()
        features = ["customer", "id", "name", "email", "flag", "text"]
        vector = [1.0 if feature in lowered else 0.0 for feature in features]
        vector.append(0.0 if any(vector) else 1.0)
        if normalize_embeddings:
            norm = math.sqrt(sum(item * item for item in vector))
            vector = [item / norm for item in vector]
        return vector


def _candidate(target: str, rank: int, score: float) -> dict:
    return {
        "target": target,
        "rank": rank,
        "score": score,
        "semantic_score": score,
        "fuzzy_score": score,
        "alias_hit": False,
        "alias_source": None,
        "lexical_overlap": 0.0,
        "type_gate": 1.0,
        "warnings": [],
    }


def _suggestion(source_field: str, recommendation: str | None, candidates: list[dict]) -> dict:
    return {
        "source_field": source_field,
        "status": "suggested" if recommendation else "no_confident_target",
        "recommendation": recommendation,
        "confidence": candidates[0]["score"] if recommendation and candidates else 0.0,
        "band": "medium",
        "mapping_basis": "mixed",
        "top_candidates": candidates,
        "review_reasons": [],
    }


def _scenario(cases: list[dict], scenario_id: str = "unit", split: str = "train") -> dict:
    return {
        "scenario_id": scenario_id,
        "split": split,
        "source_path": "data/examples/mapping/generic_customer/source_customer.csv",
        "contract_path": "contracts/generic_customer/datapackage.yaml",
        "data_root_path": "data/examples/generic_customer",
        "answer_source_path": "data/examples/mapping/generic_customer/ground_truth.json",
        "cases": cases,
    }


def _case(case_id: str, source_field: str, targets: list[str], tags: list[str] | None = None) -> dict:
    return {
        "case_id": case_id,
        "source_field": source_field,
        "expected_targets": targets,
        "expected_no_target": not targets,
        "difficulty_tags": tags or ["unit"],
    }


def _benchmark(cases: list[dict], *, scenario_id: str = "unit", split: str = "train") -> dict:
    return {
        "_meta": {
            "benchmark_id": "schema_matching_v1",
            "synthetic_demo": True,
            "ground_truth_runtime_boundary": "evaluation_only",
        },
        "scenarios": [_scenario(cases, scenario_id, split)],
    }


def _report(suggestions: list[dict]) -> dict:
    return {"mappings": suggestions}


class SchemaMatchingBenchmarkTests(unittest.TestCase):
    def test_01_fixture_loads_and_records_ground_truth_boundary(self):
        benchmark = load_benchmark(BENCHMARK)
        self.assertEqual(benchmark["_meta"]["benchmark_id"], "schema_matching_v1")
        self.assertTrue(benchmark["_meta"]["synthetic_demo"])
        self.assertEqual(benchmark["_meta"]["ground_truth_runtime_boundary"], "evaluation_only")
        self.assertFalse(benchmark["_meta"]["ground_truth_used_for_candidate_generation"])
        self.assertTrue(benchmark["_meta"]["ground_truth_used_for_evaluation"])

    def test_02_fixture_counts(self):
        benchmark = load_benchmark(BENCHMARK)
        counts = _fixture_counts(benchmark)
        self.assertEqual(counts["scenario_count"], 5)
        self.assertEqual(counts["case_count"], 72)
        self.assertEqual(counts["single_target_case_count"], 59)
        self.assertEqual(counts["multi_target_case_count"], 5)
        self.assertEqual(counts["no_target_case_count"], 8)
        self.assertEqual(counts["expected_target_link_count"], 70)

    def test_03_top1_rank2_missing_recall_and_mrr(self):
        benchmark = _benchmark(
            [
                _case("rank1", "legacy_client_id", ["customer.customer_id"], ["top1"]),
                _case("rank2", "client_name", ["customer.customer_name"], ["rank2"]),
                _case("missing", "nation", ["customer.country"], ["missing"]),
            ]
        )
        report = evaluate_benchmark(
            benchmark,
            {
                "unit": _report(
                    [
                        _suggestion("legacy_client_id", "customer.customer_id", [_candidate("customer.customer_id", 1, 0.9)]),
                        _suggestion(
                            "client_name",
                            "customer.email",
                            [_candidate("customer.email", 1, 0.8), _candidate("customer.customer_name", 2, 0.7)],
                        ),
                        _suggestion("nation", "customer.email", [_candidate("customer.email", 1, 0.8)]),
                    ]
                )
            },
        )
        self.assertEqual(report["overall"]["single_target_top1_accuracy"], 0.3333)
        self.assertEqual(report["overall"]["target_link_recall_at_1"], 0.3333)
        self.assertEqual(report["overall"]["target_link_recall_at_3"], 0.6667)
        self.assertEqual(report["overall"]["target_link_mrr"], 0.5)
        self.assertEqual([item["error_type"] for item in report["case_results"]], [
            "expected_target_missing_from_top3",
            "correct_top1",
            "correct_but_ranked_2_or_3",
        ])

    def test_04_no_target_correct_and_false_positive(self):
        benchmark = _benchmark(
            [
                _case("no_target_ok", "marketing_opt_in", [], ["no_target"]),
                _case("no_target_bad", "legacy_client_id", [], ["no_target"]),
            ]
        )
        report = evaluate_benchmark(
            benchmark,
            {
                "unit": _report(
                    [
                        _suggestion("marketing_opt_in", None, [_candidate("customer.customer_id", 1, 0.2)]),
                        _suggestion("legacy_client_id", "customer.customer_id", [_candidate("customer.customer_id", 1, 0.9)]),
                    ]
                )
            },
        )
        self.assertEqual(report["overall"]["no_target_accuracy"], 0.5)
        self.assertEqual(report["error_count_by_type"]["correct_no_target"], 1)
        self.assertEqual(report["error_count_by_type"]["false_positive_for_no_target"], 1)

    def test_05_multi_target_full_partial_and_missing(self):
        benchmark = _benchmark(
            [
                _case("multi_full", "legacy_client_id", ["customer.customer_id", "customer_bank.customer_id"], ["multi"]),
                _case("multi_partial", "client_name", ["customer.customer_name", "customer.email"], ["multi"]),
                _case("multi_missing", "nation", ["customer.country", "customer.phone"], ["multi"]),
            ]
        )
        report = evaluate_benchmark(
            benchmark,
            {
                "unit": _report(
                    [
                        _suggestion(
                            "legacy_client_id",
                            "customer.customer_id",
                            [_candidate("customer.customer_id", 1, 0.9), _candidate("customer_bank.customer_id", 2, 0.8)],
                        ),
                        _suggestion("client_name", "customer.customer_name", [_candidate("customer.customer_name", 1, 0.9)]),
                        _suggestion("nation", "customer.email", [_candidate("customer.email", 1, 0.9)]),
                    ]
                )
            },
        )
        self.assertEqual(report["overall"]["multi_target_full_coverage_at_3"], 0.3333)
        self.assertEqual(report["error_count_by_type"]["multi_target_fully_covered"], 1)
        self.assertEqual(report["error_count_by_type"]["multi_target_partially_covered"], 1)
        self.assertEqual(report["error_count_by_type"]["multi_target_missing"], 1)

    def test_06_empty_denominators_return_null(self):
        benchmark = _benchmark([_case("no_target_only", "marketing_opt_in", [], ["no_target"])])
        report = evaluate_benchmark(
            benchmark,
            {"unit": _report([_suggestion("marketing_opt_in", None, [_candidate("customer.customer_id", 1, 0.2)])])},
        )
        self.assertIsNone(report["overall"]["single_target_top1_accuracy"])
        self.assertIsNone(report["overall"]["target_link_recall_at_1"])
        self.assertIsNone(report["overall"]["target_link_recall_at_3"])
        self.assertIsNone(report["overall"]["target_link_mrr"])
        self.assertIsNone(report["overall"]["multi_target_full_coverage_at_3"])

    def test_07_duplicate_case_id_rejected(self):
        fixture = _temp_fixture(_benchmark([
            _case("dup", "legacy_client_id", ["customer.customer_id"]),
            _case("dup", "client_name", ["customer.customer_name"]),
        ]))
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Duplicate case_id"):
            load_benchmark(fixture)

    def test_08_unknown_source_and_target_rejected(self):
        bad_source = _temp_fixture(_benchmark([_case("bad_source", "not_a_source", ["customer.customer_id"])]))
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Unknown source_field"):
            load_benchmark(bad_source)
        bad_target = _temp_fixture(_benchmark([_case("bad_target", "legacy_client_id", ["customer.not_a_target"])]))
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "unknown targets"):
            load_benchmark(bad_target)

    def test_09_shared_train_split_allowed_duplicate_scenario_rejected_and_unknown_split_rejected(self):
        fixture = _benchmark([_case("one", "legacy_client_id", ["customer.customer_id"])])
        fixture["scenarios"].append(_scenario([_case("two", "client_name", ["customer.customer_name"])], "other", "train"))
        loaded = load_benchmark(_temp_fixture(fixture))
        self.assertEqual([scenario["split"] for scenario in loaded["scenarios"]], ["train", "train"])

        duplicate = _benchmark([_case("one", "legacy_client_id", ["customer.customer_id"])])
        duplicate["scenarios"].append(_scenario([_case("two", "client_name", ["customer.customer_name"])], "unit", "validation"))
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Duplicate scenario_id"):
            load_benchmark(_temp_fixture(duplicate))

        unknown = _benchmark([_case("one", "legacy_client_id", ["customer.customer_id"])], split="holdout")
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Unknown scenario split"):
            load_benchmark(_temp_fixture(unknown))

    def test_10_deterministic_ordering_and_hash(self):
        fixture = _benchmark(
            [
                _case("b", "client_name", ["customer.customer_name"], ["beta"]),
                _case("a", "legacy_client_id", ["customer.customer_id"], ["alpha"]),
            ]
        )
        reports = {
            "unit": _report(
                [
                    _suggestion("client_name", "customer.customer_name", [_candidate("customer.customer_name", 1, 0.8)]),
                    _suggestion("legacy_client_id", "customer.customer_id", [_candidate("customer.customer_id", 1, 0.9)]),
                ]
            )
        }
        first = evaluate_benchmark(fixture, reports)
        second = evaluate_benchmark(fixture, reports)
        self.assertEqual([item["case_id"] for item in first["case_results"]], ["a", "b"])
        self.assertEqual(first["_run_info"]["content_sha256"], second["_run_info"]["content_sha256"])

    def test_11_candidate_generation_runs_before_answer_evaluation(self):
        with _contract_fixture() as fixture_path:
            benchmark = load_benchmark(fixture_path)
            specs = benchmark_run_specs(benchmark)
            self.assertNotIn("cases", specs[0])
            self.assertNotIn("answer_source_path", specs[0])
            candidate_reports = generate_candidate_reports(specs, embedding_backend=FakeEmbeddingBackend())
            report = evaluate_benchmark(benchmark, candidate_reports)
            self.assertFalse(report["_meta"]["ground_truth_used_for_candidate_generation"])
            self.assertTrue(report["_meta"]["ground_truth_used_for_evaluation"])

    def test_11b_scorer_variants_are_explicit_and_metadata_is_recorded(self):
        with _contract_fixture() as fixture_path:
            benchmark = load_benchmark(fixture_path)
            specs = benchmark_run_specs(benchmark)
            baseline_reports = generate_candidate_reports(specs, embedding_backend=FakeEmbeddingBackend())
            baseline = evaluate_benchmark(benchmark, baseline_reports)
            self.assertEqual(baseline["_meta"]["scorer_variant"], "baseline")
            self.assertIsNone(baseline["_meta"]["feature_version"])

            v2_reports = generate_candidate_reports(
                specs,
                embedding_backend=FakeEmbeddingBackend(),
                scorer_variant="value_pattern_v2",
            )
            v2 = evaluate_benchmark(benchmark, v2_reports, scorer_variant="value_pattern_v2")
            self.assertEqual(v2["_meta"]["scorer_variant"], "value_pattern_v2")
            self.assertEqual(v2["_meta"]["feature_version"], "value_pattern_v1")
            first_report = next(iter(v2_reports.values()))
            self.assertTrue(first_report["_meta"]["experimental"])
            self.assertFalse(first_report["_meta"]["production_scorer_modified"])
            self.assertFalse(first_report["_meta"]["historical_blind_protocol_claimed"])
            self.assertFalse(first_report["_meta"]["ground_truth_used"])

            v3_reports = generate_candidate_reports(
                specs,
                embedding_backend=FakeEmbeddingBackend(),
                scorer_variant="target_context_v3",
            )
            v3 = evaluate_benchmark(benchmark, v3_reports, scorer_variant="target_context_v3")
            self.assertEqual(v3["_meta"]["scorer_variant"], "target_context_v3")
            self.assertEqual(v3["_meta"]["feature_version"], "target_resource_context_v1")
            first_v3_report = next(iter(v3_reports.values()))
            self.assertEqual(first_v3_report["_meta"]["parent_scorer"], "value_pattern_v2")
            self.assertEqual(first_v3_report["_meta"]["context_window"], 2)
            self.assertEqual(first_v3_report["_meta"]["anchor_score_min"], 0.45)
            self.assertEqual(first_v3_report["_meta"]["anchor_margin_min"], 0.05)
            self.assertEqual(first_v3_report["_meta"]["resource_support_min"], 0.60)
            self.assertEqual(first_v3_report["_meta"]["resource_context_bonus_weight"], 0.10)
            self.assertFalse(first_v3_report["_meta"]["production_scorer_modified"])
            self.assertFalse(first_v3_report["_meta"]["ground_truth_used"])

            v4_reports = generate_candidate_reports(
                specs,
                embedding_backend=FakeEmbeddingBackend(),
                scorer_variant="precision_tiered_v4",
            )
            v4 = evaluate_benchmark(benchmark, v4_reports, scorer_variant="precision_tiered_v4")
            self.assertEqual(v4["_meta"]["scorer_variant"], "precision_tiered_v4")
            self.assertEqual(v4["_meta"]["scorer_id"], "precision_tiered_v4")
            self.assertEqual(v4["_meta"]["feature_version"], "precision_tiered_interaction_v1")
            self.assertFalse(v4["_meta"]["ground_truth_used_for_concept_extraction"])
            first_v4_report = next(iter(v4_reports.values()))
            self.assertEqual(first_v4_report["_meta"]["parent_scorer"], "target_context_v3")
            self.assertFalse(first_v4_report["_meta"]["production_scorer_modified"])
            self.assertFalse(first_v4_report["_meta"]["ground_truth_used_for_scoring"])

            with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Unknown scorer variant"):
                generate_candidate_reports(specs, embedding_backend=FakeEmbeddingBackend(), scorer_variant="future")

    def test_11c_cli_scorer_contract_matches_registry(self):
        from src.tools.evaluate_schema_matching_benchmark import build_parser

        expected = {
            "baseline",
            "value_pattern_v2",
            "target_context_v3",
            "precision_tiered_v4",
        }
        self.assertEqual(ALLOWED_SCORERS, expected)

        parser = build_parser()
        scorer_action = next(action for action in parser._actions if action.dest == "scorer")
        self.assertEqual(set(scorer_action.choices), ALLOWED_SCORERS)
        self.assertEqual(scorer_action.default, BASELINE_SCORER_ID)

        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            parser.parse_args([
                "--benchmark",
                str(Path(tempfile.gettempdir()) / "benchmark.json"),
                "--output",
                str(Path(tempfile.gettempdir()) / "report.json"),
                "--scorer",
                "future",
            ])

        parsed = parser.parse_args([
            "--benchmark",
            str(Path(tempfile.gettempdir()) / "benchmark.json"),
            "--output",
            str(Path(tempfile.gettempdir()) / "report.json"),
            "--scorer",
            "precision_tiered_v4",
        ])
        self.assertEqual(parsed.scorer, "precision_tiered_v4")

        defaulted = parser.parse_args([
            "--benchmark",
            str(Path(tempfile.gettempdir()) / "benchmark.json"),
            "--output",
            str(Path(tempfile.gettempdir()) / "report.json"),
        ])
        self.assertEqual(defaulted.scorer, BASELINE_SCORER_ID)

    def test_12_bank_account_fixture_fields_targets_and_truth(self):
        benchmark = load_benchmark(BENCHMARK)
        scenario = next(item for item in benchmark["scenarios"] if item["scenario_id"] == "bank_account")
        self.assertEqual(scenario["split"], "train")
        self.assertEqual(len(scenario["cases"]), 16)
        self.assertEqual(len(_csv_header(BANK_SOURCE)), 16)

        truth = _read_json(BANK_TRUTH)
        self.assertTrue(truth["_meta"]["synthetic"])
        self.assertTrue(truth["_meta"]["must_not_be_read_by_mapping_engine"])
        self.assertEqual(len(truth["mappings"]), 16)
        self.assertEqual(_fixture_counts({"scenarios": [scenario]})["expected_target_link_count"], 15)
        self.assertEqual(_fixture_counts({"scenarios": [scenario]})["single_target_case_count"], 13)
        self.assertEqual(_fixture_counts({"scenarios": [scenario]})["multi_target_case_count"], 1)
        self.assertEqual(_fixture_counts({"scenarios": [scenario]})["no_target_case_count"], 2)

        targets = _target_names(BANK_CONTRACT)
        source_fields = _csv_header(BANK_SOURCE)
        for case in scenario["cases"]:
            self.assertIn(case["source_field"], source_fields)
            for target in case["expected_targets"]:
                self.assertIn(target, targets)

        fixture_tags = {tag for case in scenario["cases"] for tag in case["difficulty_tags"]}
        self.assertTrue({
            "abbreviation",
            "value_pattern_needed",
            "target_table_context",
            "temporal",
            "boolean",
            "multi_target",
            "no_target",
            "single_target",
        }.issubset(fixture_tags))

    def test_13_bank_account_source_profiles_exercise_value_patterns(self):
        from src.core.mapping.profiler import profile_source_csv

        profiles, meta = profile_source_csv(BANK_SOURCE)
        by_name = {profile.name: profile for profile in profiles}
        self.assertEqual(meta["source_row_count"], 8)
        self.assertEqual(by_name["activation_date"].inferred_kind, "date")
        self.assertEqual(by_name["closure_date"].inferred_kind, "date")
        self.assertEqual(by_name["preferred_account"].inferred_kind, "boolean")
        self.assertEqual(by_name["settlement_ccy"].observed_max_length, 3)
        self.assertEqual(by_name["account_domicile"].observed_max_length, 2)

    def test_14_sales_order_fulfillment_holdout_fixture_is_registered_and_frozen(self):
        benchmark = load_benchmark(BENCHMARK)
        scenario = next(item for item in benchmark["scenarios"] if item["scenario_id"] == "sales_order_fulfillment")
        self.assertEqual(scenario["split"], "test")
        self.assertEqual(
            scenario["source_path"],
            "data/benchmarks/fixtures/sales_order_fulfillment/source_sales_order_fulfillment.csv",
        )
        self.assertEqual(
            scenario["contract_path"],
            "data/benchmarks/fixtures/sales_order_fulfillment/contract/datapackage.yaml",
        )
        self.assertEqual(
            scenario["data_root_path"],
            "data/benchmarks/fixtures/sales_order_fulfillment/target",
        )
        self.assertEqual(
            scenario["answer_source_path"],
            "data/benchmarks/fixtures/sales_order_fulfillment/ground_truth.json",
        )
        self.assertNotIn("mapping_report_path", scenario)
        self.assertNotIn("evaluation_report_path", scenario)

        counts = _fixture_counts({"scenarios": [scenario]})
        self.assertEqual(counts["case_count"], 22)
        self.assertEqual(counts["single_target_case_count"], 17)
        self.assertEqual(counts["multi_target_case_count"], 2)
        self.assertEqual(counts["no_target_case_count"], 3)
        self.assertEqual(counts["expected_target_link_count"], 22)
        self.assertEqual(_csv_header(SALES_ORDER_SOURCE), SALES_ORDER_SOURCE_HEADER)
        self.assertEqual(len(_csv_rows(SALES_ORDER_SOURCE)), 8)

        resources = _resource_fields(SALES_ORDER_CONTRACT)
        self.assertEqual(set(resources), set(SALES_ORDER_TARGET_FIELDS))
        self.assertEqual(resources, SALES_ORDER_TARGET_FIELDS)
        targets = _target_names(SALES_ORDER_CONTRACT)

        truth = _read_json(SALES_ORDER_TRUTH)
        self.assertEqual(truth["_meta"]["purpose"], "evaluation_only")
        self.assertTrue(truth["_meta"]["synthetic"])
        self.assertEqual(truth["_meta"]["scenario_id"], "sales_order_fulfillment")
        self.assertTrue(truth["_meta"]["multi_target"])
        self.assertTrue(truth["_meta"]["must_not_be_read_by_mapping_engine"])
        self.assertTrue(truth["_meta"]["frozen_before_first_evaluation"])

        mappings = truth["mappings"]
        self.assertEqual(len(mappings), len(scenario["cases"]))
        for case, mapping in zip(scenario["cases"], mappings):
            self.assertEqual(case["source_field"], mapping["source_field"])
            self.assertEqual(case["expected_targets"], mapping["expected_targets"])
            self.assertEqual(case["expected_no_target"], not mapping["expected_targets"])
            self.assertEqual(case["expected_no_target"], "no_target" in case["difficulty_tags"])
            self.assertEqual(len(case["expected_targets"]) > 1, "multi_target" in case["difficulty_tags"])
            self.assertEqual(len(case["expected_targets"]) == 1, "single_target" in case["difficulty_tags"])
            for target in case["expected_targets"]:
                self.assertIn(target, targets)

        fixture_tags = {tag for case in scenario["cases"] for tag in case["difficulty_tags"]}
        self.assertTrue({
            "single_target",
            "multi_target",
            "no_target",
            "cross_resource_key",
            "target_table_context",
            "semantic_only",
            "temporal",
            "numeric",
            "categorical",
            "value_pattern_needed",
        }.issubset(fixture_tags))

        lock = _read_json(SALES_ORDER_LOCK)
        self.assertEqual(lock["scenario_id"], "sales_order_fulfillment")
        self.assertEqual(lock["split"], "test")
        self.assertEqual(lock["purpose"], "independent_cross_domain_holdout")
        self.assertTrue(lock["frozen_before_first_evaluation"])
        self.assertEqual(lock["evaluation_state_at_freeze"], "not_run")
        self.assertTrue(lock["fixture_changes_after_first_evaluation_forbidden"])
        self.assertEqual(lock["scenario_counts"], {
            "case_count": 22,
            "single_target_case_count": 17,
            "multi_target_case_count": 2,
            "no_target_case_count": 3,
            "expected_target_link_count": 22,
        })
        self.assertEqual(lock["benchmark_expected_counts_after_registration"], {
            "scenario_count": 5,
            "case_count": 72,
            "single_target_case_count": 59,
            "multi_target_case_count": 5,
            "no_target_case_count": 8,
            "expected_target_link_count": 70,
        })
        self.assertEqual(lock["sha256_mode"], "raw_file_bytes_sha256")
        frozen_paths = {
            "contract/datapackage.yaml": SALES_ORDER_CONTRACT,
            "source_sales_order_fulfillment.csv": SALES_ORDER_SOURCE,
            "ground_truth.json": SALES_ORDER_TRUTH,
            "target/sales_order_header.csv": SALES_ORDER_TARGET / "sales_order_header.csv",
            "target/sales_order_line.csv": SALES_ORDER_TARGET / "sales_order_line.csv",
            "target/delivery_schedule.csv": SALES_ORDER_TARGET / "delivery_schedule.csv",
        }
        self.assertEqual(
            lock["frozen_file_sha256"],
            {key: _raw_sha256(path) for key, path in frozen_paths.items()},
        )

    def test_15_sales_order_fulfillment_profiler_and_generation_boundary(self):
        from src.core.mapping.profiler import profile_source_csv

        profiles, meta = profile_source_csv(SALES_ORDER_SOURCE)
        by_name = {profile.name: profile for profile in profiles}
        self.assertEqual(meta["source_row_count"], 8)
        self.assertEqual(meta["source_field_count"], 22)
        self.assertEqual(by_name["order_created_on"].inferred_kind, "date")
        self.assertEqual(by_name["requested_ship_on"].inferred_kind, "date")
        self.assertEqual(by_name["confirmed_ship_on"].inferred_kind, "date")
        self.assertEqual(by_name["legacy_line_item_no"].inferred_kind, "integer")
        self.assertEqual(by_name["ordered_qty"].inferred_kind, "integer")
        self.assertEqual(by_name["schedule_seq"].inferred_kind, "integer")
        self.assertEqual(by_name["confirmed_qty"].inferred_kind, "integer")
        self.assertEqual(by_name["net_unit_amount"].inferred_kind, "number")
        self.assertEqual(by_name["line_total_amount"].inferred_kind, "number")

        benchmark = load_benchmark(BENCHMARK)
        spec = next(item for item in benchmark_run_specs(benchmark) if item["scenario_id"] == "sales_order_fulfillment")
        self.assertEqual(set(spec), {"scenario_id", "split", "source_path", "contract_path", "data_root_path"})
        self.assertNotIn("cases", spec)
        self.assertNotIn("answer_source_path", spec)
        self.assertNotIn("case_id", spec)
        self.assertNotIn("expected_targets", spec)

    def test_16_precision_tiered_v4_formal_artifact_contract(self):
        self.assertTrue(FORMAL_V4_EVALUATION.exists())
        raw = FORMAL_V4_EVALUATION.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))

        text = raw.decode("utf-8")
        report = json.loads(text)
        self.assertEqual(
            "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json",
            FORMAL_V4_EVALUATION.relative_to(PROJECT_ROOT).as_posix(),
        )
        self.assertIn(
            FORMAL_V4_EVALUATION.relative_to(PROJECT_ROOT).as_posix(),
            FORMAL_ARTIFACTS,
        )

        self.assertEqual(report["_meta"]["component"], "schema_matching_benchmark_evaluation")
        self.assertEqual(report["_meta"]["benchmark_id"], "schema_matching_v1")
        self.assertEqual(report["_meta"]["scorer_variant"], "precision_tiered_v4")
        self.assertEqual(report["_meta"]["scorer_id"], "precision_tiered_v4")
        self.assertEqual(report["_meta"]["feature_version"], "precision_tiered_interaction_v1")
        self.assertFalse(report["_meta"]["ground_truth_used_for_candidate_generation"])
        self.assertTrue(report["_meta"]["ground_truth_used_for_evaluation"])

        self.assertEqual(report["overall"], {
            "scenario_count": 5,
            "case_count": 72,
            "single_target_case_count": 59,
            "multi_target_case_count": 5,
            "no_target_case_count": 8,
            "expected_target_link_count": 70,
            "single_target_top1_accuracy": 0.9153,
            "target_link_recall_at_1": 0.8286,
            "target_link_recall_at_3": 0.9714,
            "target_link_mrr": 0.8929,
            "no_target_accuracy": 0.875,
            "multi_target_full_coverage_at_3": 1.0,
        })
        self.assertEqual(
            {item["scenario_id"]: item["split"] for item in report["by_scenario"]},
            {
                "generic_customer": "train",
                "supplier_reference": "validation",
                "erpnext_item_price": "test",
                "bank_account": "train",
                "sales_order_fulfillment": "test",
            },
        )

        body = {key: value for key, value in report.items() if key != "_run_info"}
        self.assertEqual(
            report["_run_info"]["content_sha256"],
            canonical_json_content_sha256(body),
        )
        self.assertNotRegex(text, r"([A-Za-z]:\\\\|/tmp/|AppData\\\\Local\\\\Temp|C:\\\\Users\\\\)")
        self.assertNotIn("multi_target_exact_field_v1", text)
        self.assertNotIn("multi_target_recommendation", text)
        self.assertNotIn("rejected", text.lower())
        self.assertFalse(any(re.search(r"(timestamp|uuid|machine|hostname|absolute_path|temp_path)", key, re.I) for key in _json_keys(report)))


def _formal_candidate_reports() -> dict[str, dict]:
    return {
        "generic_customer": _read_json(PROJECT_ROOT / "data" / "synthetic" / "generic_customer_contract_mapping.json"),
        "supplier_reference": _read_json(PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_contract_mapping.json"),
        "erpnext_item_price": _read_json(PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"),
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return next(csv.reader(handle))


def _csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        return list(reader)


def _target_names(contract_path: Path) -> set[str]:
    doc = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return {
        f"{resource['name']}.{field['name']}"
        for resource in doc["resources"]
        for field in resource["schema"]["fields"]
    }


def _resource_fields(contract_path: Path) -> dict[str, set[str]]:
    doc = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return {
        resource["name"]: {field["name"] for field in resource["schema"]["fields"]}
        for resource in doc["resources"]
    }


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_counts(benchmark: dict) -> dict[str, int]:
    counts = {
        "scenario_count": len(benchmark["scenarios"]),
        "case_count": 0,
        "single_target_case_count": 0,
        "multi_target_case_count": 0,
        "no_target_case_count": 0,
        "expected_target_link_count": 0,
    }
    for scenario in benchmark["scenarios"]:
        for case in scenario["cases"]:
            target_count = len(case["expected_targets"])
            counts["case_count"] += 1
            counts["single_target_case_count"] += int(target_count == 1)
            counts["multi_target_case_count"] += int(target_count > 1)
            counts["no_target_case_count"] += int(target_count == 0)
            counts["expected_target_link_count"] += target_count
    return counts


def _json_keys(value, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        keys = []
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.append(path)
            keys.extend(_json_keys(child, path))
        return keys
    if isinstance(value, list):
        keys = []
        for child in value:
            keys.extend(_json_keys(child, f"{prefix}[]"))
        return keys
    return []


def _temp_fixture(document: dict) -> Path:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    path = Path(handle.name)
    with handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


@contextmanager
def _contract_fixture() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT))
    try:
        data = root / "data"
        data.mkdir()
        source = root / "source.csv"
        source.write_text("legacy_client_id,client_name\nC-1,Example Customer\n", encoding="utf-8")
        (data / "customer.csv").write_text("customer_id,customer_name\n,\n", encoding="utf-8")
        contract = {
            "profile": "tabular-data-package",
            "name": "unit-contract",
            "version": "1.0.0",
            "carveops": {
                "contract_id": "unit-contract-v1",
                "adapter": "unit",
                "domain": "unit",
                "synthetic": True,
                "authoritative": False,
            },
            "resources": [
                {
                    "profile": "tabular-data-resource",
                    "name": "customer",
                    "path": "customer.csv",
                    "schema": {
                        "fields": [
                            {
                                "name": "customer_id",
                                "type": "string",
                                "carveops": {
                                    "description": "Customer identifier",
                                    "aliases": ["legacy_client_id"],
                                    "semantic_type": "identifier",
                                },
                            },
                            {
                                "name": "customer_name",
                                "type": "string",
                                "carveops": {
                                    "description": "Customer name",
                                    "aliases": ["client_name"],
                                    "semantic_type": "organization_name",
                                },
                            },
                        ]
                    },
                }
            ],
        }
        contract_path = root / "datapackage.yaml"
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        fixture = {
            "_meta": {
                "benchmark_id": "schema_matching_v1",
                "synthetic_demo": True,
                "ground_truth_runtime_boundary": "evaluation_only",
            },
            "scenarios": [
                {
                    "scenario_id": "unit_generation",
                    "split": "train",
                    "source_path": source.relative_to(PROJECT_ROOT).as_posix(),
                    "contract_path": contract_path.relative_to(PROJECT_ROOT).as_posix(),
                    "data_root_path": data.relative_to(PROJECT_ROOT).as_posix(),
                    "answer_source_path": BENCHMARK.relative_to(PROJECT_ROOT).as_posix(),
                    "cases": [_case("unit_generation__legacy_client_id", "legacy_client_id", ["customer.customer_id"])],
                }
            ],
        }
        path = root / "benchmark.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        yield path
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
