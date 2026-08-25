# Companies House Customer External Holdout v1

This preregistered Phase A fixture uses real public company register data from the Companies House Free Company Data Product.

- Official product page: https://www.gov.uk/guidance/companies-house-data-products
- Download page: https://download.companieshouse.gov.uk/
- Snapshot: 2026-08-01
- Source file: BasicCompanyData-2026-08-01-part1_7.zip
- Source universe: monthly file part1_7 only, not all seven parts.
- Licence: Open Government Licence v3.0 (https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
- Attribution: Contains Companies House information licensed under the Open Government Licence v3.0.

The committed source fixture is a deterministic 128 x 12 minimized subset. It keeps only non-person source fields from the preregistered allowlist and excludes full address fields, postcode, previous names, officer/director/PSC/person fields, phone, email, and all other source columns.

Header normalization is frozen before first evaluation: strip ASCII space (0x20) from header boundaries only. The 12 allowed raw-to-logical mappings are recorded in `source_provenance.json` and `protocol_lock.json`; this normalization does not read target aliases, ground truth, predictions, or evaluation results. Source values are not normalized, stripped, cleaned, or manually edited.

The target contract is the project's existing synthetic generic customer reference contract. This fixture is not private production customer data, not production customer validation, not evidence that the synthetic target contract is authoritative, and not a representative sample of all UK companies.

First evaluation has not been run. Do not run any schema matching scorer, model inference, candidate generation, or evaluator before the frozen source, ground truth, provenance, and protocol are reviewed.
