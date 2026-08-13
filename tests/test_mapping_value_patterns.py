from __future__ import annotations

import inspect
import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.mapping.models import ContractTargetField, SourceFieldProfile
from src.core.mapping.scorer import score_source_field
from src.core.mapping.scorer_v2 import VALUE_PATTERN_BONUS_WEIGHT, score_all_candidates_v2, score_source_field_v2
from src.core.mapping.value_patterns import value_pattern_evidence


class FakeEmbeddingBackend:
    def encode(self, sentences, normalize_embeddings=True, show_progress_bar=False):
        return [self._vector(text, normalize_embeddings) for text in sentences]

    def _vector(self, text: str, normalize_embeddings: bool):
        lowered = text.lower()
        features = ["account", "iban", "bic", "currency", "country", "date", "flag", "name"]
        vector = [1.0 if feature in lowered else 0.0 for feature in features]
        vector.append(0.0 if any(vector) else 1.0)
        if normalize_embeddings:
            norm = math.sqrt(sum(item * item for item in vector))
            vector = [item / norm for item in vector]
        return vector


def _profile(name: str, samples: tuple[str, ...], inferred_kind: str = "string") -> SourceFieldProfile:
    return SourceFieldProfile(
        name=name,
        inferred_kind=inferred_kind,
        row_count=len(samples),
        present_count=len(samples),
        missing_count=0,
        missing_ratio=0.0,
        distinct_count=len(set(samples)),
        distinct_ratio=1.0,
        observed_max_length=max((len(item) for item in samples), default=0),
        samples=samples,
    )


def _target(name: str, semantic_type: str, frictionless_type: str = "string") -> ContractTargetField:
    return ContractTargetField(
        resource="bank_account",
        name=name,
        qualified_name=f"bank_account.{name}",
        frictionless_type=frictionless_type,
        required=False,
        unique=False,
        primary_key=False,
        max_length=None,
        enum_values=(),
        pattern=None,
        description=f"Target {name}",
        aliases=(),
        semantic_type=semantic_type,
    )


class MappingValuePatternTests(unittest.TestCase):
    def test_01_iban_shape_detected(self):
        evidence = value_pattern_evidence(
            _profile("international_account", ("ZZ00TEST000000000001", "ZZ00TEST000000000002")),
            _target("iban", "iban"),
        )
        self.assertEqual(evidence.score, 1.0)
        self.assertEqual(evidence.support, 1.0)
        self.assertEqual(evidence.evidence, ("iban_shape",))

    def test_02_bic_8_and_11_character_detected(self):
        target = _target("bic", "bic")
        eight = value_pattern_evidence(_profile("swift", ("ABCDEF12", "GHIJKL34")), target)
        eleven = value_pattern_evidence(_profile("swift", ("ABCDEF12XXX", "GHIJKL34YYY")), target)
        self.assertEqual(eight.evidence, ("bic_shape",))
        self.assertEqual(eleven.evidence, ("bic_shape",))

    def test_03_uppercase_codes_match_currency_and_country(self):
        currency = value_pattern_evidence(_profile("ccy", ("USD", "EUR", "GBP")), _target("currency_code", "currency_code"))
        country = value_pattern_evidence(_profile("country", ("US", "DE", "GB")), _target("country_code", "country_code"))
        self.assertEqual(currency.score, 0.85)
        self.assertEqual(currency.evidence, ("uppercase_alpha_code_3",))
        self.assertEqual(country.score, 0.85)
        self.assertEqual(country.evidence, ("uppercase_alpha_code_2",))

    def test_04_date_and_boolean_kind_match_compatible_targets(self):
        date = value_pattern_evidence(_profile("start", ("2024-01-01",), "date"), _target("valid_from", "date", "date"))
        boolean = value_pattern_evidence(_profile("flag", ("yes", "no"), "boolean"), _target("primary_flag", "category_code", "boolean"))
        self.assertEqual(date.score, 0.8)
        self.assertEqual(date.evidence, ("date_type",))
        self.assertIsNone(date.support)
        self.assertEqual(boolean.score, 0.8)
        self.assertEqual(boolean.evidence, ("boolean_type",))

    def test_05_support_below_threshold_produces_no_evidence(self):
        evidence = value_pattern_evidence(
            _profile("mixed", ("USD", "EUR", "bad", "also_bad", "nope")),
            _target("currency_code", "currency_code"),
        )
        self.assertEqual(evidence.score, 0.0)
        self.assertEqual(evidence.support, 0.0)
        self.assertEqual(evidence.evidence, ())

    def test_06_evidence_applies_only_to_compatible_targets(self):
        iban_to_bic = value_pattern_evidence(
            _profile("international_account", ("ZZ00TEST000000000001",)),
            _target("bic", "bic"),
        )
        bic_to_identifier = value_pattern_evidence(
            _profile("swift", ("ABCDEF12XXX",)),
            _target("account_id", "identifier"),
        )
        self.assertEqual(iban_to_bic.score, 0.0)
        self.assertEqual(bic_to_identifier.score, 0.0)

    def test_07_ordinary_organization_names_produce_no_evidence(self):
        evidence = value_pattern_evidence(
            _profile("institution_name", ("Orchid Demo Holdings", "Harbor Sample Components")),
            _target("bank_name", "organization_name"),
        )
        self.assertEqual(evidence.score, 0.0)
        self.assertEqual(evidence.support, 0.0)

    def test_08_score_bonus_follows_registered_formula(self):
        profile = _profile("international_account", ("ZZ00TEST000000000001",))
        target = _target("iban", "iban")
        report = score_source_field_v2(profile, [target], FakeEmbeddingBackend())
        candidate = report["top_candidates"][0]
        expected = round(
            candidate["base_score"]
            + VALUE_PATTERN_BONUS_WEIGHT * candidate["value_pattern_score"] * (1 - candidate["base_score"]),
            4,
        )
        self.assertEqual(candidate["score"], expected)
        self.assertEqual(VALUE_PATTERN_BONUS_WEIGHT, 0.20)

    def test_09_score_never_exceeds_one(self):
        target = _target("iban", "iban")
        report = score_source_field_v2(_profile("iban", ("ZZ00TEST000000000001",)), [target], FakeEmbeddingBackend())
        self.assertLessEqual(report["top_candidates"][0]["score"], 1.0)

    def test_10_no_pattern_preserves_baseline_score(self):
        profile = _profile("institution_name", ("Orchid Demo Holdings",))
        target = _target("bank_name", "organization_name")
        baseline = score_source_field(profile, [target], FakeEmbeddingBackend()).to_dict()
        experimental = score_source_field_v2(profile, [target], FakeEmbeddingBackend())
        self.assertEqual(experimental["top_candidates"][0]["value_pattern_score"], 0.0)
        self.assertEqual(experimental["top_candidates"][0]["score"], baseline["top_candidates"][0]["score"])

    def test_11_ground_truth_is_not_an_accepted_argument(self):
        for function in (value_pattern_evidence, score_source_field_v2):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("ground_truth", parameters)
            self.assertNotIn("answer_source_path", parameters)

    def test_12_mixed_case_and_lowercase_codes_are_not_uppercase_evidence(self):
        currency_target = _target("currency_code", "currency_code")
        country_target = _target("country_code", "country_code")
        for samples in (("Nos", "Box", "Kg"), ("Usd", "Eur", "Gbp"), ("usd", "eur", "gbp"), ("Components", "Packaging", "Services")):
            evidence = value_pattern_evidence(_profile("ordinary_text", samples), currency_target)
            self.assertEqual(evidence.score, 0.0, samples)
            self.assertEqual(evidence.evidence, (), samples)
        country = value_pattern_evidence(_profile("country", ("us", "de", "gb")), country_target)
        self.assertEqual(country.score, 0.0)

    def test_13_uppercase_code_detection_still_accepts_true_codes(self):
        currency = value_pattern_evidence(_profile("ccy", ("USD", "EUR", "GBP")), _target("currency_code", "currency_code"))
        country = value_pattern_evidence(_profile("country", ("US", "DE", "GB")), _target("country_code", "country_code"))
        self.assertEqual(currency.evidence, ("uppercase_alpha_code_3",))
        self.assertEqual(country.evidence, ("uppercase_alpha_code_2",))

    def test_14_zero_evidence_candidate_exact_score_parity(self):
        profile = _profile("client_name", ("Orchid Demo Holdings", "Harbor Sample Components"))
        targets = [
            _target("customer_name", "organization_name"),
            _target("account_holder", "organization_name"),
            _target("free_text", "description"),
        ]
        baseline = score_source_field(profile, targets, FakeEmbeddingBackend()).to_dict()
        experimental = score_source_field_v2(profile, targets, FakeEmbeddingBackend())
        for base_candidate, v2_candidate in zip(baseline["top_candidates"], experimental["top_candidates"]):
            self.assertEqual(v2_candidate["value_pattern_score"], 0.0)
            self.assertEqual(v2_candidate["baseline_score"], base_candidate["score"])
            self.assertEqual(v2_candidate["score"], base_candidate["score"])
            self.assertEqual(v2_candidate["semantic_score"], base_candidate["semantic_score"])
            self.assertEqual(v2_candidate["fuzzy_score"], base_candidate["fuzzy_score"])
            self.assertEqual(v2_candidate["lexical_overlap"], base_candidate["lexical_overlap"])
            self.assertEqual(v2_candidate["type_gate"], base_candidate["type_gate"])
            self.assertEqual(v2_candidate["warnings"], base_candidate["warnings"])

    def test_15_zero_evidence_suggestion_exact_behavior_parity(self):
        profile = _profile("beneficiary_label", ("Orchid Demo Holdings", "Harbor Sample Components"))
        targets = [
            _target("account_holder", "organization_name"),
            _target("bank_name", "organization_name"),
            _target("primary_flag", "category_code", "boolean"),
        ]
        baseline = score_source_field(profile, targets, FakeEmbeddingBackend()).to_dict()
        experimental = score_source_field_v2(profile, targets, FakeEmbeddingBackend())
        self.assertEqual(_behavior_view(experimental), _behavior_view(baseline))
        self.assertTrue(all(candidate["value_pattern_score"] == 0.0 for candidate in experimental["top_candidates"]))

    def test_16_type_gate_applies_once_and_matches_baseline(self):
        profile = _profile("numeric_identifier", ("123", "456"), "integer")
        target = _target("email", "email")
        baseline = score_source_field(profile, [target], FakeEmbeddingBackend()).to_dict()["top_candidates"][0]
        experimental = score_source_field_v2(profile, [target], FakeEmbeddingBackend())["top_candidates"][0]
        self.assertLess(experimental["type_gate"], 1.0)
        self.assertEqual(experimental["value_pattern_score"], 0.0)
        self.assertEqual(experimental["baseline_score"], baseline["score"])
        self.assertEqual(experimental["score"], baseline["score"])

    def test_17_alias_floor_order_matches_baseline(self):
        profile = _profile("legacy_client_id", ("C-1", "C-2"))
        target = _target("customer_id", "identifier")
        object.__setattr__(target, "aliases", ("legacy_client_id",))
        baseline = score_source_field(profile, [target], FakeEmbeddingBackend()).to_dict()
        experimental = score_source_field_v2(profile, [target], FakeEmbeddingBackend())
        self.assertTrue(experimental["top_candidates"][0]["alias_hit"])
        self.assertEqual(experimental["top_candidates"][0]["baseline_score"], baseline["top_candidates"][0]["score"])
        self.assertEqual(_behavior_view(experimental), _behavior_view(baseline))

    def test_18_tie_break_order_matches_baseline(self):
        profile = _profile("unknown_label", ("Components", "Packaging", "Services"))
        targets = [
            _target("beta", "description"),
            _target("alpha", "description"),
        ]
        baseline = score_source_field(profile, targets, FakeEmbeddingBackend()).to_dict()
        experimental = score_source_field_v2(profile, targets, FakeEmbeddingBackend())
        self.assertEqual([item["target"] for item in experimental["top_candidates"]], [item["target"] for item in baseline["top_candidates"]])
        self.assertEqual(_behavior_view(experimental), _behavior_view(baseline))

    def test_19_all_candidates_prefix_matches_v2_top3_behavior(self):
        profile = _profile("client_name", ("Orchid Demo Holdings",))
        targets = [
            _target("account_holder", "organization_name"),
            _target("bank_name", "organization_name"),
            _target("primary_flag", "category_code", "boolean"),
            _target("free_text", "description"),
        ]
        top3_report = score_source_field_v2(profile, targets, FakeEmbeddingBackend())
        all_candidates = score_all_candidates_v2(profile, targets, FakeEmbeddingBackend())
        self.assertEqual(all_candidates[:3], top3_report["top_candidates"])
        self.assertEqual(_behavior_view(top3_report), _behavior_view(score_source_field_v2(profile, targets, FakeEmbeddingBackend())))


def _behavior_view(report: dict) -> dict:
    return {
        "status": report["status"],
        "recommendation": report["recommendation"],
        "confidence": report["confidence"],
        "band": report["band"],
        "mapping_basis": report["mapping_basis"],
        "review_reasons": report["review_reasons"],
        "top_candidates": [
            {
                "target": item["target"],
                "rank": item["rank"],
                "score": item["score"],
                "semantic_score": item["semantic_score"],
                "fuzzy_score": item["fuzzy_score"],
                "alias_hit": item["alias_hit"],
                "alias_source": item["alias_source"],
                "lexical_overlap": item["lexical_overlap"],
                "type_gate": item["type_gate"],
                "warnings": item["warnings"],
            }
            for item in report["top_candidates"]
        ],
    }


if __name__ == "__main__":
    unittest.main()
