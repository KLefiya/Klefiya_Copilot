from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from src.core.mapping.models import ContractTargetField, SourceFieldProfile


CANONICAL_EVIDENCE_SOURCE_SHA256 = "68d2d4f35b8f59000788fd44f86a442dd7ef9df8676ac40575314b10c2062c22"
FEATURE_VERSION = "precision_tiered_interaction_v1"

ROUTING_IDENTIFIER = "routing_identifier"
INSTITUTION = "institution"
GENERIC_IDENTIFIER = "generic_identifier"

# Immutable internal configuration; evidence output below remains plain JSON data.
CONCEPT_TOKENS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    ROUTING_IDENTIFIER: ("clearing", "routing", "sort", "transit"),
    INSTITUTION: ("bank", "institution"),
    GENERIC_IDENTIFIER: ("id", "identifier", "key", "ref", "reference"),
})

CONCEPT_WEIGHTS: Mapping[str, float] = MappingProxyType({
    ROUTING_IDENTIFIER: 1.0,
    INSTITUTION: 0.8,
    GENERIC_IDENTIFIER: 0.5,
})


@dataclass(frozen=True)
class InteractionChannel:
    interaction_id: str
    tier: Literal["diagnostic", "supportive"]
    bonus_weight: float
    may_displace_v3_top1: bool
    required_source_concepts: tuple[str, ...]
    required_target_concepts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


INTERACTION_CHANNELS: tuple[InteractionChannel, ...] = (
    InteractionChannel(
        interaction_id="routing_to_routing",
        tier="diagnostic",
        bonus_weight=0.10,
        may_displace_v3_top1=True,
        required_source_concepts=(ROUTING_IDENTIFIER,),
        required_target_concepts=(ROUTING_IDENTIFIER,),
    ),
    InteractionChannel(
        interaction_id="institutional_key_support",
        tier="supportive",
        bonus_weight=0.10,
        may_displace_v3_top1=False,
        required_source_concepts=(GENERIC_IDENTIFIER, INSTITUTION),
        required_target_concepts=(GENERIC_IDENTIFIER, INSTITUTION),
    ),
)


def tokenize(value: str) -> tuple[str, ...]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    text = re.sub(
        r"([A-Za-z])([0-9])|([0-9])([A-Za-z])",
        lambda match: " ".join(group for group in match.groups() if group),
        text,
    )
    return tuple(sorted(token for token in re.split(r"[\.\s_\-]+", text.lower()) if token))


def source_concept_evidence(profile: SourceFieldProfile) -> dict[str, Any]:
    return concept_evidence_from_texts((profile.name,))


def target_concept_evidence(target: ContractTargetField) -> dict[str, Any]:
    return concept_evidence_from_texts((target.name, *target.aliases))


def concept_evidence_from_texts(texts: tuple[str, ...]) -> dict[str, Any]:
    tokens = tuple(sorted({token for text in texts for token in tokenize(text)}))
    matched = {
        concept: CONCEPT_WEIGHTS[concept]
        for concept, concept_tokens in sorted(CONCEPT_TOKENS.items())
        if set(tokens) & set(concept_tokens)
    }
    used_tokens = {
        token
        for concept in matched
        for token in CONCEPT_TOKENS[concept]
        if token in tokens
    }
    return {
        "tokens": list(tokens),
        "matched_concepts": matched,
        "unmatched_tokens": [token for token in tokens if token not in used_tokens],
    }


def active_interactions(
    source_concepts: set[str],
    target_concepts: set[str],
    *,
    v3_score: float,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for channel in INTERACTION_CHANNELS:
        required_source = set(channel.required_source_concepts)
        required_target = set(channel.required_target_concepts)
        if required_source.issubset(source_concepts) and required_target.issubset(target_concepts):
            matched = sorted(required_source | required_target)
            bonus = channel.bonus_weight * (1.0 - v3_score)
            evidence.append(
                {
                    "interaction_id": channel.interaction_id,
                    "tier": channel.tier,
                    "source_concepts": sorted(source_concepts),
                    "target_concepts": sorted(target_concepts),
                    "matched_required_concepts": matched,
                    "bonus_weight": channel.bonus_weight,
                    "bonus": round(bonus, 6),
                    "may_displace_v3_top1": channel.may_displace_v3_top1,
                }
            )
    return sorted(evidence, key=lambda item: str(item["interaction_id"]))


def metadata() -> dict[str, Any]:
    return {
        "feature_version": FEATURE_VERSION,
        "canonical_evidence_source_sha256": CANONICAL_EVIDENCE_SOURCE_SHA256,
        "global_weighted_jaccard": False,
        "balanced_lexicon": False,
        "confidence_guard": False,
        "ownership": False,
        "value_distribution": False,
        "concept_tokens": {key: list(value) for key, value in sorted(CONCEPT_TOKENS.items())},
        "interaction_channels": [channel.to_dict() for channel in INTERACTION_CHANNELS],
        "ground_truth_used_for_concept_extraction": False,
        "ground_truth_used_for_interaction_activation": False,
        "ground_truth_used_for_tier_decision": False,
        "ground_truth_used_for_scoring": False,
    }
