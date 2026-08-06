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
| Blind Mapping | `99007ad5da580b6e764b01e3a9739840bcfcff1b1a16c29cf708124ebbc56703` |
| Blind Evaluation | `e75c2f8e5b6ed7794f265ceb795426045b403d2973a5bc7622af016c887e7527` |
| Protocol Lock | `bd092f06592d6a71961454cf638e2864ac3e5fb8fc0f247a1fe0b8ae36fdb2ed` |
| Protocol Compatibility Amendment | `2b15b49a87f031312534a28e376345e463de2a3aaae14380150f3c7c0a58888a` |
| Generic Manifest | `3915849c255cafa9baf3011e212bb287985d8c20e785e3bbc3baa47aad234c5c` |
| Supplier-reference Manifest | `0dcd68ef1422747be95223aacac782735c7cdad59fa2e865a0a36ff8154ff17e` |
| ERPNext Manifest | `5c8f6d523a60887ce2b0173e3a89cae94cf484f0212b9cc247c7bd56738d0dfe` |
| ERPNext Generated Validation | `a4c688073df9799a05f5a6d5cba4c584c175c51e7615c87559923efa2b65f012` |
| Migration Findings | `74dfc9310502fadaeb4cc27ec31c2e630d0fb97e9dbe645ba3e755d298fbaf60` |
| Cutover Plan | `160b1fc7777c71d435eda910686188febb957f21004ae4466f96b08c44c89767` |
| Cutover Status | `c44900cb3561bfc411664ae7602061ca801faf55587d5eb9b187e096d9b586c5` |
| Cutover Daily | `e6d095a93a2e05d6b13b7f6d88790d09613dbde1d5f416466df20b0d8e028bd8` |
| Cutover Agent Trace | `ac99965064bd5900686ebbb9cb1762eebdfcf63ba5dff66f45ae82a61e17522d` |

The historical blind protocol lock remains fixed. The compatibility amendment validates only the provenance-mode profiler change and mapping-engine metadata propagation; mapping source provenance uses `normalized_text_sha256_v1`, while formal JSON report chains use `_run_info.content_sha256`. This is maintenance compatibility evidence, not a new original blind lock.
