# RetailPulse — Resume Bullets & Interview Preparation

Every number here is pulled directly from `artifacts/metrics.json`, `data/raw/sales_raw.manifest.json`, or a passing test run (209 tests, `pytest -q`) — none are invented. Where a real number doesn't exist yet (the NL-to-SQL correctness rate needs your own LLM key), that's marked explicitly rather than guessed.

## Resume bullets

Pick 3-4 depending on the role (data engineering vs. backend vs. ML-leaning). Replace `[Your Name]` in `LICENSE` before publishing the repo.

- **Built an end-to-end retail data pipeline** (Python, pandas, PostgreSQL) that validates and cleans raw sales data through 14 distinct rejection/repair rules, loading 25,371 of 26,437 raw rows while rejecting the rest with an exact, auditable reason per row — verified against a ground-truth manifest in 52 automated tests.
- **Designed and trained a per-item demand forecasting model** (scikit-learn Linear Regression, 23 products) using a strictly chronological train/test split, beating a seasonal-naive baseline on 23/23 items and a recent-mean baseline on 22/23 (7.67 vs. 9.79/10.00 units/day MAE; 16.6% WAPE), with results reproducing exactly on re-run.
- **Built a safety-constrained natural-language-to-SQL feature** (FastAPI + LLM API) with four independent defense layers — prompt constraints, an independent `sqlparse`-based validator, a database-enforced read-only PostgreSQL role, and query timeouts — and proved the database-level layer directly against real PostgreSQL (no application code involved) rather than only unit-testing it.
- **Shipped a fully tested REST API** (FastAPI, PostgreSQL, Docker) with 209 automated tests covering ETL correctness, forecast reproducibility, and 57 adversarial security cases for the NL-to-SQL validator (destructive statements, non-whitelisted tables, comment-injection, resource-exhaustion attempts) — all rejected with a specific, audited reason.

## 30-second explanation

"I built a pipeline that cleans raw retail sales data into a proper database, forecasts demand per product with an explainable model, and lets someone ask plain-English questions about the data — which get safely translated into read-only SQL with four independent layers of protection, two of which I proved work even if my own application code has a bug."

## 2-minute explanation

Raw CSV goes through pandas cleaning with 14 explicit validation/repair rules, landing in a normalized PostgreSQL schema (`items`, `sales`, `forecasts`) with real constraints and indexes. On top of that: SQL-backed analytics endpoints, and a Linear Regression forecast per item trained on lag/weekday/seasonal features with a chronological split — never random, since that would let the model see the future. The forecast beats two honest baselines, and I have the actual MAE numbers, not a claim.

The differentiator is NL-to-SQL: a question comes in, one LLM call turns it into SQL, an independent validator checks it (SELECT-only, table whitelist, no dangerous functions, forced LIMIT), and it executes under a Postgres role that is *structurally* incapable of writing — I proved this by connecting as that role directly and watching Postgres itself refuse a `DELETE`, with zero application code involved. Every attempt, accepted or rejected, is logged for audit.

## 10-minute deep explanation

Cover, in order: (1) why retail demand forecasting and self-serve analytics are real, common needs; (2) the ETL's exact validate → clean → transform → load pipeline and how a ground-truth manifest from the synthetic-data generator lets the tests assert rejection/repair counts *exactly*, not approximately; (3) the schema — grain of `sales` is one row per item per day, enforced by a `UNIQUE` constraint that also makes reloads idempotent; (4) the forecasting model, its 12 features, the chronological split, and the actual measured MAE/WAPE against two baselines, with the honest caveat that the dataset is synthetic; (5) NL-to-SQL in full: the four layers, a concrete example of a `DROP`/`DELETE` being rejected with the exact reason logged, and the direct-to-Postgres proof that layers 3-4 don't depend on the validator being perfect; (6) the 209-test suite, including the 57 adversarial validator cases and the two tests that bypass the app entirely; (7) honest limitations: synthetic data, no live LLM evaluation yet, Docker not build-tested, single-process rate limiter.

## Viva / interview questions

**Why chronological split instead of random for time-series data?**
Random shuffling would let the model "see the future" during training — evaluating on data that comes *after* the training window is the only way to know how it'll perform going forward.

**How do you stop the LLM from generating a destructive query?**
Four independent layers: the prompt only asks for SELECT (a quality measure, not security); an independent validator parses the actual output and rejects anything that isn't a single SELECT against whitelisted tables; the database connection itself uses a role with no write grants and `default_transaction_read_only = on`; and resource limits (timeout, row cap) bound what a read can do. I proved the third layer directly — connected as that role with no app code at all, and Postgres refused a raw `DELETE`.

**Why a separate Postgres role instead of just checking in application code?**
Application-level checks can have bugs. A database-level permission is enforced by Postgres itself regardless of any app-layer mistake — that's the actual meaning of "defense in depth," not just a phrase. I demonstrated this is real, not aspirational, with a test that skips the validator entirely.

**Why Linear Regression over a more complex forecasting model?**
Small dataset (23 active items, ~2.5 years of daily data each), full interpretability, and it's a strong enough baseline to report an honest, defensible MAE. It beat seasonal-naive on every item and recent-mean on all but one — added complexity wasn't justified by the data I had.

**What features did you engineer for the forecast?**
12 features per item: 7- and 14-day lags, a 7-day rolling mean taken at the 7-day lag (so it never looks at the future), a linear time trend, day-of-week dummies, and sine/cosine encodings of day-of-year for annual seasonality.

**How did you evaluate the NL-to-SQL feature specifically?**
I built an evaluation harness that executes the LLM's generated SQL and a hand-written reference query for 20 labeled questions, and compares the *results* (not the SQL text, since two different queries can be equally correct). I sanity-tested the harness itself — feeding it the reference SQL as a stand-in for the LLM scores 100%, and a deliberately wrong query scores 0%, which proves the scoring logic is correct. [If you've run it with a real key: state the actual correctness rate from `evaluation/results.json` here.]

**What happens if the LLM generates invalid SQL?**
The validator rejects it before execution with a specific reason (e.g., `non_whitelisted_table`, `dangerous_function`), the API returns a 422 with that reason, and the attempt is still written to the audit log — nothing is silently dropped, and the raw database error is never echoed to the client.

**Why index/constrain on `(item_id, sale_date)`?**
Every analytics and forecast query filters by a specific item and date range, so a composite index directly serves that pattern. Making it `UNIQUE` (not just indexed) also enforces the table's actual grain — one row per item per day — and makes the ETL's upsert idempotent for free.

**What's a limitation of the read-only role approach?**
It stops writes, but doesn't stop an expensive read query from being slow — that's why there's also a statement timeout (proved directly: a 150ms timeout kills `pg_sleep(2)`) and a row cap.

**What would you do differently with more time?**
Run the NL-to-SQL evaluation against a real LLM and report the actual correctness rate rather than a sanity-tested-but-unrun harness; move the rate limiter to a shared store (Redis) so it holds under multiple replicas; build-test and actually deploy the Docker image; extend the validator toward a real SQL grammar parser instead of a lexical one, to close the gap layer 3 currently covers for.

## SQL questions

**What does this window function do — `RANK() OVER (ORDER BY SUM(s.revenue) DESC)`?**
Ranks items by total revenue without collapsing the underlying rows — used in `/analytics/top-items` so ties get the same rank and the query stays a single pass over the data instead of a self-join.

**Why parameterize date filters instead of building the WHERE clause as a string?**
SQL injection: values always go through psycopg's parameter binding (`%(start)s`), never string interpolation. The only things ever interpolated as raw text are fixed constants from a whitelist dict (e.g., `revenue` vs. `units` for sort order) — never user input.

## ETL questions

**How do you decide what's repaired vs. rejected?**
Repaired only when the fix is unambiguous: whitespace/case normalization, `$1,234.50` → `1234.50`, a clearly-DD/MM/YYYY date, a missing category filled from that item's majority category. Anything ambiguous (a genuinely invalid date, a negative price) is rejected with a specific reason, never guessed at.

**How do you know the ETL is correct, not just "runs without crashing"?**
The synthetic-data generator records exactly how many rows of each defect type it injected. The test suite asserts the ETL's actual rejection/repair counts match that ground truth exactly, reason by reason — not just "some rows got rejected."

## ML questions

**What's WAPE and why report it alongside MAE?**
Weighted Absolute Percentage Error — total absolute error divided by total actual demand. MAE alone doesn't say whether 7.67 units/day is a big or small miss; WAPE (16.6% here) gives that context in relative terms.

**Why skip `Trash Bags 30ct` from training?**
It's inactive in the dataset (no recent sales) — forecasting demand from no data would just be extrapolating noise, so it's explicitly skipped rather than silently given a meaningless prediction.

## FastAPI questions

**Why a single JSON error envelope (`{"error": {...}}`) instead of raising raw exceptions?**
Consistency for API consumers, and it means an internal error (a stack trace, a raw psycopg exception) is never accidentally exposed to the client — every error path funnels through one shape.

**Why does `/query` take a `Request` and pull the pool from `request.app.state` instead of a plain dependency?**
The endpoint needs a *second*, independent connection (the read/write pool for audit logging) alongside the read-only connection injected via `Depends`, so the two pools stay structurally distinct rather than reachable through the same code path.

## PostgreSQL questions

**What does `default_transaction_read_only = on` at the role level actually buy you, versus just not granting write permissions?**
Grants (`InsufficientPrivilege`) and the read-only session flag (`ReadOnlySqlTransaction`) are two separate mechanisms; I catch both in the executor. Setting both means a write attempt is refused even in edge cases where one mechanism alone might not catch it.

## AI / NL-to-SQL questions

**Doesn't the LLM see your data, which could leak sensitive information into a third-party API?**
The LLM only ever sees the user's question and the fixed schema in the prompt — never actual row data or query results. That also closes off a specific prompt-injection vector (data engineered to manipulate a later LLM call that reads it back), since there is no later call.

## Security questions

**Is this "secure"?**
No — and the project doesn't claim that. It claims to be constrained by several independent, individually-tested layers, with the honest limitation that `sqlparse` is lexical, not a full parser, and this hasn't been independently red-teamed. That's exactly why the database-level backstop exists and was proven separately from the validator.

## Deployment questions

**Why isn't this deployed to a public URL yet?**
The Dockerfile and compose file are written and reviewed against the actual code (paths, non-root user, healthcheck, volumes), but they were built in an environment with no Docker daemon available, so they haven't been build-tested. That's a known open item, not something glossed over — see the Roadmap in `README.md`.

## Difficult interviewer follow-ups

**"Your forecast beats a naive baseline on synthetic data you generated yourself with the exact seasonality your features capture. Isn't that circular?"**
Yes, and that's stated directly in the README/architecture doc. This proves the training-and-evaluation *pipeline* is implemented correctly — chronological split, correct feature construction, correct baseline computation — not that the accuracy generalizes to real retail data. That would need re-running on a real dataset, which is an explicit next step.

**"You say 'defense in depth' — how do I know layer 3 actually works and isn't just documentation?"**
Two tests connect directly to PostgreSQL as the read-only role, with no validator and no application code involved at all, and assert that a raw `DELETE`/`INSERT` raises `ReadOnlySqlTransaction` and that `pg_sleep(2)` against a 150ms timeout raises `QueryCanceled`. That's not a mock — it's the actual database refusing the actual operation.

**"You haven't run the NL-to-SQL evaluation — so how do you know it works at all?"**
The full pipeline (LLM interface → validator → executor → audit log) is exercised end-to-end in 14 integration tests using a `FakeLLMClient` that returns real, hand-written SQL — including realistic multi-table joins, a rejected `DELETE`, and a dangerous-function attempt — against a real PostgreSQL database. What's *not* yet measured is one specific number: how often a real LLM's translation is correct. Those are different claims, and I'm not conflating them.
