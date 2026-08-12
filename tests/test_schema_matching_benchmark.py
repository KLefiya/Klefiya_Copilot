from __future__ import annotations

import csv
import json
import math
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.benchmark import (
    SchemaMatchingBenchmarkError,
    benchmark_run_specs,
    evaluate_benchmark,
    generate_candidate_reports,
    load_benchmark,
)


BENCHMARK = PROJECT_ROOT / "data" / "benchmarks" / "schema_matching_v1.json"


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
        report = evaluate_benchmark(benchmark, _formal_candidate_reports())
        self.assertEqual(report["overall"]["scenario_count"], 3)
        self.assertEqual(report["overall"]["case_count"], 34)
        self.assertEqual(report["overall"]["single_target_case_count"], 29)
        self.assertEqual(report["overall"]["multi_target_case_count"], 2)
        self.assertEqual(report["overall"]["no_target_case_count"], 3)
        self.assertEqual(report["overall"]["expected_target_link_count"], 33)

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

    def test_09_duplicate_scenario_split_rejected(self):
        fixture = _benchmark([_case("one", "legacy_client_id", ["customer.customer_id"])])
        fixture["scenarios"].append(_scenario([_case("two", "client_name", ["customer.customer_name"])], "other", "train"))
        with self.assertRaisesRegex(SchemaMatchingBenchmarkError, "Duplicate scenario split"):
            load_benchmark(_temp_fixture(fixture))

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


def _formal_candidate_reports() -> dict[str, dict]:
    return {
        "generic_customer": _read_json(PROJECT_ROOT / "data" / "synthetic" / "generic_customer_contract_mapping.json"),
        "supplier_reference": _read_json(PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_contract_mapping.json"),
        "erpnext_item_price": _read_json(PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"),
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
