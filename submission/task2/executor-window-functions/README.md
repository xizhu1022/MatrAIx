# executor-window-functions (reviewer notes)

Add SQL window functions to sqlglot's pure-Python executor (`sqlglot.executor.execute`):
ranking functions, aggregates over frames, `ROWS`/`RANGE`/`GROUPS` frames (value offsets,
`EXCLUDE`), navigation functions with `IGNORE NULLS`, `QUALIFY`, named windows, windows over
`GROUP BY`/joins/subqueries, and all of it fast enough for a 50,000-row table. Today the executor raises on any `OVER (...)` and silently
ignores `QUALIFY`. This is a real gap in sqlglot at the pinned commit (v30.33.0), not a
reverted upstream change.

| | |
|---|---|
| Oracle size | ~630 added lines in `sqlglot/planner.py` (226) and `sqlglot/executor/python.py` (406) |
| Verifier | 212 window cases in 6 groups + 60 regression cases, differential against DuckDB 2.0.1 |
| Verifier runtime | ~26 s measured for the oracle, ~16 s for the no-op (timeout 900 s) |
| Reward | fraction of complete groups (0, 1/6, ..., 1), 0 if any regression case fails |

## Difficulty explanation

**What it tests.** Designing a feature across two layers of an unfamiliar query engine and
getting a large body of SQL semantics exactly right:

- *Planning.* The executor plans a SELECT as a chain of steps (scan, join, aggregate, sort)
  and evaluates projections with code generated from the AST. Window functions are evaluated
  after `WHERE`/`GROUP BY`/`HAVING` but before `QUALIFY`/`DISTINCT`/`ORDER BY`/`LIMIT`, and
  their arguments may themselves be aggregates (`SUM(SUM(x)) OVER (...)`,
  `RANK() OVER (ORDER BY COUNT(*))`). The agent has to find where this fits into
  `planner.Step.from_expression`, which mixes projection rewriting, aggregate extraction and
  naming conventions, and has to keep the existing paths intact.
- *Semantics.* Peers vs rows (`RANGE` vs `ROWS`, and the default frame being `RANGE` through
  the last peer), value-offset `RANGE` frames where `PRECEDING` flips meaning under `DESC`,
  empty frames, `NTILE` bucket sizes, `PERCENT_RANK`/`CUME_DIST` on ties and single-row
  partitions, `IGNORE NULLS` counting for `LAG`/`LEAD`/`NTH_VALUE`, NULL partitions, NULL
  ordering, `COUNT(*)` vs `COUNT(x)` over frames.
- *Interaction with the rest of sqlglot.* `execute()` first runs the optimizer, so the agent
  plans the *optimized* AST: named windows are still references, `QUALIFY` aliases are
  expanded, subqueries become CTEs (and the last step of a CTE is renamed), `COUNT(*)` relies
  on a planner rewrite of `*` that windows also need.

- *Algorithms.* The scale group runs the same kinds of queries on a 50,000-row table with a
  20 s limit per query. The obvious implementation (evaluate each row's aggregate over its
  frame) is quadratic: measured on the reference code before it was optimised, a running
  `SUM`, `MIN` over `ROWS UNBOUNDED PRECEDING`, a wide `RANGE` offset or `FIRST_VALUE IGNORE
  NULLS` over `CURRENT ROW AND UNBOUNDED FOLLOWING` each took **more than 60 s** (killed), versus
  0.8 to 1.1 s after optimisation. Passing requires prefix sums, sparse tables (or another
  O(log n) range-min structure that works for strings and with NULLs), binary search for
  `RANGE` offsets and peer groups for `GROUPS`, index lists for `IGNORE NULLS`, and handling
  `EXCLUDE` by splitting frames into pieces, all while staying exactly correct.

Each group requires *every* case to be right, so "mostly works" is not rewarded; a single
semantic slip in, say, peer handling costs a whole group. The verifier's cases are generated
from random data and random combinations, so an agent cannot pass by testing only the
examples it thought of.

**How difficulty was checked.** See "Agent runs" below.

### Agent runs

Each agent got only `instruction.md` and a clean copy of the shipped sqlglot (told not to
read anything else; its transcript was afterwards searched for any access to the tests,
the oracle or other runs, and none was found). Its final code was then scored through the
exact trial lifecycle (agent container, export `/app`, fresh verifier container). The agents
ran on the host with Python 3.12 and no DuckDB, not inside the offline container, so this is
an approximation of a real trial.

| Version | Agent | Time | Reward | Notes |
|---|---|---|---|---|
| v1 (small tables, `ROWS`/`RANGE` only) | Claude Sonnet | ~4 min | **1.0** | correct, naive (quadratic) frame evaluation |
| v2 (this version) | same v1 Sonnet solution, re-scored | | 0.5 | fails `GROUPS`/`EXCLUDE` (frames) and scale |
| v2 (this version) | Claude Sonnet, fresh run | ~9 min | **1.0** | wrote a brute-force reference from the instruction and cross-checked ~5,800 random queries; O(n) frame evaluation |
| v1 | stronger Claude model, re-scored on v2 | ~33 min | 1.0 on v1, 0.5 on v2 | its v1 instruction had no `GROUPS`/`EXCLUDE`/scale |

**GPT-5.6 Sol in the real offline container.** A minimal agent harness (bash and write-file
tools, OpenRouter API, reasoning effort high, limits of 150 turns / $20 / 90 min) ran the model
inside a container started from the environment image with `--network none`, 2 CPUs and
4 GB, exactly like a trial; `/app` was then exported and scored in a fresh verifier container.

| Trial | Reward | Groups passed | Groups failed | Window cases passed | Time / cost |
|---|---|---|---|---|---|
| 1 | 0.1667 | 1/6 | ranking, aggregates, frames, composition, scale | 199/212 (94%) | 5.0 min / $1.43 |
| 2 | (excluded) | | harness network error after 26 turns | | |
| 3 | **1.0** | 6/6 | | 212/212 | 6.0 min / $1.65 |
| 4 | 0.1667 | 1/6 | aggregates, frames, navigation, composition, scale | 197/212 (93%) | 6.1 min / $1.33 |

Mean over the 3 valid trials 0.44, pass rate 1/3. Trials 1 and 4 failed for the same reason:
`DESC` ordering implemented by negating keys, which crashes on strings and NULLs. Because every
group contains some `DESC` string or NULL key, this one bug zeroes five groups although 93-94%
of the cases pass. The rewards are therefore bimodal (0.17 or 1.0): the groups are too coarse
and too correlated (ordering is shared by all of them). Splitting the score into ~15
independent capability groups, with a separate group for ordering and NULL placement, would
make the reward track progress; see "Known holes".

**Honest assessment.** The task is valid, fair and hard to game, and its partial credit is
well graded (see the variant table under "Verification explanation"), but it is **not hard
for current frontier agents**: a mid-tier model solved both versions. The main reason is
that the instruction has to be a complete, precise specification (fairness), and with a
precise specification an agent can write its own brute-force reference and differential-test
against it, which is exactly what the successful run did. Hardening from v1 to v2 (a 50,000-row
scale group, `GROUPS`, `EXCLUDE`) turned a v1-level solution into 0.5 but did not stop a fresh
attempt. It is a useful *medium* task: weaker agents and first attempts that stop at "works on
my examples" or at the quadratic implementation get partial credit, and the full reward
requires a complete, efficient implementation.

**A harder axis found while testing (not in this version).** Window functions inside an
`IN (subquery)` fail in both the oracle and the Sonnet solution:
`SELECT id FROM t WHERE id IN (SELECT id FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY ...) = 1)`.
The cause is in the optimizer, not the window code: `unnest_subqueries` rewrites the `IN` into a
join on a derived table and adds `GROUP BY id` to the subquery, which changes the meaning of its
window functions (`g` is no longer available). Requiring windowed `IN`/`EXISTS`/scalar
subqueries would force agents to trace a wrong result back across the optimizer, which their
self-written references cannot reveal (both sides see the same rewritten AST). This is the
first thing I would add in a v3.

## Solution explanation

The oracle (`solution/solve.sh` copies two files) adds a `Window` step to the plan.

*Planner (`planner.py`).* When a SELECT has window functions in its projections, `QUALIFY` or
`ORDER BY`, `_split_windows` rewrites it into two queries:

- an inner query that keeps `FROM`, joins, `WHERE`, `GROUP BY`, `HAVING` and projects every
  value the windows and the remaining clauses need (maximal window-free sub-expressions such
  as `x`, `SUM(x)`, `COUNT(*)`, `x * 2 + y`), named `_wi_N`;
- an outer query over it (`FROM (inner) AS _w`), whose window functions only reference
  `_w._wi_N`, with `QUALIFY` turned into `WHERE`, plus `DISTINCT`, `ORDER BY`, `LIMIT`, `OFFSET`.

Named windows are inlined first (`WINDOW w AS (...), w2 AS (w ORDER BY ...)`,
`OVER (w2 ROWS ...)`). The window expressions of the outer query are replaced by columns
`_wv_N` and handed to a new `Window` step placed between the scan of `_w` and the existing
distinct/sort steps. Reusing the existing planner for both halves means grouping, `HAVING`,
`DISTINCT`, ordering and limits keep their current, tested behaviour; nothing changes for
queries without windows.

*Executor (`executor/python.py`).* `window()` evaluates each window: it partitions the input
rows by the `PARTITION BY` values, sorts each partition with the executor's own `ORDERED(...)`
sort key (so `ASC`/`DESC`/`NULLS FIRST|LAST` match `ORDER BY`), computes peer groups, and then
per row: ranking functions from positions and peer groups; `LAG`/`LEAD` by position (with an
`IGNORE NULLS` walk); `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE` over the frame; and aggregates by
pointing the executor's existing range reader at the frame `[lo, hi)` of the sorted rows, so
`SUM`/`COUNT`/`AVG`/`MIN`/`MAX` reuse the engine's aggregate implementations and NULL rules.
Each partition is sorted once; `_FrameBuilder` turns every row's frame into position ranges in
O(1) (`ROWS`, peer-based `RANGE`, `GROUPS` via peer-group start/end arrays) or O(log n)
(`RANGE` value offsets, by binary search over the sorted keys, with `DESC` handled by negating),
and `EXCLUDE` splits the range into up to three ordered pieces. `SUM`/`COUNT`/`AVG` are answered
from prefix sums of values and non-NULL counts (exact for integers), `MIN`/`MAX` from a sparse
table whose combine step skips NULLs, `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE` and `LAG`/`LEAD`
with `IGNORE NULLS` by binary search in the list of non-NULL positions. The whole evaluation
is O(n log n) per window; other aggregate functions fall back to evaluating the engine's own
aggregate over the frame.

Checked against DuckDB on all 272 generated cases (the slowest scale case takes 1.7 s
locally) and on sqlglot's own executor test suite (60 tests, including its DuckDB-compared
TPC-H queries), all passing.

## Verification explanation

**Behaviour, not strings.** `verify.py` generates 5 small datasets (1 to 37 rows, NULLs, ties,
shuffled row order) plus one 50,000-row dataset, and 212 window queries from templates with
random partitions, keys, directions, NULL orderings, frames (`ROWS`/`RANGE`/`GROUPS`,
`EXCLUDE`), functions and offsets (fixed seed, so the run is deterministic), plus 12 regression
queries per small dataset. It executes every query with the
agent's `sqlglot.executor.execute` and with DuckDB 2.0.1 (pinned, installed only in the
verifier image), and compares the result rows as multisets: positional values, numbers
compared numerically (floats with 1e-9 relative tolerance), NULL as `None`.

**Fairness.** Every query is in the subset both engines read the same way; every `ORDER BY`
key states `NULLS FIRST/LAST` (sqlglot's default differs from DuckDB's); whenever a result
depends on the order of tied rows (`ROW_NUMBER`, `NTILE`, navigation functions, `ROWS`
frames, `LIMIT`), `id` is appended as a final key so the answer is unique; `RANGE` offsets
are only used on the never-NULL integer column `y`; `GROUPS` frames and `EXCLUDE GROUP/TIES`
are only combined with order-insensitive aggregates, and navigation functions only use
`EXCLUDE CURRENT ROW` over a total order. Each of these restrictions, the tables,
the API call, the comparison rule, the groups and the time limits are stated in
`instruction.md`. A case-by-case check that every construct the generator emits is described
in the instruction found two omissions (`RANGE <start>` short form, total time limit), which
were added.

**Timing.** Only the scale group is time-sensitive. The limit (20 s per query on 2 CPUs) is
about 12x the reference's slowest case and far below the >60 s of a frame-scanning
implementation, so the oracle's result is stable (three selfcheck repeats, identical). A
submission that happens to sit near the limit could be flaky; the instruction states the
limit and the reference's speed so the agent can aim well inside it.

**Partial credit.** Six equal groups: ranking, aggregates with default frames, explicit
frames, navigation, composition, scale. A group counts only if all of its cases pass, so the reward
measures complete capabilities, not the number of lucky cases. If any regression case
(queries without windows that work today: filters, grouping, joins, `DISTINCT`, subqueries,
CTEs, `UNION ALL`, `ORDER BY ... LIMIT`) fails, the reward is 0, so breaking the engine to make
windows work is not rewarded. Plausible incomplete solutions, run through the full trial
lifecycle, score as intended:

| Variant | Reward |
|---|---|
| oracle, explicit frames ignored | 0.3333 |
| first (naive, quadratic) version of the oracle: correct semantics, no `GROUPS`/`EXCLUDE` | 0.5 |
| oracle, default frame stops at the current row instead of its last peer | 0.5 |
| oracle, `IGNORE NULLS` ignored | 0.6667 |
| oracle, `QUALIFY` not applied | 0.6667 |
| oracle, windows correct but `LEFT JOIN` broken | 0.0 |
| oracle plus scratch files named `random.py`, `json.py`, `re.py`, ... in `/app` | 1.0 |

**Isolation.** Everything in `/app` is treated as hostile. `test.sh` only starts `verify.py`
with the reference interpreter (`/opt/ref/bin/python -I`, cwd `/tests`), which never imports
from `/app`. Before running anything from `/app`, the verifier checks that `/app/sqlglot` is a
real directory inside `/app` and clears setuid/setgid bits under `/app`. The code under test
runs in a child process started with `setpriv` as `nobody` with `no_new_privs`, no
capabilities and a clean environment; `/tests`, `/opt/ref` (the DuckDB environment) and `/logs`
are `0700 root`. The child gets only queries and data, and answers over a pipe; its stdout is
redirected so stray prints cannot corrupt the protocol, and only plain JSON values are
accepted. DuckDB reference results are computed after the child has exited. The reward file
is written last, by root, replacing whatever is there.

**Hacks tried** (each built as a "solution" and run through the full lifecycle; all scored 0,
with the blocking error visible in the verifier log):

| Attack | Result |
|---|---|
| write `/logs/verifier/reward.txt`, overwrite `/tests/verify.py` and `test.sh` | `Permission denied` |
| proxy `execute` to DuckDB from the reference environment | `No module named 'duckdb'` (directory not readable) |
| setuid-root copies of `bash` and `python` in `/app` | run as `uid=65534(nobody)`; bits stripped, `no_new_privs` |
| `sitecustomize.py`, `usercustomize.py`, `conftest.py`, `*.pth`, `pytest.ini`, stdlib-shadowing `json.py`/`decimal.py`/... | never executed by the verifier (isolated interpreter, not on its path) |
| `/app/sqlglot` replaced by a symlink | rejected by the integrity check |
| steal the nonce from memory and forge result lines on every fd | forged rows are wrong without the answers; only lowers the agent's own score |
| return row objects whose `__eq__` is always true | rejected, only plain values are accepted |
| kill the verifier / PID 1, read `/tests/querygen.py` | `Operation not permitted` / `Permission denied` |

**Known holes.**

- Reward granularity: the six groups share the ordering machinery, so one bug in sort keys
  (seen in 2 of 3 GPT-5.6 Sol trials) fails five groups at once. Finer, independent groups
  would give a less bimodal, more informative reward.

- Windowed `IN` subqueries are not tested (see "A harder axis" above); both the oracle and
  agent solutions currently fail them because of an optimizer rewrite.

- Memorization across episodes: the seed is fixed, so the same queries are used every time.
  An agent that could see the verifier's queries (it cannot inside one trial) could
  special-case them. Rotating the seed per trial would remove this, at the cost of the
  reward no longer being a fixed function of `/app` (the format asks for determinism).
- Coverage: features outside the generator's templates (`GROUPS` frames, `EXCLUDE`,
  `FILTER`, `DISTINCT` inside window aggregates) are neither required nor checked; an
  implementation can be wrong there and still score 1.0.
- The isolation relies on Linux permissions inside the container; a kernel or `setpriv`
  privilege escalation would defeat it.
- Denial of service (fork bombs, filling memory) can only make the agent's own trial fail.
