# Window functions for sqlglot's Python executor

`/app` contains sqlglot (v30.33.0), installed in editable mode. Its pure-Python SQL engine,
`sqlglot.executor.execute`, cannot evaluate window functions today: queries such as
`SELECT id, SUM(x) OVER (PARTITION BY g ORDER BY id) FROM t` raise an error, and a `QUALIFY`
clause is silently ignored. Implement window-function support in the executor
(the planner in `sqlglot/planner.py` and the engine under `sqlglot/executor/` are the natural
places; how you structure it is up to you).

## How your work is checked

A hidden verifier imports `sqlglot` from `/app` and calls

```python
sqlglot.executor.execute(sql, schema=schema, tables=tables)
```

with tables given as lists of row dicts and an explicit `schema`, then compares `result.rows`
with the result DuckDB returns for the same SQL text on the same data. Rows are compared as a
multiset (row order is ignored unless you are asked for it; `ORDER BY ... LIMIT` must still
select the right rows). Within a row, values are compared positionally in SELECT-list order;
column names are not compared. Integers and floats are compared numerically (floats with a
relative tolerance of 1e-9), `NULL` must be `None`, and strings must match exactly. Result
values must be plain `None`, `bool`, `int`, `float` or `str`.

Tables used by the verifier:

- `t(id INT, g VARCHAR, h INT, x INT, y INT, s VARCHAR)`. `id` is unique and never NULL; `y`
  is never NULL; `g`, `x` and `s` contain NULLs; `g`, `h` and `s` have few distinct values (so
  there are many ties). Rows are supplied in arbitrary order. Most cases use instances of `t`
  with 1 to about 40 rows; the *scale* cases use one instance with **50,000 rows**, in which `x`
  is about 90% NULL and `y` ranges up to 200,000.
- `u(g VARCHAR, w INT)` with a few rows and unique `g`.

Every query is valid in both sqlglot's default dialect and DuckDB, and only uses orderings
whose result is fully determined (for example, whenever the result depends on the order of
rows that tie on the ORDER BY keys, `id` is added as a final key). Every `ORDER BY` key, inside
`OVER (...)` and in the outer query, states `NULLS FIRST` or `NULLS LAST` explicitly.

## Required behaviour (DuckDB / SQL-standard semantics)

**Window specification.** `OVER ()`, `OVER (PARTITION BY ...)`, `OVER (ORDER BY ...)`,
`OVER (PARTITION BY ... ORDER BY ... <frame>)`, with `ASC`/`DESC` and `NULLS FIRST`/`NULLS LAST`
per key and one or more keys. `NULL` partition keys form their own partition. Rows whose ORDER
BY keys are all equal are *peers*.

**Ranking functions** (ignore the frame): `ROW_NUMBER()`, `RANK()`, `DENSE_RANK()`,
`NTILE(n)` (n a positive integer literal; the first `count % n` buckets get one extra row),
`PERCENT_RANK()` (`(rank - 1) / (rows in partition - 1)`, or `0.0` for a single-row
partition) and `CUME_DIST()` (rows up to and including the current row's peers, divided by
rows in the partition). Without ORDER BY, all rows of a partition are peers.

**Aggregates as window functions:** `SUM`, `COUNT(expr)`, `COUNT(*)`, `AVG`, `MIN`, `MAX`
over any expression (for example `SUM(x * 2 + y)`, `MIN(s)`), evaluated over the frame. They
ignore NULL inputs; over a frame with no non-NULL input `SUM`/`AVG`/`MIN`/`MAX` return `NULL`
and `COUNT` returns `0`.

**Frames.** Default frame: the whole partition without ORDER BY; with ORDER BY,
`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` (from the partition start through the last
peer of the current row). Explicit frames:

- `ROWS BETWEEN <start> AND <end>` and the short form `ROWS <start>` (meaning
  `... AND CURRENT ROW`), with bounds `UNBOUNDED PRECEDING`, `n PRECEDING`, `CURRENT ROW`,
  `n FOLLOWING`, `UNBOUNDED FOLLOWING` (`n` a non-negative integer literal). Frames may be
  empty (for example `ROWS BETWEEN 3 PRECEDING AND 2 PRECEDING` on the first row).
- `RANGE` frames with `UNBOUNDED`/`CURRENT ROW` bounds, where `CURRENT ROW` means the
  current row's peer group (also with several ORDER BY keys, and without ORDER BY, where the
  whole partition is one peer group). The short form `RANGE <start>` means
  `RANGE BETWEEN <start> AND CURRENT ROW`.
- `RANGE BETWEEN ... n PRECEDING / n FOLLOWING ...` (value offsets). These are only used with
  exactly one ORDER BY key, the never-NULL integer column `y`, ascending or descending. With
  `DESC`, `PRECEDING` means larger values.
- `GROUPS BETWEEN <start> AND <end>` and `GROUPS <start>`: like `ROWS`, but `n PRECEDING` /
  `n FOLLOWING` count peer groups (`n PRECEDING` starts at the first row of the peer group `n`
  groups before the current row's group, `n FOLLOWING` as end reaches the last row of the group
  `n` groups after it), and `CURRENT ROW` means the current row's peer group.
- An optional `EXCLUDE` clause after any frame: `EXCLUDE CURRENT ROW` (remove the current row
  from the frame), `EXCLUDE GROUP` (remove the current row and its peers), `EXCLUDE TIES`
  (remove the current row's peers but keep the current row), `EXCLUDE NO OTHERS` (the
  default). The excluded rows may split the frame in two; functions see the remaining rows in
  frame order.

**Navigation functions:** `LAG(expr [, offset [, default]])` and `LEAD(...)` (offset a
positive integer literal, default 1; `default` may be any expression, evaluated on the current
row, and is returned when the target row is outside the partition; they ignore the frame),
and `FIRST_VALUE(expr)`, `LAST_VALUE(expr)`, `NTH_VALUE(expr, n)` (n a positive integer
literal), which use the frame (`NULL` when the frame has no such row). All five accept
`IGNORE NULLS`, written after the last argument, e.g. `LAG(x, 2, -1 IGNORE NULLS)`: rows whose
`expr` is NULL are skipped when counting.

**Composition.** All of the following must work, also in combination:

- several window functions with different specifications in one SELECT, and window
  functions inside larger expressions (`x - LAG(x) OVER (...)`,
  `100.0 * x / SUM(x) OVER (...)`, `CASE WHEN ROW_NUMBER() OVER (...) <= 3 THEN ... END`);
- `WHERE` (applied before windows), `QUALIFY` (filters on window results after they are
  computed; it may use a window expression or a SELECT-list alias), `DISTINCT`, `ORDER BY`
  (including by a window expression or by a window alias) with `LIMIT`;
- named windows: `WINDOW w AS (...)`, `w2 AS (w ORDER BY ...)`, used as `OVER w` or
  `OVER (w ORDER BY ...)` / `OVER (w2 ROWS ...)`;
- window functions over grouped results: `GROUP BY` / `HAVING` queries where window
  functions use aggregates or group keys, e.g. `RANK() OVER (ORDER BY COUNT(*) DESC NULLS LAST)`,
  `SUM(SUM(x)) OVER (...)`, `MAX(y) - MIN(MAX(y)) OVER ()`;
- window functions over `JOIN` / `LEFT JOIN` results, and inside subqueries and CTEs that an
  outer query then filters.

**Subqueries.** Window functions must also work inside subqueries used as predicates or
values, for example `WHERE id IN (SELECT id FROM t QUALIFY ROW_NUMBER() OVER (...) = 1)`,
`NOT IN (... QUALIFY RANK() OVER (...) <= 2)`, `WHERE x IN (SELECT LAG(y) OVER (...) FROM t)`,
`WHERE y IN (SELECT MAX(y) OVER (PARTITION BY g) FROM t)`, a correlated
`EXISTS (SELECT 1 FROM u WHERE u.g = t.g QUALIFY ROW_NUMBER() OVER (...) = 1)`,
`y > ALL (SELECT MAX(y) OVER (...) FROM t WHERE ...)`, a correlated scalar subquery over a
derived table that computes a window function, and scalar subqueries (correlated or not) in a
SELECT list that also contains window functions. Note that `execute()` runs sqlglot's
optimizer (`sqlglot.optimizer.optimize`) on the query before planning it; you may change any
code under `/app`.

**Scale.** Window evaluation must be efficient: on the 50,000-row table, every case (a mix of
running and whole-partition aggregates, `MIN`/`MAX` over unbounded `ROWS` frames, wide
`RANGE` offsets, `GROUPS` frames with `EXCLUDE`, `FIRST_VALUE`/`LAST_VALUE`/`NTH_VALUE` and
`LAG`/`LEAD` with `IGNORE NULLS` on the mostly-NULL `x`, ranking with large peer groups,
`QUALIFY`) must finish within **20 seconds** on the verifier (2 CPUs). An implementation whose
cost grows with the frame size of each row (for example, re-scanning every row's frame) is far
too slow for this; a reference implementation takes about 2 seconds per case.

## Scoring

Cases are grouped into ten equally weighted groups: *types and NULLs* (ordering by several
keys, `DESC`, `NULLS FIRST`/`NULLS LAST`, string and NULL-containing keys, and `MIN`/`MAX` over
strings), *ranking* functions, *aggregates* with default frames, *ROWS frames*, *RANGE
frames*, *GROUPS frames and EXCLUDE*, *navigation* functions (including `IGNORE NULLS`),
*composition*, *subqueries*, and *scale*. Outside the types-and-NULLs group, window `ORDER BY`
keys are the never-NULL integer columns `y`, `h` and `id`, and window aggregates are over
numbers. A group counts
only if **every** case in it is correct (and on time); the reward is the fraction of complete
groups. Queries without window functions that work today (filters, `CASE`/`COALESCE`,
grouping, joins, `DISTINCT`, subqueries, CTEs, `UNION ALL`, ordering with `LIMIT`) must keep
returning correct results: if any of them fails, the reward is 0. Each small-table case must
finish within 10 seconds, each scale case within 20 seconds, and all cases together (a few
hundred) within 600 seconds.
