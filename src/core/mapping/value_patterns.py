from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.mapping.models import ContractTargetField, SourceFieldProfile


VALUE_PATTERN_FEATURE_VERSION = "value_pattern_v1"
VALUE_PATTERN_SUPPORT_THRESHOLD = 0.8
VALUE_PATTERN_STRENGTHS = {
    "contract_enum_match": 1.0,
    "contract_regex_match": 1.0,
    "iban_shape": 1.0,
    "bic_shape": 1.0,
    "uppercase_alpha_code_3": 0.85,
    "uppercase_alpha_code_2": 0.85,
    "date_type": 0.8,
    "boolean_type": 0.8,
}

IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$")
BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
UPPERCASE_ALPHA_CODE_3_RE = re.compile(r"^[A-Z]{3}$")
UPPERCASE_ALPHA_CODE_2_RE = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True)
class ValuePatternEvidence:
    score: float
    support: float | None
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "support": self.support,
            "evidence": list(self.evidence),
        }


def value_pattern_evidence(
    profile: SourceFieldProfile,
    target: ContractTargetField,
) -> ValuePatternEvidence:
    evidence_support: dict[str, float | None] = {}
    samples = _non_empty_samples(profile)

    if target.enum_values:
        support = _support(samples, lambda value: value in target.enum_values)
        if _supported(support):
            evidence_support["contract_enum_match"] = support
    if target.pattern:
        pattern = re.compile(target.pattern)
        support = _support(samples, lambda value: bool(pattern.fullmatch(value)))
        if _supported(support):
            evidence_support["contract_regex_match"] = support

    shape_rules = (
        ("iban_shape", IBAN_RE, target.semantic_type == "iban"),
        ("bic_shape", BIC_RE, target.semantic_type == "bic"),
        ("uppercase_alpha_code_3", UPPERCASE_ALPHA_CODE_3_RE, target.semantic_type == "currency_code"),
        ("uppercase_alpha_code_2", UPPERCASE_ALPHA_CODE_2_RE, target.semantic_type == "country_code"),
    )
    for name, regex, compatible in shape_rules:
        if compatible:
            support = _support(samples, lambda value, expression=regex: bool(expression.fullmatch(value)))
            if _supported(support):
                evidence_support[name] = support

    if profile.inferred_kind in {"date", "datetime"} and target.frictionless_type in {"date", "datetime"}:
        evidence_support["date_type"] = None
    if profile.inferred_kind == "boolean" and target.frictionless_type == "boolean":
        evidence_support["boolean_type"] = None

    if not evidence_support:
        return ValuePatternEvidence(score=0.0, support=0.0, evidence=())

    evidence = tuple(sorted(evidence_support))
    score = max(VALUE_PATTERN_STRENGTHS[item] for item in evidence)
    support_values = [value for value in evidence_support.values() if value is not None]
    support = max(support_values) if support_values else None
    return ValuePatternEvidence(score=score, support=support, evidence=evidence)


def _non_empty_samples(profile: SourceFieldProfile) -> tuple[str, ...]:
    return tuple(value.strip() for value in profile.samples if value.strip())


def _support(samples: tuple[str, ...], predicate) -> float | None:
    if not samples:
        return None
    return round(sum(1 for value in samples if predicate(value)) / len(samples), 4)


def _supported(support: float | None) -> bool:
    return support is not None and support >= VALUE_PATTERN_SUPPORT_THRESHOLD
