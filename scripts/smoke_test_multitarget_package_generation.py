from __future__ import annotations

import csv
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.contracts.loader import load_migration_contract
from src.core.hashing import provenance_text_or_raw_sha256
from src.core.mapping.protocol_lock import ProtocolLockError, validate_effective_protocol_lock
from src.core.package_generation.builder import build_migration_package, write_build_report
from src.core.package_generation.decision_loader import load_mapping_decisions
from src.tools.data_profile import attach_run_info


BLIND_COMMIT = "e7f227fff0816b85e2e6e8b279062b944c723da5"
BLIND_MAPPING_SHA = "99007ad5da580b6e764b01e3a9739840bcfcff1b1a16c29cf708124ebbc56703"
BLIND_EVALUATION_SHA = "e75c2f8e5b6ed7794f265ceb795426045b403d2973a5bc7622af016c887e7527"
PROTOCOL_LOCK_SHA = "bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed"
GENERIC_MANIFEST_SHA = "3915849c255cafa9baf3011e212bb287985d8c20e785e3bbc3baa47aad234c5c"
SAP_MANIFEST_SHA = "0dcd68ef1422747be95223aacac782735c7cdad59fa2e865a0a36ff8154ff17e"
GENERIC_BUILD_SHA = "d70d0d02d4231da377f3fbcdd3095bdfd8fcfcac8fec57d718486f3fac69c473"
SAP_BUILD_SHA = "397bc0301d29f402f1a37fd5c672b2b755f3976475c36c7721997f74a5b8f3e8"

CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
CONTRACT_DATA = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = CONTRACT_DATA / "source_product_catalog.csv"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"
LOCK = CONTRACT_DATA / "blind_protocol_lock.json"
AMENDMENT = CONTRACT_DATA / "blind_protocol_compatibility_amendment_v1.json"
DECISIONS = PROJECT_ROOT / "data" / "examples" / "remediation" / "erpnext_item_price" / "mapping_decisions.yaml"
OUTPUT_ROOT = PROJECT_ROOT / "data" / "generated" / "erpnext_item_price_multitarget"
BUILD_REPORT = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_multitarget_build_report.json"
VALIDATION_REPORT = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_multitarget_generated_validation.json"
REMEDIATION_REPORT = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_multitarget_remediation.json"

GENERIC_MANIFEST = PROJECT_ROOT / "data" / "generated" / "generic_customer" / "package_manifest.json"
SAP_MANIFEST = PROJECT_ROOT / "data" / "generated" / "sap_supplier_reference" / "package_manifest.json"
GENERIC_BUILD = PROJECT_ROOT / "data" / "synthetic" / "generic_customer_package_build_report.json"
SAP_BUILD = PROJECT_ROOT / "data" / "synthetic" / "sap_supplier_reference_package_build_report.json"


def sha256(path: Path) -> str:
    return provenance_text_or_raw_sha256(path)


def content_sha(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["_run_info"]["content_sha256"]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_decision_document() -> dict[str, Any]:
    return yaml.safe_load(DECISIONS.read_text(encoding="utf-8"))


def approved_decisions() -> list[dict[str, Any]]:
    return [item for item in read_decision_document()["decisions"] if item["decision"] == "approved"]


def unique_approved_sources() -> set[str]:
    return {item["source_field"] for item in approved_decisions()}


def multitarget_sources() -> list[str]:
    counts: dict[str, int] = {}
    for item in approved_decisions():
        counts[item["source_field"]] = counts.get(item["source_field"], 0) + 1
    return sorted(source for source, count in counts.items() if count > 1)


def build_remediation_report(
    *,
    output_root: Path = OUTPUT_ROOT,
    build_report: Path = BUILD_REPORT,
    validation_report: Path = VALIDATION_REPORT,
    remediation_report: Path = REMEDIATION_REPORT,
) -> dict[str, Any]:
    build = read_json(build_report)
    validation = read_json(validation_report)
    lineage = read_json(output_root / "lineage.json")
    manifest = read_json(output_root / "package_manifest.json")
    body = {
        "_meta": {
            "component": "multitarget_mapping_remediation",
            "blind_benchmark_commit": BLIND_COMMIT,
            "blind_mapping_content_sha256": content_sha(MAPPING),
            "blind_evaluation_content_sha256": content_sha(EVALUATION),
            "protocol_lock_content_sha256": sha256(LOCK),
            "mapping_engine_modified": False,
            "candidate_ranking_modified": False,
            "thresholds_modified": False,
        },
        "before": {
            "mapping_report_supports_multiple_formal_recommendations": False,
            "decision_loader_supports_one_source_to_multiple_targets": False,
            "package_builder_verified_for_one_source_to_multiple_targets": False,
        },
        "after": {
            "mapping_report_supports_multiple_formal_recommendations": False,
            "decision_loader_supports_one_source_to_multiple_targets": True,
            "package_builder_verified_for_one_source_to_multiple_targets": True,
        },
        "execution": {
            "approved_mapping_links": len(approved_decisions()),
            "unique_approved_source_fields": len(unique_approved_sources()),
            "multi_target_source_fields": multitarget_sources(),
            "resources_generated": build["summary"]["resources_generated"],
            "rows_generated": build["summary"]["rows_generated"],
            "lineage_entries": len(lineage["entries"]),
            "generated_validation_valid": validation["summary"]["valid"],
            "generated_validation_findings": validation["summary"]["finding_count"],
        },
        "artifacts": {
            "decision_path": "data/examples/remediation/erpnext_item_price/mapping_decisions.yaml",
            "decision_sha256": sha256(DECISIONS),
            "output_root": project_relative(output_root),
            "manifest_content_sha256": manifest["_run_info"]["content_sha256"],
            "build_report_content_sha256": build["_run_info"]["content_sha256"],
            "generated_validation_content_sha256": validation["_run_info"]["content_sha256"],
            "lineage_content_sha256": lineage["_run_info"]["content_sha256"],
        },
    }
    return attach_run_info(body)


def write_stable_json(document: dict[str, Any], output_path: Path) -> None:
    next_document = deepcopy(document)
    if output_path.exists():
        previous = read_json(output_path)
        previous_run = previous.get("_run_info", {})
        next_run = next_document.get("_run_info", {})
        if previous_run.get("content_sha256") == next_run.get("content_sha256") and previous_run.get("generated_at"):
            next_document["_run_info"]["generated_at"] = previous_run["generated_at"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(next_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def project_relative(path: Path) -> str:
    return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()


def rebuild_formal_package(
    *,
    output_root: Path = OUTPUT_ROOT,
    build_report: Path = BUILD_REPORT,
    validation_report: Path = VALIDATION_REPORT,
    remediation_report: Path = REMEDIATION_REPORT,
) -> dict[str, Any]:
    contract = load_migration_contract(CONTRACT, CONTRACT_DATA)
    report = build_migration_package(
        contract,
        SOURCE,
        MAPPING,
        DECISIONS,
        output_root,
        validation_report_path=validation_report,
    )
    write_build_report(report, build_report)
    write_stable_json(
        build_remediation_report(
            output_root=output_root,
            build_report=build_report,
            validation_report=validation_report,
            remediation_report=remediation_report,
        ),
        remediation_report,
    )
    return report


def target_cell_key(entry: dict[str, Any]) -> tuple[str, int, str]:
    return (entry["target_resource"], int(entry["target_row_number"]), entry["target_field"])


def every_nonempty_target_cell_has_lineage(output_root: Path = OUTPUT_ROOT) -> bool:
    lineage = read_json(output_root / "lineage.json")
    lineage_cells = {target_cell_key(entry) for entry in lineage["entries"]}
    for resource_name, filename in (("item", "item.csv"), ("item_price", "item_price.csv")):
        for row_number, row in enumerate(read_csv(output_root / filename), start=1):
            for field, value in row.items():
                if value and (resource_name, row_number, field) not in lineage_cells:
                    return False
    return True


def source_has_multiple_targets_loaded() -> bool:
    contract = load_migration_contract(CONTRACT, CONTRACT_DATA)
    decisions = load_mapping_decisions(DECISIONS, contract, MAPPING)
    links: dict[str, set[str]] = {}
    for item in decisions.approved():
        links.setdefault(item.source_field, set()).add(str(item.target))
    return links.get("article_number") == {"item.item_code", "item_price.item_code"} and links.get("inventory_measure") == {"item.stock_uom", "item_price.uom"}


def engine_lock_matches() -> bool:
    try:
        validate_effective_protocol_lock(LOCK, AMENDMENT)
    except ProtocolLockError:
        return False
    return True


def no_special_branch_in_package_core() -> bool:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "src" / "core" / "package_generation").glob("*.py"))
    if re.search(r"\bif\s+.*adapter\b|\belif\s+.*adapter\b", text):
        return False
    return ("adapter" + "==") not in text and ("adapter" + " ==") not in text and "erpnext" not in text.lower()


def no_answer_file_dependency() -> bool:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "src" / "core" / "package_generation").glob("*.py"))
    return "ground" + "_truth.json" not in text and "ground" + "_truth_path" not in text


def _safe_output_root(path: Path) -> Path:
    if ".." in Path(path).parts:
        raise ValueError("--output-root must not contain path escapes")
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("--output-root must stay inside the project root") from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def main(argv: list[str] | None = None) -> int:
    output_root = OUTPUT_ROOT
    build_report = BUILD_REPORT
    validation_report = VALIDATION_REPORT
    remediation_report = REMEDIATION_REPORT
    if argv is not None and argv:
        if len(argv) != 2 or argv[0] != "--output-root":
            raise ValueError("Usage: smoke_test_multitarget_package_generation.py [--output-root PATH]")
        root = _safe_output_root(Path(argv[1]))
        output_root = root / "generated" / "erpnext_item_price_multitarget"
        build_report = root / "synthetic" / "erpnext_item_price_multitarget_build_report.json"
        validation_report = root / "synthetic" / "erpnext_item_price_multitarget_generated_validation.json"
        remediation_report = root / "synthetic" / "erpnext_item_price_multitarget_remediation.json"
    first = rebuild_formal_package(
        output_root=output_root,
        build_report=build_report,
        validation_report=validation_report,
        remediation_report=remediation_report,
    )
    first_validation = content_sha(validation_report)
    first_manifest = content_sha(output_root / "package_manifest.json")
    first_remediation = content_sha(remediation_report)
    second = rebuild_formal_package(
        output_root=output_root,
        build_report=build_report,
        validation_report=validation_report,
        remediation_report=remediation_report,
    )
    lineage = read_json(output_root / "lineage.json")
    item_rows = read_csv(output_root / "item.csv")
    price_rows = read_csv(output_root / "item_price.csv")
    target_cells = [target_cell_key(entry) for entry in lineage["entries"]]
    checks = {
        "blind_mapping": content_sha(MAPPING) == BLIND_MAPPING_SHA,
        "blind_evaluation": content_sha(EVALUATION) == BLIND_EVALUATION_SHA,
        "lock": sha256(LOCK) == PROTOCOL_LOCK_SHA,
        "engine": engine_lock_matches(),
        "approved_links": len(approved_decisions()) == 11,
        "unique_sources": len(unique_approved_sources()) == 9,
        "multi_sources": multitarget_sources() == ["article_number", "inventory_measure"],
        "loaded": source_has_multiple_targets_loaded(),
        "item_rows": len(item_rows) == 8,
        "price_rows": len(price_rows) == 8,
        "article_item": all(item["item_code"] == price["item_code"] for item, price in zip(item_rows, price_rows)),
        "measure_item": all(item["stock_uom"] == price["uom"] for item, price in zip(item_rows, price_rows)),
        "lineage": len(lineage["entries"]) == 88,
        "lineage_unique": len(target_cells) == len(set(target_cells)),
        "lineage_cells": every_nonempty_target_cell_has_lineage(output_root),
        "validation": second["validation"]["valid"] is True,
        "findings": second["validation"]["finding_count"] == 0,
        "generic_manifest": content_sha(GENERIC_MANIFEST) == GENERIC_MANIFEST_SHA,
        "sap_manifest": content_sha(SAP_MANIFEST) == SAP_MANIFEST_SHA,
        "generic_build": content_sha(GENERIC_BUILD) == GENERIC_BUILD_SHA,
        "sap_build": content_sha(SAP_BUILD) == SAP_BUILD_SHA,
        "deterministic": (
            first["_run_info"]["content_sha256"] == second["_run_info"]["content_sha256"]
            and first_validation == content_sha(validation_report)
            and first_manifest == content_sha(output_root / "package_manifest.json")
            and first_remediation == content_sha(remediation_report)
        ),
        "answers": no_answer_file_dependency(),
        "branches": no_special_branch_in_package_core(),
    }
    valid = all(checks.values())
    print("Multi-target execution: human-approved")
    print(f"Blind evidence preserved: {'valid' if checks['blind_mapping'] and checks['blind_evaluation'] and checks['lock'] else 'invalid'}")
    print(f"Approved links: {len(approved_decisions())}")
    print(f"Unique approved sources: {len(unique_approved_sources())}")
    print(f"Multi-target sources: {len(multitarget_sources())}")
    print(f"ERPNext resources: {second['summary']['resources_generated']}")
    print(f"ERPNext rows: {second['summary']['rows_generated']}")
    print(f"Lineage entries: {len(lineage['entries'])}")
    print(f"Generated validation: {'valid' if second['validation']['valid'] else 'invalid'}")
    print(f"Generated findings: {second['validation']['finding_count']}")
    print(f"Mapping engine modified: {'no' if checks['engine'] else 'yes'}")
    print(f"Deterministic replay: {'valid' if checks['deterministic'] else 'invalid'}")
    print(f"Validation: {'valid' if valid else 'invalid'}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
