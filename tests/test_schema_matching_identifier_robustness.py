from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import evaluate_schema_matching_identifier_robustness as robustness


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "schema_matching_identifier_robustness_v1.json"
FORMAL_DIR = PROJECT_ROOT / "data" / "synthetic"
EXPECTED_CATEGORIES = {
    "formatting",
    "identifier_alias",
    "abbreviation",
    "ambiguous_generic_name",
    "no_target",
}


def _candidate(target: str, rank: int, score: float = 0.9) -> dict:
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


def _mapping(source_field: str, recommendation: str | None, targets: list[str], status: str | None = None) -> dict:
    return {
        "source_field": source_field,
        "status": status or ("suggested" if recommendation else "no_confident_target"),
        "recommendation": recommendation,
        "confidence": 0.9 if recommendation else 0.0,
        "band": "high" if recommendation else "low",
        "mapping_basis": "unit",
        "top_candidates": [_candidate(target, index + 1) for index, target in enumerate(targets)],
        "review_reasons": [],
    }


def _report(mappings: list[dict], feature_version: str | None = "unit_feature") -> dict:
    return {
        "_meta": {
            "embedding_model": robustness.MODEL_NAME,
            "feature_version": feature_version,
            "ground_truth_used": False,
        },
        "mappings": mappings,
    }


class IdentifierRobustnessFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = robustness.load_fixture(FIXTURE)
        cls.cases = [case for scenario in cls.fixture["scenarios"] for case in scenario["cases"]]

    def test_01_fixture_schema_and_non_formal_boundary(self) -> None:
        meta = self.fixture["_meta"]
        self.assertEqual(meta["fixture_id"], "schema_matching_identifier_robustness_v1")
        self.assertTrue(meta["diagnostic"])
        self.assertFalse(meta["formal_benchmark_artifact"])
        self.assertEqual(meta["ground_truth_runtime_boundary"], "evaluation_only")
        self.assertFalse(meta["ground_truth_used_for_candidate_generation"])
        self.assertTrue(meta["ground_truth_used_for_evaluation"])
        self.assertTrue(meta["synthetic_data"])

    def test_02_case_ids_are_unique(self) -> None:
        case_ids = [case["case_id"] for case in self.cases]
        self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_03_fixture_has_at_least_36_cases(self) -> None:
        self.assertGreaterEqual(len(self.cases), 36)

    def test_04_three_contracts_are_covered(self) -> None:
        self.assertEqual(set(robustness.contract_counts(self.fixture)), {
            "generic-customer",
            "supplier-reference",
            "erpnext-item-price",
        })

    def test_05_category_counts_cover_required_categories(self) -> None:
        counts = robustness.category_counts(self.fixture)
        self.assertEqual(set(counts), EXPECTED_CATEGORIES)
        for category in EXPECTED_CATEGORIES:
            self.assertGreater(counts[category], 0)
        self.assertGreaterEqual(counts["identifier_alias"], 9)
        self.assertGreaterEqual(counts["abbreviation"], 7)

    def test_06_expected_targets_belong_to_contract_allowlists(self) -> None:
        for scenario in self.fixture["scenarios"]:
            allowlist = set(robustness.contract_target_fields(scenario["contract_id"]))
            for case in scenario["cases"]:
                self.assertTrue(set(case["expected_targets"]).issubset(allowlist), case["case_id"])

    def test_07_no_target_cases_have_empty_expected_targets(self) -> None:
        no_target_fields = set()
        for case in self.cases:
            if case["category"] == "no_target":
                self.assertTrue(case["expected_no_target"])
                self.assertEqual(case["expected_targets"], [])
                no_target_fields.add(case["source_field"])
        self.assertTrue({"legacy_status", "migration_comment", "internal_flag", "source_row_number"}.issubset(no_target_fields))

    def test_08_fixture_contains_only_synthetic_values(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").lower()
        forbidden = ["gmail.com", "hotmail.com", "outlook.com", "microsoft", "amazon", "john ", "jane "]
        for marker in forbidden:
            self.assertNotIn(marker, text)
        self.assertIn("example.invalid", text)

    def test_09_source_csv_builder_preserves_case_order(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            scenario = self.fixture["scenarios"][0]
            path = robustness.build_source_csv(scenario, Path(tmp))
            header = path.read_text(encoding="utf-8").splitlines()[0].split(",")
        self.assertEqual(header, [case["source_field"] for case in scenario["cases"]])


class IdentifierRobustnessEvaluatorTests(unittest.TestCase):
    def test_10_runtime_dispatch_receives_no_ground_truth(self) -> None:
        contract = object()
        source_path = Path("source.csv")
        with patch("src.core.mapping.runtime.suggest_runtime_contract_mappings", return_value={"mappings": []}) as dispatch:
            robustness.suggest_runtime_dispatch(contract, source_path, "baseline")
        _, args, kwargs = dispatch.mock_calls[0]
        self.assertEqual(args, (contract, source_path))
        self.assertEqual(kwargs["scorer_id"], "baseline")
        self.assertNotIn("expected_targets", kwargs)
        self.assertNotIn("category", kwargs)
        self.assertNotIn("fixture", kwargs)

    def test_11_fake_backend_metrics(self) -> None:
        fixture = {
            "_meta": {
                "fixture_id": "schema_matching_identifier_robustness_v1",
                "diagnostic": True,
                "formal_benchmark_artifact": False,
            },
            "scenarios": [
                {
                    "scenario_id": "unit",
                    "contract_id": "generic-customer",
                    "cases": [
                        {
                            "case_id": "top1",
                            "source_field": "client_number",
                            "category": "identifier_alias",
                            "expected_targets": ["customer.customer_id"],
                            "expected_no_target": False,
                            "sample_values": ["C1"],
                        },
                        {
                            "case_id": "rank2",
                            "source_field": "mail_addr",
                            "category": "abbreviation",
                            "expected_targets": ["customer.email"],
                            "expected_no_target": False,
                            "sample_values": ["a@example.invalid"],
                        },
                        {
                            "case_id": "no_target",
                            "source_field": "legacy_status",
                            "category": "no_target",
                            "expected_targets": [],
                            "expected_no_target": True,
                            "sample_values": ["ready"],
                        },
                    ],
                }
            ],
        }
        reports = {
            "unit": _report([
                _mapping("client_number", "customer.customer_id", ["customer.customer_id"]),
                _mapping("mail_addr", "customer.customer_name", ["customer.customer_name", "customer.email"]),
                _mapping("legacy_status", None, ["customer.customer_id"]),
            ])
        }
        result = robustness.evaluate_scorer(fixture, reports, "baseline")
        self.assertEqual(result["overall"]["case_count"], 3)
        self.assertEqual(result["overall"]["top1_accuracy"], 0.5)
        self.assertEqual(result["overall"]["recall_at_3"], 1.0)
        self.assertEqual(result["overall"]["mrr"], 0.75)
        self.assertEqual(result["overall"]["no_target_accuracy"], 1.0)
        self.assertEqual(result["overall"]["suggested_precision"], 0.5)

    def test_12_recall_mrr_and_no_target_false_positive(self) -> None:
        fixture = copy.deepcopy(robustness.load_fixture(FIXTURE))
        scenario = {
            "scenario_id": "unit",
            "contract_id": "generic-customer",
            "cases": [
                {
                    "case_id": "missing",
                    "source_field": "customer_no",
                    "category": "identifier_alias",
                    "expected_targets": ["customer.customer_id"],
                    "expected_no_target": False,
                    "sample_values": ["C1"],
                },
                {
                    "case_id": "false_positive",
                    "source_field": "legacy_status",
                    "category": "no_target",
                    "expected_targets": [],
                    "expected_no_target": True,
                    "sample_values": ["ready"],
                },
            ],
        }
        fixture["scenarios"] = [scenario]
        reports = {
            "unit": _report([
                _mapping("customer_no", "customer.email", ["customer.email", "customer.phone"]),
                _mapping("legacy_status", "customer.customer_id", ["customer.customer_id"]),
            ])
        }
        result = robustness.evaluate_scorer(fixture, reports, "baseline")
        self.assertEqual(result["overall"]["recall_at_3"], 0.0)
        self.assertEqual(result["overall"]["mrr"], 0.0)
        self.assertEqual(result["overall"]["no_target_accuracy"], 0.0)
        self.assertEqual(result["error_taxonomy_counts"]["correct_target_below_top3"], 1)
        self.assertEqual(result["error_taxonomy_counts"]["false_positive_no_target"], 1)

    def test_13_error_taxonomy_keys_are_stable(self) -> None:
        result = robustness.evaluate_scorer(
            {
                "_meta": {"fixture_id": "schema_matching_identifier_robustness_v1"},
                "scenarios": [
                    {
                        "scenario_id": "unit",
                        "contract_id": "generic-customer",
                        "cases": [
                            {
                                "case_id": "over",
                                "source_field": "customer_no",
                                "category": "identifier_alias",
                                "expected_targets": ["customer.customer_id"],
                                "expected_no_target": False,
                                "sample_values": ["C1"],
                            }
                        ],
                    }
                ],
            },
            {"unit": _report([_mapping("customer_no", None, ["customer.customer_id"])])},
            "baseline",
        )
        self.assertEqual(set(result["error_taxonomy_counts"]), set(robustness.TAXONOMY))
        self.assertEqual(result["error_taxonomy_counts"]["over_abstention"], 1)

    def test_14_diagnostic_report_is_deterministically_ordered(self) -> None:
        fixture = {
            "_meta": {"fixture_id": "schema_matching_identifier_robustness_v1"},
            "scenarios": [
                {
                    "scenario_id": "unit",
                    "contract_id": "generic-customer",
                    "cases": [
                        {
                            "case_id": "a",
                            "source_field": "client_number",
                            "category": "identifier_alias",
                            "expected_targets": ["customer.customer_id"],
                            "expected_no_target": False,
                            "sample_values": ["C1"],
                        }
                    ],
                }
            ],
        }
        reports = {"unit": _report([_mapping("client_number", "customer.customer_id", ["customer.customer_id"])])}
        first = robustness.build_diagnostic_report(
            fixture,
            fixture_path=FIXTURE,
            fixture_sha="abc",
            reports_by_scorer={"baseline": reports, "precision_tiered_v4": reports, "precision_tiered_v5": reports},
        )
        second = robustness.build_diagnostic_report(
            fixture,
            fixture_path=FIXTURE,
            fixture_sha="abc",
            reports_by_scorer={"baseline": reports, "precision_tiered_v4": reports, "precision_tiered_v5": reports},
        )
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_15_default_output_is_outside_formal_artifacts(self) -> None:
        output = robustness.default_output_path("a" * 64).resolve()
        self.assertNotEqual(output.parent, FORMAL_DIR.resolve())
        self.assertNotIn("data/synthetic", output.as_posix().replace("\\", "/"))

    def test_16_import_does_not_load_sentence_transformer_model(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "evaluate_schema_matching_identifier_robustness.py").read_text(encoding="utf-8")
        self.assertNotIn("SentenceTransformer(", source)
        module = importlib.import_module("scripts.evaluate_schema_matching_identifier_robustness")
        self.assertIs(module, robustness)

    def test_17_protocol_locked_cli_is_not_called(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "evaluate_schema_matching_identifier_robustness.py").read_text(encoding="utf-8")
        self.assertNotIn("suggest_contract_mappings.py", source)
        self.assertNotIn("protocol_lock", source)

    def test_18_run_runtime_reports_uses_runtime_dispatch(self) -> None:
        fixture = {
            "_meta": {
                "fixture_id": "schema_matching_identifier_robustness_v1",
                "diagnostic": True,
                "formal_benchmark_artifact": False,
            },
            "scenarios": [
                {
                    "scenario_id": "unit",
                    "contract_id": "generic-customer",
                    "cases": [
                        {
                            "case_id": "a",
                            "source_field": "client_number",
                            "category": "identifier_alias",
                            "expected_targets": ["customer.customer_id"],
                            "expected_no_target": False,
                            "sample_values": ["C1", "C2"],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            with patch("scripts.evaluate_schema_matching_identifier_robustness.suggest_runtime_dispatch", return_value=_report([])) as dispatch:
                reports = robustness.run_runtime_reports(fixture, "baseline", workspace=Path(tmp))
        self.assertEqual(set(reports), {"unit"})
        dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
