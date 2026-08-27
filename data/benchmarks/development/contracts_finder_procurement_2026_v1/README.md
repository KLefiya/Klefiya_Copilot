# Contracts Finder Procurement Development Benchmark

This is a public-data development benchmark for schema-matching iteration. It is not part of the sealed five-scenario formal benchmark and is not evidence of unseen production generalization.

## Source

- Dataset: United Kingdom Contracts Finder OCDS CSV export via the Open Contracting Partnership Data Registry.
- Registry page: <https://data.open-contracting.org/en/publication/128>
- GOV.UK open contracting page: <https://www.gov.uk/government/publications/open-contracting>
- Source archive: `https://fastly.data.open-contracting.org/downloads/united_kingdom_contracts_finder_releases/4062/2026.csv.tar.gz`
- Retrieved date: 2026-08-27.
- Source version: 2026 annual CSV export; OCP registry last retrieved Aug 6, 2026.
- License: Open Government Licence v3.0.
- Attribution: Contains public sector information licensed under the Open Government Licence v3.0; data accessed through the Open Contracting Partnership Data Registry.

IDB Project procurement bidding notices and contract awards were also considered. Contracts Finder was selected because the OCDS tender fields map more naturally to the existing sales-order fulfillment contract and provide more positive target links without changing the target schema.

## Fixture

- Source file: `source_contracts_finder_procurement_2026_v1.csv`
- Source archive size: 15670363 bytes
- Source archive SHA-256: `77a56e26ebbb6c7ec754b0795032ca8a8cf031455e673ac85c80a2eeb41da654`
- Source rows: 24
- Source columns: 23
- Sampling: first 24 rows from `2026/main.csv` where the required target-bearing fields are non-empty.
- Source field names: copied from the public CSV header; no semantic renaming.
- Source value normalization: none; values are written as read from the CSV export except UTF-8 output encoding and CSV escaping.
- Sensitive column handling: only notice-level `main.csv` fields are used; parties/contact fields containing names, emails, telephone numbers, and street addresses are excluded.
- Target contract: `data/benchmarks/fixtures/sales_order_fulfillment/contract/datapackage.yaml`
- Target contract status: existing synthetic benchmark fixture, not an authoritative production schema.

## Ground Truth

- Source fields: 23
- Target-bearing source fields: 16
- Single-target cases: 14
- Multi-target cases: 2
- No-target cases: 7
- Expected target links: 19

Positive labels were assigned before scorer execution by comparing the public OCDS field semantics to the existing sales-order fulfillment target field descriptions. Ambiguous procurement metadata is kept as no-target when the target contract has no natural field.

## Evaluation Status

This benchmark is development-only:

- `development_benchmark`: true
- `formal_evaluation`: false
- `sealed_holdout`: false
- `repeated_evaluation_allowed`: true
- `not_evidence_of_unseen_generalization`: true

Single-scenario results are written under `results/`. Combined public-development results are written under `data/benchmarks/development/combined_public_dev_v1/results/`.
