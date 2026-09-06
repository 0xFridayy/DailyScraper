# ML Project Guardrails

## Repository and Scope

- Read the relevant roadmap/specification and current git state before modifying code.
- Treat approved experiment specifications as contracts.
- Do not redesign an approved experiment while implementing it.
- Do not modify unrelated files or pre-existing untracked scratch files.
- Preserve existing production behavior unless the user explicitly approves a change.

## ML and Data Integrity

- Never introduce look-ahead leakage.
- All market, broker, ownership, and derived features must obey strict point-in-time / as-of semantics.
- Do not use future observations when constructing historical features.
- Do not silently forward-fill ownership data unless the source semantics explicitly prove that absence means unchanged.
- Use identical OOS rows when comparing experiment variants unless the approved experiment explicitly states otherwise.
- Never weaken validation, tests, sample gates, or acceptance criteria after observing experiment results.

## Broker / Ownership Research State

- `observable_inventory` was rejected by Experiment #1. Do not reintroduce it unless explicitly requested.
- `broker_identity` remains a research-grade feature and is not production-approved.
- Owner-broker mapping is outside the current scope unless explicitly authorized.
- Do not infer beneficial ownership, controllers, custodians, or owner-broker relationships from fuzzy name similarity.
- Ambiguous ownership entities must remain separate unless supported by explicit evidence.
- Global canonical entity IDs are the ownership identity standard.
- Preserve raw ownership names exactly.

## Implementation Discipline

- Prefer deterministic and auditable transformations.
- Preserve provenance for canonicalization and entity-resolution decisions.
- Verify idempotency where database backfills or recurring workflows are involved.
- Run targeted tests first, then the broader relevant test suite.
- Check for data leakage and train/test contamination in ML work.
- Do not claim a feature improves performance based only on one pooled metric.
- Robustness results must be considered before promoting a research feature.

## Git

- Inspect git status before modifications.
- Do not include unrelated or pre-existing scratch files in commits.
- Do not commit or push unless explicitly requested.
- Never force-push unless explicitly authorized.