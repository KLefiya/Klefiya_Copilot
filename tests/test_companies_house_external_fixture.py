from __future__ import annotations

import ast
import csv
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import prepare_companies_house_external_fixture as prepare_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "data/benchmarks/external/companies_house_customer_v1"
SOURCE_PATH = FIXTURE_DIR / "source_companies_house_customer.csv"
GROUND_TRUTH_PATH = FIXTURE_DIR / "ground_truth.json"
PROVENANCE_PATH = FIXTURE_DIR / "source_provenance.json"
PROTOCOL_PATH = FIXTURE_DIR / "protocol_lock.json"
README_PATH = FIXTURE_DIR / "README.md"
SCRIPT_PATH = PROJECT_ROOT / "scripts/prepare_companies_house_external_fixture.py"
CONTRACT_PATH = PROJECT_ROOT / "contracts/generic_customer/datapackage.yaml"
EXPECTED_COLUMNS = [
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
FORBIDDEN_COLUMNS = {
    "RegAddress.AddressLine1",
    "RegAddress.AddressLine2",
    "RegAddress.PostTown",
    "RegAddress.County",
    "RegAddress.PostCode",
    "PreviousName_1.CompanyName",
    "PreviousName_1.CONDATE",
    "OfficerName",
    "DirectorName",
    "PSCName",
    "Email",
    "Phone",
}
EXPECTED_CONTRACT_GIT_BLOB_SHA = "8fe32d08f23a2c97dedea8d43d37d96925003766acbd4f69326b7646b90da792"
EXPECTED_V4_SHA = "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b"
EXPECTED_V5_SHA = "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_blob_sha(path: str) -> str:
    return hashlib.sha256(subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=PROJECT_ROOT)).hexdigest()


class CompaniesHouseExternalFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with SOURCE_PATH.open("r", encoding="utf-8", newline="") as handle:
            cls.rows = list(csv.DictReader(handle))
        cls.ground_truth = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
        cls.provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        cls.protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))

    def test_01_source_shape_header_and_required_fields(self) -> None:
        self.assertEqual(len(self.rows), 128)
        self.assertEqual(list(self.rows[0].keys()), EXPECTED_COLUMNS)
        for row in self.rows:
            self.assertNotEqual(row["CompanyName"], "")
            self.assertNotEqual(row["CompanyNumber"], "")
            self.assertNotEqual(row["RegAddress.Country"], "")

    def test_02_company_numbers_are_unique_and_deterministic_order(self) -> None:
        company_numbers = [row["CompanyNumber"] for row in self.rows]
        self.assertEqual(len(company_numbers), len(set(company_numbers)))
        expected = sorted(
            self.rows,
            key=lambda row: (
                hashlib.sha256(("companies_house_customer_v1|" + row["CompanyNumber"]).encode("utf-8")).hexdigest(),
                row["CompanyNumber"],
            ),
        )
        self.assertEqual([row["CompanyNumber"] for row in self.rows], [row["CompanyNumber"] for row in expected])

    def test_03_forbidden_personal_and_full_address_fields_are_absent(self) -> None:
        header = set(self.rows[0].keys())
        self.assertFalse(header & FORBIDDEN_COLUMNS)
        self.assertEqual(header, set(EXPECTED_COLUMNS))

    def test_04_ground_truth_cases_counts_and_mappings(self) -> None:
        cases = {case["source_field"]: case for case in self.ground_truth["cases"]}
        self.assertEqual(set(cases), set(EXPECTED_COLUMNS))
        self.assertEqual(self.ground_truth["counts"], {
            "single_target_cases": 3,
            "multi_target_cases": 0,
            "no_target_cases": 9,
            "target_links": 3,
        })
        self.assertEqual(cases["CompanyName"]["expected_targets"], ["customer.customer_name"])
        self.assertEqual(cases["CompanyNumber"]["expected_targets"], ["customer.customer_id"])
        self.assertEqual(cases["RegAddress.Country"]["expected_targets"], ["customer.country"])
        self.assertIn("normalization", cases["RegAddress.Country"]["notes"])
        for field in EXPECTED_COLUMNS[3:]:
            self.assertEqual(cases[field]["case_type"], "no_target")
            self.assertEqual(cases[field]["expected_targets"], [])

    def test_05_ground_truth_metadata_and_target_allowlist(self) -> None:
        metadata = self.ground_truth["metadata"]
        self.assertEqual(metadata["purpose"], "evaluation_only")
        self.assertEqual(metadata["scenario_id"], "companies_house_customer_external_v1")
        self.assertTrue(metadata["real_public_source_data"])
        self.assertFalse(metadata["synthetic_source_data"])
        self.assertTrue(metadata["target_contract_is_synthetic_reference"])
        self.assertTrue(metadata["must_not_be_read_by_mapping_engine"])
        self.assertTrue(metadata["frozen_before_first_evaluation"])
        self.assertFalse(metadata["first_evaluation_run"])
        self.assertFalse(metadata["production_validation"])
        allowlist = set(self.ground_truth["target_allowlist"])
        for case in self.ground_truth["cases"]:
            self.assertTrue(set(case["expected_targets"]).issubset(allowlist))

    def test_06_contract_git_blob_sha_and_protocol_state(self) -> None:
        self.assertEqual(git_blob_sha("contracts/generic_customer/datapackage.yaml"), EXPECTED_CONTRACT_GIT_BLOB_SHA)
        self.assertEqual(self.protocol["target_contract"]["target_contract_git_blob_content_sha256"], EXPECTED_CONTRACT_GIT_BLOB_SHA)
        self.assertEqual(self.protocol["first_evaluation_status"], "not_run")
        self.assertIsNone(self.protocol["evaluation_artifact"])
        self.assertIsNone(self.protocol["evaluation_output"])
        self.assertNotIn("protocol_lock_raw_sha256", self.protocol)

    def test_07_provenance_and_protocol_hashes_match_files(self) -> None:
        self.assertEqual(self.provenance["final_source_fixture_raw_sha256"], sha256_file(SOURCE_PATH))
        self.assertEqual(self.provenance["preparation_script_raw_sha256"], sha256_file(SCRIPT_PATH))
        self.assertEqual(self.protocol["source_fixture"]["raw_sha256"], sha256_file(SOURCE_PATH))
        self.assertEqual(self.protocol["ground_truth"]["raw_sha256"], sha256_file(GROUND_TRUTH_PATH))
        self.assertEqual(self.protocol["source_provenance"]["raw_sha256"], sha256_file(PROVENANCE_PATH))
        self.assertEqual(self.protocol["preparation_script"]["raw_sha256"], sha256_file(SCRIPT_PATH))
        self.assertRegex(self.provenance["archive_raw_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(self.provenance["selected_member_raw_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(self.provenance["archive_byte_size"], 0)
        self.assertGreater(self.provenance["selected_member_byte_size"], 0)

    def test_08_no_local_paths_or_generated_evaluation_outputs(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in [GROUND_TRUTH_PATH, PROVENANCE_PATH, PROTOCOL_PATH])
        self.assertNotIn(str(PROJECT_ROOT), text)
        self.assertNotRegex(text, r"(?<![A-Za-z])[A-Za-z]:[\\/]")
        forbidden_names = {
            "mapping_report.json",
            "evaluation_result.json",
            "scorer_output.json",
            "benchmark_result.json",
        }
        self.assertFalse({path.name for path in FIXTURE_DIR.iterdir()} & forbidden_names)

    def test_09_preparation_script_does_not_import_or_call_mapping_engines(self) -> None:
        tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
        forbidden_imports = ("src.core.mapping", "backend.mapping_jobs", "sentence_transformers")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith(forbidden_imports), alias.name)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertFalse(module.startswith(forbidden_imports), module)
            if isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                self.assertNotIn(name, {"suggest_runtime_contract_mappings", "evaluate_schema_matching", "run_benchmark"})

    def test_10_provenance_readme_and_protocol_content(self) -> None:
        self.assertEqual(self.provenance["provider"], "Companies House")
        self.assertEqual(self.provenance["snapshot_part"], "part1_7")
        self.assertEqual(self.provenance["selected_row_count"], 128)
        self.assertEqual(self.provenance["final_column_count"], 12)
        self.assertFalse(self.provenance["source_values_manually_edited"])
        self.assertFalse(self.provenance["source_values_normalized"])
        self.assertEqual(self.provenance["allowed_columns"], EXPECTED_COLUMNS)
        expected_mapping = [{"raw": column, "logical": column} for column in EXPECTED_COLUMNS]
        expected_mapping[1] = {"raw": " CompanyNumber", "logical": "CompanyNumber"}
        self.assertEqual(self.provenance["allowed_raw_to_logical_header_mapping"], expected_mapping)
        self.assertEqual(self.protocol["allowed_raw_to_logical_header_mapping"], expected_mapping)
        self.assertEqual(self.provenance["header_normalization_rule"], "strip ASCII space (0x20) from header boundaries only")
        self.assertEqual(self.protocol["header_normalization_rule"], "strip ASCII space (0x20) from header boundaries only")
        self.assertTrue(self.provenance["header_normalization_frozen_before_first_evaluation"])
        self.assertFalse(self.provenance["header_normalization_reads_target_aliases_ground_truth_or_predictions"])
        self.assertEqual(self.protocol["source_field_order"], EXPECTED_COLUMNS)
        self.assertEqual(self.protocol["positive_single_target_case_count"], 3)
        self.assertEqual(self.protocol["multi_target_case_count"], 0)
        self.assertEqual(self.protocol["no_target_case_count"], 9)
        self.assertEqual(self.protocol["expected_target_link_count"], 3)
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn("Contains Companies House information licensed under the Open Government Licence v3.0.", readme)
        self.assertIn("part1_7", readme)
        self.assertIn("First evaluation has not been run", readme)
        self.assertIn("strip ASCII space (0x20) from header boundaries only", readme)

    def test_11_header_normalization_is_ascii_space_only_and_collision_safe(self) -> None:
        raw_headers = EXPECTED_COLUMNS.copy()
        raw_headers[1] = " CompanyNumber"
        mapping = prepare_fixture.build_header_mapping(raw_headers)
        self.assertEqual(mapping["CompanyNumber"], " CompanyNumber")

        for bad_header in ["\tCompanyNumber", "\u00a0CompanyNumber", "\nCompanyNumber", "companynumber", "Company-Number"]:
            with self.subTest(bad_header=bad_header):
                headers = EXPECTED_COLUMNS.copy()
                headers[1] = bad_header
                with self.assertRaisesRegex(RuntimeError, "missing required logical columns"):
                    prepare_fixture.build_header_mapping(headers)

        with self.assertRaisesRegex(RuntimeError, "normalization collisions"):
            prepare_fixture.build_header_mapping(["CompanyNumber", " CompanyNumber"])

    def test_12_source_values_are_not_stripped_by_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.csv"
            raw_headers = EXPECTED_COLUMNS.copy()
            raw_headers[1] = " CompanyNumber"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=raw_headers, lineterminator="\n")
                writer.writeheader()
                for index in range(128):
                    row = {header: "" for header in raw_headers}
                    row["CompanyName"] = f" Company {index:03d} "
                    row[" CompanyNumber"] = f" {index:06d} "
                    row["RegAddress.Country"] = " England "
                    writer.writerow(row)
            rows, header_info = prepare_fixture.select_rows(path)
        self.assertEqual(header_info["allowed_raw_to_logical_header_mapping"][1], {"raw": " CompanyNumber", "logical": "CompanyNumber"})
        self.assertIn(" Company 000 ", {row["CompanyName"] for row in rows})
        self.assertIn(" 000000 ", {row["CompanyNumber"] for row in rows})
        self.assertIn(" England ", {row["RegAddress.Country"] for row in rows})

    def test_13_formal_artifacts_and_v4_v5_hashes_are_unchanged(self) -> None:
        import scripts.verify_formal_artifacts_immutable as formal

        self.assertEqual(len(formal.FORMAL_ARTIFACTS), 45)
        self.assertEqual(sha256_file(PROJECT_ROOT / "data/synthetic/schema_matching_precision_tiered_v4_5scenario_evaluation.json"), EXPECTED_V4_SHA)
        self.assertEqual(sha256_file(PROJECT_ROOT / "data/synthetic/schema_matching_precision_tiered_v5_5scenario_evaluation.json"), EXPECTED_V5_SHA)
        formal_diff = subprocess.run(["git", "diff", "--name-only", "--", "data/synthetic", "data/generated"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(formal_diff.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
