from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.contracts.loader import LoadedMigrationContract
from src.core.mapping.engine import suggest_contract_mappings
from src.core.mapping.scorer import DEFAULT_MODEL_NAME, EmbeddingBackend
from src.core.mapping.scorer_v4 import SCORER_ID as PRECISION_TIERED_V4_SCORER_ID
from src.core.mapping.scorer_v4 import suggest_contract_mappings_v4
from src.core.mapping.scorer_v5 import SCORER_ID as PRECISION_TIERED_V5_SCORER_ID
from src.core.mapping.scorer_v5 import suggest_contract_mappings_v5
from src.core.mapping.target_index import build_target_field_index
from src.tools.data_profile import attach_run_info


BASELINE_SCORER_ID = "baseline"
SUPPORTED_RUNTIME_SCORERS = frozenset({BASELINE_SCORER_ID, PRECISION_TIERED_V4_SCORER_ID})
EXPERIMENTAL_RUNTIME_SCORERS = frozenset({PRECISION_TIERED_V5_SCORER_ID})


class RuntimeScorerError(ValueError):
    def __init__(self, scorer_id: str):
        super().__init__(f"Unsupported runtime scorer: {scorer_id}")
        self.code = "unknown_runtime_scorer"
        self.message = f"Unsupported runtime scorer: {scorer_id}"


def suggest_runtime_contract_mappings(
    contract: LoadedMigrationContract,
    source_path: Path,
    *,
    scorer_id: str = BASELINE_SCORER_ID,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    if scorer_id == BASELINE_SCORER_ID:
        return suggest_contract_mappings(
            contract,
            source_path,
            model_name=model_name,
            embedding_backend=embedding_backend,
        )
    if scorer_id == PRECISION_TIERED_V4_SCORER_ID:
        report = suggest_contract_mappings_v4(
            contract,
            source_path,
            model_name=model_name,
            embedding_backend=embedding_backend,
        )
        return _runtime_report_with_summary(contract, report)
    if scorer_id == PRECISION_TIERED_V5_SCORER_ID:
        report = suggest_contract_mappings_v5(
            contract,
            source_path,
            model_name=model_name,
            embedding_backend=embedding_backend,
        )
        return _runtime_report_with_summary(contract, report)
    raise RuntimeScorerError(scorer_id)


def _runtime_report_with_summary(contract: LoadedMigrationContract, report: dict[str, Any]) -> dict[str, Any]:
    mappings = deepcopy(report["mappings"])
    recommended_targets = {
        item["recommendation"]
        for item in mappings
        if item.get("recommendation") is not None
    }
    targets = build_target_field_index(contract)
    status_counts = {
        status: sum(1 for item in mappings if item["status"] == status)
        for status in (
            "suggested",
            "needs_review",
            "possible_false_friend",
            "no_confident_target",
        )
    }
    body = {
        "_meta": deepcopy(report["_meta"]),
        "summary": {
            **status_counts,
            "alias_based": sum(1 for item in mappings if item["mapping_basis"] == "alias"),
            "semantic_based": sum(1 for item in mappings if item["mapping_basis"] == "semantic"),
            "target_coverage": round(len(recommended_targets) / len(targets), 4) if targets else 0.0,
        },
        "mappings": mappings,
        "unmapped_target_fields": sorted(
            field.qualified_name
            for field in targets
            if field.qualified_name not in recommended_targets
        ),
    }
    return attach_run_info(body)
