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
- **v2 is still not hard for frontier agents.** A fresh Sonnet run solved v2 to 1.0 in about
  9 minutes by writing its own brute-force reference from the (necessarily precise)
  instruction and cross-checking thousands of random queries. I decided to freeze the task
  here and document this honestly rather than add difficulty through vaguer requirements.
  While probing for a harder axis I found a real optimizer bug (`unnest_subqueries` adds a
  `GROUP BY` to a windowed `IN` subquery, changing its meaning) that both my oracle and the
  agent miss; requiring windowed subqueries would be the first step of a v3.
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

- v3 of this task: windowed `IN`/`EXISTS`/scalar subqueries (fix in the optimizer's
  `unnest_subqueries`), and re-measure difficulty with several runs per model.
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
copy of the repository with no access to the tests or the solution, then scoring their final
code through the real trial lifecycle. I reviewed the design decisions and results.
`REPORT.md` is written by me.
