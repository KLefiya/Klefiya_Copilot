# Value Profile Identifier Detector V1

This development-only experiment asks a narrow question: can aggregate source-column value profiles identify whether a source column has an identifier role? It is not full schema matching, not a ranking improvement, not runtime integration, and not a production promotion.

The detector is fully header-blind. Model inputs are numeric statistics computed from column values only; source field names, target identity, scenario id, contract family, case id, mapping ground truth, mapping scores, and labels are excluded from the feature matrix. Identifier-role labels are separate semantic annotations, not mapping target labels. A no-target column can still be an identifier if it primarily carries a key, code, or reference role.

Companies House and FDIC motivated this question through published README evidence, but their data, ground truth, result artifacts, and per-case records are excluded from annotation, training, validation, and feature analysis.

## Data And Labels

The experiment uses the existing development corpus only: 5 synthetic scenarios plus 2 public development scenarios, covering 107 source-field annotations across 7 scenarios and 5 contract families. Modeled labels are 36 identifier and 71 non-identifier cases; ambiguous exclusions are 0.

## Models

Three fixed strategies are compared: a constant prevalence baseline, a simple deterministic heuristic declared before running the experiment, and a StandardScaler plus deterministic L2 logistic regression with a fixed 0.5 probability threshold. The ablation is limited to distribution-only, pattern-only, and combined feature sets.

## Development Results

Leave-one-scenario-out combined logistic accuracy is 0.8318, balanced accuracy 0.8116, identifier precision 0.7500, identifier recall 0.7500, and F1 0.7500.

Leave-one-contract-family-out combined logistic accuracy is 0.8505, balanced accuracy 0.8326, identifier precision 0.7778, identifier recall 0.7778, and F1 0.7778. The simple heuristic contract-family-out accuracy is 0.7103.

These grouped development metrics cannot be treated as external generalization. Even if the detector performs well, it would need a separate ranking ablation before any V5 or runtime integration could be considered. Negative results are retained as evidence.
