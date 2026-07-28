from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Protocol

from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer

from src.core.mapping.models import ContractTargetField, MappingCandidate, MappingSuggestion, SourceFieldProfile


DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
HIGH_CONFIDENCE = 0.70
MEDIUM_CONFIDENCE = 0.45
NO_MATCH_THRESHOLD = 0.40
ALIAS_CONFIDENCE_FLOOR = 0.90
TYPE_GATE_FLOOR = 0.60
TOP_N_CANDIDATES = 3
SYNONYMS = {
    "client": "customer",
    "vendor": "supplier",
    "telephone": "phone",
    "vat": "tax",
    "nation": "country",
    "terms": "payment terms",
    "condition": "payment terms",
    "recon": "reconciliation",
    "gl": "general ledger",
    "legal": "company",
    "entity": "company",
    "iban": "bank account",
    "currency": "currency code",
    "partner": "category",
}


class EmbeddingBackend(Protocol):
    def encode(self, sentences, normalize_embeddings: bool = True, show_progress_bar: bool = False):
        ...


class MappingModelError(Exception):
    pass


def load_embedding_backend(model_name: str = DEFAULT_MODEL_NAME) -> EmbeddingBackend:
    try:
        return SentenceTransformer(model_name, local_files_only=True)
    except Exception as exc:  # pragma: no cover - exact upstream errors vary
        raise MappingModelError(
            f"Embedding model is not available locally: {model_name}"
        ) from exc


def normalize_text(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", " ", value).lower().strip()
    return re.sub(r"\s+", " ", text)


def tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in normalize_text(value).split() if token)


def expanded_text(value: str) -> str:
    result: list[str] = []
    for token in tokens(value):
        result.append(token)
        if token in SYNONYMS:
            result.extend(tokens(SYNONYMS[token]))
    return " ".join(result)


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


def _as_vector(value) -> list[float]:
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def _embedding_scores(backend: EmbeddingBackend, source_text: str, target_texts: list[str]) -> list[float]:
    encoded = backend.encode(
        [source_text, *target_texts],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    source_vector = _as_vector(encoded[0])
    return [_cosine(source_vector, _as_vector(vector)) for vector in encoded[1:]]


def _lexical_overlap(source_text: str, target: ContractTargetField) -> float:
    source_tokens = set(tokens(expanded_text(source_text)))
    target_tokens = set(tokens(expanded_text(f"{target.name} {target.description} {target.semantic_type}")))
    if not source_tokens or not target_tokens:
        return 0.0
    return len(source_tokens & target_tokens) / len(source_tokens | target_tokens)


def _alias_hit(source_name: str, target: ContractTargetField) -> tuple[bool, str | None]:
    normalized_source = normalize_text(source_name)
    for alias in target.aliases:
        normalized_alias = normalize_text(alias)
        if normalized_source == normalized_alias:
            return True, alias
        if fuzz.token_sort_ratio(normalized_source, normalized_alias) >= 96:
            return True, alias
    return False, None


def _fuzzy_score(source_name: str, target: ContractTargetField) -> float:
    candidates = [
        target.name,
        target.qualified_name,
        target.semantic_type,
        target.description,
    ]
    return max(fuzz.token_sort_ratio(expanded_text(source_name), expanded_text(item)) for item in candidates) / 100


def _type_gate(profile: SourceFieldProfile, target: ContractTargetField) -> tuple[float, tuple[str, ...]]:
    warnings: list[str] = []
    kind = profile.inferred_kind
    semantic = target.semantic_type
    target_type = target.frictionless_type
    gate = 1.0
    if kind in {"date", "datetime"} and target_type not in {"date", "datetime"}:
        gate = 0.82
        warnings.append("weak_date_to_text_compatibility")
    if kind == "boolean" and semantic not in {"category_code"}:
        gate = min(gate, TYPE_GATE_FLOOR)
        warnings.append("boolean_source_for_text_target")
    if kind in {"integer", "number"} and semantic in {"organization_name", "email", "phone"}:
        gate = min(gate, TYPE_GATE_FLOOR)
        warnings.append("numeric_source_for_textual_target")
    if semantic == "identifier" and profile.distinct_ratio >= 0.9 and profile.observed_max_length <= 24:
        gate = max(gate, 1.0)
    if semantic == "identifier" and profile.distinct_ratio < 0.5 and profile.observed_max_length > 40:
        gate = min(gate, 0.75)
        warnings.append("low_distinct_long_text_for_identifier")
    return gate, tuple(warnings)


def _target_text(target: ContractTargetField) -> str:
    return expanded_text(f"{target.name} {target.description} {target.semantic_type}")


def _basis(best: MappingCandidate) -> str:
    if best.alias_hit:
        return "alias"
    if best.semantic_score >= 0.68 and best.fuzzy_score < 0.65:
        return "semantic"
    if best.fuzzy_score >= 0.72 and best.semantic_score < 0.68:
        return "fuzzy"
    if best.score < NO_MATCH_THRESHOLD:
        return "none"
    return "mixed"


def _status(best: MappingCandidate, mapping_basis: str) -> tuple[str, str, tuple[str, ...]]:
    reasons: list[str] = []
    if best.score < NO_MATCH_THRESHOLD:
        return "no_confident_target", "low", ("best_score_below_threshold",)
    no_anchor = not best.alias_hit and best.lexical_overlap == 0
    if best.score >= MEDIUM_CONFIDENCE and no_anchor and best.type_gate <= TYPE_GATE_FLOOR:
        return "possible_false_friend", "medium", ("no_lexical_anchor", "type_gate_warning")
    if best.score >= HIGH_CONFIDENCE:
        return "suggested", "high", tuple(reasons)
    if best.score >= MEDIUM_CONFIDENCE:
        reasons.append("medium_confidence")
        if no_anchor:
            reasons.append("no_lexical_anchor")
        reasons.extend(best.warnings)
        return "needs_review", "medium", tuple(dict.fromkeys(reasons))
    return "no_confident_target", "low", ("best_score_below_threshold",)


def score_source_field(
    profile: SourceFieldProfile,
    targets: list[ContractTargetField],
    backend: EmbeddingBackend,
) -> MappingSuggestion:
    source_text = expanded_text(profile.name)
    semantic_scores = _embedding_scores(backend, source_text, [_target_text(target) for target in targets])
    candidates: list[MappingCandidate] = []
    for target, semantic_score in zip(targets, semantic_scores):
        alias_hit, alias_source = _alias_hit(profile.name, target)
        fuzzy_score = _fuzzy_score(profile.name, target)
        lexical_overlap = _lexical_overlap(profile.name, target)
        type_gate, warnings = _type_gate(profile, target)
        blended = (0.58 * semantic_score) + (0.27 * fuzzy_score) + (0.15 * lexical_overlap)
        if alias_hit:
            blended = max(blended, ALIAS_CONFIDENCE_FLOOR)
        score = round(min(1.0, max(0.0, blended * type_gate)), 4)
        candidates.append(
            MappingCandidate(
                target=target.qualified_name,
                rank=0,
                score=score,
                semantic_score=round(semantic_score, 4),
                fuzzy_score=round(fuzzy_score, 4),
                alias_hit=alias_hit,
                alias_source=alias_source,
                lexical_overlap=round(lexical_overlap, 4),
                type_gate=round(type_gate, 4),
                warnings=warnings,
            )
        )
    ranked = sorted(candidates, key=lambda item: (-item.score, item.target))
    ranked = [replace(candidate, rank=index + 1) for index, candidate in enumerate(ranked)]
    top = tuple(ranked[:TOP_N_CANDIDATES])
    best = top[0]
    basis = _basis(best)
    status, band, reasons = _status(best, basis)
    recommendation = best.target if status != "no_confident_target" else None
    confidence = best.score if recommendation else 0.0
    return MappingSuggestion(
        source_field=profile.name,
        status=status,
        recommendation=recommendation,
        confidence=confidence,
        band=band,
        mapping_basis=basis,
        source_profile=profile,
        top_candidates=top,
        review_reasons=reasons,
    )
