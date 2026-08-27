# V5 Correctness Calibration v1

This experiment asks whether the current strongest ranker, `precision_tiered_v5`, can be wrapped with an interpretable Top-1 correctness gate. The gate predicts whether the V5 Top-1 recommendation is suitable for automatic acceptance. Rejected cases go to human review; rejection is not an automatic no-target decision. Multi-target full coverage is outside this Top-1 gate and remains a separate ranking/evaluation problem.

An initial pre-artifact run aborted because of a missing `_sigmoid`
helper. No predictions or evaluation artifacts were produced. The
implementation was fixed before the single completed experiment run.

The completed experiment ran once but did not persist case-level outer OOF audit records. A first `failure_analysis.json` version incorrectly used pooled `threshold: null` values for case decisions. A later deterministic forensic replay under the frozen protocol reproduced the original aggregate metrics, thresholds, and counts exactly, then added `outer_oof_predictions.json` and repaired only the derived failure analysis. No model was retrained, no threshold was reselected, no protocol was changed, and `outer_oof_predictions.json` is an evaluation audit artifact, not training data.

## Why Calibration

Pairwise LTR v1 did not beat V5 on the development corpus, so this run does not tune another ranker. Correctness calibration and ranking improvement are different questions: V5 keeps producing the ranking, while this model estimates whether the already-selected Top-1 should be accepted without review.

## Corpus

- Cases: 107
- Positive Top-1 labels: 72
- Negative Top-1 labels: 35
- Contract families: bank_account, generic_customer, item_item_price, sales_order_fulfillment, supplier_reference
- Development-only corpus: synthetic five-scenario benchmark plus two public development benchmarks

The development benchmarks have already been used for iteration. These results cannot be treated as sealed final evidence, production generalization, or statistically significant improvement.

## Leakage Controls

- Features are numeric/boolean V5 ranking diagnostics only.
- No source field names, source text, target paths, target text, scenario id, benchmark id, contract family id, case id, expected target count, expected target list, or label-derived feature enters the training matrix.
- Outer folds leave out an entire contract family.
- Thresholds are selected only from inner contract-family out-of-fold probabilities.
- The held-out family is not used for training, scaling, threshold selection, regularization choice, probability binning, or failure handling.

## Results

- Score-only Brier: 0.14090701
- Multifeature Brier: 0.17758211
- Existing V5 policy coverage: 0.21495327, accepted precision: 1.0, accepted incorrect: 0
- Multifeature 90% policy coverage: 0.63551402, accepted precision: 0.80882353, accepted incorrect: 13
- Multifeature 95% policy coverage: 0.53271028, accepted precision: 0.8245614, accepted incorrect: 10

See `comparison.json`, `contract_family_out_results.json`, and `failure_analysis.json` for pooled counts, family-level shifts, and disagreements. `development_model.json` is JSON-only and explicitly marked `production_promoted: false`, `sealed_holdout_validated: false`, and `development_only: true`.

## Limitations

The sample has only five contract families and limited negative Top-1 cases. The estimates are useful for deciding whether future sealed holdout work is worth doing, but only an independent sealed holdout can support stronger generalization claims.
