# practical-v2 Reviewer Guide

Use this guide to review practical-v2 without rerunning the whole project in an ad hoc order.

## Review Order

1. Contract Loader and Validator.
2. Mapping Engine.
3. Blind Benchmark.
4. Decision Loader.
5. Package Builder.
6. Lineage and Manifest.
7. Migration Workspace API.
8. React Workspace.
9. Tests and evidence.

## Important Files By Layer

- Contracts: `contracts/`, `references/`
- Source examples: `data/examples/`
- Deterministic generated artifacts: `data/generated/`
- Formal reports: `data/synthetic/`
- Core migration code: `src/core/`
- Tools: `src/tools/`
- Backend workspace API: `backend/`
- React workspace: `frontend/src/`
- Tests: `tests/`

## Trust Boundaries

- Answer data is used only by evaluator code.
- Blind Protocol Lock was fixed before the first mapping run.
- Decision files only approve targets that appear in Top-3 candidates.
- Runtime workspace state cannot overwrite formal committed evidence.
- API paths come from the workspace Registry, not from request payloads.
- Build calls core Python functions directly; it does not shell out to a CLI.
- Lineage avoids raw Source Value storage and records field-level provenance.

## Review Questions

- Does the contract define the target resources and field constraints used by the builder?
- Does the Mapping Engine expose candidates while keeping the frozen blind evidence unchanged?
- Does the Decision Loader reject duplicate target ownership and non-candidate approvals?
- Does the Package Builder produce both target resources from approved decisions?
- Does generated validation reuse the same contract?
- Does lineage explain each target cell without leaking raw source values?
- Does the workspace write only runtime state?
- Does reset restore the committed Seed Decision behavior?
- Do tests cover stale decision SHA, conflict rejection, build, reset, validation, and lineage preview?

## Evidence Table

| Evidence | SHA256 |
| --- | --- |
| Blind Mapping | `f2b1a3b578222694b845950165334b628a6e8285d54287457af10fb2fd836164` |
| Blind Evaluation | `d665596750403d5928daa332f318dec3078bce1a7ab977c8192b3a5edd106fed` |
| Protocol Lock | `bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed` |
| ERPNext Manifest | `8a63eafaface02dc3d04cebec1dea58a722b6f10cb8d2763a6077af96f9052c3` |
| ERPNext Generated Validation | `a4c688073df9799a05f5a6d5cba4c584c175c51e7615c87559923efa2b65f012` |
