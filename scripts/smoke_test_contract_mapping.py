from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import load_migration_contract
from src.core.mapping.engine import suggest_contract_mappings, write_mapping_report
from src.core.mapping.evaluator import evaluate_mapping_report, write_evaluation_report
from src.core.mapping.scorer import DEFAULT_MODEL_NAME


FORBIDDEN_CORE_STRINGS = (
    "OrganizationBPName1",
    "BusinessPartner",
    "CompanyCode",
    "PurchasingOrganization",
    "S/4HANA",
)


def _contains_absolute_path(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    if isinstance(value, str):
        return bool(re.match(r"^[A-Za-z]:[\\/]", value))
    return False


def _core_has_adapter_branch() -> bool:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "src" / "core" / "mapping").glob("*.py")
    )
    return bool(re.search(r"\bif\s+.*adapter\b|\belif\s+.*adapter\b|adapter\s*==", text))


def _core_has_forbidden_strings() -> bool:
    files = list((PROJECT_ROOT / "src" / "core" / "mapping").glob("*.py"))
    files.extend([
        PROJECT_ROOT / "src" / "tools" / "suggest_contract_mappings.py",
        PROJECT_ROOT / "src" / "tools" / "evaluate_contract_mappings.py",
    ])
    return any(
        needle in path.read_text(encoding="utf-8")
        for path in files
        for needle in FORBIDDEN_CORE_STRINGS
    )


def _engine_reads_truth_boundary() -> bool:
    files = [
        PROJECT_ROOT / "src" / "core" / "mapping" / "profiler.py",
        PROJECT_ROOT / "src" / "core" / "mapping" / "target_index.py",
        PROJECT_ROOT / "src" / "core" / "mapping" / "scorer.py",
        PROJECT_ROOT / "src" / "core" / "mapping" / "engine.py",
        PROJECT_ROOT / "src" / "tools" / "suggest_contract_mappings.py",
    ]
    patterns = (
        "ground_truth.json",
        "ground_truth_path",
        "ground-truth",
        "Ground Truth",
    )
    return any(
        pattern in path.read_text(encoding="utf-8")
        for path in files
        for pattern in patterns
    )


def _run_case(
    contract_path: Path,
    data_root: Path,
    source_path: Path,
    truth_path: Path,
    output_dir: Path,
    stem: str,
) -> tuple[dict, dict, dict, dict]:
    contract = load_migration_contract(contract_path, data_root)
    mapping_output = output_dir / f"{stem}_mapping.json"
    first = suggest_contract_mappings(contract, source_path)
    write_mapping_report(first, mapping_output)
    evaluation = evaluate_mapping_report(mapping_output, truth_path)
    evaluation_output = output_dir / f"{stem}_evaluation.json"
    write_evaluation_report(evaluation, evaluation_output)
    second = suggest_contract_mappings(contract, source_path)
    write_mapping_report(second, mapping_output)
    second_evaluation = evaluate_mapping_report(mapping_output, truth_path)
    write_evaluation_report(second_evaluation, evaluation_output)
    return first, evaluation, second, second_evaluation


def main() -> int:
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temp_dir:
        output_dir = Path(temp_dir)
        generic, generic_eval, generic_replay, generic_eval_replay = _run_case(
            PROJECT_ROOT / "contracts" / "generic_customer" / "datapackage.yaml",
            PROJECT_ROOT / "data" / "examples" / "generic_customer",
            PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "source_customer.csv",
            PROJECT_ROOT / "data" / "examples" / "mapping" / "generic_customer" / "ground_truth.json",
            output_dir,
            "generic",
        )
        supplier, supplier_eval, supplier_replay, supplier_eval_replay = _run_case(
            PROJECT_ROOT / "contracts" / "sap_supplier_reference" / "datapackage.yaml",
            PROJECT_ROOT / "data" / "examples" / "sap_supplier_reference",
            PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "source_supplier.csv",
            PROJECT_ROOT / "data" / "examples" / "mapping" / "sap_supplier_reference" / "ground_truth.json",
            output_dir,
            "supplier",
        )
    deterministic = (
        generic["_run_info"]["content_sha256"] == generic_replay["_run_info"]["content_sha256"]
        and supplier["_run_info"]["content_sha256"] == supplier_replay["_run_info"]["content_sha256"]
        and generic_eval["_run_info"]["content_sha256"] == generic_eval_replay["_run_info"]["content_sha256"]
        and supplier_eval["_run_info"]["content_sha256"] == supplier_eval_replay["_run_info"]["content_sha256"]
    )
    no_target_ok = (
        generic_eval["summary"]["no_target_accuracy"] == 1.0
        and supplier_eval["summary"]["no_target_accuracy"] == 1.0
    )
    group_stats_ok = (
        generic_eval["by_evaluation_group"]["alias_backed"]["fields"] > 0
        and generic_eval["by_evaluation_group"]["semantic_only"]["fields"] > 0
        and supplier_eval["by_evaluation_group"]["alias_backed"]["fields"] > 0
        and supplier_eval["by_evaluation_group"]["semantic_only"]["fields"] > 0
    )
    checks = {
        "top3": generic_eval["summary"]["top3_recall"] == 1.0
        and supplier_eval["summary"]["top3_recall"] == 1.0,
        "no_target": no_target_ok,
        "groups": group_stats_ok,
        "truth_isolation": not _engine_reads_truth_boundary(),
        "deterministic": deterministic,
        "absolute_paths": not _contains_absolute_path(generic)
        and not _contains_absolute_path(supplier)
        and not _contains_absolute_path(generic_eval)
        and not _contains_absolute_path(supplier_eval),
        "adapter_branching": not _core_has_adapter_branch(),
        "hardcoding": not _core_has_forbidden_strings(),
    }
    print("Mapping engine: contract-driven")
    print("Embedding model: all-MiniLM-L6-v2")
    print("Contracts: 2")
    print(f"Generic mappings: {len(generic['mappings'])}")
    print(
        "Generic top1/top3: "
        f"{generic_eval['summary']['top1_accuracy']:.4f}/"
        f"{generic_eval['summary']['top3_recall']:.4f}"
    )
    print(f"SAP mappings: {len(supplier['mappings'])}")
    print(
        "SAP top1/top3: "
        f"{supplier_eval['summary']['top1_accuracy']:.4f}/"
        f"{supplier_eval['summary']['top3_recall']:.4f}"
    )
    print(f"Ground truth isolation: {'valid' if checks['truth_isolation'] else 'invalid'}")
    print(f"Deterministic replay: {'valid' if checks['deterministic'] else 'invalid'}")
    print(f"Adapter branching: {'none' if checks['adapter_branching'] else 'found'}")
    print(f"Absolute paths: {'none' if checks['absolute_paths'] else 'found'}")
    valid = all(checks.values())
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
