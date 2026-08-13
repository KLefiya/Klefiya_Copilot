from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.models import SourceFieldProfile
from src.core.mapping.resource_context import (
    ANCHOR_MARGIN_MIN,
    ANCHOR_SCORE_MIN,
    CONTEXT_WINDOW,
    RESOURCE_CONTEXT_BONUS_WEIGHT,
    RESOURCE_SUPPORT_MIN,
    anchor_from_candidates,
    resource_context_for_index,
)
from src.core.mapping.scorer_v3 import score_source_field_v3


def _profile(name: str) -> SourceFieldProfile:
    return SourceFieldProfile(
        name=name,
        inferred_kind="string",
        row_count=2,
        present_count=2,
        missing_count=0,
        missing_ratio=0.0,
        distinct_count=2,
        distinct_ratio=1.0,
        observed_max_length=16,
        samples=("sample one", "sample two"),
    )


def _candidate(target: str, score: float, *, rank: int = 1, type_gate: float = 1.0) -> dict:
    return {
        "target": target,
        "rank": rank,
        "score": score,
        "semantic_score": score,
        "fuzzy_score": score,
        "alias_hit": False,
        "alias_source": None,
        "lexical_overlap": 0.0,
        "type_gate": type_gate,
        "warnings": [],
        "value_pattern_score": 0.0,
        "value_pattern_support": 0.0,
        "value_pattern_evidence": [],
    }


def _ranked(resource: str, top1: float = 0.80, top2: float = 0.60) -> list[dict]:
    return [
        _candidate(f"{resource}.primary", top1, rank=1),
        _candidate("other.secondary", top2, rank=2),
    ]


class MappingResourceContextTests(unittest.TestCase):
    def test_01_registered_configuration_is_fixed(self):
        self.assertEqual(CONTEXT_WINDOW, 2)
        self.assertEqual(ANCHOR_SCORE_MIN, 0.45)
        self.assertEqual(ANCHOR_MARGIN_MIN, 0.05)
        self.assertEqual(RESOURCE_SUPPORT_MIN, 0.60)
        self.assertEqual(RESOURCE_CONTEXT_BONUS_WEIGHT, 0.10)

    def test_02_window_excludes_self_edges_and_wraparound(self):
        fields = ["a", "b", "c", "d", "e"]
        ranked = {field: _ranked("bank_account") for field in fields}
        context = resource_context_for_index(fields, ranked, 0)["bank_account"]
        neighbors = [item["neighbor_source_field"] for item in context["evidence"]]
        self.assertEqual(neighbors, ["b", "c"])
        self.assertNotIn("a", neighbors)
        self.assertNotIn("e", neighbors)

    def test_03_distance_weights_are_one_and_half(self):
        fields = ["a", "b", "c"]
        ranked = {field: _ranked("bank_account") for field in fields}
        context = resource_context_for_index(fields, ranked, 0)["bank_account"]
        weights = {item["distance"]: item["distance_weight"] for item in context["evidence"]}
        self.assertEqual(weights[1], 1.0)
        self.assertEqual(weights[2], 0.5)

    def test_04_low_score_and_low_margin_do_not_become_anchors(self):
        self.assertIsNone(anchor_from_candidates("low_score", _ranked("bank_account", top1=0.44, top2=0.10)))
        self.assertIsNone(anchor_from_candidates("low_margin", _ranked("bank_account", top1=0.80, top2=0.76)))

    def test_05_requires_at_least_two_valid_neighbor_anchors(self):
        fields = ["a", "b", "c"]
        ranked = {
            "a": _ranked("bank_account"),
            "b": _ranked("bank_account"),
            "c": _ranked("bank_account", top1=0.40, top2=0.10),
        }
        self.assertEqual(resource_context_for_index(fields, ranked, 0), {})

    def test_06_support_below_threshold_does_not_activate(self):
        fields = ["a", "b", "c", "d"]
        ranked = {
            "a": _ranked("bank_account"),
            "b": _ranked("bank_account"),
            "c": _ranked("bank_branch"),
            "d": _ranked("bank_branch"),
        }
        self.assertNotIn("bank_account", resource_context_for_index(fields, ranked, 1))

    def test_07_support_equal_threshold_activates(self):
        fields = ["a", "b", "c", "d"]
        ranked = {
            "a": _ranked("bank_account"),
            "b": _ranked("bank_branch"),
            "c": _ranked("bank_branch"),
            "d": _ranked("bank_account"),
        }
        context = resource_context_for_index(fields, ranked, 1)
        self.assertEqual(context["bank_account"]["support"], 0.6)

    def test_08_context_boosts_matching_resource_without_penalty(self):
        suggestion = score_source_field_v3(
            _profile("branch_hint"),
            [_candidate("bank_account.account_id", 0.70), _candidate("bank_branch.branch_id", 0.69)],
            {"bank_branch": {"support": 1.0, "evidence": []}},
        )
        self.assertEqual(suggestion["top_candidates"][0]["target"], "bank_branch.branch_id")
        self.assertEqual(suggestion["top_candidates"][1]["v2_score"], 0.70)
        self.assertEqual(suggestion["top_candidates"][1]["score"], 0.70)

    def test_09_type_gate_participates_once_in_context_formula(self):
        suggestion = score_source_field_v3(
            _profile("branch_hint"),
            [_candidate("bank_branch.branch_id", 0.50, type_gate=0.60)],
            {"bank_branch": {"support": 1.0, "evidence": []}},
        )
        expected = round(0.50 + 0.10 * 1.0 * 0.60 * (1 - 0.50), 4)
        self.assertEqual(suggestion["top_candidates"][0]["score"], expected)

    def test_10_score_never_exceeds_one(self):
        suggestion = score_source_field_v3(
            _profile("branch_hint"),
            [_candidate("bank_branch.branch_id", 0.99)],
            {"bank_branch": {"support": 1.0, "evidence": []}},
        )
        self.assertLessEqual(suggestion["top_candidates"][0]["score"], 1.0)

    def test_11_zero_context_candidate_and_suggestion_exact_v2_parity(self):
        candidates = [_candidate("bank_account.account_id", 0.72), _candidate("bank_branch.branch_id", 0.50, rank=2)]
        suggestion = score_source_field_v3(_profile("lonely_field"), candidates, {})
        self.assertEqual(suggestion["recommendation"], "bank_account.account_id")
        self.assertEqual(suggestion["confidence"], 0.72)
        self.assertEqual([item["score"] for item in suggestion["top_candidates"]], [0.72, 0.50])
        self.assertTrue(all(item["resource_context_score"] == 0.0 for item in suggestion["top_candidates"]))

    def test_12_rerank_happens_before_top3_truncation(self):
        candidates = [
            _candidate("other.field", 0.70),
            _candidate("bank_account.account_id", 0.69, rank=2),
            _candidate("bank_branch.branch_id", 0.68, rank=3),
            _candidate("item_price.amount", 0.67, rank=4),
        ]
        suggestion = score_source_field_v3(_profile("price_hint"), candidates, {"item_price": {"support": 1.0, "evidence": []}})
        self.assertIn("item_price.amount", [item["target"] for item in suggestion["top_candidates"]])

    def test_13_deterministic_tie_break_uses_target_name(self):
        candidates = [_candidate("z.field", 0.50), _candidate("a.field", 0.50, rank=2)]
        suggestion = score_source_field_v3(_profile("tie"), candidates, {})
        self.assertEqual([item["target"] for item in suggestion["top_candidates"]], ["a.field", "z.field"])

    def test_14_ground_truth_is_not_an_accepted_argument(self):
        for function in (resource_context_for_index, score_source_field_v3):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("ground_truth", parameters)
            self.assertNotIn("expected_targets", parameters)
            self.assertNotIn("case_id", parameters)

    def test_15_resource_labels_are_generic_not_bank_specific(self):
        examples = (
            ("bank_account", "account_id"),
            ("bank_branch", "branch_id"),
            ("item", "item_code"),
            ("item_price", "price_list_rate"),
        )
        for resource, field in examples:
            suggestion = score_source_field_v3(
                _profile(f"{resource}_hint"),
                [_candidate(f"{resource}.{field}", 0.50)],
                {resource: {"support": 1.0, "evidence": []}},
            )
            self.assertEqual(suggestion["top_candidates"][0]["resource_context_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
