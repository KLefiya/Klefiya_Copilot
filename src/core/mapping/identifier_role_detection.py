from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from src.core.contracts.loader import PROJECT_ROOT


EXPERIMENT_ID = "value_profile_identifier_detector_v1"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data/experiments/schema_matching_identifier_role_v1"
BENCHMARK_PATHS = (
    PROJECT_ROOT / "data/benchmarks/schema_matching_v1.json",
    PROJECT_ROOT / "data/benchmarks/schema_matching_public_dev_v1.json",
)
SCENARIO_ORDER = (
    "generic_customer",
    "supplier_reference",
    "erpnext_item_price",
    "bank_account",
    "sales_order_fulfillment",
    "open_food_facts_products",
    "contracts_finder_procurement_2026",
)
CONTRACT_FAMILY_ORDER = (
    "bank_account",
    "generic_customer",
    "item_item_price",
    "sales_order_fulfillment",
    "supplier_reference",
)
LABEL_IDENTIFIER = "identifier"
LABEL_NON_IDENTIFIER = "non_identifier"
LABEL_EXCLUDED = "ambiguous_excluded"
LOGISTIC_FEATURE_SETS = {
    "distribution_only": (
        "row_count",
        "non_null_count",
        "null_ratio",
        "distinct_count",
        "uniqueness_ratio",
        "duplicate_ratio",
        "normalized_value_frequency_entropy",
    ),
    "pattern_only": (
        "mean_value_length",
        "value_length_stddev",
        "value_length_coefficient_of_variation",
        "dominant_length_ratio",
        "numeric_only_value_ratio",
        "alphabetic_only_value_ratio",
        "alphanumeric_mixed_ratio",
        "digit_character_fraction",
        "alphabetic_character_fraction",
        "punctuation_character_fraction",
        "whitespace_character_fraction",
        "leading_zero_ratio",
        "uuid_like_ratio",
        "hex_like_ratio",
        "fixed_width_ratio",
        "dominant_normalized_pattern_ratio",
        "normalized_pattern_entropy",
        "integer_monotonicity_signal",
        "integer_sequentiality_signal",
    ),
}
COMBINED_FEATURE_ORDER = LOGISTIC_FEATURE_SETS["distribution_only"] + LOGISTIC_FEATURE_SETS["pattern_only"]
LOGISTIC_FEATURE_SETS["combined"] = COMBINED_FEATURE_ORDER
FORBIDDEN_FEATURE_TOKENS = (
    "field",
    "name",
    "source",
    "target",
    "path",
    "scenario",
    "contract",
    "family",
    "case",
    "expected",
    "ground",
    "truth",
    "label",
    "rank",
    "score",
)
LEARNING_RATE = 0.12
L2_PENALTY = 0.04
EPOCHS = 900


@dataclass(frozen=True)
class SourceColumnCase:
    scenario_id: str
    contract_family: str
    source_path: str
    source_field: str
    case_id: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class RoleAnnotation:
    scenario_id: str
    contract_family: str
    source_field: str
    role_label: str
    rationale: str
    evidence_type: str
    ambiguous_exclusion_reason: str | None = None

    @property
    def case_id(self) -> str:
        return f"{self.scenario_id}__{self.source_field}"

    @property
    def is_excluded(self) -> bool:
        return self.role_label == LABEL_EXCLUDED

    @property
    def numeric_label(self) -> int:
        if self.role_label == LABEL_IDENTIFIER:
            return 1
        if self.role_label == LABEL_NON_IDENTIFIER:
            return 0
        raise ValueError("Excluded annotations do not have a numeric model label")


@dataclass(frozen=True)
class DetectorSample:
    scenario_id: str
    contract_family: str
    case_id: str
    source_field: str
    role_label: str
    label: int
    features: dict[str, float]


ANNOTATION_ROWS: tuple[tuple[str, str, str, str, str, str, str | None], ...] = (
    ("generic_customer", "generic_customer", "legacy_client_id", LABEL_IDENTIFIER, "Legacy customer identifier used to distinguish source records.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "client_name", LABEL_NON_IDENTIFIER, "Customer display name, not a stable key.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "nation", LABEL_NON_IDENTIFIER, "Country/category value rather than entity identifier.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "contact_email", LABEL_NON_IDENTIFIER, "Contact channel, not the primary identifier role for the row.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "telephone_number", LABEL_NON_IDENTIFIER, "Contact channel, not the primary identifier role for the row.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "vat_registration_number", LABEL_IDENTIFIER, "Tax registration number is a stable business identifier.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "terms_code", LABEL_IDENTIFIER, "Payment term code references a stable terms master value.", "fixture documentation", None),
    ("generic_customer", "generic_customer", "bank_record_key", LABEL_IDENTIFIER, "Bank record key is explicitly a key-like reference.", "fixture documentation", None),
    ("generic_customer", "generic_customer", "bank_customer_reference", LABEL_IDENTIFIER, "Reference back to a customer record.", "fixture documentation", None),
    ("generic_customer", "generic_customer", "bank_account_iban", LABEL_IDENTIFIER, "IBAN is a stable bank account identifier.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "account_currency", LABEL_NON_IDENTIFIER, "Currency code is an attribute/category here.", "human semantic annotation", None),
    ("generic_customer", "generic_customer", "marketing_opt_in", LABEL_NON_IDENTIFIER, "Boolean preference flag.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "old_vendor_number", LABEL_IDENTIFIER, "Legacy vendor number identifies supplier records.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "vendor_legal_name", LABEL_NON_IDENTIFIER, "Supplier legal name, not a key.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "partner_type", LABEL_NON_IDENTIFIER, "Low-cardinality category.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "vendor_country", LABEL_NON_IDENTIFIER, "Country attribute.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "preferred_language", LABEL_NON_IDENTIFIER, "Language attribute.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "vat_number", LABEL_IDENTIFIER, "VAT number is a tax identifier.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "company_assignment_key", LABEL_IDENTIFIER, "Assignment key links supplier and company context.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "company_vendor_reference", LABEL_IDENTIFIER, "Company-specific supplier reference.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "legal_entity_code", LABEL_IDENTIFIER, "Legal entity code is a stable coded identifier.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "recon_gl_account", LABEL_IDENTIFIER, "G/L account code references an account master.", "human semantic annotation", None),
    ("supplier_reference", "supplier_reference", "payment_condition", LABEL_IDENTIFIER, "Payment condition code references payment term logic.", "fixture documentation", None),
    ("supplier_reference", "supplier_reference", "legacy_created_by", LABEL_IDENTIFIER, "Legacy user reference identifies the creator account even without a mapping target.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "article_number", LABEL_IDENTIFIER, "Article number identifies a product/item.", "fixture documentation", None),
    ("erpnext_item_price", "item_item_price", "catalogue_caption", LABEL_NON_IDENTIFIER, "Caption/name text.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "merchandise_family", LABEL_NON_IDENTIFIER, "Product category family.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "inventory_measure", LABEL_NON_IDENTIFIER, "Unit of measure attribute.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "lifecycle_block", LABEL_NON_IDENTIFIER, "Lifecycle status flag/category.", "fixture documentation", None),
    ("erpnext_item_price", "item_item_price", "tariff_name", LABEL_NON_IDENTIFIER, "Price-list display name.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "retail_amount", LABEL_NON_IDENTIFIER, "Monetary amount.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "effective_start", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "effective_end", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("erpnext_item_price", "item_item_price", "data_steward", LABEL_NON_IDENTIFIER, "Person/team ownership text rather than row identifier.", "fixture documentation", None),
    ("bank_account", "bank_account", "legacy_bank_ref", LABEL_IDENTIFIER, "Legacy bank reference identifies bank-account records.", "fixture documentation", None),
    ("bank_account", "bank_account", "beneficiary_label", LABEL_NON_IDENTIFIER, "Beneficiary display label.", "human semantic annotation", None),
    ("bank_account", "bank_account", "domestic_account", LABEL_IDENTIFIER, "Domestic account number identifies an account.", "human semantic annotation", None),
    ("bank_account", "bank_account", "international_account", LABEL_IDENTIFIER, "IBAN-like account identifier.", "human semantic annotation", None),
    ("bank_account", "bank_account", "institution_ref", LABEL_IDENTIFIER, "Institution reference identifies a bank/institution.", "fixture documentation", None),
    ("bank_account", "bank_account", "settlement_ccy", LABEL_NON_IDENTIFIER, "Currency attribute.", "human semantic annotation", None),
    ("bank_account", "bank_account", "account_domicile", LABEL_NON_IDENTIFIER, "Country attribute.", "human semantic annotation", None),
    ("bank_account", "bank_account", "activation_date", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("bank_account", "bank_account", "closure_date", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("bank_account", "bank_account", "preferred_account", LABEL_NON_IDENTIFIER, "Boolean preference flag.", "fixture documentation", None),
    ("bank_account", "bank_account", "institution_name", LABEL_NON_IDENTIFIER, "Institution display name.", "human semantic annotation", None),
    ("bank_account", "bank_account", "swift_identifier", LABEL_IDENTIFIER, "SWIFT/BIC is a bank identifier.", "human semantic annotation", None),
    ("bank_account", "bank_account", "clearing_code", LABEL_IDENTIFIER, "Clearing code identifies a clearing route/institution.", "human semantic annotation", None),
    ("bank_account", "bank_account", "branch_domicile", LABEL_NON_IDENTIFIER, "Branch country attribute.", "human semantic annotation", None),
    ("bank_account", "bank_account", "legacy_operator", LABEL_IDENTIFIER, "Legacy operator account reference is an identifier despite no target mapping.", "human semantic annotation", None),
    ("bank_account", "bank_account", "free_text_note", LABEL_NON_IDENTIFIER, "Free text note.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "legacy_sales_order_id", LABEL_IDENTIFIER, "Sales order id identifies an order.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "customer_po_ref", LABEL_IDENTIFIER, "Customer purchase-order reference identifies an external order.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "sold_to_ref", LABEL_IDENTIFIER, "Sold-to reference identifies the customer account.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "order_created_on", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "document_currency", LABEL_NON_IDENTIFIER, "Currency attribute.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "distribution_channel", LABEL_NON_IDENTIFIER, "Sales channel category.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "order_state", LABEL_NON_IDENTIFIER, "Status category.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "header_audit_marker", LABEL_IDENTIFIER, "Audit marker is a no-target identifier-like lineage token.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "legacy_line_item_no", LABEL_IDENTIFIER, "Line item number identifies a line within an order.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "material_ref", LABEL_IDENTIFIER, "Material reference identifies the item.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "ordered_qty", LABEL_NON_IDENTIFIER, "Quantity measure.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "sales_uom", LABEL_NON_IDENTIFIER, "Unit of measure attribute.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "net_unit_amount", LABEL_NON_IDENTIFIER, "Monetary amount.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "line_total_amount", LABEL_NON_IDENTIFIER, "Monetary amount.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "item_description", LABEL_NON_IDENTIFIER, "Description text.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "line_analyst_note", LABEL_NON_IDENTIFIER, "Free text note.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "schedule_seq", LABEL_IDENTIFIER, "Schedule sequence number identifies schedule lines.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "requested_ship_on", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "confirmed_ship_on", LABEL_NON_IDENTIFIER, "Date attribute.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "confirmed_qty", LABEL_NON_IDENTIFIER, "Quantity measure.", "human semantic annotation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "fulfillment_state", LABEL_NON_IDENTIFIER, "Status category.", "fixture documentation", None),
    ("sales_order_fulfillment", "sales_order_fulfillment", "migration_batch_label", LABEL_IDENTIFIER, "Migration batch label identifies a processing batch but has no mapping target.", "human semantic annotation", None),
    ("open_food_facts_products", "item_item_price", "code", LABEL_IDENTIFIER, "Public product barcode/code identifies a product.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "product_name", LABEL_NON_IDENTIFIER, "Product display name.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "generic_name", LABEL_NON_IDENTIFIER, "Generic product description.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "quantity", LABEL_NON_IDENTIFIER, "Package quantity text.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "categories", LABEL_NON_IDENTIFIER, "Category labels.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "brands", LABEL_NON_IDENTIFIER, "Brand names.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "countries", LABEL_NON_IDENTIFIER, "Country list attribute.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "nutrition_grades", LABEL_NON_IDENTIFIER, "Nutrition category.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "nova_group", LABEL_NON_IDENTIFIER, "Nutrition classification.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "packaging", LABEL_NON_IDENTIFIER, "Container description/category.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "stores", LABEL_NON_IDENTIFIER, "Retailer names.", "fixture documentation", None),
    ("open_food_facts_products", "item_item_price", "ingredients_text", LABEL_NON_IDENTIFIER, "Long ingredient text.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "id", LABEL_IDENTIFIER, "Notice id identifies a public procurement record.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "date", LABEL_NON_IDENTIFIER, "Date attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "ocid", LABEL_IDENTIFIER, "Open Contracting ID is an identifier even though no target is present.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "language", LABEL_NON_IDENTIFIER, "Language attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "initiationType", LABEL_NON_IDENTIFIER, "Procurement initiation category.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "buyer_id", LABEL_IDENTIFIER, "Buyer id identifies an organization.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "buyer_name", LABEL_NON_IDENTIFIER, "Buyer display name.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_id", LABEL_IDENTIFIER, "Tender id identifies a tender.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_title", LABEL_NON_IDENTIFIER, "Tender title text.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_mainProcurementCategory", LABEL_NON_IDENTIFIER, "Procurement category.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_status", LABEL_NON_IDENTIFIER, "Status category.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_description", LABEL_NON_IDENTIFIER, "Description text.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_procurementMethod", LABEL_NON_IDENTIFIER, "Procurement method category.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_value_amount", LABEL_NON_IDENTIFIER, "Monetary amount.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_value_currency", LABEL_NON_IDENTIFIER, "Currency attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_tenderPeriod_endDate", LABEL_NON_IDENTIFIER, "Date attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_classification_id", LABEL_IDENTIFIER, "Classification id is a coded reference.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_classification_scheme", LABEL_NON_IDENTIFIER, "Classification scheme name/category.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_classification_description", LABEL_NON_IDENTIFIER, "Classification description text.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_contractPeriod_startDate", LABEL_NON_IDENTIFIER, "Date attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_contractPeriod_endDate", LABEL_NON_IDENTIFIER, "Date attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "tender_datePublished", LABEL_NON_IDENTIFIER, "Date attribute.", "fixture documentation", None),
    ("contracts_finder_procurement_2026", "sales_order_fulfillment", "title", LABEL_NON_IDENTIFIER, "Short publication title text.", "fixture documentation", None),
)


def annotations() -> list[RoleAnnotation]:
    return [RoleAnnotation(*row) for row in ANNOTATION_ROWS]


def annotation_artifact() -> dict[str, Any]:
    rows = sorted(annotations(), key=lambda item: item.case_id)
    counts = Counter(item.role_label for item in rows)
    return {
        "experiment_id": EXPERIMENT_ID,
        "annotation_task": "source_column_identifier_role_detection",
        "development_only": True,
        "sealed_or_external_data_used": False,
        "annotation_inputs_allowed": [
            "development source field identifiers",
            "fixture documentation",
            "human semantic annotation",
        ],
        "annotation_inputs_forbidden": [
            "detector predictions",
            "V5 candidate rankings",
            "calibration probabilities",
            "LTR scores",
            "Companies House data or results",
            "FDIC sealed data or results",
            "mapping target labels as direct identifier labels",
        ],
        "label_definitions": {
            LABEL_IDENTIFIER: "Column primarily carries a stable identifier, code, key, or reference role.",
            LABEL_NON_IDENTIFIER: "Column primarily carries names, descriptions, dates, quantities, statuses, categories, amounts, or free text.",
            LABEL_EXCLUDED: "Ambiguous role excluded from model training and evaluation.",
        },
        "counts": {
            "total_annotations": len(rows),
            "identifier": counts[LABEL_IDENTIFIER],
            "non_identifier": counts[LABEL_NON_IDENTIFIER],
            "ambiguous_excluded": counts[LABEL_EXCLUDED],
        },
        "annotations": [
            {
                "scenario_id": item.scenario_id,
                "contract_family": item.contract_family,
                "source_field": item.source_field,
                "role_label": item.role_label,
                "rationale": item.rationale,
                "evidence_type": item.evidence_type,
                "ambiguous_exclusion_reason": item.ambiguous_exclusion_reason,
            }
            for item in rows
        ],
    }


def load_development_source_cases(benchmark_paths: Iterable[Path] = BENCHMARK_PATHS) -> list[SourceColumnCase]:
    cases: list[SourceColumnCase] = []
    for benchmark_path in benchmark_paths:
        path_text = benchmark_path.as_posix().replace("\\", "/")
        if "/external/" in path_text or "/sealed/" in path_text or "schema_matching_public_sealed" in path_text:
            raise ValueError(f"Only development benchmark registries are allowed: {benchmark_path}")
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
        meta = benchmark.get("_meta", {})
        if meta.get("sealed_holdout") or "sealed" in str(meta.get("benchmark_id", "")):
            raise ValueError(f"Sealed benchmark registries are rejected: {benchmark_path}")
        for scenario in benchmark["scenarios"]:
            for required in ("source_path", "contract_path", "scenario_id", "cases"):
                if required not in scenario:
                    raise ValueError(f"Scenario missing {required}: {scenario}")
            split = str(scenario.get("split", ""))
            if split == "sealed_holdout" or "external" in split:
                raise ValueError(f"Only development splits are allowed: {scenario['scenario_id']}")
            source_path = str(scenario["source_path"]).replace("\\", "/")
            if "/external/" in source_path or "/sealed/" in source_path:
                raise ValueError(f"External/sealed source paths are rejected: {source_path}")
            source_full_path = PROJECT_ROOT / source_path
            with source_full_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            if not rows:
                raise ValueError(f"Source fixture has no rows: {source_path}")
            family = contract_family(str(scenario["contract_path"]))
            case_fields = {case["source_field"] for case in scenario["cases"]}
            for field in rows[0].keys():
                if field not in case_fields:
                    raise ValueError(f"Source field lacks benchmark case: {scenario['scenario_id']}::{field}")
                cases.append(
                    SourceColumnCase(
                        scenario_id=str(scenario["scenario_id"]),
                        contract_family=family,
                        source_path=source_path,
                        source_field=str(field),
                        case_id=f"{scenario['scenario_id']}__{field}",
                        values=tuple(row.get(field, "") for row in rows),
                    )
                )
    ordered = {scenario: index for index, scenario in enumerate(SCENARIO_ORDER)}
    return sorted(cases, key=lambda item: (ordered[item.scenario_id], item.source_field))


def contract_family(contract_path: str) -> str:
    if "generic_customer" in contract_path:
        return "generic_customer"
    if "sap_supplier_reference" in contract_path:
        return "supplier_reference"
    if "erpnext_item_price" in contract_path:
        return "item_item_price"
    if "bank_account" in contract_path:
        return "bank_account"
    if "sales_order_fulfillment" in contract_path:
        return "sales_order_fulfillment"
    raise ValueError(f"Unknown contract family for {contract_path}")


def build_samples() -> tuple[list[DetectorSample], list[RoleAnnotation]]:
    cases = load_development_source_cases()
    annotation_by_case = {item.case_id: item for item in annotations()}
    if len(annotation_by_case) != len(ANNOTATION_ROWS):
        raise ValueError("Duplicate role annotations")
    case_ids = {case.case_id for case in cases}
    missing = sorted(case_ids - set(annotation_by_case))
    extra = sorted(set(annotation_by_case) - case_ids)
    if missing or extra:
        raise ValueError(f"Annotation/case mismatch: missing={missing}, extra={extra}")
    samples: list[DetectorSample] = []
    excluded: list[RoleAnnotation] = []
    for case in cases:
        annotation = annotation_by_case[case.case_id]
        if annotation.contract_family != case.contract_family:
            raise ValueError(f"Annotation contract family mismatch for {case.case_id}")
        if annotation.is_excluded:
            excluded.append(annotation)
            continue
        samples.append(
            DetectorSample(
                scenario_id=case.scenario_id,
                contract_family=case.contract_family,
                case_id=case.case_id,
                source_field=case.source_field,
                role_label=annotation.role_label,
                label=annotation.numeric_label,
                features=extract_value_profile_features(case.values),
            )
        )
    return sorted(samples, key=lambda item: item.case_id), sorted(excluded, key=lambda item: item.case_id)


def extract_value_profile_features(values: Iterable[str]) -> dict[str, float]:
    raw_values = ["" if value is None else str(value) for value in values]
    row_count = len(raw_values)
    observed = [value for value in raw_values if value != ""]
    non_null_count = len(observed)
    null_ratio = _ratio(row_count - non_null_count, row_count)
    distinct_values = set(observed)
    distinct_count = len(distinct_values)
    lengths = [len(value) for value in observed]
    length_counts = Counter(lengths)
    patterns = [normalize_value_pattern(value, compress=True) for value in observed]
    pattern_counts = Counter(patterns)
    numeric_values = [_integer_value(value) for value in observed if _integer_value(value) is not None]
    char_total = sum(len(value) for value in observed)
    features = {
        "row_count": float(row_count),
        "non_null_count": float(non_null_count),
        "null_ratio": null_ratio,
        "distinct_count": float(distinct_count),
        "uniqueness_ratio": _ratio(distinct_count, non_null_count),
        "duplicate_ratio": 1.0 - _ratio(distinct_count, non_null_count) if non_null_count else 0.0,
        "normalized_value_frequency_entropy": _normalized_entropy(Counter(observed)),
        "mean_value_length": float(statistics.mean(lengths)) if lengths else 0.0,
        "value_length_stddev": float(statistics.pstdev(lengths)) if len(lengths) > 1 else 0.0,
        "value_length_coefficient_of_variation": _ratio(float(statistics.pstdev(lengths)), float(statistics.mean(lengths))) if len(lengths) > 1 and statistics.mean(lengths) else 0.0,
        "dominant_length_ratio": _ratio(max(length_counts.values()) if length_counts else 0, non_null_count),
        "numeric_only_value_ratio": _value_ratio(observed, lambda value: bool(re.fullmatch(r"[0-9]+", value))),
        "alphabetic_only_value_ratio": _value_ratio(observed, lambda value: bool(re.fullmatch(r"[A-Za-z]+", value))),
        "alphanumeric_mixed_ratio": _value_ratio(observed, lambda value: bool(re.fullmatch(r"[A-Za-z0-9]+", value)) and any(ch.isalpha() for ch in value) and any(ch.isdigit() for ch in value)),
        "digit_character_fraction": _char_fraction(observed, str.isdigit, char_total),
        "alphabetic_character_fraction": _char_fraction(observed, str.isalpha, char_total),
        "punctuation_character_fraction": _char_fraction(observed, lambda ch: not ch.isalnum() and not ch.isspace(), char_total),
        "whitespace_character_fraction": _char_fraction(observed, str.isspace, char_total),
        "leading_zero_ratio": _value_ratio(observed, lambda value: len(value) > 1 and value[0] == "0" and any(ch.isdigit() for ch in value)),
        "uuid_like_ratio": _value_ratio(observed, lambda value: bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value))),
        "hex_like_ratio": _value_ratio(observed, lambda value: len(value) >= 8 and bool(re.fullmatch(r"[0-9a-fA-F]+", value)) and any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)),
        "fixed_width_ratio": _ratio(max(length_counts.values()) if length_counts else 0, non_null_count),
        "dominant_normalized_pattern_ratio": _ratio(max(pattern_counts.values()) if pattern_counts else 0, non_null_count),
        "normalized_pattern_entropy": _normalized_entropy(pattern_counts),
        "integer_monotonicity_signal": _integer_monotonicity(numeric_values),
        "integer_sequentiality_signal": _integer_sequentiality(numeric_values),
    }
    return {name: round(float(value), 8) for name, value in features.items()}


def normalize_value_pattern(value: str, *, compress: bool = True) -> str:
    symbols: list[str] = []
    for char in value:
        if "A" <= char <= "Z":
            symbol = "A"
        elif "a" <= char <= "z":
            symbol = "a"
        elif "0" <= char <= "9":
            symbol = "9"
        else:
            symbol = "x"
        if not compress or not symbols or symbols[-1] != symbol:
            symbols.append(symbol)
    return "".join(symbols)


def feature_schema() -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "model_input_boundary": "header_blind_value_profile_only",
        "feature_order": list(COMBINED_FEATURE_ORDER),
        "ablation_feature_sets": {name: list(features) for name, features in LOGISTIC_FEATURE_SETS.items()},
        "features": [
            {
                "name": name,
                "type": "numeric",
                "requires_scaling": True,
                "source": "CSV column value distribution only",
                "formula": _feature_formula(name),
                "leakage_rationale": (
                    "Computed from column values only and stored as aggregate numeric statistics. "
                    "The feature matrix excludes source field names, target identity, scenario id, "
                    "contract family, case id, mapping ground truth, mapping scores, ranks, and labels."
                ),
            }
            for name in COMBINED_FEATURE_ORDER
        ],
        "pattern_normalization": {
            "uppercase_letter": "A",
            "lowercase_letter": "a",
            "digit": "9",
            "other_symbol": "x",
            "compression": "consecutive identical pattern classes are compressed for distribution features",
        },
        "forbidden_model_inputs": list(FORBIDDEN_FEATURE_TOKENS),
        "raw_values_written_to_artifacts": False,
    }


def run_experiment() -> dict[str, Any]:
    samples, excluded = build_samples()
    if len(samples) + len(excluded) != 107:
        raise ValueError(f"Expected 107 annotated cases, found {len(samples) + len(excluded)}")
    if {sample.scenario_id for sample in samples} != set(SCENARIO_ORDER):
        raise ValueError("Unexpected scenario coverage")
    if {sample.contract_family for sample in samples} != set(CONTRACT_FAMILY_ORDER):
        raise ValueError("Unexpected contract-family coverage")
    scenario_results = grouped_evaluation(samples, "scenario_id", SCENARIO_ORDER)
    family_results = grouped_evaluation(samples, "contract_family", CONTRACT_FAMILY_ORDER)
    model = development_model(samples)
    records = {
        "samples": samples,
        "excluded": excluded,
        "scenario_results": scenario_results,
        "contract_family_results": family_results,
        "development_model": model,
    }
    return records


def write_experiment_artifacts(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = run_experiment()
    samples: list[DetectorSample] = records["samples"]
    excluded: list[RoleAnnotation] = records["excluded"]
    scenario_results = records["scenario_results"]
    family_results = records["contract_family_results"]
    comparison_body = comparison(scenario_results, family_results, samples, excluded)
    artifacts = {
        "README.md": readme_text(comparison_body),
        "source_field_role_labels.json": annotation_artifact(),
        "feature_schema.json": feature_schema(),
        "fold_manifest.json": fold_manifest(scenario_results, family_results),
        "scenario_out_results.json": scenario_results,
        "contract_family_out_results.json": family_results,
        "comparison.json": comparison_body,
        "failure_analysis.json": failure_analysis(scenario_results, family_results),
        "development_model.json": records["development_model"],
    }
    written: dict[str, str] = {}
    for filename, payload in artifacts.items():
        path = output_dir / filename
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8", newline="\n")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        written[path.relative_to(PROJECT_ROOT).as_posix()] = _raw_sha(path)
    return written


def grouped_evaluation(samples: list[DetectorSample], group_attr: str, group_order: tuple[str, ...]) -> dict[str, Any]:
    predictions_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fold_records: list[dict[str, Any]] = []
    for held_out in group_order:
        train = [sample for sample in samples if getattr(sample, group_attr) != held_out]
        test = [sample for sample in samples if getattr(sample, group_attr) == held_out]
        if not test:
            raise ValueError(f"No held-out samples for {held_out}")
        support = class_support(train)
        fold: dict[str, Any] = {
            "fold_id": f"leave_one_{group_attr}__{held_out}",
            "held_out_group": held_out,
            "held_out_case_count": len(test),
            "train_case_count": len(train),
            "train_class_support": support,
            "held_out_class_support": class_support(test),
            "held_out_used_for_scaler_or_model_fit": False,
            "strategies": {},
        }
        constant = fit_constant_baseline(train)
        constant_predictions = [_prediction_record(sample, "constant_prevalence_baseline", constant["probability"], constant["predicted_label"]) for sample in test]
        predictions_by_strategy["constant_prevalence_baseline"].extend(constant_predictions)
        fold["strategies"]["constant_prevalence_baseline"] = {"model": constant, "metrics": metrics(constant_predictions)}
        heuristic_predictions = [_prediction_record(sample, "simple_deterministic_heuristic", float(heuristic_predict(sample.features)), heuristic_predict(sample.features)) for sample in test]
        predictions_by_strategy["simple_deterministic_heuristic"].extend(heuristic_predictions)
        fold["strategies"]["simple_deterministic_heuristic"] = {"model": heuristic_policy(), "metrics": metrics(heuristic_predictions)}
        for feature_set_name, feature_order in LOGISTIC_FEATURE_SETS.items():
            strategy = f"value_profile_logistic_{feature_set_name}"
            if support["identifier"] == 0 or support["non_identifier"] == 0:
                fold["strategies"][strategy] = {"unsupported": True, "reason": "training fold has only one class"}
                continue
            model = fit_logistic(train, feature_order)
            fold_predictions = [
                _prediction_record(sample, strategy, predict_probability(model, sample.features), None)
                for sample in test
            ]
            fold_predictions = [
                {**item, "predicted_label": int(float(item["probability"]) >= 0.5)}
                for item in fold_predictions
            ]
            predictions_by_strategy[strategy].extend(fold_predictions)
            fold["strategies"][strategy] = {
                "feature_set": feature_set_name,
                "feature_order": list(feature_order),
                "scaler_fit_scope": "training_fold_only",
                "model_fit_scope": "training_fold_only",
                "decision_threshold": 0.5,
                "metrics": metrics(fold_predictions),
            }
        fold_records.append(fold)
    return {
        "experiment_id": EXPERIMENT_ID,
        "grouping": group_attr,
        "fold_count": len(fold_records),
        "folds": fold_records,
        "pooled": {
            strategy: {
                "metrics": metrics(predictions),
                "prediction_count": len(predictions),
                "predictions": sorted(predictions, key=lambda item: item["case_id"]),
            }
            for strategy, predictions in sorted(predictions_by_strategy.items())
        },
    }


def fit_constant_baseline(samples: list[DetectorSample]) -> dict[str, Any]:
    positives = sum(sample.label for sample in samples)
    negatives = len(samples) - positives
    majority_label = 1 if positives >= negatives else 0
    return {
        "strategy": "constant_prevalence_baseline",
        "training_positive_count": positives,
        "training_negative_count": negatives,
        "probability": round(_ratio(positives, len(samples)), 8),
        "predicted_label": majority_label,
    }


def heuristic_policy() -> dict[str, Any]:
    return {
        "strategy": "simple_deterministic_heuristic",
        "declared_before_experiment": True,
        "rule": (
            "identifier if uniqueness >= 0.90, non-null count >= 3, fixed-width or dominant pattern ratio >= 0.70, "
            "and numeric/alphanumeric/uuid/hex/leading-zero or integer sequence signal >= 0.35"
        ),
        "decision_threshold": 0.5,
    }


def heuristic_predict(features: dict[str, float]) -> int:
    shape_signal = max(features["fixed_width_ratio"], features["dominant_normalized_pattern_ratio"])
    token_signal = max(
        features["numeric_only_value_ratio"],
        features["alphanumeric_mixed_ratio"],
        features["uuid_like_ratio"],
        features["hex_like_ratio"],
        features["leading_zero_ratio"],
        features["integer_monotonicity_signal"],
        features["integer_sequentiality_signal"],
    )
    return int(
        features["non_null_count"] >= 3
        and features["uniqueness_ratio"] >= 0.90
        and shape_signal >= 0.70
        and token_signal >= 0.35
    )


def fit_logistic(samples: list[DetectorSample], feature_order: tuple[str, ...]) -> dict[str, Any]:
    x_raw, y = _matrix(samples, feature_order)
    mean, scale, x = _fit_transform(x_raw)
    weights = np.zeros(x.shape[1], dtype=float)
    intercept = _initial_intercept(y)
    for _ in range(EPOCHS):
        logits = np.clip(x @ weights + intercept, -40.0, 40.0)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        error = probabilities - y
        weights -= LEARNING_RATE * ((x.T @ error) / len(y) + L2_PENALTY * weights)
        intercept -= LEARNING_RATE * float(np.mean(error))
    return {
        "model_type": "standard_scaler_l2_logistic_regression_json_v1",
        "feature_order": list(feature_order),
        "scaler_mean": _round_list(mean),
        "scaler_scale": _round_list(scale),
        "linear_coefficients": _round_list(weights),
        "intercept": round(float(intercept), 8),
        "decision_threshold": 0.5,
        "training": {
            "case_count": len(samples),
            "positive_label_count": int(np.sum(y)),
            "negative_label_count": int(len(y) - np.sum(y)),
            "scaler_fit_scope": "provided_training_cases_only",
            "model_fit_scope": "provided_training_cases_only",
            "l2_penalty": L2_PENALTY,
            "epochs": EPOCHS,
            "learning_rate": LEARNING_RATE,
            "random_state": 0,
            "solver": "deterministic_batch_gradient_descent",
            "threshold_tuning": "none; fixed probability threshold 0.5",
        },
    }


def predict_probability(model: dict[str, Any], features: dict[str, float]) -> float:
    x = np.asarray([float(features[name]) for name in model["feature_order"]], dtype=float)
    mean = np.asarray(model["scaler_mean"], dtype=float)
    scale = np.asarray(model["scaler_scale"], dtype=float)
    weights = np.asarray(model["linear_coefficients"], dtype=float)
    logit = float(((x - mean) / scale) @ weights + float(model["intercept"]))
    return _sigmoid(logit)


def development_model(samples: list[DetectorSample]) -> dict[str, Any]:
    return {
        "_meta": {
            "experiment_id": EXPERIMENT_ID,
            "development_only": True,
            "production_promoted": False,
            "ranking_integrated": False,
            "sealed_holdout_validated": False,
            "model_format": "json",
            "pickle_used": False,
            "decision_threshold": 0.5,
        },
        "model": fit_logistic(samples, COMBINED_FEATURE_ORDER),
    }


def metrics(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [int(item["label"]) for item in predictions]
    pred_labels = [int(item["predicted_label"]) for item in predictions]
    probabilities = [float(item["probability"]) for item in predictions]
    tp = sum(1 for y, p in zip(labels, pred_labels, strict=True) if y == 1 and p == 1)
    tn = sum(1 for y, p in zip(labels, pred_labels, strict=True) if y == 0 and p == 0)
    fp = sum(1 for y, p in zip(labels, pred_labels, strict=True) if y == 0 and p == 1)
    fn = sum(1 for y, p in zip(labels, pred_labels, strict=True) if y == 1 and p == 0)
    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    specificity = _safe_divide(tn, tn + fp)
    return {
        "case_count": len(predictions),
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "accuracy": _safe_divide(tp + tn, len(predictions)),
        "balanced_accuracy": None if recall is None or specificity is None else round((recall + specificity) / 2.0, 8),
        "identifier_precision": precision,
        "identifier_recall": recall,
        "identifier_f1": _f1(precision, recall),
        "specificity": specificity,
        "roc_auc": _roc_auc(labels, probabilities),
        "average_precision": _average_precision(labels, probabilities),
        "brier_score": _brier(labels, probabilities),
    }


def comparison(
    scenario_results: dict[str, Any],
    family_results: dict[str, Any],
    samples: list[DetectorSample],
    excluded: list[RoleAnnotation],
) -> dict[str, Any]:
    labels = [sample.label for sample in samples]
    return {
        "experiment_id": EXPERIMENT_ID,
        "development_only": True,
        "production_promoted": False,
        "ranking_integrated": False,
        "sealed_holdout_validated": False,
        "task": "header_blind_source_column_identifier_role_detection",
        "case_counts": {
            "total_annotations": len(samples) + len(excluded),
            "modeled_cases": len(samples),
            "identifier": sum(labels),
            "non_identifier": len(labels) - sum(labels),
            "ambiguous_excluded": len(excluded),
            "scenario_count": len({sample.scenario_id for sample in samples}),
            "contract_family_count": len({sample.contract_family for sample in samples}),
        },
        "pooled_metrics": {
            "leave_one_scenario_out": _metrics_without_predictions(scenario_results["pooled"]),
            "leave_one_contract_family_out": _metrics_without_predictions(family_results["pooled"]),
        },
        "ablation_feature_sets": {name: list(order) for name, order in LOGISTIC_FEATURE_SETS.items()},
        "conclusion": (
            "This development-only experiment asks whether value profiles can identify identifier-role columns. "
            "It is not full schema matching, not V5 integration, and not external or sealed generalization evidence."
        ),
    }


def fold_manifest(scenario_results: dict[str, Any], family_results: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "scenario_order": list(SCENARIO_ORDER),
        "contract_family_order": list(CONTRACT_FAMILY_ORDER),
        "leakage_controls": {
            "fold_group_used_only_for_splitting": True,
            "held_out_group_used_for_scaler_fit": False,
            "held_out_group_used_for_model_fit": False,
            "field_level_random_split": False,
            "feature_schema_contains_identity_or_ground_truth": False,
        },
        "leave_one_scenario_out": _fold_summary(scenario_results),
        "leave_one_contract_family_out": _fold_summary(family_results),
    }


def failure_analysis(scenario_results: dict[str, Any], family_results: dict[str, Any]) -> dict[str, Any]:
    scenario_combined = scenario_results["pooled"]["value_profile_logistic_combined"]["predictions"]
    family_combined = family_results["pooled"]["value_profile_logistic_combined"]["predictions"]
    family_heuristic = family_results["pooled"]["simple_deterministic_heuristic"]["predictions"]
    heuristic_by_case = {item["case_id"]: item for item in family_heuristic}
    return {
        "experiment_id": EXPERIMENT_ID,
        "privacy": "Contains scenario/case/source-field identifiers for traceability, but no raw column values.",
        "leave_one_contract_family_out": {
            "false_positives": _failures(family_combined, label=0, predicted=1),
            "false_negatives": _failures(family_combined, label=1, predicted=0),
            "heuristic_logistic_disagreements": [
                {
                    "case_id": item["case_id"],
                    "scenario_id": item["scenario_id"],
                    "contract_family": item["contract_family"],
                    "source_field": item["source_field"],
                    "heuristic_prediction": heuristic_by_case[item["case_id"]]["predicted_label"],
                    "logistic_prediction": item["predicted_label"],
                    "true_label": item["label"],
                }
                for item in family_combined
                if heuristic_by_case[item["case_id"]]["predicted_label"] != item["predicted_label"]
            ],
        },
        "leave_one_scenario_out": {
            "false_positives": _failures(scenario_combined, label=0, predicted=1),
            "false_negatives": _failures(scenario_combined, label=1, predicted=0),
        },
        "per_contract_family_failure_counts": {
            family: {
                "false_positive": sum(1 for item in family_combined if item["contract_family"] == family and item["label"] == 0 and item["predicted_label"] == 1),
                "false_negative": sum(1 for item in family_combined if item["contract_family"] == family and item["label"] == 1 and item["predicted_label"] == 0),
            }
            for family in CONTRACT_FAMILY_ORDER
        },
    }


def readme_text(comparison_body: dict[str, Any]) -> str:
    loso = comparison_body["pooled_metrics"]["leave_one_scenario_out"]
    loco = comparison_body["pooled_metrics"]["leave_one_contract_family_out"]
    combined_loso = loso["value_profile_logistic_combined"]["metrics"]
    combined_loco = loco["value_profile_logistic_combined"]["metrics"]
    heuristic_loco = loco["simple_deterministic_heuristic"]["metrics"]
    return (
        "# Value Profile Identifier Detector V1\n\n"
        "This development-only experiment asks a narrow question: can aggregate source-column value profiles identify whether a source column has an identifier role? It is not full schema matching, not a ranking improvement, and not a production promotion.\n\n"
        "The detector is fully header-blind. Model inputs are numeric statistics computed from column values only; source field names, target identity, scenario id, contract family, case id, mapping ground truth, mapping scores, and labels are excluded from the feature matrix. Identifier-role labels are separate semantic annotations, not mapping target labels. A no-target column can still be an identifier if it primarily carries a key, code, or reference role.\n\n"
        "Companies House and FDIC motivated this question through published README evidence, but their data, ground truth, result artifacts, and per-case records are excluded from annotation, training, validation, and feature analysis.\n\n"
        "## Data And Labels\n\n"
        f"The experiment uses the existing development corpus only: 5 synthetic scenarios plus 2 public development scenarios, covering {comparison_body['case_counts']['total_annotations']} source-field annotations across {comparison_body['case_counts']['scenario_count']} scenarios and {comparison_body['case_counts']['contract_family_count']} contract families. Modeled labels are {comparison_body['case_counts']['identifier']} identifier and {comparison_body['case_counts']['non_identifier']} non-identifier cases; ambiguous exclusions are {comparison_body['case_counts']['ambiguous_excluded']}.\n\n"
        "## Models\n\n"
        "Three fixed strategies are compared: a constant prevalence baseline, a simple deterministic heuristic declared before running the experiment, and a StandardScaler plus deterministic L2 logistic regression with a fixed 0.5 probability threshold. The ablation is limited to distribution-only, pattern-only, and combined feature sets.\n\n"
        "## Development Results\n\n"
        f"Leave-one-scenario-out combined logistic accuracy is {_format_metric(combined_loso['accuracy'])}, balanced accuracy {_format_metric(combined_loso['balanced_accuracy'])}, identifier precision {_format_metric(combined_loso['identifier_precision'])}, identifier recall {_format_metric(combined_loso['identifier_recall'])}, and F1 {_format_metric(combined_loso['identifier_f1'])}.\n\n"
        f"Leave-one-contract-family-out combined logistic accuracy is {_format_metric(combined_loco['accuracy'])}, balanced accuracy {_format_metric(combined_loco['balanced_accuracy'])}, identifier precision {_format_metric(combined_loco['identifier_precision'])}, identifier recall {_format_metric(combined_loco['identifier_recall'])}, and F1 {_format_metric(combined_loco['identifier_f1'])}. The simple heuristic contract-family-out accuracy is {_format_metric(heuristic_loco['accuracy'])}.\n\n"
        "These grouped development metrics cannot be treated as external generalization. Even if the detector performs well, it would need a separate ranking ablation before any V5 or runtime integration could be considered. Negative results are retained as evidence.\n"
    )


def _prediction_record(sample: DetectorSample, strategy: str, probability: float, predicted_label: int | None) -> dict[str, Any]:
    prediction = int(probability >= 0.5) if predicted_label is None else int(predicted_label)
    return {
        "strategy": strategy,
        "scenario_id": sample.scenario_id,
        "contract_family": sample.contract_family,
        "case_id": sample.case_id,
        "source_field": sample.source_field,
        "label": sample.label,
        "predicted_label": prediction,
        "probability": round(float(probability), 8),
    }


def _matrix(samples: list[DetectorSample], feature_order: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray([[sample.features[name] for name in feature_order] for sample in samples], dtype=float)
    y = np.asarray([sample.label for sample in samples], dtype=float)
    return x, y


def _fit_transform(x_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = np.mean(x_raw, axis=0)
    scale = np.std(x_raw, axis=0)
    scale = np.where(scale == 0.0, 1.0, scale)
    return mean, scale, (x_raw - mean) / scale


def _initial_intercept(y: np.ndarray) -> float:
    positive = float(np.mean(y))
    clipped = min(max(positive, 1e-6), 1.0 - 1e-6)
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _round_list(values: np.ndarray) -> list[float]:
    return [round(float(value), 8) for value in values]


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else float(numerator) / float(denominator)


def _safe_divide(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else round(float(numerator) / float(denominator), 8)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return round(2.0 * precision * recall / (precision + recall), 8)


def _value_ratio(values: list[str], predicate: Any) -> float:
    return _ratio(sum(1 for value in values if predicate(value)), len(values))


def _char_fraction(values: list[str], predicate: Any, total: int) -> float:
    return _ratio(sum(1 for value in values for char in value if predicate(char)), total)


def _normalized_entropy(counts: Counter[Any]) -> float:
    total = sum(counts.values())
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = -sum((count / total) * math.log(count / total, 2) for count in counts.values())
    return entropy / math.log(len(counts), 2)


def _integer_value(value: str) -> int | None:
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _integer_monotonicity(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    comparisons = [int(second > first) for first, second in zip(values, values[1:], strict=False)]
    return _ratio(sum(comparisons), len(comparisons))


def _integer_sequentiality(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    diffs = [second - first for first, second in zip(values, values[1:], strict=False)]
    if not diffs:
        return 0.0
    common = Counter(diffs).most_common(1)[0][1]
    return _ratio(common, len(diffs))


def _roc_auc(labels: list[int], probabilities: list[float]) -> float | None:
    positives = [(probability, index) for index, (label, probability) in enumerate(zip(labels, probabilities, strict=True)) if label == 1]
    negatives = [(probability, index) for index, (label, probability) in enumerate(zip(labels, probabilities, strict=True)) if label == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive, _positive_index in positives:
        for negative, _negative_index in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return round(wins / (len(positives) * len(negatives)), 8)


def _average_precision(labels: list[int], probabilities: list[float]) -> float | None:
    positive_count = sum(labels)
    if positive_count == 0:
        return None
    sorted_pairs = sorted(zip(probabilities, labels, strict=True), key=lambda item: item[0], reverse=True)
    hits = 0
    precision_sum = 0.0
    for index, (_probability, label) in enumerate(sorted_pairs, start=1):
        if label:
            hits += 1
            precision_sum += hits / index
    return round(precision_sum / positive_count, 8)


def _brier(labels: list[int], probabilities: list[float]) -> float | None:
    if not labels:
        return None
    return round(sum((probability - label) ** 2 for label, probability in zip(labels, probabilities, strict=True)) / len(labels), 8)


def class_support(samples: list[DetectorSample]) -> dict[str, int]:
    positives = sum(sample.label for sample in samples)
    return {
        "identifier": positives,
        "non_identifier": len(samples) - positives,
    }


def _metrics_without_predictions(pooled: dict[str, Any]) -> dict[str, Any]:
    return {
        strategy: {
            "metrics": body["metrics"],
            "prediction_count": body["prediction_count"],
        }
        for strategy, body in pooled.items()
    }


def _fold_summary(results: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "fold_id": fold["fold_id"],
            "held_out_group": fold["held_out_group"],
            "train_case_count": fold["train_case_count"],
            "held_out_case_count": fold["held_out_case_count"],
            "train_class_support": fold["train_class_support"],
            "held_out_class_support": fold["held_out_class_support"],
            "held_out_used_for_scaler_or_model_fit": fold["held_out_used_for_scaler_or_model_fit"],
        }
        for fold in results["folds"]
    ]


def _failures(predictions: list[dict[str, Any]], *, label: int, predicted: int) -> list[dict[str, Any]]:
    return [
        {
            "case_id": item["case_id"],
            "scenario_id": item["scenario_id"],
            "contract_family": item["contract_family"],
            "source_field": item["source_field"],
            "probability": item["probability"],
        }
        for item in predictions
        if item["label"] == label and item["predicted_label"] == predicted
    ]


def _format_metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _feature_formula(name: str) -> str:
    formulas = {
        "row_count": "number of rows in the source column",
        "non_null_count": "count of non-empty string values",
        "null_ratio": "empty value count / row count",
        "distinct_count": "count of distinct non-empty values",
        "uniqueness_ratio": "distinct non-empty values / non-empty count",
        "duplicate_ratio": "1 - uniqueness ratio",
        "normalized_value_frequency_entropy": "Shannon entropy over value frequencies divided by log2(distinct value count)",
        "mean_value_length": "mean character length of non-empty values",
        "value_length_stddev": "population standard deviation of non-empty value lengths",
        "value_length_coefficient_of_variation": "length stddev / mean length",
        "dominant_length_ratio": "most common non-empty value length frequency / non-empty count",
        "numeric_only_value_ratio": "non-empty values matching [0-9]+ / non-empty count",
        "alphabetic_only_value_ratio": "non-empty values matching [A-Za-z]+ / non-empty count",
        "alphanumeric_mixed_ratio": "non-empty alphanumeric values containing at least one letter and one digit / non-empty count",
        "digit_character_fraction": "digit characters / all non-empty value characters",
        "alphabetic_character_fraction": "alphabetic characters / all non-empty value characters",
        "punctuation_character_fraction": "non-alnum non-space characters / all non-empty value characters",
        "whitespace_character_fraction": "whitespace characters / all non-empty value characters",
        "leading_zero_ratio": "values beginning with 0 and containing a digit / non-empty count",
        "uuid_like_ratio": "UUID-shaped values / non-empty count",
        "hex_like_ratio": "long hex-like mixed letter/digit values / non-empty count",
        "fixed_width_ratio": "most common value length frequency / non-empty count",
        "dominant_normalized_pattern_ratio": "most common compressed value pattern frequency / non-empty count",
        "normalized_pattern_entropy": "Shannon entropy over compressed value patterns divided by log2(distinct pattern count)",
        "integer_monotonicity_signal": "share of adjacent numeric-only values that increase",
        "integer_sequentiality_signal": "share of adjacent numeric-only differences equal to the dominant difference",
    }
    return formulas[name]


def _raw_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the header-blind identifier role detector experiment.")
    parser.add_argument("--write-artifacts", action="store_true", help="Write deterministic development artifacts.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    if not args.write_artifacts:
        parser.error("--write-artifacts is required")
    written = write_experiment_artifacts(args.output_dir)
    print(json.dumps({"experiment_id": EXPERIMENT_ID, "written": written}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
