# Project Guardrails

- Read the relevant roadmap/spec and current git state before modifying code.
- Do not redesign an approved experiment while implementing it.
- Preserve existing production behavior unless the user explicitly approves a change.
- Never introduce look-ahead leakage. All market, broker, and ownership features must use strict point-in-time/as-of semantics.
- Ambiguous ownership entities must remain separate unless supported by explicit evidence.
- Do not infer beneficial owners, controllers, custodians, or owner-broker relationships from fuzzy name similarity.
- `observable_inventory` was rejected by Experiment #1 and must not be reintroduced unless explicitly requested.
- Broker identity remains research-grade, not production-approved.
- Owner-broker mapping is outside the current scope unless explicitly authorized.
- Do not touch unrelated or pre-existing untracked scratch files.
- Prefer the smallest change that satisfies the approved specification.
- Never silently relax validation, tests, sample gates, or experiment acceptance criteria.