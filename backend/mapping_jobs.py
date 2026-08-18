from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.contracts.loader import PROJECT_ROOT, ContractLoadError, LoadedMigrationContract, load_migration_contract
from src.core.hashing import raw_file_sha256


router = APIRouter(prefix="/api/mapping", tags=["mapping-jobs"])

MAX_FILENAME_LENGTH = 128
MAX_CSV_BYTES = 1024 * 1024
MAX_DATA_ROWS = 10_000
MAX_COLUMNS = 200
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RUNTIME_ROOT = PROJECT_ROOT / "data" / "runtime" / "mapping_jobs"
JOB_LOCK = threading.Lock()
SUPPORTED_RUNTIME_SCORERS = frozenset({"baseline", "precision_tiered_v4"})


@dataclass(frozen=True)
class ContractSpec:
    registry_id: str
    title: str
    contract_path: Path
    data_root: Path


CONTRACTS: dict[str, ContractSpec] = {
    "generic-customer": ContractSpec(
        registry_id="generic-customer",
        title="Generic Customer Migration Contract",
        contract_path=PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml",
        data_root=PROJECT_ROOT / "data" / "examples" / "generic_customer",
    ),
    "supplier-reference": ContractSpec(
        registry_id="supplier-reference",
        title="SAP Supplier Reference Migration Contract",
        contract_path=PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml",
        data_root=PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference",
    ),
    "erpnext-item-price": ContractSpec(
        registry_id="erpnext-item-price",
        title="ERPNext Item and Item Price Reference Contract",
        contract_path=PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml",
        data_root=PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price",
    ),
}


class CreateMappingJobPayload(BaseModel):
    contract_id: str
    filename: str
    csv_text: str
    scorer: Literal["baseline", "precision_tiered_v4"]


def suggest_runtime_contract_mappings(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from src.core.mapping.runtime import suggest_runtime_contract_mappings as dispatch

    return dispatch(*args, **kwargs)


def _project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _http_error(status_code: int, error: str, message: str, details: dict[str, Any] | None = None) -> HTTPException:
    body: dict[str, Any] = {"error": error, "message": message}
    if details:
        body["details"] = details
    return HTTPException(status_code=status_code, detail=body)


def _contract_spec_or_404(contract_id: str) -> ContractSpec:
    spec = CONTRACTS.get(contract_id)
    if spec is None:
        raise _http_error(404, "unknown_mapping_contract", f"Unknown mapping contract `{contract_id}`.")
    return spec


def _load_contract(spec: ContractSpec) -> LoadedMigrationContract:
    try:
        return load_migration_contract(spec.contract_path, spec.data_root)
    except ContractLoadError as exc:
        raise _http_error(500, "mapping_contract_unavailable", "Registered mapping contract is unavailable.") from exc


def _target_field_count(contract: LoadedMigrationContract) -> int:
    count = 0
    for resource in contract.descriptor.get("resources", []):
        if not isinstance(resource, dict):
            continue
        schema = resource.get("schema", {})
        if not isinstance(schema, dict):
            continue
        fields = schema.get("fields", [])
        if isinstance(fields, list):
            count += sum(1 for field in fields if isinstance(field, dict))
    return count


def _contract_summary(spec: ContractSpec) -> dict[str, Any]:
    contract = _load_contract(spec)
    return {
        "contract_id": spec.registry_id,
        "title": spec.title,
        "domain": contract.domain,
        "version": contract.version,
        "target_resource_count": len(contract.resource_names),
        "target_field_count": _target_field_count(contract),
        "supported_scorers": sorted(SUPPORTED_RUNTIME_SCORERS),
    }


def _validate_filename(filename: str) -> str:
    if len(filename) > MAX_FILENAME_LENGTH:
        raise _http_error(422, "invalid_mapping_filename", "Filename is too long.")
    if not filename or filename in {".", ".."}:
        raise _http_error(422, "invalid_mapping_filename", "Filename must be a single CSV filename.")
    if "\x00" in filename or "/" in filename or "\\" in filename or ".." in filename:
        raise _http_error(422, "invalid_mapping_filename", "Filename must not contain path components.")
    path = Path(filename)
    if path.name != filename or path.suffix.lower() != ".csv":
        raise _http_error(422, "invalid_mapping_filename", "Filename must end with .csv.")
    return path.name


def _validate_csv_text(csv_text: str) -> tuple[int, int]:
    if not csv_text:
        raise _http_error(422, "empty_mapping_csv", "CSV text must not be empty.")
    try:
        csv_bytes = csv_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _http_error(422, "invalid_mapping_csv_encoding", "CSV text must be UTF-8 encodable.") from exc
    if len(csv_bytes) > MAX_CSV_BYTES:
        raise _http_error(413, "mapping_csv_too_large", "CSV text exceeds the 1 MiB limit.")
    if csv_text.lstrip().lower().startswith(("http://", "https://", "ftp://", "s3://")):
        raise _http_error(422, "remote_mapping_source_not_allowed", "CSV text must not be a URL.")

    try:
        rows = list(csv.reader(StringIO(csv_text)))
    except csv.Error as exc:
        raise _http_error(422, "invalid_mapping_csv", "CSV text is not parseable CSV.") from exc
    if not rows or not rows[0]:
        raise _http_error(422, "empty_mapping_csv", "CSV must contain a header.")
    header = rows[0]
    if len(header) > MAX_COLUMNS:
        raise _http_error(413, "mapping_csv_too_many_columns", "CSV exceeds the 200 column limit.")
    if len(set(header)) != len(header):
        raise _http_error(422, "duplicate_mapping_csv_headers", "CSV header contains duplicate columns.")
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise _http_error(422, "ragged_mapping_csv", "CSV rows must have a consistent column count.")
    data_rows = max(0, len(rows) - 1)
    if data_rows > MAX_DATA_ROWS:
        raise _http_error(413, "mapping_csv_too_many_rows", "CSV exceeds the 10000 data row limit.")
    return data_rows, len(header)


def _runtime_root() -> Path:
    return RUNTIME_ROOT.resolve()


def _ensure_under_runtime(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(_runtime_root())
    except ValueError as exc:
        raise _http_error(500, "mapping_job_path_escape", "Mapping job path must stay inside the runtime root.") from exc
    return resolved


def _new_job_dir() -> tuple[str, Path]:
    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        job_id = uuid4().hex
        job_dir = _ensure_under_runtime(root / job_id)
        try:
            job_dir.mkdir()
        except FileExistsError:
            continue
        return job_id, job_dir
    raise _http_error(500, "mapping_job_id_collision", "Could not allocate a unique mapping job id.")


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    target = _ensure_under_runtime(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(content)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _write_mapping_report_atomic(report: dict[str, Any], path: Path) -> None:
    from src.core.mapping.engine import write_mapping_report

    target = _ensure_under_runtime(path)
    tmp = target.parent / f".{target.name}.tmp"
    try:
        write_mapping_report(report, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _job_dir(job_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise _http_error(422, "invalid_mapping_job_id", "Mapping job id must be a lowercase 32-character hex string.")
    return _ensure_under_runtime(_runtime_root() / job_id)


def _safe_source_profile(profile: Any) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    safe = {}
    for key in (
        "name",
        "inferred_kind",
        "missing_ratio",
        "distinct_ratio",
        "observed_max_length",
        "observed_min_length",
        "observed_mean_length",
    ):
        if key in profile:
            safe[key] = profile[key]
    return safe


def _safe_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "target",
        "rank",
        "score",
        "semantic_score",
        "fuzzy_score",
        "alias_hit",
        "lexical_overlap",
        "type_gate",
        "value_pattern_evidence",
        "resource_context_evidence",
        "activated_interactions",
        "interaction_evidence",
        "diagnostic_bonus",
        "supportive_bonus",
        "top1_selection_reason",
        "warnings",
    )
    return {key: deepcopy_json(candidate[key]) for key in allowed if key in candidate}


def deepcopy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _safe_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        "source_field": mapping.get("source_field"),
        "status": mapping.get("status"),
        "recommendation": mapping.get("recommendation"),
        "confidence": mapping.get("confidence"),
        "band": mapping.get("band"),
        "mapping_basis": mapping.get("mapping_basis"),
        "review_reasons": deepcopy_json(mapping.get("review_reasons", [])),
        "top_candidates": [
            _safe_candidate(candidate)
            for candidate in mapping.get("top_candidates", [])
            if isinstance(candidate, dict)
        ][:3],
    }
    safe_profile = _safe_source_profile(mapping.get("source_profile"))
    if safe_profile is not None:
        cleaned["source_profile"] = safe_profile
    return cleaned


def _safe_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {})
    return {
        key: summary.get(key)
        for key in (
            "suggested",
            "needs_review",
            "possible_false_friend",
            "no_confident_target",
            "target_coverage",
        )
        if key in summary
    }


def _job_response(job: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "job": deepcopy_json(job),
        "summary": _safe_summary(report),
        "mappings": [
            _safe_mapping(mapping)
            for mapping in report.get("mappings", [])
            if isinstance(mapping, dict)
        ],
    }


def _is_runtime_scorer_error(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "unknown_runtime_scorer" or exc.__class__.__name__ == "RuntimeScorerError"


def _is_mapping_model_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "MappingModelError"


def _is_source_profile_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "SourceProfileError"


def _load_job_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _http_error(500, "malformed_mapping_job", "Mapping job metadata is not readable JSON.") from exc
    if not isinstance(value, dict):
        raise _http_error(500, "malformed_mapping_job", "Mapping job metadata must be a JSON object.")
    return value


def _load_mapping_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _http_error(500, "malformed_mapping_report", "Mapping report is not readable JSON.") from exc
    if not isinstance(value, dict):
        raise _http_error(500, "malformed_mapping_report", "Mapping report must be a JSON object.")
    return value


def _job_metadata(
    *,
    job_id: str,
    original_filename: str,
    contract_registry_id: str,
    contract: LoadedMigrationContract,
    scorer: str,
    source_path: Path,
    source_row_count: int,
    source_field_count: int,
    mapping_report_path: Path,
    mapping_content_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": "carveops.mapping_job",
        "version": "1.0.0",
        "job_id": job_id,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "original_filename": original_filename,
        "contract_registry_id": contract_registry_id,
        "contract": {
            "contract_id": contract.contract_id,
            "title": contract.title,
            "domain": contract.domain,
            "version": contract.version,
            "contract_sha256": contract.descriptor_sha256,
            "target_resource_count": len(contract.resource_names),
            "target_field_count": _target_field_count(contract),
        },
        "scorer": scorer,
        "source": {
            "path": _project_relative(source_path),
            "sha256": raw_file_sha256(source_path),
            "hash_mode": "raw_file_bytes_sha256",
            "row_count": source_row_count,
            "field_count": source_field_count,
        },
        "mapping_report": {
            "path": _project_relative(mapping_report_path),
            "content_sha256": mapping_content_sha256,
        },
    }


@router.get("/contracts")
def list_contracts() -> dict[str, Any]:
    return {"contracts": [_contract_summary(spec) for spec in CONTRACTS.values()]}


@router.post("/jobs", status_code=201)
def create_mapping_job(payload: CreateMappingJobPayload) -> dict[str, Any]:
    filename = _validate_filename(payload.filename)
    csv_row_count, csv_field_count = _validate_csv_text(payload.csv_text)
    spec = _contract_spec_or_404(payload.contract_id)
    if payload.scorer not in SUPPORTED_RUNTIME_SCORERS:
        raise _http_error(422, "unknown_runtime_scorer", f"Unsupported runtime scorer `{payload.scorer}`.")
    if not JOB_LOCK.acquire(blocking=False):
        raise _http_error(409, "mapping_job_in_progress", "A mapping job is already running in this process.")
    job_id = ""
    job_dir: Path | None = None
    try:
        contract = _load_contract(spec)
        job_id, job_dir = _new_job_dir()
        source_path = _ensure_under_runtime(job_dir / "source.csv")
        mapping_report_path = _ensure_under_runtime(job_dir / "mapping_report.json")
        job_json_path = _ensure_under_runtime(job_dir / "job.json")
        _write_bytes_atomic(source_path, payload.csv_text.encode("utf-8"))
        try:
            report = suggest_runtime_contract_mappings(
                contract,
                source_path,
                scorer_id=payload.scorer,
            )
        except Exception as exc:
            if _is_source_profile_error(exc):
                raise _http_error(
                    422,
                    str(getattr(exc, "code", "invalid_source_profile")),
                    str(getattr(exc, "message", str(exc))),
                ) from exc
            if _is_runtime_scorer_error(exc):
                raise _http_error(
                    422,
                    "unknown_runtime_scorer",
                    getattr(exc, "message", str(exc)),
                ) from exc
            if _is_mapping_model_error(exc):
                raise _http_error(503, "mapping_model_unavailable", str(exc)) from exc
            raise
        _write_mapping_report_atomic(report, mapping_report_path)
        mapping_content_sha256 = str(report.get("_run_info", {}).get("content_sha256", ""))
        job = _job_metadata(
            job_id=job_id,
            original_filename=filename,
            contract_registry_id=spec.registry_id,
            contract=contract,
            scorer=payload.scorer,
            source_path=source_path,
            source_row_count=csv_row_count,
            source_field_count=csv_field_count,
            mapping_report_path=mapping_report_path,
            mapping_content_sha256=mapping_content_sha256,
        )
        _write_json_atomic(job_json_path, job)
        return _job_response(job, report)
    except HTTPException:
        if job_dir is not None and job_dir.exists():
            shutil.rmtree(job_dir)
        raise
    finally:
        JOB_LOCK.release()


@router.get("/jobs/{job_id:path}")
def get_mapping_job(job_id: str) -> dict[str, Any]:
    job_dir = _job_dir(job_id)
    if not job_dir.is_dir():
        raise _http_error(404, "mapping_job_not_found", f"Mapping job `{job_id}` was not found.")
    job_path = _ensure_under_runtime(job_dir / "job.json")
    report_path = _ensure_under_runtime(job_dir / "mapping_report.json")
    if not job_path.is_file() or not report_path.is_file():
        raise _http_error(500, "malformed_mapping_job", "Mapping job files are incomplete.")
    job = _load_job_json(job_path)
    report = _load_mapping_report(report_path)
    return _job_response(job, report)
