# Identifier Role Ranking Ablation V1

This development-only ablation tests whether header-blind source identifier probability can improve stored V5 candidate ranking without changing V5 itself. It does not modify the scorer, backend, frontend, runtime, aliases, features, thresholds, Companies House, or FDIC artifacts. Companies House and FDIC data, ground truth, result artifacts, and per-case records were not used for training, alpha selection, or this ablation.

The fixed alpha grid is `0.0`, `0.01`, `0.02`, and `0.05`. Nested evaluation uses leave-one-contract-family-out outer folds. Alpha is selected inside each outer training set by inner leave-one-contract-family-out MRR, constrained so no-target accuracy and multi-target full coverage@3 do not fall below V5; ties choose the smaller alpha.

Compared systems are `v5_reference`, `v5_plus_heuristic_identifier_role`, `v5_plus_learned_identifier_role`, and the non-deployable `v5_plus_oracle_identifier_role` diagnostic.

## Development Result

V5 reference MRR is 0.9067; learned reranker MRR is 0.9067; oracle MRR is 0.9067. Learned improved 0 cases, regressed 0 cases, and left 84 unchanged. All outer folds selected `alpha=0` for the learned and heuristic rerankers, and the oracle diagnostic also produced no ranking lift.

No-target accuracy changed from 0.9565 to 0.9565. Multi-target full coverage@3 changed from 0.8889 to 0.8889.

Future sealed holdout gate: False. This negative result is retained as evidence, not tuned away. It does not justify V5, ranking, production, or runtime integration.
