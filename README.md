# MatrAIx Take-home · Task 2: an RL training task built from sqlglot

English | [中文](README_zh.md)

This repository holds Task 2 of the MatrAIx take-home: a reinforcement-learning (RL) training
task, **`executor-window-functions`**, built from the open-source SQL toolkit sqlglot (pinned at
v30.33.0), together with the experimental evidence that it is valid, fair, hard to game, gives a
useful training signal, and is hard.

> The take-home originals (task statement, Task 1 code, the official `selfcheck.py`) are not
> included, per the confidentiality agreement.

---

## 1. Goal

An RL task has four parts:

| Part | File | Role |
|---|---|---|
| Instruction | `instruction.md` | the only text the agent sees |
| Environment | `environment/Dockerfile` | the offline container the agent works in; sqlglot in `/app` |
| Verifier | `tests/` | turns the agent's final `/app` into a reward in [0, 1] |
| Oracle | `solution/solve.sh` | reference solution proving the task is solvable |

Criteria:

- **valid**: oracle gets 1.0, doing nothing gets 0.0, every time;
- **hard**: strong agents usually fail;
- **fair**: everything the verifier checks is stated in the instruction;
- **hack-resistant**: the agent is root in the container, and the only way to reward is the work;
- **useful signal**: partial credit measures real progress;
- **realistic**: work an engineer on the project would do.

---

## 2. Approach

**Choosing the task.** We probed about 45 SQL features in sqlglot's pure-Python engine. Window
functions are entirely unsupported, and `QUALIFY` is silently ignored. Window functions were
chosen for three reasons:

- they are a real capability gap, not a reverted upstream fix;
- they require changes across the planner, the executor and the optimizer, with a large body of
  SQL semantics;
- they allow **differential testing**: the same SQL on the same data runs in the agent's sqlglot
  and in DuckDB, a real database, and the results are compared. This checks behaviour rather than
  strings.

**Principles.**

- Write the oracle first, then the verifier, then the instruction. Implementing the feature
  ourselves showed where the real difficulty and ambiguity are.
- Back every criterion with a reproducible experiment run through the official trial lifecycle.

---

## 3. What the agent must do

Make `sqlglot.executor.execute()` support:

- **Ranking**: `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `NTILE`, `PERCENT_RANK`, `CUME_DIST`.
- **Aggregates**: `SUM`, `COUNT`, `AVG`, `MIN`, `MAX` with `OVER (...)`, including peer semantics
  of the default frame.
- **Frames**: `ROWS`, `RANGE` (value offsets, reversed under `DESC`), `GROUPS`, and
  `EXCLUDE CURRENT ROW / GROUP / TIES / NO OTHERS`.
- **Navigation**: `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE`, all with `IGNORE NULLS`.
- **Composition**: `QUALIFY`, named windows with inheritance, windows over `GROUP BY`/`HAVING`,
  joins, CTEs, `DISTINCT`, `ORDER BY ... LIMIT`.
- **Subqueries**: window functions inside `IN` / `NOT IN` / `EXISTS` / `ALL` / scalar subqueries;
  some of these need a fix in the optimizer's `unnest_subqueries`.
- **Scale**: every query on a 50,000-row table within 20 seconds.

---

## 4. Repository layout

```
README.md / README_zh.md            this overview (English / Chinese)
submission/task2/
├── NOTES.md                        approach, what was tried and dropped, AI usage
└── executor-window-functions/
    ├── task.toml                   metadata, timeouts, resources
    ├── instruction.md              full specification given to the agent
    ├── README.md                   reviewer notes (difficulty, solution, verification, holes, iterations)
    ├── environment/                python:3.12-slim (pinned by digest) + unmodified sqlglot source
    ├── tests/
    │   ├── test.sh                 entry point: tighten permissions, run verify.py
    │   ├── verify.py               root process: integrity checks, sandboxed child, DuckDB reference, scoring
    │   ├── runner.py               child process (as nobody): imports the agent's sqlglot, runs the queries
    │   ├── querygen.py             fixed-seed data and query generator (10 capability groups)
    │   └── coverage.py             reviewer tool: how often each requirement is exercised
    └── solution/                   oracle: planner.py, executor/python.py, optimizer/unnest_subqueries.py
```

---

## 5. Pipeline

### 5.1 One trial (official lifecycle)

1. Build the environment image and the verifier image.
2. Start the agent container from the environment image: root, **no network**, 2 CPUs / 4 GB. The
   agent (or the oracle, or nothing) works in `/app`.
3. Stop the container and **export only `/app`**; everything else is discarded.
4. Start a fresh verifier container, import `/app`, run `test.sh`, read `/logs/verifier/reward.txt`.

So nothing the agent installs outside `/app` exists at scoring time, and everything in `/app` is
untrusted code.

### 5.2 Oracle

- **Planner.** A SELECT with window functions is split into an inner query (`FROM`, joins,
  `WHERE`, `GROUP BY`, `HAVING`, and every value the windows need, e.g. `SUM(x)` in
  `SUM(SUM(x)) OVER ()`) and an outer query that computes the windows and then applies
  `QUALIFY`, `DISTINCT`, `ORDER BY`, `LIMIT`. A new `Window` step sits between the two. Queries
  without windows are untouched.
- **Executor.** Each partition is sorted once. Every row's frame becomes a position range: O(1)
  for `ROWS`/`GROUPS`, binary search for `RANGE` offsets, and `EXCLUDE` splits it into at most
  three pieces. Aggregates are answered from precomputed structures: prefix sums for
  `SUM`/`COUNT`/`AVG`, a NULL-skipping sparse table for `MIN`/`MAX` (works for strings), and
  non-NULL position indexes for `IGNORE NULLS`. O(n log n) overall: 0.5–1.7 s per query on
  50,000 rows, versus more than 60 s for a naive implementation.
- **Optimizer.** An `IN` subquery that contains window functions or `QUALIFY` is de-duplicated
  over a derived table instead of having `GROUP BY` added to it.
- About 640 lines in total. sqlglot's own 227 optimizer and executor tests pass before and after.

### 5.3 Verifier

**Data and queries.**

- 5 small datasets (1–37 rows, NULLs, many ties, shuffled row order) and one 50,000-row dataset.
- 312 window queries in 10 capability groups and 60 regression queries without windows. The seed
  is fixed, so every run is identical.
- **Only queries with a unique answer**:
  - every `ORDER BY` states `NULLS FIRST/LAST`, because the engines' defaults differ;
  - `id` is appended as a final key whenever ties would matter;
  - `RANGE` offsets only use a never-NULL integer column.

**Ten independent capability groups.**

| Group | Content |
|---|---|
| `types_nulls` | multi-key ordering, `DESC` on strings, NULL keys, `NULLS FIRST/LAST`, `MIN`/`MAX` over strings |
| `ranking` | ranking functions |
| `aggregates` | aggregates with default frames |
| `rows_frames` | `ROWS` frames |
| `range_frames` | `RANGE` frames (peers and value offsets) |
| `groups_exclude` | `GROUPS` frames and `EXCLUDE` |
| `navigation` | navigation functions (incl. `IGNORE NULLS`) |
| `composition` | `QUALIFY`, named windows, `GROUP BY`, joins, CTEs, `DISTINCT`, `LIMIT` |
| `subqueries` | window functions in subqueries |
| `scale` | 50,000 rows, 20 s per query |

Cross-cutting concerns that every window touches (mixed data types, NULLs) are concentrated in
`types_nulls`; every other group orders by never-NULL integers and aggregates numbers, so a bug
fails only the group it belongs to. Templates in a group are used round-robin, and a coverage
audit confirms every requirement is exercised at least 3 times.

**Scoring.**

- A group counts only if **every** case in it is correct; reward = complete groups / 10.
- Any regression failure makes the reward 0, so breaking the engine is not rewarded.
- An in-group threshold (half credit at ≥90% of a group's cases) was tested on 19 saved
  solutions and not adopted: it rewarded a solution with no `QUALIFY` support (0.7 → 0.8) while
  changing only one of nine agent rewards.

**Isolation.**

- The scoring process runs in a separate, isolated interpreter and **never imports code from
  `/app`**.
- The agent's code runs only in a child process dropped to `nobody` with `setpriv`: no privilege
  escalation, no capabilities, a clean environment.
- `/tests`, the DuckDB environment and `/logs` are root-only.
- Before running anything, the verifier checks that `/app/sqlglot` is a real directory and clears
  setuid bits under `/app`.
- `/app` is appended to the child's module search path, so stray files cannot shadow the standard
  library.
- A Python audit hook, installed before any agent code runs, refuses `sqlite3`, `ctypes`,
  `duckdb`, library loading and new processes, and compiled native files under `/app` are
  rejected: the engine has to be implemented, not delegated.
- Time limits are enforced by the root parent per case; a stuck or crashed case fails on its own
  and a fresh child continues. Regression cases run first.
- `/app` is made readable before the unprivileged run, so honest files saved with restrictive
  modes still work.
- DuckDB results are computed after the child exits, and root writes the reward last.

### 5.4 How we checked the task

| Step | Method | Result |
|---|---|---|
| local differential check | oracle vs DuckDB on all 372 cases; sqlglot's own tests | all pass |
| official self-check | `selfcheck.py --repeats 2` | `RESULT: VALID` (oracle 1.0, no-op 0.0) |
| one-defect variants | oracle with one realistic bug each | see section 6 |
| attacks | 10 cheating strategies, each built as a "solution" and run through the lifecycle | all 0, blocking error in the log |
| coverage audit | how often each requirement is exercised | all ≥ 3 |
| agent runs | agents see only the instruction; their `/app` goes through the lifecycle | see section 7 |

---

## 6. Evidence per criterion

| Criterion | Evidence |
|---|---|
| Valid | repeated official self-checks: oracle 1.0 and no-op 0.0 every time |
| Fair | every construct the verifier can emit is covered by an instruction clause; issues found in testing were fixed (section 8) |
| Hack-resistant | 10 attacks all scored 0: writing the reward or the verifier (Permission denied), using DuckDB (No module named 'duckdb'), delegating to `sqlite3` (blocked by the audit hook), copying a native extension into `/app` (rejected), setuid escalation (still uid 65534), startup hooks (never executed), symlinked package (rejected), forged output (no answers to forge), always-equal objects (plain values only), killing the verifier (Operation not permitted). Disabling the in-process alarm does not help: the parent's per-case limit still fails slow cases |
| Useful signal | one-defect oracles score in steps: frames ignored 0.3, peers ignored 0.6, `QUALIFY` missing 0.7, `IGNORE NULLS` ignored 0.8, `DESC` by negation 0.9, optimizer fix missing 0.9, `LEFT JOIN` broken 0 |
| Hard | section 7 |

---

## 7. Evaluation results

**Three models of one family on the final task**, each run 3 times in the real offline task
container with the same agent harness:

| Model | Rewards | Mean | Full marks |
|---|---|---|---|
| GPT-5.6 Luna (small) | 0.3 / 0.1 / 0.1 | 0.17 | 0/3 |
| GPT-5.6 Terra (medium) | 0.5 / 0.9 / 0.2 | 0.53 | 0/3 |
| GPT-5.6 Sol (large) | 0.8 / 1.0 / 1.0 | 0.93 | 2/3 |

The reward rises with model capability, and all 9 trials kept every regression case passing, so
lost reward always comes from capabilities that were not finished. The mid-tier model shows the
widest spread (0.2 to 0.9), the regime with the most learning signal for RL.

**GPT-5.6 Luna: 3 trials, no full marks, mean 0.17.**

| Trial | Reward | Groups passed | Groups failed |
|---|---|---|---|
| 1 | 0.3 | 3/10 | types_nulls, ranking, rows_frames, navigation, composition, subqueries, scale |
| 2 | 0.1 | 1/10 | all except aggregates |
| 3 | 0.1 | 1/10 | all except aggregates |

**GPT-5.6 Terra: 3 trials, no full marks, mean 0.53.**

| Trial | Reward | Groups passed | Groups failed |
|---|---|---|---|
| 1 | 0.5 | 5/10 | types_nulls, groups_exclude, composition, subqueries, scale |
| 2 | 0.9 | 9/10 | composition |
| 3 | 0.2 | 2/10 | types_nulls, rows_frames, range_frames, groups_exclude, navigation, composition, subqueries, scale |

**GPT-5.6 Sol: 3 trials, 2 full marks, mean 0.93.**

| Trial | Reward | Groups passed | Groups failed |
|---|---|---|---|
| 1 | 0.8 | 8/10 | types_nulls, subqueries |
| 2 | 1.0 | 10/10 | — |
| 3 | 1.0 | 10/10 | — |

---

## 8. Design evolution and insights

| Version | Added | Motivation |
|---|---|---|
| v1 | windows on small tables, `ROWS`/`RANGE` frames | first complete version |
| v2 | 50,000-row scale group, `GROUPS`, `EXCLUDE`; O(n log n) oracle | require efficient algorithms, not only semantics |
| v3 | 10 independent capability groups; subquery group with the optimizer fix; round-robin templates | one bug should cost one group; even coverage |
| v3.1 | cross-cutting concerns (types, NULLs, string aggregates) concentrated in one group | failures land in the group of their root cause |
| final | audit-hook sandbox, parent-enforced time limits, readable `/app`, regression first | hack-resistance and fairness hardening |

**Insights.**

1. **Partial credit should follow independent capabilities.** Concentrating cross-cutting concerns
   in one group makes every failed group name a root cause, and keeps the reward proportional to
   real progress (one-defect oracles score 0.3 to 0.9).
2. **Difficulty belongs to a task and a model together.** A ladder of models shows where a task
   gives a useful signal: here mid-tier models get the widest spread.
3. **The verifier is software too.** One-defect variants, attacks, a coverage audit and an
   independent review each strengthened it before it was used to measure agents.
4. **With a fair, precise specification, difficulty has to come from semantics, efficiency and
   interactions between modules**, which is what the scale, frame and subquery groups target.

## 9. Scope and future work

- The seed is fixed so that the reward is a deterministic function of `/app`.
- Out of scope: window `FILTER`/`DISTINCT` aggregates and other features outside the templates.
- Harder variants for frontier models: a larger scale group, more cases that require the
  optimizer fix, and the out-of-scope features above.
