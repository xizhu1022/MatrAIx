# Task 2 notes

## Submitted task

`executor-window-functions/`: add SQL window functions to sqlglot's pure-Python executor, with
a differential verifier against DuckDB. One task, built end to end (valid, fair, attacked,
partial credit, difficulty measured with agents). The reviewer details are in its `README.md`.

## Approach

1. **Look for a real capability gap with an independent reference.** I probed sqlglot's
   executor with ~45 SQL features. It errors on window functions, `ROLLUP`/`CUBE`/`GROUPING
   SETS`, `FILTER`, recursive CTEs, `LATERAL`/`UNNEST`/`VALUES`, `PIVOT`, several aggregates
   and scalar functions, and returns *wrong* results (silently) for `QUALIFY`, `ANTI JOIN` and
   `DISTINCT ON`. Window functions were the deepest gap (planner and executor, lots of
   semantics) and DuckDB executes the same SQL, so a verifier can check behaviour by running
   both engines on the same data.
2. **Oracle first.** Writing the solution first showed where the semantic traps are (NULL
   ordering defaults differ between sqlglot and DuckDB, `IGNORE NULLS` syntax that both accept,
   `COUNT(*)` relying on a planner rewrite, CTE renaming), and each trap became either an
   instruction clause or a generator restriction.
3. **Verifier as a differential test.** Random datasets and templated queries (fixed seed),
   multiset comparison with DuckDB, only queries with a unique answer, explicit `NULLS
   FIRST/LAST` everywhere. Grouped scoring with an all-or-nothing rule per group and a
   regression gate.
4. **Hostile `/app`.** The code under test only runs as `nobody` in a child process; the
   verifier never imports from `/app`; DuckDB, `/tests` and `/logs` are root-only.
5. **Evidence for every criterion**, each produced by running the real trial lifecycle
   (agent container, export `/app`, fresh verifier container):
   - valid: `selfcheck.py --repeats 3` → `RESULT: VALID`;
   - hack resistance: 8 attack "solutions", all 0, with the blocking error in the log;
   - partial credit: oracle variants with one realistic defect each score 1/3 to 2/3;
   - fairness: every construct the generator can emit was checked against the instruction;
   - difficulty: agents solved the task from the instruction alone (see below).

## What I tried and changed

- **v1 was too easy.** The first version (small tables only, `ROWS`/`RANGE` frames) was solved
  to 1.0 by a mid-tier model (Claude Sonnet) in about 4 minutes. I checked its transcript for
  access to the oracle or tests (none; it solved it honestly). I made it harder through real
  difficulty, not hidden requirements: a scale group (50,000 rows, 20 s per query, stated in the
  instruction) that rejects the natural quadratic implementation, plus `GROUPS` frames and
  `EXCLUDE`. That required rewriting my own oracle to O(n log n). The v1 Sonnet solution scores
  0.5 on v2.
- **v2 was solved by Sonnet again** (1.0 in about 9 minutes, by writing its own brute-force
  reference from the necessarily precise instruction). GPT-5.6 Sol, run in the real offline
  container through a small agent harness, scored 0.17 / 1.0 / 0.17. The failures showed a
  scoring problem rather than a difficulty one: one bug (`DESC` implemented by negating keys,
  crashing on strings/NULLs) zeroed 5 of 6 groups at 93-94% case accuracy.
- **v3: 10 independent capability groups and a subquery group.** Ordering and NULL handling got
  their own group; other groups order by never-NULL integers. Windowed `IN` subqueries break in
  the optimizer (`unnest_subqueries` adds a `GROUP BY` to the subquery), so the oracle fixes
  `unnest_subqueries`. A coverage audit (`tests/coverage.py`) had shown several requirements
  tested only 1-2 times; templates are now used round-robin. GPT-5.6 Sol: 0.4 / 1.0 / 0.4, with a
  string `MIN`/`MAX` bug zeroing 6 groups, because string aggregates appeared in every group.
- **v3.1: string aggregates moved into the types-and-NULLs group**, numeric aggregates elsewhere,
  30-60 cases per group, every requirement exercised at least 3 times. Re-scoring the saved v3 Sol
  solutions gives 0.9 / 1.0 / 0.8, with failures landing only in the groups of their root cause.
  Fresh GPT-5.6 Sol runs on v3.1: 0.8 / 1.0 / 1.0 (mean 0.93). The failing run lost
  `types_nulls` (`DESC` by negation again) and `subqueries` (a scalar subquery next to a window
  aggregate, broken by the optimizer). The subquery group therefore discriminates only
  sometimes: all v3 runs passed it, one of three v3.1 runs failed it.
- The comparison with a peer's task (13 groups, rewards 0.77-0.92) is what made the
  group-independence problem visible.
- **Fairness fixes found by testing:** (1) a `UNION ALL` regression query failed because of an
  existing executor bug unrelated to windows; I replaced it so agents are not blamed for
  pre-existing bugs. (2) The child process originally put `/app` first on `sys.path`, so an
  honest agent's scratch file named `random.py` or `json.py` would have broken everything; it
  now appends `/app` (like an editable install), and a variant with such files scores 1.0.
  (3) The instruction was missing the `RANGE <start>` short form and the total time limit.
- **Dropped:** optimizer-rule tasks (hard to find a valuable missing rule quickly), transpile
  tasks (only DuckDB is available as a reference engine, so they drift toward string
  matching), pure performance tasks without a correctness core (timing alone is flaky).

## With more time

- More trials per model (and other models) to estimate pass rates with tighter error bars.
- A subquery or optimizer requirement that truly cannot pass without the cross-module fix.
- A second task from the same verifier framework: `ROLLUP`/`CUBE`/`GROUPING SETS` with
  `GROUPING()`, plus aggregate `FILTER (WHERE ...)`.
- The task generator: the probe above already lists a dozen executor gaps; each gap is a task
  family, and the generator's template set and table size are natural difficulty knobs.
- Rotate the verifier seed per trial (removes memorization across episodes; trades off the
  "reward is a fixed function of `/app`" property).
- More agent runs per version, to estimate pass rates rather than single outcomes.

## How I used AI

I used Claude Code (Claude) throughout: to read and summarise the repository and the task
format, probe sqlglot and DuckDB behaviour, write the oracle, the verifier and the query
generator, build the Docker images, run selfcheck, write and run the attack and partial-credit
variants, and draft this file and the task README. Difficulty was measured by giving the
instruction to separate Claude agents (Claude Sonnet and a stronger Claude model) in a clean
copy of the repository with no access to the tests or the solution, and to GPT-5.6 Sol through
OpenRouter with a small agent harness that runs the model inside the real offline task
container; all final code was scored through the real trial lifecycle. I reviewed the design decisions and results.
`REPORT.md` is written by me.
