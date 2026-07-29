from __future__ import annotations

import csv
import hashlib
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
from src.core.package_generation.builder import build_migration_package, write_build_report
from src.core.package_generation.decision_loader import load_mapping_decisions
from src.tools.data_profile import attach_run_info


BLIND_COMMIT = "e7f227fff0816b85e2e6e8b279062b944c723da5"
BLIND_MAPPING_SHA = "f2b1a3b578222694b845950165334b628a6e8285d54287457af10fb2fd836164"
BLIND_EVALUATION_SHA = "d665596750403d5928daa332f318dec3078bce1a7ab977c8192b3a5edd106fed"
PROTOCOL_LOCK_SHA = "bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed"
GENERIC_MANIFEST_SHA = "d2e4abbc5b0e451787fecd38ab0bf57a4af5492436ac5b6df55c95eb9e22ae59"
SAP_MANIFEST_SHA = "71874bf500dbe39fc9f7bf0ff19d292f1eb0cd870c891f9a0ee3db99062a1ba5"
GENERIC_BUILD_SHA = "5ee837b005fbe3162363e9587bc0b57128e53b75efc2cb404efc66ab3b5dc789"
SAP_BUILD_SHA = "56725ac76a07fcdf7c37e9d2f64d3aeb3a7c96b7261b4cabf1e927231c0b303a"

CONTRACT = PROJECT_ROOT / "contracts" / "erpnext_item_price_reference" / "datapackage.yaml"
CONTRACT_DATA = PROJECT_ROOT / "data" / "examples" / "blind" / "erpnext_item_price"
SOURCE = CONTRACT_DATA / "source_product_catalog.csv"
MAPPING = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_mapping.json"
EVALUATION = PROJECT_ROOT / "data" / "synthetic" / "erpnext_item_price_blind_evaluation.json"
LOCK = CONTRACT_DATA / "blind_protocol_lock.json"
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
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def build_remediation_report() -> dict[str, Any]:
    build = read_json(BUILD_REPORT)
    validation = read_json(VALIDATION_REPORT)
    lineage = read_json(OUTPUT_ROOT / "lineage.json")
    manifest = read_json(OUTPUT_ROOT / "package_manifest.json")
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
            "output_root": "data/generated/erpnext_item_price_multitarget",
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


def rebuild_formal_package() -> dict[str, Any]:
    contract = load_migration_contract(CONTRACT, CONTRACT_DATA)
    report = build_migration_package(
        contract,
        SOURCE,
        MAPPING,
        DECISIONS,
        OUTPUT_ROOT,
        validation_report_path=VALIDATION_REPORT,
    )
    write_build_report(report, BUILD_REPORT)
    write_stable_json(build_remediation_report(), REMEDIATION_REPORT)
    return report


def target_cell_key(entry: dict[str, Any]) -> tuple[str, int, str]:
    return (entry["target_resource"], int(entry["target_row_number"]), entry["target_field"])


def every_nonempty_target_cell_has_lineage() -> bool:
    lineage = read_json(OUTPUT_ROOT / "lineage.json")
    lineage_cells = {target_cell_key(entry) for entry in lineage["entries"]}
    for resource_name, filename in (("item", "item.csv"), ("item_price", "item_price.csv")):
        for row_number, row in enumerate(read_csv(OUTPUT_ROOT / filename), start=1):
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
    lock = read_json(LOCK)
    for rel_path, expected in lock["engine_files"].items():
        if sha256(PROJECT_ROOT / rel_path) != expected:
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


def main() -> int:
    first = rebuild_formal_package()
    first_validation = content_sha(VALIDATION_REPORT)
    first_manifest = content_sha(OUTPUT_ROOT / "package_manifest.json")
    first_remediation = content_sha(REMEDIATION_REPORT)
    second = rebuild_formal_package()
    lineage = read_json(OUTPUT_ROOT / "lineage.json")
    item_rows = read_csv(OUTPUT_ROOT / "item.csv")
    price_rows = read_csv(OUTPUT_ROOT / "item_price.csv")
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
        "lineage_cells": every_nonempty_target_cell_has_lineage(),
        "validation": second["validation"]["valid"] is True,
        "findings": second["validation"]["finding_count"] == 0,
        "generic_manifest": content_sha(GENERIC_MANIFEST) == GENERIC_MANIFEST_SHA,
        "sap_manifest": content_sha(SAP_MANIFEST) == SAP_MANIFEST_SHA,
        "generic_build": content_sha(GENERIC_BUILD) == GENERIC_BUILD_SHA,
        "sap_build": content_sha(SAP_BUILD) == SAP_BUILD_SHA,
        "deterministic": (
            first["_run_info"]["content_sha256"] == second["_run_info"]["content_sha256"]
            and first_validation == content_sha(VALIDATION_REPORT)
            and first_manifest == content_sha(OUTPUT_ROOT / "package_manifest.json")
            and first_remediation == content_sha(REMEDIATION_REPORT)
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
    raise SystemExit(main())
