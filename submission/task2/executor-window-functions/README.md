# executor-window-functions (reviewer notes)

Add SQL window functions to sqlglot's pure-Python executor (`sqlglot.executor.execute`):
ranking functions, aggregates over frames, `ROWS`/`RANGE`/`GROUPS` frames (value offsets,
`EXCLUDE`), navigation functions with `IGNORE NULLS`, `QUALIFY`, named windows, windows over
`GROUP BY`/joins/CTEs, windows inside `IN`/`EXISTS`/scalar subqueries, and all of it fast
enough for a 50,000-row table. Today the executor raises on any `OVER (...)` and silently
ignores `QUALIFY`. This is a real gap at the pinned commit (v30.33.0), not a reverted
upstream change.

| | |
|---|---|
| Oracle | ~640 added lines: `sqlglot/planner.py` (226), `sqlglot/executor/python.py` (406), `sqlglot/optimizer/unnest_subqueries.py` (8) |
| Verifier | 312 window cases in 10 capability groups + 60 regression cases, differential against DuckDB 2.0.1 |
| Verifier runtime | ~27 s for the oracle, ~15 s for the no-op (timeout 900 s) |
| Reward | fraction of the 10 groups in which every case is correct; 0 if any regression case fails |
| Version | 3.1 (see "Iterations" for v1 to v3.1) |

## Difficulty explanation

**What it tests.** Designing a feature across three layers of an unfamiliar query engine and
getting a large body of SQL semantics exactly right, efficiently:

- *Planning.* Window functions are evaluated after `WHERE`/`GROUP BY`/`HAVING` and before
  `QUALIFY`/`DISTINCT`/`ORDER BY`/`LIMIT`, and their arguments can be aggregates
  (`SUM(SUM(x)) OVER (...)`, `RANK() OVER (ORDER BY COUNT(*))`). The agent must fit this into
  `planner.Step.from_expression` without breaking existing paths, and plan the *optimized* AST
  that `execute()` produces (named windows still references, `QUALIFY` aliases expanded,
  subqueries turned into CTEs whose last step is renamed, `COUNT(*)` relying on a planner
  rewrite of `*`).
- *Semantics.* Peers vs rows (the default frame is `RANGE` through the last peer), value-offset
  `RANGE` frames where `PRECEDING` flips under `DESC`, `GROUPS` frames, `EXCLUDE
  CURRENT ROW/GROUP/TIES`, empty frames, `NTILE` bucket sizes, `PERCENT_RANK`/`CUME_DIST` on
  ties, `IGNORE NULLS` counting, NULL partitions, NULL ordering, `DESC` on strings,
  `MIN`/`MAX` over strings.
- *Algorithms.* The scale group runs on 50,000 rows with a 20 s limit per query. The obvious
  implementation (evaluate each row's aggregate over its frame) is quadratic: a running `SUM`,
  `MIN` over `ROWS UNBOUNDED PRECEDING`, a wide `RANGE` offset or `FIRST_VALUE IGNORE NULLS`
  over a growing frame each took more than 60 s (killed) in the first version of the oracle,
  versus 0.8 to 1.1 s after optimisation (prefix sums, sparse tables, binary search, index
  lists).
- *Optimizer interaction.* `IN (SELECT ... QUALIFY ...)` and `x IN (SELECT LAG(y) OVER ...)`
  are broken by the optimizer's `unnest_subqueries`, which adds a `GROUP BY` to the subquery
  (changing what its windows compute) or skips de-duplication (window functions are
  `AggFunc` subclasses).

### Agent runs

Agents received only `instruction.md`. A small agent harness (`../evidence/or_agent.py`: bash and
write-file tools, OpenRouter, reasoning effort high, limits 150 turns / $20 / 90 min) ran each
model inside a container started from the environment image with `--network none`, 2 CPUs and
4 GB; the final `/app` was exported and scored in a fresh verifier container, exactly like a
trial. Three models of one family, 3 trials each:

| Model | Trial rewards | Mean | Groups passed per trial |
|---|---|---|---|
| GPT-5.6 Luna (small) | 0.3 / 0.1 / 0.1 | 0.17 | 3, 1, 1 of 10 |
| GPT-5.6 Terra (medium) | 0.5 / 0.9 / 0.2 | 0.53 | 5, 9, 2 of 10 |
| GPT-5.6 Sol (large) | 0.8 / 1.0 / 1.0 | 0.93 | 8, 10, 10 of 10 |

GPT-5.6 Sol in detail:

| Trial | Reward | Groups passed | Groups failed | Window cases passed | Time / cost |
|---|---|---|---|---|---|
| 1 | 0.8 | 8/10 | `types_nulls`, `subqueries` | 304/312 | 7.8 min / $2.24 |
| 2 | 1.0 | 10/10 | | 312/312 | 5.2 min / $1.49 |
| 3 | 1.0 | 10/10 | | 312/312 | 5.0 min / $1.51 |

Typical failure modes are exactly the ones the groups are designed to isolate: `DESC` ordering
implemented by negating keys (crashes on strings and NULLs, `types_nulls`), quadratic frame
evaluation (`scale`), frame-boundary errors (`rows_frames`, `range_frames`, `groups_exclude`), and
a scalar subquery next to a window aggregate that the optimizer rewrites into an aggregate query
(`subqueries`). All 9 trials passed every regression case, and re-scoring the 9 saved solutions
under the final verifier gives identical rewards (`../evidence/agent_runs_rescored_final.txt`).

**Assessment.** The reward tracks model capability (Luna 0.17, Terra 0.53, Sol 0.93). Mid-tier
models get the widest spread (Terra: 0.2 to 0.9 across trials), the regime with the most RL
signal; the strongest model tested solves the task completely in 2 of 3 trials, and partial
solutions earn graded credit that names what is missing.

## Solution explanation

The oracle (`solution/solve.sh` copies three files) adds a `Window` step to the plan.

*Planner (`planner.py`).* When a SELECT has window functions in its projections, `QUALIFY` or
`ORDER BY`, `_split_windows` rewrites it into an inner query that keeps `FROM`, joins,
`WHERE`, `GROUP BY`, `HAVING` and projects every value the windows and the remaining clauses
need (maximal window-free sub-expressions such as `x`, `SUM(x)`, `COUNT(*)`), and an outer
query over it whose window functions only reference plain columns, with `QUALIFY` turned into
`WHERE`, plus `DISTINCT`, `ORDER BY`, `LIMIT`, `OFFSET`. Named windows are inlined first. The
window expressions become columns produced by a new `Window` step between the scan of the inner
query and the existing distinct/sort steps. Reusing the existing planner for both halves keeps
grouping, `HAVING`, `DISTINCT`, ordering and limits unchanged; queries without windows are
untouched.

*Executor (`executor/python.py`).* Each partition is sorted once with the executor's own
`ORDERED(...)` key (so `ASC`/`DESC`/`NULLS FIRST|LAST` match `ORDER BY`); `_FrameBuilder` turns
every row's frame into position ranges in O(1) (`ROWS`, peer `RANGE`, `GROUPS` via peer-group
arrays) or O(log n) (`RANGE` offsets by binary search), and `EXCLUDE` splits a frame into up to
three ordered pieces. `SUM`/`COUNT`/`AVG` use prefix sums (exact for integers), `MIN`/`MAX` a
sparse table whose combine skips NULLs (works for strings), `FIRST_VALUE`/`LAST_VALUE`/
`NTH_VALUE` and `LAG`/`LEAD` with `IGNORE NULLS` binary search over non-NULL positions. O(n log n)
per window.

*Optimizer (`optimizer/unnest_subqueries.py`).* When an `IN` subquery contains window functions
or `QUALIFY`, it is de-duplicated over a derived table instead of having `GROUP BY` added to it.

Checked against DuckDB on all 372 generated cases (slowest scale case 1.7 s locally) and on
sqlglot's own optimizer and executor test suites (227 tests), identical before and after.

## Verification explanation

**Behaviour, not strings.** `verify.py` generates 5 small datasets (1 to 37 rows, NULLs, many
ties, shuffled row order) and one 50,000-row dataset, and 312 window queries from templates
(fixed seed, so every run is identical), plus 12 regression queries per small dataset. It
executes each query with the agent's `sqlglot.executor.execute` and with DuckDB 2.0.1 (pinned,
verifier image only), and compares rows as multisets (positional values, numbers compared
numerically with 1e-9 relative tolerance, NULL as `None`).

**Independent capability groups.** `types_nulls`, `ranking`, `aggregates`, `rows_frames`,
`range_frames`, `groups_exclude`, `navigation`, `composition`, `subqueries`, `scale`. Concerns
that every window touches (multi-key ordering, `DESC` on strings, NULL keys, `NULLS FIRST/LAST`,
`MIN`/`MAX` over strings) are concentrated in `types_nulls`; every other group orders by the
never-NULL integer columns `y`, `h`, `id` and aggregates numbers. This was learned from the
agent runs: with coarser groups, one cross-cutting bug zeroed most groups (see the table above).
The instruction lists the groups and this rule. Templates within a group are used
round-robin, and a coverage audit (`tests/coverage.py`) confirms every feature named in the
instruction is exercised at least 3 times.

**Fairness.** Every query is in the subset both engines read the same way; every `ORDER BY` key
states `NULLS FIRST/LAST` (the engines' defaults differ); whenever a result depends on the order
of tied rows, `id` is appended as a final key so the answer is unique; `RANGE` offsets only use
the never-NULL integer column `y`; `GROUPS`/`EXCLUDE GROUP|TIES` only combine with
order-insensitive aggregates. Every construct, the tables, the API call, the comparison rule,
the groups and the time limits are stated in `instruction.md`.

**Timing.** Only the scale group is time-sensitive: 20 s per query on 2 CPUs, about 12x the
oracle's slowest case and far below a frame-scanning implementation. A submission that sits
near the limit could be flaky; the instruction states the limit and the reference's speed.

**Partial credit.** A group counts only if all its cases pass; reward = complete groups / 10;
any regression failure gives 0. One-defect variants of the oracle (v3.1 verifier):

| Variant | Groups | Reward |
|---|---|---|
| explicit frames ignored | 3/10 | 0.3 |
| default frame stops at the current row (peers ignored) | 6/10 | 0.6 |
| `QUALIFY` not applied | 7/10 | 0.7 |
| `IGNORE NULLS` ignored | 8/10 | 0.8 |
| `DESC` by negating keys (crashes on strings) | 9/10 | 0.9 |
| optimizer fix missing | 9/10 | 0.9 |
| windows correct but `LEFT JOIN` broken | 8/10, regression fails | 0.0 |
| oracle plus scratch files `random.py`, `json.py`, `re.py`, ... in `/app` | 10/10 | 1.0 |
| oracle with every file saved as mode 0600 / directories 0700 | 10/10 | 1.0 |

An in-group threshold (half credit for a group with at least 90% of its cases correct) was
evaluated on 19 saved solutions and not adopted: it changed only one of nine agent rewards
(0.80 to 0.85) but raised a solution with no `QUALIFY` support from 0.7 to 0.8, because a group
that bundles several features can stay above 90% while one feature is missing entirely.

**Isolation.** `/app` is treated as hostile. `test.sh` only starts `verify.py` with the reference
interpreter (`/opt/ref/bin/python -I`, cwd `/tests`), which never imports from `/app`. Before
running anything it checks that `/app/sqlglot` is a real directory inside `/app`, rejects
compiled native files (ELF) under `/app`, clears setuid/setgid bits, and makes `/app` readable
(so files saved with restrictive modes still work). The code under test runs in a child process
started with `setpriv` as `nobody` with `no_new_privs`, no capabilities and a clean environment,
under a Python audit hook installed before any code from `/app` runs: importing `sqlite3`,
`ctypes`, `duckdb` or `cffi`, loading libraries and starting processes are refused, so the engine
has to be implemented rather than delegated (the instruction states this). `/tests`, `/opt/ref`
(DuckDB) and `/logs` are `0700 root`; `/app` is appended to the child's `sys.path`, so stray files
cannot shadow the standard library. Time limits are enforced by the root parent: if a case does
not answer within its limit (plus a 5 s grace) or the child dies, that case fails and a fresh
child continues with the remaining cases. Regression cases run first. The child receives queries
and data only and answers over a pipe (stdout redirected, nonce-tagged lines, plain JSON values
only, accepted only in case order). DuckDB results are computed after the child exits; the reward
is written last, by root.

**Hacks tried** (each built as a "solution" and run through the full lifecycle on v3.1; all
scored 0, with the blocking error in the verifier log):

| Attack | Result |
|---|---|
| write `/logs/verifier/reward.txt`, overwrite `/tests/verify.py` and `test.sh` | `Permission denied` |
| proxy `execute` to DuckDB from the reference environment | `No module named 'duckdb'` |
| setuid-root copies of `bash` and `python` in `/app` | run as `uid=65534(nobody)` |
| `sitecustomize.py`, `usercustomize.py`, `conftest.py`, `*.pth`, `pytest.ini`, stdlib-named files | never executed by the verifier |
| `/app/sqlglot` replaced by a symlink | rejected by the integrity check |
| steal the nonce and forge result lines on every fd | forged rows are wrong without the answers |
| row objects whose `__eq__` is always true | rejected, plain values only |
| kill the verifier / PID 1, read `/tests` | `Operation not permitted` / `Permission denied` |
| delegate every query to the standard-library `sqlite3` (via `sqlglot.transpile`) | `ImportError` from the audit hook |
| copy the compiled `_sqlite3` extension into `/app` | rejected before running (native code under `/app`) |

A timing attack (oracle that disables the in-process alarm and takes 40 s per large-table query)
loses exactly the scale group (0.9): the parent's per-case limit still applies.

**Known holes.**

- The seed is fixed (required for a deterministic reward), so the same queries are used every
  time; memorisation across episodes is possible in principle.
- Features outside the templates (`FILTER`, `DISTINCT` inside window aggregates) are neither
  required nor checked.
- Optimizer-dependent cases form one group (0.1 of the reward), and lenient implementations can
  pass some of them without changing the optimizer.
- Isolation relies on Linux permissions and a Python audit hook inside the container.

## Design evolution

| Version | Added | Motivation |
|---|---|---|
| v1 | windows on small tables, `ROWS`/`RANGE` frames | first complete version |
| v2 | 50,000-row scale group, `GROUPS`, `EXCLUDE`; O(n log n) oracle | require efficient algorithms, not only semantics |
| v3 | 10 independent capability groups; subquery group with the optimizer fix; round-robin templates | one bug should cost one group; even coverage |
| v3.1 | cross-cutting concerns (types, NULLs, string aggregates) concentrated in `types_nulls` | failures land in the group of their root cause |
| final | audit-hook sandbox, parent-enforced time limits, readable `/app`, regression first | hack-resistance and fairness hardening |

Logs and scripts behind every number in this README are in `../evidence/`.
