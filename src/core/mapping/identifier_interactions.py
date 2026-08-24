from __future__ import annotations

import re
from typing import Any

from src.core.mapping.models import ContractTargetField, SourceFieldProfile


FEATURE_VERSION = "entity_identifier_interaction_v1"
INTERACTION_ID = "shared_entity_identifier"
TIER = "diagnostic"
IDENTIFIER_BONUS_WEIGHT = 0.32
MAX_IDENTIFIER_BONUS = 0.18

ENTITY_CONCEPTS = {
    "customer_entity": frozenset({"customer", "client"}),
    "supplier_entity": frozenset({"supplier", "vendor"}),
    "item_entity": frozenset({"item", "material", "product"}),
}
GENERIC_IDENTIFIER_TOKENS = frozenset({
    "id",
    "identifier",
    "number",
    "no",
    "num",
    "nr",
    "code",
    "sku",
    "key",
    "ref",
    "reference",
})


def tokenize_identifier(value: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    spaced = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", spaced)
    spaced = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", spaced)
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", spaced).lower()
    return tuple(token for token in normalized.split() if token)


def _concepts(tokens: tuple[str, ...], *, semantic_type: str = "") -> tuple[str, ...]:
    found: list[str] = []
    token_set = set(tokens)
    for concept, aliases in ENTITY_CONCEPTS.items():
        if token_set & aliases:
            found.append(concept)
    if token_set & GENERIC_IDENTIFIER_TOKENS:
        found.append("generic_identifier")
    if semantic_type == "identifier" and "generic_identifier" not in found:
        found.append("generic_identifier")
    return tuple(sorted(found))


def source_identifier_evidence(profile: SourceFieldProfile) -> dict[str, Any]:
    tokens = tokenize_identifier(profile.name)
    return {
        "source_tokens": list(tokens),
        "source_concepts": list(_concepts(tokens)),
    }


def target_identifier_evidence(target: ContractTargetField) -> dict[str, Any]:
    tokens = tokenize_identifier(" ".join((target.name, *target.aliases, target.semantic_type)))
    return {
        "target_tokens": list(tokens),
        "target_concepts": list(_concepts(tokens, semantic_type=target.semantic_type)),
    }


def identifier_interaction_evidence(
    profile: SourceFieldProfile,
    target: ContractTargetField,
    *,
    v4_score: float,
) -> list[dict[str, Any]]:
    source = source_identifier_evidence(profile)
    target_evidence = target_identifier_evidence(target)
    source_concepts = set(source["source_concepts"])
    target_concepts = set(target_evidence["target_concepts"])
    matched_entities = sorted(
        concept
        for concept in source_concepts & target_concepts
        if concept != "generic_identifier"
    )
    active = (
        bool(matched_entities)
        and "generic_identifier" in source_concepts
        and "generic_identifier" in target_concepts
    )
    if not active:
        return []
    raw_bonus = IDENTIFIER_BONUS_WEIGHT * (1.0 - v4_score)
    bonus = round(min(MAX_IDENTIFIER_BONUS, max(0.0, raw_bonus)), 6)
    return [{
        **source,
        **target_evidence,
        "matched_entity_concepts": matched_entities,
        "interaction_id": INTERACTION_ID,
        "tier": TIER,
        "bonus_weight": IDENTIFIER_BONUS_WEIGHT,
        "bonus": bonus,
        "may_displace_v4_top1": True,
    }]


def metadata() -> dict[str, Any]:
    return {
        "feature_version": FEATURE_VERSION,
        "interaction_id": INTERACTION_ID,
        "tier": TIER,
        "identifier_bonus_weight": IDENTIFIER_BONUS_WEIGHT,
        "max_identifier_bonus": MAX_IDENTIFIER_BONUS,
        "entity_concepts": {
            concept: sorted(tokens)
            for concept, tokens in sorted(ENTITY_CONCEPTS.items())
        },
        "generic_identifier_tokens": sorted(GENERIC_IDENTIFIER_TOKENS),
        "source_fields_used": ["SourceFieldProfile.name"],
        "target_fields_used": ["target.name", "target.aliases", "target.semantic_type"],
        "target_resource_used": False,
        "qualified_target_used": False,
    }
