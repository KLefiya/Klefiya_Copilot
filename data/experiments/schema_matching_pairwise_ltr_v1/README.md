# Schema Matching Pairwise LTR v1

This is an interpretable development-only learning-to-rank prototype. It is not connected to runtime, backend, frontend, or any production scorer.

## Corpus

- Cases: 107
- Expected target links: 95
- Scenario groups: 7
- Contract families: 5
- Candidate rows: 1736

The corpus combines the five existing synthetic schema-matching scenarios with two public development scenarios. It is development evidence only, not a sealed holdout.

## Leakage Controls

- Candidate features are generated before pair construction.
- Ground truth is used only for pair construction and evaluation.
- Scaler and ranker are fit inside each train fold only.
- Folds are grouped by complete scenario or complete contract family.
- No raw source field names, target qualified names, scenario ids, contract ids, case ids, candidate ranks, or label-derived statistics are used as model features.
- No-target cases are retained in counts but LTR v1 does not learn abstention or calibration.

## Results

- Leave-one-scenario-out folds: 7
- Leave-one-contract-family-out folds: 5

See `comparison.json`, `leave_one_scenario_out.json`, and `leave_one_contract_out.json` for pooled metrics and failure cases.
