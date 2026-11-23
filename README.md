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
- DuckDB results are computed after the child exits, and root writes the reward last.

### 5.4 How we checked the task

| Step | Method | Result |
|---|---|---|
| local differential check | oracle vs DuckDB on all 372 cases; sqlglot's own tests | all pass |
| official self-check | `selfcheck.py --repeats 2` | `RESULT: VALID` (oracle 1.0, no-op 0.0) |
| one-defect variants | oracle with one realistic bug each | see section 6 |
| attacks | 8 cheating strategies, each built as a "solution" and run through the lifecycle | all 0, blocking error in the log |
| coverage audit | how often each requirement is exercised | all ≥ 3 |
| agent runs | agents see only the instruction; their `/app` goes through the lifecycle | see section 7 |

---

## 6. Evidence per criterion

| Criterion | Evidence |
|---|---|
| Valid | repeated official self-checks: oracle 1.0 and no-op 0.0 every time |
| Fair | every construct the verifier can emit is covered by an instruction clause; issues found in testing were fixed (section 8) |
| Hack-resistant | 8 attacks all scored 0: writing the reward or the verifier (Permission denied), using DuckDB (No module named 'duckdb'), setuid escalation (still uid 65534), startup hooks (never executed), symlinked package (rejected), forged output (no answers to forge), always-equal objects (plain values only), killing the verifier (Operation not permitted) |
| Useful signal | one-defect oracles score in steps: frames ignored 0.3, peers ignored 0.6, `QUALIFY` missing 0.7, `IGNORE NULLS` ignored 0.8, `DESC` by negation 0.9, optimizer fix missing 0.9, `LEFT JOIN` broken 0 |
| Hard | section 7 |

---

## 7. Evaluation results

**3 trials finished, 2 with full marks, mean 0.93. GPT-5.6 Sol on the final version (v3.1),
run in the real offline task container:**

| Trial | Reward | Groups passed | Groups failed |
|---|---|---|---|
| 1 | 0.8 | 8/10 | types_nulls, subqueries |
| 2 | 1.0 | 10/10 | — |
| 3 | 1.0 | 10/10 | — |

Trial 1: `DESC` implemented by negating keys crashes on strings/NULLs (`types_nulls`, 25/30), and
a scalar subquery next to a window aggregate is broken by the optimizer, which treats the window
aggregate as a plain aggregate (`subqueries`, 27/30). Each trial took 5–8 minutes and $1.5–2.2;
regression cases passed in all trials. Compared with a peer's task (mean 0.82, no full marks),
ours is somewhat easier for this model.

**History** (same agent harness, or Claude agents run on the host):

| Version | Model | Rewards | Notes |
|---|---|---|---|
| v1 | Claude Sonnet | 1.0 | ~4 min, naive implementation |
| v2 | Claude Sonnet | 1.0 | ~9 min, cross-checked against its own brute-force reference |
| v2 | GPT-5.6 Sol ×3 | 0.17 / 1.0 / 0.17 | one sort bug zeroed 5 of 6 groups |
| v3 | GPT-5.6 Sol ×3 | 0.4 / 1.0 / 0.4 | a string `MIN`/`MAX` bug zeroed 6 groups |
| v3 solutions re-scored on v3.1 | GPT-5.6 Sol | 0.9 / 1.0 / 0.8 | failures land only in `types_nulls` and `range_frames` |

---

## 8. Iterations and lessons

| Version | Change | Why |
|---|---|---|
| v1 | small tables, `ROWS`/`RANGE` | first version |
| v2 | + 50,000-row scale group, `GROUPS`, `EXCLUDE`; O(n log n) oracle | v1 solved by Claude Sonnet in 4 minutes |
| v3 | 10 groups; subquery group (oracle fixes the optimizer); round-robin templates | coverage audit found requirements tested only 1–2 times; one sort bug zeroed 5 of 6 groups |
| v3.1 | string aggregates moved into `types_nulls`, numeric aggregates elsewhere; 30–60 cases per group | a string `MIN`/`MAX` bug still zeroed 6 of 10 groups |

**Lessons.**

1. **Measure difficulty.** v1, which we expected to be hard, fell to a mid-tier model in 4 minutes.
2. **A precise instruction invites self-verification.** Fairness requires a complete
   specification, and the successful agents wrote brute-force references from it and tested
   against them. That caps how hard such a task can be.
3. **Score by independent capabilities.** Compared with a peer's design (13 groups, rewards
   0.77–0.92), our early groups shared ordering and type handling, so one cross-cutting bug zeroed
   most groups and the reward only told "bug or no bug". Concentrating cross-cutting concerns in
   one group made failures name their root cause.
4. **Test the verifier too.** One-defect variants, attacks and the coverage audit all found real
   problems: a regression case failing on a pre-existing bug, honest scratch files being punished,
   instruction omissions, thin coverage.
5. **Test assumptions with data.** We expected the optimizer bug to force cross-module fixes, but
   in v3 two of three runs passed the subquery group without touching the optimizer: its added
   `GROUP BY id` groups by a unique key, so lenient implementations are still right. In v3.1 the
   group did catch one run, on a different optimizer interaction (a scalar subquery next to a
   window aggregate). One run is not enough to judge a group.

## 9. Known limitations

- The seed is fixed, so queries could in principle be memorised across episodes.
- Features outside the templates (`FILTER`, `DISTINCT` window aggregates) are neither required
  nor checked.
- The subquery group discriminates only sometimes (see above).
- Isolation relies on Linux permissions.
- Only the scale group is time-sensitive; the 20 s limit is about 12x the oracle's slowest case,
  so a solution near the limit could be flaky.
- GPT-5.6 Sol results come from our own minimal agent harness; other harnesses may score
  differently.
