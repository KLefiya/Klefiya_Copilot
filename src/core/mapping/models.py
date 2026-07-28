from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceFieldProfile:
    name: str
    inferred_kind: str
    row_count: int
    present_count: int
    missing_count: int
    missing_ratio: float
    distinct_count: int
    distinct_ratio: float
    observed_max_length: int
    samples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["samples"] = list(self.samples)
        return data


@dataclass(frozen=True)
class ContractTargetField:
    resource: str
    name: str
    qualified_name: str
    frictionless_type: str
    required: bool
    unique: bool
    primary_key: bool
    max_length: int | None
    enum_values: tuple[str, ...]
    pattern: str | None
    description: str
    aliases: tuple[str, ...]
    semantic_type: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["enum_values"] = list(self.enum_values)
        data["aliases"] = list(self.aliases)
        return data


@dataclass(frozen=True)
class MappingCandidate:
    target: str
    rank: int
    score: float
    semantic_score: float
    fuzzy_score: float
    alias_hit: bool
    alias_source: str | None
    lexical_overlap: float
    type_gate: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class MappingSuggestion:
    source_field: str
    status: str
    recommendation: str | None
    confidence: float
    band: str
    mapping_basis: str
    source_profile: SourceFieldProfile
    top_candidates: tuple[MappingCandidate, ...]
    review_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.source_field,
            "status": self.status,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "band": self.band,
            "mapping_basis": self.mapping_basis,
            "source_profile": self.source_profile.to_dict(),
            "review_reasons": list(self.review_reasons),
            "top_candidates": [candidate.to_dict() for candidate in self.top_candidates],
        }
