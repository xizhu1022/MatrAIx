# Task 2 notes

## Submitted task

`executor-window-functions/`: add SQL window functions to sqlglot's pure-Python executor,
verified differentially against DuckDB. One task, built end to end and backed by experiments
for every criterion (valid, fair, attacked, graded partial credit, difficulty measured on a
ladder of models). Reviewer details are in its `README.md`; logs and scripts are in
`evidence/`.

## Approach

1. **A real capability gap with an independent reference.** I probed sqlglot's executor with
   ~45 SQL features. It errors on window functions, `ROLLUP`/`CUBE`/`GROUPING SETS`, `FILTER`,
   recursive CTEs, `LATERAL`/`UNNEST`/`VALUES`, `PIVOT` and several functions, and silently
   returns wrong results for `QUALIFY`, `ANTI JOIN` and `DISTINCT ON`. Window functions are the
   deepest gap (planner, executor and optimizer, rich semantics), and DuckDB runs the same SQL,
   so the verifier can check behaviour by executing both engines on the same data.
2. **Oracle first.** Implementing the feature myself showed where the semantic traps are (NULL
   ordering defaults differ between the engines, `IGNORE NULLS` syntax, `COUNT(*)` relying on a
   planner rewrite, CTE renaming, an optimizer rewrite of windowed `IN` subqueries); each became
   an instruction clause or a generator restriction.
3. **Differential verifier with unique answers.** Random datasets and templated queries (fixed
   seed), multiset comparison with DuckDB, only queries whose answer is unique, explicit `NULLS
   FIRST/LAST` everywhere.
4. **Reward by independent capabilities.** Ten groups, each counted only if every case is
   correct; cross-cutting concerns (types, NULLs) live in one group, so a bug costs only the
   group it belongs to; a regression gate protects existing behaviour.
5. **Hostile `/app`.** The code under test runs as `nobody` in a child process with an audit hook
   (no `sqlite3`/`ctypes`/`duckdb`, no subprocesses); compiled native files in `/app` are
   rejected; time limits are enforced by the root parent; the verifier never imports from `/app`;
   DuckDB, `/tests` and `/logs` are root-only.
6. **Evidence for every criterion**, produced through the real trial lifecycle (agent container,
   export `/app`, fresh verifier container): repeated `selfcheck.py` runs, 10 attack "solutions"
   (all 0), one-defect oracle variants (0.3 to 0.9), a coverage audit (every requirement
   exercised at least 3 times), and agent runs on three models.

## Design evolution (each step driven by measurements)

| Version | Added | Evidence that motivated it |
|---|---|---|
| v1 | windows on small tables, `ROWS`/`RANGE` frames | first complete version |
| v2 | 50,000-row scale group (20 s per query), `GROUPS`, `EXCLUDE`; O(n log n) oracle | agents solved v1 with a quadratic implementation; the scale group now rejects it (>60 s vs ~1 s) |
| v3 | 10 independent capability groups; subquery group (oracle fixes `unnest_subqueries`); round-robin templates | agent failures showed that one cross-cutting bug should cost one group; coverage audit |
| v3.1 | string aggregates concentrated in `types_nulls`; 30 cases per group (60 composition, 12 scale) | failures now land in the group of their root cause |
| final | audit-hook sandbox, parent-enforced time limits, readable `/app`, regression cases first | independent review of hack-resistance and fairness |

**Model ladder on the final task** (same harness, offline container, 3 trials each): GPT-5.6
Luna 0.3 / 0.1 / 0.1 (mean 0.17), Terra 0.5 / 0.9 / 0.2 (0.53), Sol 0.8 / 1.0 / 1.0 (0.93). The
reward tracks capability, all 9 trials kept every regression case passing, and the mid-tier
model gets the widest spread, the regime with the most RL signal.

**Considered and not adopted:** an in-group threshold (half credit at ≥90% of a group), tested
on 19 saved solutions: it would have rewarded a solution with no `QUALIFY` support (0.7 → 0.8).

**Fairness checks built in:** a regression case that hit a pre-existing executor bug was
replaced; `/app` is appended (not prepended) to `sys.path`, so scratch files such as
`random.py` cannot break an honest solution (verified: 1.0); `/app` is made readable before the
unprivileged run, so files saved with mode 0600 still work (verified: 1.0).

**Task families considered:** optimizer-rule tasks, transpilation tasks (only DuckDB is
available as a reference engine, so they drift toward string matching) and pure performance
tasks (timing alone is noisy). The window-function task combines a correctness core with a
performance requirement.

## With more time

- Harder variants for frontier models: a larger scale group, window `FILTER`/`DISTINCT`
  aggregates, and more cases that require the optimizer fix.
- A second task from the same framework: `ROLLUP`/`CUBE`/`GROUPING SETS` with `GROUPING()`.
- The task generator: the probe lists a dozen executor gaps, each a task family, with the
  template set and table size as difficulty knobs.
- More trials per model to tighten pass-rate estimates.

## How I used AI

I used Claude Code (Claude) throughout: to read and summarise the repository and the task
format, probe sqlglot and DuckDB behaviour, write the oracle, the verifier and the query
generator, build the Docker images, run selfcheck, write and run the attack and partial-credit
variants, and draft this file and the task README. Difficulty was measured by giving only the
instruction to agents: Claude agents in a clean copy of the repository, and GPT-5.6 Luna, Terra
and Sol through OpenRouter with a small agent harness (`evidence/or_agent.py`) that runs the
model inside the real offline task container; all final code was scored through the real trial
lifecycle. I reviewed the design decisions and results. `REPORT.md` is written by me.
