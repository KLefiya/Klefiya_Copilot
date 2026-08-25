from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "companies_house_customer_external_v1"
PROTOCOL_ID = "companies_house_customer_external_holdout"
PROTOCOL_VERSION = "v1"
BASELINE_COMMIT = "23add9d90fe93c590f32e946f471fb929cb88ac3"
FIXTURE_DIR = PROJECT_ROOT / "data/benchmarks/external/companies_house_customer_v1"
SOURCE_PATH = FIXTURE_DIR / "source_companies_house_customer.csv"
GROUND_TRUTH_PATH = FIXTURE_DIR / "ground_truth.json"
PROVENANCE_PATH = FIXTURE_DIR / "source_provenance.json"
PROTOCOL_PATH = FIXTURE_DIR / "protocol_lock.json"
README_PATH = FIXTURE_DIR / "README.md"
SCRIPT_PATH = PROJECT_ROOT / "scripts/prepare_companies_house_external_fixture.py"
CONTRACT_PATH = PROJECT_ROOT / "contracts/generic_customer/datapackage.yaml"
EXPECTED_CONTRACT_GIT_BLOB_SHA = "8fe32d08f23a2c97dedea8d43d37d96925003766acbd4f69326b7646b90da792"
ARCHIVE_URL = "https://download.companieshouse.gov.uk/BasicCompanyData-2026-08-01-part1_7.zip"
ARCHIVE_FILENAME = "BasicCompanyData-2026-08-01-part1_7.zip"
CSV_MEMBER = "BasicCompanyData-2026-08-01-part1_7.csv"
SNAPSHOT_DATE = "2026-08-01"
PART = "part1_7"
SELECTION_SEED = "companies_house_customer_v1|"
PRODUCT_URL = "https://www.gov.uk/guidance/companies-house-data-products"
DOWNLOAD_PAGE = "https://download.companieshouse.gov.uk/"
LICENSE_NAME = "Open Government Licence v3.0"
LICENSE_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
ATTRIBUTION = "Contains Companies House information licensed under the Open Government Licence v3.0."

ALLOWED_COLUMNS = [
    "CompanyName",
    "CompanyNumber",
    "RegAddress.Country",
    "CompanyCategory",
    "CompanyStatus",
    "IncorporationDate",
    "Accounts.AccountCategory",
    "Accounts.NextDueDate",
    "SICCode.SicText_1",
    "ConfStmtNextDueDate",
    "Mortgages.NumMortCharges",
    "URI",
]

NO_TARGET_FIELDS = [
    "CompanyCategory",
    "CompanyStatus",
    "IncorporationDate",
    "Accounts.AccountCategory",
    "Accounts.NextDueDate",
    "SICCode.SicText_1",
    "ConfStmtNextDueDate",
    "Mortgages.NumMortCharges",
    "URI",
]

TARGET_ALLOWLIST = [
    "customer.customer_id",
    "customer.customer_name",
    "customer.country",
    "customer.email",
    "customer.phone",
    "customer.tax_number",
    "customer.payment_terms",
    "customer_bank.bank_id",
    "customer_bank.customer_id",
    "customer_bank.iban",
    "customer_bank.currency",
]

SCORER_SOURCE_FILES = {
    "baseline": "src/core/mapping/scorer.py",
    "precision_tiered_v4": "src/core/mapping/scorer_v4.py",
    "precision_tiered_v5": "src/core/mapping/scorer_v5.py",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=PROJECT_ROOT)


def validate_contract() -> dict[str, Any]:
    blob = git_blob("contracts/generic_customer/datapackage.yaml")
    raw = CONTRACT_PATH.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    orphan_cr_count = sum(1 for index, byte in enumerate(raw) if byte == 13 and (index + 1 >= len(raw) or raw[index + 1] != 10))
    blob_sha = sha256_bytes(blob)
    if blob_sha != EXPECTED_CONTRACT_GIT_BLOB_SHA:
        raise RuntimeError(f"contract git blob SHA mismatch: {blob_sha}")
    if lf != blob:
        raise RuntimeError("contract worktree differs from git blob by more than CRLF/LF")
    if orphan_cr_count:
        raise RuntimeError("contract worktree contains orphan CR bytes")
    diff = subprocess.run(["git", "diff", "--exit-code", "--", "contracts/generic_customer/datapackage.yaml"], cwd=PROJECT_ROOT)
    if diff.returncode != 0:
        raise RuntimeError("contract has tracked diff")
    return {
        "target_contract_path": "contracts/generic_customer/datapackage.yaml",
        "target_contract_git_blob_content_sha256": blob_sha,
        "target_contract_worktree_raw_sha256": sha256_bytes(raw),
        "target_contract_worktree_lf_normalized_sha256": sha256_bytes(lf),
        "target_contract_worktree_differs_only_by_crlf": True,
        "target_contract_orphan_cr_count": orphan_cr_count,
    }


class CompaniesHouseRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.hostname != "download.companieshouse.gov.uk":
            raise RuntimeError(f"redirected outside Companies House download domain: {newurl}")
        if Path(parsed.path).name != ARCHIVE_FILENAME:
            raise RuntimeError(f"redirected to unexpected archive filename: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_archive_url() -> None:
    parsed = urllib.parse.urlparse(ARCHIVE_URL)
    if parsed.scheme != "https":
        raise RuntimeError("archive URL must use https")
    if parsed.hostname != "download.companieshouse.gov.uk":
        raise RuntimeError("archive URL is not on download.companieshouse.gov.uk")
    if Path(parsed.path).name != ARCHIVE_FILENAME:
        raise RuntimeError("archive filename does not match the preregistered snapshot")


def download_archive(temp_dir: Path) -> tuple[Path, dict[str, Any]]:
    validate_archive_url()
    archive_path = temp_dir / ARCHIVE_FILENAME
    opener = urllib.request.build_opener(CompaniesHouseRedirectHandler)
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "carveops-fixture-preparation/1.0"})
    with opener.open(request, timeout=120) as response:
        final_url = response.geturl()
        parsed = urllib.parse.urlparse(final_url)
        if parsed.hostname != "download.companieshouse.gov.uk":
            raise RuntimeError(f"response URL is outside Companies House download domain: {final_url}")
        if Path(parsed.path).name != ARCHIVE_FILENAME:
            raise RuntimeError(f"response filename does not match preregistered snapshot: {final_url}")
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
        with archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)
    first_bytes = archive_path.read_bytes()[:4]
    if first_bytes[:2] != b"PK" or not zipfile.is_zipfile(archive_path):
        raise RuntimeError(f"downloaded response is not a ZIP archive; content type was {content_type!r}")
    return archive_path, {
        "response_status": status,
        "response_content_type": content_type,
        "response_final_url": final_url,
    }


def extract_member(archive_path: Path, temp_dir: Path) -> tuple[Path, dict[str, Any]]:
    member_path = temp_dir / CSV_MEMBER
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if CSV_MEMBER not in names:
            raise RuntimeError(f"expected CSV member missing: {CSV_MEMBER}; archive contains {names[:10]}")
        info = archive.getinfo(CSV_MEMBER)
        if info.is_dir():
            raise RuntimeError("expected CSV member is a directory")
        digest = hashlib.sha256()
        size = 0
        with archive.open(info, "r") as source, member_path.open("wb") as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
    return member_path, {
        "selected_csv_member_name": CSV_MEMBER,
        "selected_member_byte_size": size,
        "selected_member_raw_sha256": digest.hexdigest(),
    }


def row_hash(company_number: str) -> str:
    return hashlib.sha256((SELECTION_SEED + company_number).encode("utf-8")).hexdigest()


def normalize_header(raw_header: str) -> str:
    return raw_header.strip(" ")


def build_header_mapping(raw_headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for raw_header in raw_headers:
        logical_header = normalize_header(raw_header)
        if logical_header in mapping:
            collisions.setdefault(logical_header, [mapping[logical_header]]).append(raw_header)
        else:
            mapping[logical_header] = raw_header
    if collisions:
        raise RuntimeError(f"Companies House CSV header normalization collisions: {collisions}")
    missing = [column for column in ALLOWED_COLUMNS if column not in mapping]
    if missing:
        raise RuntimeError(f"Companies House CSV missing required logical columns after ASCII-space header normalization: {missing}; actual raw header: {raw_headers}")
    changed = {logical: raw for logical, raw in mapping.items() if logical != raw}
    for logical, raw in changed.items():
        if logical != raw.strip(" "):
            raise RuntimeError(f"header requires a non-ASCII-space transformation: {raw!r} -> {logical!r}")
    return {column: mapping[column] for column in ALLOWED_COLUMNS}


def select_rows(member_path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    by_company_number: dict[str, dict[str, str]] = {}
    with member_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RuntimeError("Companies House CSV has no header")
        raw_headers = list(reader.fieldnames)
        header_mapping = build_header_mapping(raw_headers)
        for row in reader:
            company_name = row.get(header_mapping["CompanyName"], "")
            company_number = row.get(header_mapping["CompanyNumber"], "")
            country = row.get(header_mapping["RegAddress.Country"], "")
            if not company_name:
                continue
            if not company_number:
                continue
            if not country:
                continue
            if company_number in by_company_number:
                continue
            by_company_number[company_number] = {column: row.get(raw_header, "") for column, raw_header in header_mapping.items()}
    if len(by_company_number) < 128:
        raise RuntimeError(f"only {len(by_company_number)} eligible records found; need 128")
    selected = sorted(by_company_number.values(), key=lambda row: (row_hash(row["CompanyNumber"]), row["CompanyNumber"]))[:128]
    header_info = {
        "raw_header": raw_headers,
        "allowed_raw_to_logical_header_mapping": [{"raw": header_mapping[column], "logical": column} for column in ALLOWED_COLUMNS],
        "normalization_rule": "strip ASCII space (0x20) from header boundaries only",
        "normalization_collision_count": 0,
        "normalization_frozen_before_first_evaluation": True,
        "normalization_reads_target_aliases_ground_truth_or_predictions": False,
        "source_values_normalized": False,
    }
    return selected, header_info


def write_source(rows: list[dict[str, str]]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with SOURCE_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALLOWED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def ground_truth() -> dict[str, Any]:
    cases = [
        {
            "source_field": "CompanyName",
            "case_type": "single_target",
            "expected_targets": ["customer.customer_name"],
            "notes": "Legal or trading company name maps at schema level to the synthetic customer name field.",
        },
        {
            "source_field": "CompanyNumber",
            "case_type": "single_target",
            "expected_targets": ["customer.customer_id"],
            "notes": "Companies House company number is the stable public company identifier for this source fixture; it must not be mapped to customer.tax_number or customer_bank.customer_id.",
        },
        {
            "source_field": "RegAddress.Country",
            "case_type": "single_target",
            "expected_targets": ["customer.country"],
            "notes": "Schema-level semantic match. Source values may require normalization into the target contract's two-letter country codes; matching correctness does not imply raw-copy compatibility.",
        },
    ]
    cases.extend(
        {
            "source_field": field,
            "case_type": "no_target",
            "expected_targets": [],
            "notes": "No target field is preregistered for this Companies House source field in the existing synthetic generic customer contract.",
        }
        for field in NO_TARGET_FIELDS
    )
    return {
        "metadata": {
            "purpose": "evaluation_only",
            "scenario_id": SCENARIO_ID,
            "real_public_source_data": True,
            "synthetic_source_data": False,
            "target_contract_is_synthetic_reference": True,
            "must_not_be_read_by_mapping_engine": True,
            "frozen_before_first_evaluation": True,
            "first_evaluation_run": False,
            "production_validation": False,
        },
        "source_fields": ALLOWED_COLUMNS,
        "target_contract_path": "contracts/generic_customer/datapackage.yaml",
        "target_allowlist": TARGET_ALLOWLIST,
        "cases": cases,
        "counts": {
            "single_target_cases": 3,
            "multi_target_cases": 0,
            "no_target_cases": 9,
            "target_links": 3,
        },
        "forbidden_mappings": [
            {"source_field": "CompanyNumber", "target": "customer.tax_number"},
            {"source_field": "CompanyNumber", "target": "customer_bank.customer_id"},
            {"source_field": "CompanyStatus", "target": "customer.payment_terms"},
            {"source_field": "SICCode.SicText_1", "target": "customer.customer_name"},
            {"source_field": "URI", "target": "customer.email"},
        ],
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def write_ground_truth() -> None:
    write_json(GROUND_TRUTH_PATH, ground_truth())


def selection_rules() -> list[str]:
    return [
        "Stream the official CSV member without loading the full dataset into memory.",
        "Require CompanyName, CompanyNumber, and RegAddress.Country to be non-empty.",
        "Deduplicate by exact CompanyNumber, keeping the first encountered official row.",
        "Compute SHA256('companies_house_customer_v1|' + CompanyNumber) using the original CompanyNumber text.",
        "Sort by that hash ascending, then CompanyNumber ascending.",
        "Select the first 128 rows.",
        "Write rows in the selected order with exactly the 12 allowlisted source columns.",
        "Preserve official source values without case changes, filling, semantic cleaning, value mapping, or manual edits.",
        "Write UTF-8 CSV with LF line endings and deterministic CSV quoting.",
    ]


def write_provenance(archive_path: Path, archive_info: dict[str, Any], member_info: dict[str, Any], header_info: dict[str, Any]) -> None:
    provenance = {
        "provider": "Companies House",
        "product_title": "Companies House Free Company Data Product",
        "official_product_url": PRODUCT_URL,
        "official_download_page": DOWNLOAD_PAGE,
        "exact_download_url": ARCHIVE_URL,
        "snapshot_date": SNAPSHOT_DATE,
        "snapshot_part": PART,
        "archive_filename": ARCHIVE_FILENAME,
        "retrieval_utc_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "license_name": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "required_attribution": ATTRIBUTION,
        "archive_byte_size": archive_path.stat().st_size,
        "archive_raw_sha256": sha256_file(archive_path),
        "source_universe_limited_to_snapshot_part": PART,
        "selection_seed_string": SELECTION_SEED,
        "selection_filter_order_rules": selection_rules(),
        "allowed_columns": ALLOWED_COLUMNS,
        "raw_header": header_info["raw_header"],
        "allowed_raw_to_logical_header_mapping": header_info["allowed_raw_to_logical_header_mapping"],
        "header_normalization_rule": header_info["normalization_rule"],
        "header_normalization_collision_count": header_info["normalization_collision_count"],
        "header_normalization_frozen_before_first_evaluation": header_info["normalization_frozen_before_first_evaluation"],
        "header_normalization_reads_target_aliases_ground_truth_or_predictions": header_info["normalization_reads_target_aliases_ground_truth_or_predictions"],
        "source_values_normalized": header_info["source_values_normalized"],
        "excluded_personal_address_categories": [
            "full registered office address lines",
            "care-of and PO box",
            "town, county, and postcode",
            "previous company names",
            "officer, director, PSC, or natural-person fields",
            "phone",
            "email",
            "all non-allowlisted source columns",
        ],
        "selected_row_count": 128,
        "final_column_count": 12,
        "final_source_fixture_path": "data/benchmarks/external/companies_house_customer_v1/source_companies_house_customer.csv",
        "final_source_fixture_raw_sha256": sha256_file(SOURCE_PATH),
        "preparation_script_path": "scripts/prepare_companies_house_external_fixture.py",
        "preparation_script_raw_sha256": sha256_file(SCRIPT_PATH),
        "transformations_performed": [
            "column minimization to the preregistered 12 source fields",
            "eligibility filtering on non-empty CompanyName, CompanyNumber, and RegAddress.Country",
            "deterministic deduplication and hash ordering",
            "CSV serialization as UTF-8 with LF line endings",
        ],
        "source_values_manually_edited": False,
        "local_paths_recorded": False,
    }
    provenance.update(archive_info)
    provenance.update(member_info)
    write_json(PROVENANCE_PATH, provenance)


def scorer_source_hashes() -> dict[str, dict[str, str]]:
    return {
        scorer_name: {
            "path": path,
            "raw_sha256": sha256_file(PROJECT_ROOT / path),
        }
        for scorer_name, path in SCORER_SOURCE_FILES.items()
    }


def write_protocol(contract_info: dict[str, Any], header_info: dict[str, Any]) -> None:
    protocol = {
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "scenario_id": SCENARIO_ID,
        "freeze_utc_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "algorithm_baseline_commit": BASELINE_COMMIT,
        "source_fixture": {
            "path": "data/benchmarks/external/companies_house_customer_v1/source_companies_house_customer.csv",
            "raw_sha256": sha256_file(SOURCE_PATH),
        },
        "ground_truth": {
            "path": "data/benchmarks/external/companies_house_customer_v1/ground_truth.json",
            "raw_sha256": sha256_file(GROUND_TRUTH_PATH),
        },
        "source_provenance": {
            "path": "data/benchmarks/external/companies_house_customer_v1/source_provenance.json",
            "raw_sha256": sha256_file(PROVENANCE_PATH),
        },
        "preparation_script": {
            "path": "scripts/prepare_companies_house_external_fixture.py",
            "raw_sha256": sha256_file(SCRIPT_PATH),
        },
        "target_contract": contract_info,
        "source_field_order": ALLOWED_COLUMNS,
        "allowed_raw_to_logical_header_mapping": header_info["allowed_raw_to_logical_header_mapping"],
        "header_normalization_rule": header_info["normalization_rule"],
        "header_normalization_collision_count": header_info["normalization_collision_count"],
        "header_normalization_frozen_before_first_evaluation": header_info["normalization_frozen_before_first_evaluation"],
        "header_normalization_reads_target_aliases_ground_truth_or_predictions": header_info["normalization_reads_target_aliases_ground_truth_or_predictions"],
        "source_values_normalized": header_info["source_values_normalized"],
        "ground_truth_case_count": 12,
        "positive_single_target_case_count": 3,
        "multi_target_case_count": 0,
        "no_target_case_count": 9,
        "expected_target_link_count": 3,
        "planned_scorers": ["baseline", "precision_tiered_v4", "precision_tiered_v5"],
        "scorer_source_file_hashes": scorer_source_hashes(),
        "planned_metrics": [
            "single-target Top-1",
            "target-link Recall@1",
            "target-link Recall@3",
            "MRR",
            "no-target accuracy",
            "coverage/review status counts",
        ],
        "first_evaluation_status": "not_run",
        "evaluation_artifact": None,
        "evaluation_output": None,
        "freeze_rules": [
            "Do not change source, ground truth, contract, scorer, aliases, weights, or thresholds after viewing first evaluation results.",
            "Future fixes require a new protocol/version and cannot rewrite this result.",
        ],
        "notes": [
            "Real public source data from Companies House monthly Free Company Data Product, limited to snapshot part1_7.",
            "Pre-existing synthetic reference target contract; not production customer validation and not evidence that the target contract is authoritative.",
            "This is not a representative sample of all UK companies.",
            "The protocol lock intentionally does not record its own SHA-256.",
        ],
    }
    write_json(PROTOCOL_PATH, protocol)


def write_readme() -> None:
    README_PATH.write_text(
        "\n".join(
            [
                "# Companies House Customer External Holdout v1",
                "",
                "This preregistered Phase A fixture uses real public company register data from the Companies House Free Company Data Product.",
                "",
                f"- Official product page: {PRODUCT_URL}",
                f"- Download page: {DOWNLOAD_PAGE}",
                f"- Snapshot: {SNAPSHOT_DATE}",
                f"- Source file: {ARCHIVE_FILENAME}",
                f"- Source universe: monthly file {PART} only, not all seven parts.",
                f"- Licence: {LICENSE_NAME} ({LICENSE_URL})",
                f"- Attribution: {ATTRIBUTION}",
                "",
                "The committed source fixture is a deterministic 128 x 12 minimized subset. It keeps only non-person source fields from the preregistered allowlist and excludes full address fields, postcode, previous names, officer/director/PSC/person fields, phone, email, and all other source columns.",
                "",
                "Header normalization is frozen before first evaluation: strip ASCII space (0x20) from header boundaries only. The 12 allowed raw-to-logical mappings are recorded in `source_provenance.json` and `protocol_lock.json`; this normalization does not read target aliases, ground truth, predictions, or evaluation results. Source values are not normalized, stripped, cleaned, or manually edited.",
                "",
                "The target contract is the project's existing synthetic generic customer reference contract. This fixture is not private production customer data, not production customer validation, not evidence that the synthetic target contract is authoritative, and not a representative sample of all UK companies.",
                "",
                "First evaluation has not been run. Do not run any schema matching scorer, model inference, candidate generation, or evaluator before the frozen source, ground truth, provenance, and protocol are reviewed.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def prepare() -> dict[str, Any]:
    contract_info = validate_contract()
    temp_dir = Path(tempfile.mkdtemp(prefix="companies-house-fixture-"))
    try:
        archive_path, archive_info = download_archive(temp_dir)
        member_path, member_info = extract_member(archive_path, temp_dir)
        rows, header_info = select_rows(member_path)
        write_source(rows)
        write_ground_truth()
        write_provenance(archive_path, archive_info, member_info, header_info)
        write_protocol(contract_info, header_info)
        write_readme()
        return {
            "fixture_dir": "data/benchmarks/external/companies_house_customer_v1",
            "archive_filename": ARCHIVE_FILENAME,
            "archive_byte_size": archive_path.stat().st_size,
            "archive_raw_sha256": sha256_file(archive_path),
            "csv_member_name": CSV_MEMBER,
            "csv_member_byte_size": member_info["selected_member_byte_size"],
            "csv_member_raw_sha256": member_info["selected_member_raw_sha256"],
            "source_rows": len(rows),
            "source_columns": len(ALLOWED_COLUMNS),
            "source_raw_sha256": sha256_file(SOURCE_PATH),
            "ground_truth_raw_sha256": sha256_file(GROUND_TRUTH_PATH),
            "source_provenance_raw_sha256": sha256_file(PROVENANCE_PATH),
            "protocol_lock_raw_sha256": sha256_file(PROTOCOL_PATH),
            "preparation_script_raw_sha256": sha256_file(SCRIPT_PATH),
            "target_contract_git_blob_content_sha256": contract_info["target_contract_git_blob_content_sha256"],
            "temporary_directory_removed": False,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    result = prepare()
    result["temporary_directory_removed"] = True
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
