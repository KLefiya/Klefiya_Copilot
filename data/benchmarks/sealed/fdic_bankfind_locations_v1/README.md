# FDIC BankFind Locations Sealed Holdout

This fixture preregisters a public sealed holdout for schema-matching evaluation. It must not be used for development iteration, calibration threshold selection, scorer tuning, or prompt/alias changes before its first evaluation.

## Source

The source sample comes from the FDIC BankFind Suite locations endpoint. The selected source is the public locations/branches endpoint, not the institution-level endpoint, because it has a clearer semantic connection to the existing synthetic bank branch target contract while remaining independent of the Open Food Facts, UK Contracts Finder, and Companies House public datasets already used elsewhere.

The frozen source query uses `SERVTYPE:11`, projects 14 public fields, sorts by `UNINUM` ascending, and keeps the first 128 rows. Headers and values are not semantically normalized; values are only serialized as UTF-8 CSV with LF line endings.

Street addresses, phone numbers, websites, contact names, email addresses, account numbers, and routing-number-like operational values are excluded from the fixture.

## Ground Truth

The target contract is `data/benchmarks/fixtures/bank_account/contract/datapackage.yaml`, a synthetic benchmark contract. The 14 source fields produce 3 single-target cases, 0 multi-target cases, 11 no-target cases, and 3 expected target links.

Target-bearing labels are:

- `UNINUM` -> `bank_branch.bank_key`
- `NAME` -> `bank_branch.bank_name`
- `OFFNAME` -> `bank_branch.bank_name`

Notable exclusions are also frozen. `CERT` is not mapped to `bank_branch.bank_key` because it is institution-level rather than location-level. `STALP` is not mapped to `bank_branch.country_code` because it is a U.S. state abbreviation, not a country code. `MAINOFF` is not mapped to `bank_account.primary_flag` because it describes the office role, not preferred account usage.

## Preregistered Evaluation

The preregistration files remain immutable. `protocol.json` and `fixture_lock.json` still record first evaluation status `not_run`, with result and metrics artifact paths set to `null`, because they are the locked pre-evaluation snapshot. The later evidence is recorded separately in [first_evaluation_attempt.json](first_evaluation_attempt.json) and [first_evaluation.json](first_evaluation.json).

The preregistered future comparison includes the existing baseline, V4, V5, the existing V5 acceptance policy, and frozen development-only V5 correctness calibrators. The primary selective decision rule compares `score_only_calibrator_target_precision_95` against the existing V5 policy. The sealed holdout must not be used to choose features, thresholds, aliases, scorer weights, or reporting filters.

Primary decision evidence is limited to `existing_v5_policy` versus `score_only_calibrator_target_precision_95`. Baseline ranking, V4 ranking, V5 ranking, and multifeature 95% calibration are secondary diagnostics; V4 and the multifeature calibrator are not promotion candidates, and secondary results cannot override the primary decision rule.

This holdout has only 3 positive-bearing source-field cases, so ranking metric resolution is low. It is more useful for observing no-target false acceptance across 11 no-target cases. The FDIC source is new, but the `bank_account` contract family already appears in development data, so this is an unseen-source holdout rather than an unseen-contract-family holdout. Multi-target metrics are N/A because the denominator is 0. The fixture cannot by itself support broad generalization or precise 95% population precision claims.

If future sealed results are unfavorable, they should be retained as evidence rather than used for same-round tuning.

## First Sealed Evaluation

The first sealed evaluation was run once from the guarded runner commit. Baseline, V4, and V5 ranking results were identical: Top-1 `2/3`, Recall@1 `2/3`, Recall@3 `2/3`, MRR `2/3`, no-target accuracy `11/11`, and multi-target full coverage N/A because the denominator is 0.

The target-bearing outcomes were:

- `NAME`: correct target rank 1.
- `OFFNAME`: correct target rank 1.
- `UNINUM`: correct target did not enter Top-3.

For the primary selective comparison, both `existing_v5_policy` and `score_only_calibrator_target_precision_95` accepted `0/14` cases, with coverage `0`, review rate `1`, and accepted error `0`. The score-only policy therefore did not improve coverage over the existing V5 policy, does not support runtime integration, and is not promoted. The primary decision rule remains negative evidence rather than a reason to tune against this holdout.

As a secondary diagnostic, `multifeature_calibrator_target_precision_95` also accepted `0/14` cases. Its descriptive calibration metrics were better than score-only on this tiny sample, but there are only 14 cases and only 3 target-bearing fields. Those descriptive metrics cannot override the primary decision rule and are not a basis for switching to or promoting the multifeature policy.

Two correct V5 Top-1 mappings were rejected by all selective policies, `UNINUM` was a ranking failure, and all 11 no-target cases were safely rejected. This is an all-review result: safe on false acceptance, but with no automation utility on this sealed banking source. The source is unseen, the contract family is not unseen, there are no multi-target cases, and the result cannot support broad generalization or precise population-level 95% precision claims. No post-unseal tuning or rerun will be performed against this holdout.
