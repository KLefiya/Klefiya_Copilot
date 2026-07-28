from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TransformationSpec:
    type: str
    value: str | None = None
    values: dict[str, str] | None = None
    on_missing: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class MappingDecision:
    source_field: str
    target: str | None
    decision: str
    reason: str | None
    transformation: TransformationSpec

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["transformation"] = self.transformation.to_dict()
        return data


@dataclass(frozen=True)
class LoadedMappingDecisions:
    version: str
    contract_id: str
    mapping_report_path: Path
    mapping_report_sha256: str
    source_path: Path
    source_sha256: str
    source_row_count: int
    source_field_count: int
    record_id_field: str
    decisions: tuple[MappingDecision, ...]
    decision_path: Path
    decision_sha256: str
    mapping_report: dict[str, Any]

    def approved(self) -> tuple[MappingDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "approved")

    def rejected(self) -> tuple[MappingDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "rejected")

    def deferred(self) -> tuple[MappingDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "deferred")


@dataclass(frozen=True)
class TargetResourceBuild:
    resource: str
    target_path: str
    field_names: tuple[str, ...]
    row_count: int
    rejected_row_count: int
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["field_names"] = list(self.field_names)
        return data


@dataclass(frozen=True)
class LineageEntry:
    source_path: str
    source_row_number: int
    source_record_id: str
    source_field: str
    source_value_sha256: str
    target_resource: str
    target_row_number: int
    target_field: str
    transformation_type: str
    decision_source: str
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}
