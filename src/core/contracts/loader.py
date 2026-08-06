from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from frictionless import Package

from src.core.hashing import normalized_text_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REQUIRED_TOP_LEVEL_KEYS = ("profile", "name", "version", "resources")
REQUIRED_EXTENSION_KEYS = (
    "contract_id",
    "adapter",
    "domain",
    "synthetic",
    "authoritative",
)


class ContractLoadError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class LoadedMigrationContract:
    contract_id: str
    name: str
    title: str
    version: str
    adapter: str
    domain: str
    synthetic: bool
    authoritative: bool
    descriptor_path: Path
    descriptor_sha256: str
    data_root: Path
    resource_names: tuple[str, ...]
    frictionless_package: Package
    descriptor: dict[str, Any]


def _is_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://", "ftp://", "s3://"))


def _ensure_under_project(path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ContractLoadError(
            "path_outside_project",
            f"{label} must be inside the project root",
            {"path": str(path)},
        ) from exc
    return resolved


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _scan_for_disallowed_strings(value: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_for_disallowed_strings(item, (*trail, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_disallowed_strings(item, (*trail, str(index)))
        return
    if not isinstance(value, str):
        return
    if _is_url(value):
        raise ContractLoadError(
            "remote_source_not_allowed",
            "Contract descriptors must not reference remote data sources",
            {"field": ".".join(trail), "value": value},
        )
    if Path(value).is_absolute():
        raise ContractLoadError(
            "absolute_path_not_allowed",
            "Contract descriptors must use project-relative resource paths",
            {"field": ".".join(trail), "value": value},
        )
    lowered = value.lower()
    if "authorization" in lowered or "api_key" in lowered or "sk-" in value:
        raise ContractLoadError(
            "secret_like_value",
            "Contract descriptors must not contain credentials or credential hints",
            {"field": ".".join(trail)},
        )


def _load_descriptor(descriptor_path: Path) -> dict[str, Any]:
    try:
        content = descriptor_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractLoadError(
            "descriptor_missing",
            "Contract descriptor does not exist",
            {"path": str(descriptor_path)},
        ) from exc
    try:
        descriptor = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ContractLoadError(
            "descriptor_parse_error",
            "Contract descriptor is not valid YAML",
            {"path": _project_relative(descriptor_path)},
        ) from exc
    if not isinstance(descriptor, dict):
        raise ContractLoadError(
            "descriptor_schema_error",
            "Contract descriptor must be a mapping",
            {"path": _project_relative(descriptor_path)},
        )
    return descriptor


def _validate_metadata(descriptor: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in descriptor]
    carveops = descriptor.get("carveops")
    if not isinstance(carveops, dict):
        missing.append("carveops")
        carveops = {}
    missing.extend(
        f"carveops.{key}" for key in REQUIRED_EXTENSION_KEYS if key not in carveops
    )
    if missing:
        raise ContractLoadError(
            "missing_required_metadata",
            "Contract descriptor is missing required metadata",
            {"missing": missing},
        )
    resources = descriptor.get("resources")
    if not isinstance(resources, list) or not resources:
        raise ContractLoadError(
            "missing_resources",
            "Contract descriptor must define at least one resource",
            {},
        )
    for resource in resources:
        if not isinstance(resource, dict):
            raise ContractLoadError(
                "resource_schema_error",
                "Each contract resource must be a mapping",
                {"resource": resource},
            )
        for key in ("name", "path", "schema"):
            if key not in resource:
                raise ContractLoadError(
                    "resource_schema_error",
                    "Contract resource is missing required metadata",
                    {"missing": key, "resource": resource.get("name")},
                )
    return carveops


def _validate_resource_paths(
    descriptor: dict[str, Any],
    data_root: Path,
) -> tuple[str, ...]:
    resource_names: list[str] = []
    for resource in descriptor["resources"]:
        name = str(resource["name"])
        resource_names.append(name)
        resource_path = resource["path"]
        if not isinstance(resource_path, str):
            raise ContractLoadError(
                "resource_path_error",
                "Resource path must be a string",
                {"resource": name},
            )
        if _is_url(resource_path):
            raise ContractLoadError(
                "remote_source_not_allowed",
                "Resource path must not be remote",
                {"resource": name, "path": resource_path},
            )
        candidate = Path(resource_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ContractLoadError(
                "path_escape_not_allowed",
                "Resource path must stay under the declared data root",
                {"resource": name, "path": resource_path},
            )
        resolved = (data_root / candidate).resolve()
        try:
            resolved.relative_to(data_root)
        except ValueError as exc:
            raise ContractLoadError(
                "path_escape_not_allowed",
                "Resource path must stay under the declared data root",
                {"resource": name, "path": resource_path},
            ) from exc
        if not resolved.exists():
            raise ContractLoadError(
                "resource_missing",
                "Contract resource file does not exist",
                {"resource": name, "path": _project_relative(resolved)},
            )
    return tuple(resource_names)


def load_migration_contract(
    descriptor_path: Path,
    data_root: Path,
) -> LoadedMigrationContract:
    descriptor_abs = _ensure_under_project(Path(descriptor_path), "descriptor_path")
    data_root_abs = _ensure_under_project(Path(data_root), "data_root")
    if not data_root_abs.exists():
        raise ContractLoadError(
            "data_root_missing",
            "Data root does not exist",
            {"path": str(data_root)},
        )
    descriptor = _load_descriptor(descriptor_abs)
    _scan_for_disallowed_strings(descriptor)
    carveops = _validate_metadata(descriptor)
    resource_names = _validate_resource_paths(descriptor, data_root_abs)
    descriptor_sha256 = normalized_text_sha256(descriptor_abs)
    package = Package(descriptor, basepath=str(data_root_abs))
    return LoadedMigrationContract(
        contract_id=str(carveops["contract_id"]),
        name=str(descriptor["name"]),
        title=str(descriptor.get("title", descriptor["name"])),
        version=str(descriptor["version"]),
        adapter=str(carveops["adapter"]),
        domain=str(carveops["domain"]),
        synthetic=bool(carveops["synthetic"]),
        authoritative=bool(carveops["authoritative"]),
        descriptor_path=descriptor_abs,
        descriptor_sha256=descriptor_sha256,
        data_root=data_root_abs,
        resource_names=resource_names,
        frictionless_package=package,
        descriptor=descriptor,
    )
