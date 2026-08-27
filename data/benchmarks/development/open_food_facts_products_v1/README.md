# Open Food Facts Products Development Benchmark

This is a public-data development benchmark for schema-matching iteration. It is not part of the sealed five-scenario formal benchmark and should not be cited as evidence of unseen production generalization.

## Source

- Dataset: Open Food Facts product search API.
- API documentation: <https://openfoodfacts.github.io/openfoodfacts-server/api/>
- Source download page: <https://world.openfoodfacts.org/data>
- Retrieved date: 2026-08-27.
- License and attribution: Open Food Facts database under ODbL, individual contents under the Database Contents License, attributed to Open Food Facts contributors.

USDA FoodData Central was also considered because it is public food data under CC0/public-domain terms. Open Food Facts was selected for this development benchmark because its product catalog fields naturally exercise the existing ERPNext Item and Item Price reference contract without inventing nutrition-specific target semantics.

## Fixture

- Source file: `source_open_food_facts_products.csv`
- Source rows: 24
- Source columns: 12
- Sampling: first products returned by the Open Food Facts search API sorted by `unique_scans_n`, keeping rows with non-empty `code`, `product_name`, `quantity`, and `categories`.
- Source value normalization: none; values are written as returned by the API except for CSV escaping.
- Target contract: `contracts/erpnext_item_price_reference/datapackage.yaml`
- Target contract status: synthetic reference contract, not an authoritative ERPNext production schema.

## Ground Truth

- Single-target cases: 2
- Multi-target cases: 2
- No-target cases: 8
- Expected target links: 6

Expected target-bearing source fields:

- `code` -> `item.item_code`, `item_price.item_code`
- `product_name` -> `item.item_name`
- `quantity` -> `item.stock_uom`, `item_price.uom`
- `categories` -> `item.item_group`

All other source fields are intentionally marked as no-target for this reference contract.

## Evaluation Status

This benchmark is development-only:

- `development_benchmark`: true
- `formal_evaluation`: false
- `sealed_holdout`: false
- `repeated_evaluation_allowed`: true
- `not_evidence_of_unseen_generalization`: true

Results, when generated, are written under `results/` and should remain separate from the formal benchmark artifacts.
