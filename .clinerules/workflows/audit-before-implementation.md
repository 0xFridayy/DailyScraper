# Audit Before Implementation

Use this workflow before implementing a new experiment, feature family, schema change, ownership analysis, or major architecture change.

1. Inspect:
   - git status
   - recent git history
   - relevant roadmap/specification
   - relevant source files
   - database schema/tables
   - existing tests
2. Identify the exact hypothesis or decision being evaluated.
3. Measure the actual available data. Do not rely only on documentation.
4. Audit:
   - date range
   - number of observations
   - number of distinct dates
   - ticker coverage
   - entity coverage
   - missing or sparse history
   - actual transition/change counts
   - concentration in a small number of tickers/entities/brokers
5. Check point-in-time validity:
   - when each field became observable
   - whether same-day data can be used safely
   - whether as-of joins are required
   - whether missing observations mean unchanged or unknown
6. Check leakage risks and ambiguous joins.
7. Separate findings into:
   - testable now
   - requires more history/data
   - cannot be reconstructed safely
8. Quantify sample sufficiency using effective observations, not only calendar duration.
9. Report concrete counts and evidence.
10. Recommend the smallest defensible next step.
11. STOP for user review.

Do not edit files, modify schema, train models, change roadmap documents, commit, or push during this workflow unless explicitly instructed otherwise.