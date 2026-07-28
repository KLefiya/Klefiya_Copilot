from __future__ import annotations

from typing import Any

from src.core.contracts.loader import LoadedMigrationContract
from src.core.mapping.models import ContractTargetField


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _primary_keys(schema: dict[str, Any]) -> set[str]:
    value = schema.get("primaryKey", [])
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def build_target_field_index(contract: LoadedMigrationContract) -> list[ContractTargetField]:
    fields: list[ContractTargetField] = []
    for resource in contract.descriptor.get("resources", []):
        resource_name = str(resource.get("name", ""))
        schema = resource.get("schema", {})
        primary_keys = _primary_keys(schema)
        for field in schema.get("fields", []):
            constraints = field.get("constraints", {})
            extension = field.get("carveops") or {}
            name = str(field.get("name", ""))
            fields.append(
                ContractTargetField(
                    resource=resource_name,
                    name=name,
                    qualified_name=f"{resource_name}.{name}",
                    frictionless_type=str(field.get("type", "")),
                    required=bool(constraints.get("required", False)),
                    unique=bool(constraints.get("unique", False)),
                    primary_key=name in primary_keys,
                    max_length=constraints.get("maxLength"),
                    enum_values=_as_tuple(constraints.get("enum")),
                    pattern=constraints.get("pattern"),
                    description=str(extension.get("description", "")),
                    aliases=_as_tuple(extension.get("aliases")),
                    semantic_type=str(extension.get("semantic_type", "")),
                )
            )
    return fields
