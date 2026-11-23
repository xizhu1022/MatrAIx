"""Deterministic generator of datasets and SQL test cases for the window-function verifier.

Every query is written in the SQL subset that both sqlglot (default dialect) and DuckDB
accept with the same meaning, and only uses orderings and frames whose result is uniquely
determined, so there is exactly one correct answer.

Cases are organised in independent capability groups. Concerns that every window touches
(sorting by several keys, DESC on strings, NULL keys, NULLS FIRST/LAST, and MIN/MAX over
strings) are concentrated in the `types_nulls` group; every other group orders by the
never-NULL integer columns `y` and `h` (and `id` for a total order) and aggregates numbers,
so a bug in one capability does not fail unrelated groups.
Templates inside a group are used round-robin, so each is exercised equally often.
"""

from __future__ import annotations

import random

SEED = 20260530

SCHEMA = {
    "t": {"id": "INT", "g": "VARCHAR", "h": "INT", "x": "INT", "y": "INT", "s": "VARCHAR"},
    "u": {"g": "VARCHAR", "w": "INT"},
}

DATASET_SIZES = (1, 4, 9, 18, 37)
WORDS = ("apple", "berry", "cherry", "date", "fig", "grape", "kiwi")
BIG_ROWS = 50_000
CASES_PER_DATASET = 6  # per group and small dataset -> 30 cases per group
SCALE_CASES = 12

ALL_KEYS = ("x", "y", "s", "g", "h")  # ordering group only
SIMPLE_KEYS = ("y", "h")  # never NULL integers

TIERS = (
    "types_nulls",
    "ranking",
    "aggregates",
    "rows_frames",
    "range_frames",
    "groups_exclude",
    "navigation",
    "composition",
    "subqueries",
    "scale",
)

# group -> template sets (methods of Gen) it draws from, round-robin
GROUP_TEMPLATES = {
    "types_nulls": ("ordering",),
    "ranking": ("row_number_ntile", "rank_dense_rank", "percent_rank_cume_dist"),
    "aggregates": ("aggregates_partition", "aggregates_running"),
    "rows_frames": ("rows_frames",),
    "range_frames": ("range_peer_frames", "range_offset_frames"),
    "groups_exclude": ("groups_frames", "exclude_"),
    "navigation": ("lag_lead", "first_last_nth_value", "ignore_nulls"),
    "composition": ("qualify_named_windows", "composition"),
    "subqueries": ("subqueries",),
}


def make_dataset(rng: random.Random, n: int) -> dict:
    t = []
    for i in range(1, n + 1):
        t.append(
            {
                "id": i,
                "g": rng.choice(("a", "a", "b", "b", "c", None)),
                "h": rng.choice((1, 2)),
                "x": rng.choice((None, None, 1, 2, 3, 3, 4, 5, 6)),
                "y": rng.randint(1, 9),
                "s": rng.choice(WORDS + (None,)),
            }
        )
    rng.shuffle(t)  # row order must not matter
    u = [{"g": "a", "w": 10}, {"g": "b", "w": 20}, {"g": "d", "w": 40}]
    rng.shuffle(u)
    return {"t": t, "u": u}


def make_big_dataset(rng: random.Random, n: int) -> dict:
    """A large table: many ties on g/h/s, `x` is 90% NULL, `y` has a wide range."""
    t = []
    for i in range(1, n + 1):
        t.append(
            {
                "id": i,
                "g": rng.choice(("a", "a", "b", "b", "c", None)),
                "h": rng.choice((1, 2)),
                "x": rng.randint(1, 9) if rng.random() < 0.1 else None,
                "y": rng.randint(1, 4 * n),
                "s": rng.choice(WORDS + (None,)),
            }
        )
    rng.shuffle(t)
    u = [{"g": "a", "w": 10}, {"g": "b", "w": 20}, {"g": "d", "w": 40}]
    return {"t": t, "u": u}


class Gen:
    def __init__(self, rng: random.Random):
        self.r = rng

    # ------------------------------------------------------------------ pieces

    def nulls(self) -> str:
        return self.r.choice(("NULLS FIRST", "NULLS LAST"))

    def direction(self) -> str:
        return self.r.choice(("", " ASC", " DESC"))

    def key(self, columns=SIMPLE_KEYS) -> str:
        return f"{self.r.choice(columns)}{self.direction()} {self.nulls()}"

    def keys(self, total: bool, max_keys: int = 2, columns=SIMPLE_KEYS) -> str:
        """ORDER BY keys. With total=True a final `id` key makes the order a total order."""
        parts: list[str] = []
        for _ in range(self.r.randint(1, max_keys)):
            k = self.key(columns)
            if k.split()[0] not in {p.split()[0] for p in parts}:
                parts.append(k)
        if total:
            parts.append(f"id{self.r.choice(('', ' DESC'))} {self.nulls()}")
        return ", ".join(parts)

    def partition(self, allow_empty: bool = True) -> str:
        options = ["PARTITION BY g", "PARTITION BY h", "PARTITION BY g, h"]
        if allow_empty:
            options += ["", ""]
        return self.r.choice(options)

    @staticmethod
    def over(*parts: str) -> str:
        return "(" + " ".join(p for p in parts if p) + ")"

    def agg(self) -> str:
        """Numeric aggregates (NULL-skipping on x); string aggregates are tested in the types group."""
        return self.r.choice(
            ("SUM(x)", "COUNT(x)", "COUNT(*)", "AVG(x)", "MIN(x)", "MAX(x)", "SUM(x * 2 + y)",
             "MAX(y - x)", "AVG(y)", "MIN(y)")
        )

    def str_agg(self) -> str:
        return self.r.choice(("MIN(s)", "MAX(s)", "MIN(g)", "MAX(g)"))

    def _bound(self, kind: int, max_n: int = 3) -> str:
        n = self.r.randint(0, max_n)
        return {0: "UNBOUNDED PRECEDING", 1: f"{n} PRECEDING", 2: "CURRENT ROW",
                3: f"{n} FOLLOWING", 4: "UNBOUNDED FOLLOWING"}[kind]

    def _bound_pair(self, need_offset: bool = False) -> tuple[int, int]:
        while True:  # bound types ordered: UP < nP < CR < nF < UF
            a, b = self.r.randint(0, 3), self.r.randint(1, 4)
            if a <= b and (not need_offset or a in (1, 3) or b in (1, 3)):
                return a, b

    def rows_frame(self) -> str:
        if self.r.random() < 0.2:
            return "ROWS " + self.r.choice(("UNBOUNDED PRECEDING", f"{self.r.randint(0, 3)} PRECEDING", "CURRENT ROW"))
        a, b = self._bound_pair()
        return f"ROWS BETWEEN {self._bound(a)} AND {self._bound(b)}"

    def range_offset_frame(self) -> str:
        a, b = self._bound_pair(need_offset=True)
        return f"RANGE BETWEEN {self._bound(a)} AND {self._bound(b)}"

    def groups_frame(self) -> str:
        if self.r.random() < 0.3:
            return "GROUPS " + self.r.choice(("UNBOUNDED PRECEDING", f"{self.r.randint(0, 2)} PRECEDING", "CURRENT ROW"))
        a, b = self._bound_pair()
        return f"GROUPS BETWEEN {self._bound(a, 2)} AND {self._bound(b, 2)}"

    def range_peer_frame(self) -> str:
        return self.r.choice(
            ("RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
             "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
             "RANGE BETWEEN CURRENT ROW AND CURRENT ROW",
             "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
             "RANGE UNBOUNDED PRECEDING",
             "RANGE CURRENT ROW")
        )

    def exclude(self) -> str:
        return self.r.choice(("EXCLUDE CURRENT ROW", "EXCLUDE GROUP", "EXCLUDE TIES", "EXCLUDE NO OTHERS"))

    def ign(self) -> str:
        return " IGNORE NULLS"

    # ------------------------------------------------------------------ groups
    # Each group is a list of templates; a template returns one SQL string.

    def ordering(self):
        p = self.partition
        sa = self.str_agg
        return [
            lambda: f"SELECT id, {sa()} OVER {self.over(p())} AS v1, {sa()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v2 FROM t",
            lambda: f"SELECT id, {sa()} OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v FROM t",
            lambda: f"SELECT id, {sa()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False), self.r.choice((self.range_peer_frame(), self.groups_frame())) + ' ' + self.exclude())} AS v FROM t",
            lambda: f"SELECT id, ROW_NUMBER() OVER {self.over(p(), 'ORDER BY ' + self.keys(True, 3, ALL_KEYS))} AS v FROM t",
            lambda: f"SELECT id, RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False, 3, ALL_KEYS))} AS v FROM t",
            lambda: f"SELECT id, DENSE_RANK() OVER (ORDER BY s DESC {self.nulls()}, x DESC {self.nulls()}) AS v FROM t",
            lambda: f"SELECT id, SUM(y) OVER {self.over(p(), 'ORDER BY ' + self.keys(False, 2, ('s', 'g', 'x')))} AS v FROM t",
            lambda: f"SELECT id, COUNT(*) OVER (ORDER BY g DESC {self.nulls()}, s {self.nulls()}) AS v, MIN(id) OVER (ORDER BY x DESC {self.nulls()}) AS w FROM t",
        ]

    def row_number_ntile(self):
        p = self.partition
        return [
            lambda: f"SELECT id, ROW_NUMBER() OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v FROM t",
            lambda: f"SELECT id, NTILE({self.r.randint(1, 7)}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v FROM t",
            lambda: f"SELECT id, ROW_NUMBER() OVER (ORDER BY id {self.nulls()}) AS v1, NTILE({self.r.randint(2, 5)}) OVER {self.over(p(False), 'ORDER BY ' + self.keys(True, 1))} AS v2 FROM t",
        ]

    def rank_dense_rank(self):
        p = self.partition
        return [
            lambda: f"SELECT id, RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v1, DENSE_RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v2 FROM t",
            lambda: f"SELECT id, RANK() OVER {self.over(p(False))} AS v1, DENSE_RANK() OVER () AS v2 FROM t",
            lambda: f"SELECT id, DENSE_RANK() OVER (ORDER BY h{self.direction()} {self.nulls()}, y{self.direction()} {self.nulls()}) AS v FROM t",
        ]

    def percent_rank_cume_dist(self):
        p = self.partition
        return [
            lambda: f"SELECT id, PERCENT_RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v1, CUME_DIST() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v2 FROM t",
            lambda: f"SELECT id, CUME_DIST() OVER {self.over(p(False))} AS v1, PERCENT_RANK() OVER {self.over(p(False))} AS v2 FROM t",
            lambda: f"SELECT id, PERCENT_RANK() OVER (ORDER BY h {self.nulls()}) AS v1, CUME_DIST() OVER (PARTITION BY g ORDER BY y DESC {self.nulls()}) AS v2 FROM t",
        ]

    def aggregates_partition(self):
        p = self.partition
        return [
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(False))} AS v FROM t",
            lambda: f"SELECT id, x, {self.agg()} OVER () AS v1, {self.agg()} OVER () AS v2 FROM t",
            lambda: f"SELECT id, {self.agg()} OVER (PARTITION BY g, h) AS v1, {self.agg()} OVER (PARTITION BY h) AS v2 FROM t",
        ]

    def aggregates_running(self):
        p = self.partition
        return [
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v FROM t",
            lambda: f"SELECT id, {self.agg()} OVER (ORDER BY h{self.direction()} {self.nulls()}) AS v1, COUNT(*) OVER {self.over(p(), 'ORDER BY ' + self.keys(False, 1))} AS v2 FROM t",
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v FROM t",
        ]

    def rows_frames(self):
        p = self.partition
        return [
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v1, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v2 FROM t",
            lambda: (lambda n, kt: f"SELECT id, SUM(x) OVER {self.over(p(), 'ORDER BY ' + kt, f'ROWS BETWEEN {n + 2} PRECEDING AND {n} PRECEDING')} AS v1, COUNT(*) OVER {self.over(p(), 'ORDER BY ' + kt, f'ROWS BETWEEN {n} FOLLOWING AND {n + 1} FOLLOWING')} AS v2 FROM t")(self.r.randint(1, 3), self.keys(True, 1)),
        ]

    def range_peer_frames(self):
        p = self.partition
        return [
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False), self.range_peer_frame())} AS v FROM t",
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(False), self.range_peer_frame())} AS v FROM t",
        ]

    def range_offset_frames(self):
        p = self.partition
        def tmpl():
            key = f"y{self.direction()} {self.nulls()}"
            return (f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + key, self.range_offset_frame())} AS v1, "
                    f"COUNT(*) OVER {self.over(p(), 'ORDER BY ' + key, self.range_offset_frame())} AS v2 FROM t")
        return [tmpl, lambda: f"SELECT id, SUM(id) OVER (ORDER BY y DESC {self.nulls()} {self.range_offset_frame()}) AS v FROM t"]

    def groups_frames(self):
        p = self.partition
        return [
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False), 'GROUPS ' + self.r.choice(('UNBOUNDED PRECEDING', '1 PRECEDING', '2 PRECEDING', 'CURRENT ROW')))} AS v FROM t",
            lambda: (lambda k: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + k, self.groups_frame())} AS v1, COUNT(*) OVER {self.over(p(), 'ORDER BY ' + k, self.groups_frame())} AS v2 FROM t")(self.keys(False)),
        ]

    def exclude_(self):
        p = self.partition
        return [
            lambda: (lambda k, f: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + k, f + ' ' + self.exclude())} AS v1, COUNT(*) OVER {self.over(p(), 'ORDER BY ' + k, f + ' ' + self.exclude())} AS v2 FROM t")(
                self.keys(False), self.r.choice((self.range_peer_frame(), self.groups_frame(), "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"))),
            lambda: f"SELECT id, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame() + ' EXCLUDE CURRENT ROW')} AS v FROM t",
        ]

    def lag_lead(self):
        p = self.partition
        def lag():
            fn = self.r.choice(("LAG", "LEAD"))
            args = self.r.choice(("x", "s", "x + y"))
            if self.r.random() < 0.7:
                args += f", {self.r.randint(1, 3)}"
                if self.r.random() < 0.6 and not args.startswith("s"):
                    args += f", {self.r.choice(('-1', '0', 'y', '100'))}"
            return f"SELECT id, {fn}({args}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v FROM t"
        return [lag, lambda: f"SELECT id, LAG(s, 2, g) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v1, LEAD(id, {self.r.randint(1, 4)}, -id) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v2 FROM t"]

    def first_last_nth_value(self):
        p = self.partition
        col = lambda: self.r.choice(("x", "s", "x + y"))  # noqa: E731
        return [
            lambda: f"SELECT id, FIRST_VALUE({col()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v1, LAST_VALUE({col()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v2, NTH_VALUE({col()}, {self.r.randint(1, 3)}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v3 FROM t",
            lambda: f"SELECT id, FIRST_VALUE({col()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v1, LAST_VALUE({col()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v2, NTH_VALUE({col()}, {self.r.randint(1, 3)}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v3 FROM t",
        ]

    def ignore_nulls(self):
        p = self.partition
        col = lambda: self.r.choice(("x", "s"))  # noqa: E731
        return [
            lambda: f"SELECT id, LAG({col()}, {self.r.randint(1, 3)}{self.ign()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v1, LEAD(x, {self.r.randint(1, 3)}, -1{self.ign()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v2 FROM t",
            lambda: f"SELECT id, FIRST_VALUE({col()}{self.ign()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v1, LAST_VALUE({col()}{self.ign()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS v2 FROM t",
            lambda: f"SELECT id, NTH_VALUE({col()}, {self.r.randint(1, 3)}{self.ign()}) OVER {self.over(p(), 'ORDER BY ' + self.keys(True), self.rows_frame())} AS v FROM t",
        ]

    def qualify_named_windows(self):
        p = self.partition
        return [
            lambda: f"SELECT id, g, x FROM t QUALIFY ROW_NUMBER() OVER {self.over(p(False), 'ORDER BY ' + self.keys(True))} <= {self.r.randint(1, 2)}",
            lambda: f"SELECT id, RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v, {self.agg()} OVER {self.over(p())} AS a FROM t QUALIFY v <= {self.r.randint(1, 3)}",
            lambda: (lambda pp, kt, k: f"SELECT id, {self.agg()} OVER w AS v1, ROW_NUMBER() OVER w2 AS v2, {self.agg()} OVER (w2 {self.rows_frame()}) AS v3, RANK() OVER (w ORDER BY {k}) AS v4 FROM t WINDOW w AS ({pp}), w2 AS (w ORDER BY {kt})")(p(False), self.keys(True), self.keys(False)),
        ]

    def composition(self):
        p = self.partition
        nl = self.nulls
        return [
            lambda: (lambda grp, having: f"SELECT {grp}, COUNT(*) AS n, SUM(x) AS sx, RANK() OVER (ORDER BY COUNT(*) DESC NULLS LAST) AS r, SUM(SUM(x)) OVER (ORDER BY {grp} {nl()} ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS running, MAX(y) - MIN(MAX(y)) OVER () AS spread FROM t GROUP BY {grp}{having}")(
                self.r.choice(("g", "h")), ""),
            lambda: (lambda grp, having: f"SELECT {grp}, COUNT(*) AS n, SUM(x) AS sx, RANK() OVER (ORDER BY COUNT(*) DESC NULLS LAST) AS r, SUM(SUM(x)) OVER (ORDER BY {grp} {nl()} ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS running, MAX(y) - MIN(MAX(y)) OVER () AS spread FROM t GROUP BY {grp}{having}")(
                self.r.choice(("g", "h")), " HAVING COUNT(*) > 1"),
            lambda: f"SELECT id, x - LAG(x) OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS dx, 100.0 * x / SUM(x) OVER {self.over(p())} AS pct, CASE WHEN ROW_NUMBER() OVER (ORDER BY {self.keys(True)}) <= 3 THEN 'top' ELSE 'rest' END AS bucket FROM t",
            lambda: f"SELECT id, x FROM t ORDER BY SUM(x) OVER {self.over(p())} DESC NULLS LAST, id {nl()} LIMIT {self.r.randint(1, 5)}",
            lambda: f"SELECT id, s, DENSE_RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v FROM t ORDER BY v DESC NULLS LAST, id NULLS LAST LIMIT {self.r.randint(1, 5)}",
            lambda: f"SELECT DISTINCT g, {self.agg()} OVER (PARTITION BY g) AS m FROM t",
            lambda: f"SELECT t.id, u.w, SUM(u.w) OVER (PARTITION BY t.h ORDER BY t.id {nl()}) AS v1, ROW_NUMBER() OVER (PARTITION BY u.g ORDER BY t.y {nl()}, t.id {nl()}) AS v2 FROM t JOIN u ON t.g = u.g",
            lambda: f"SELECT t.id, u.w, SUM(u.w) OVER (PARTITION BY t.h ORDER BY t.id {nl()}) AS v1, ROW_NUMBER() OVER (PARTITION BY u.g ORDER BY t.y {nl()}, t.id {nl()}) AS v2 FROM t LEFT JOIN u ON t.g = u.g",
            lambda: f"SELECT q.id, q.r FROM (SELECT id, x, ROW_NUMBER() OVER {self.over(p(), 'ORDER BY ' + self.keys(True))} AS r FROM t) AS q WHERE q.r <= 3 OR q.x > 4",
            lambda: f"WITH c AS (SELECT g, COUNT(*) AS n FROM t GROUP BY g) SELECT g, n, SUM(n) OVER (ORDER BY n {nl()}) AS cum, LAG(n) OVER (ORDER BY n DESC {nl()}, g {nl()}) AS prev FROM c",
            lambda: f"SELECT id, COUNT(*) OVER () AS c, {self.agg()} OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS v FROM t WHERE x > {self.r.randint(1, 4)} OR s IS NULL",
            lambda: f"WITH r AS (SELECT id, g, {self.agg()} OVER {self.over(p(False))} AS a, ROW_NUMBER() OVER {self.over(p(False), 'ORDER BY ' + self.keys(True))} AS rn FROM t) SELECT id, a FROM r WHERE rn <= {self.r.randint(1, 3)}",
        ]

    def subqueries(self):
        p = self.partition
        nl = self.nulls
        return [
            lambda: f"SELECT id FROM t WHERE id IN (SELECT id FROM t QUALIFY ROW_NUMBER() OVER {self.over(p(False), 'ORDER BY ' + self.keys(True))} = 1)",
            lambda: f"SELECT id, x FROM t WHERE id NOT IN (SELECT id FROM t QUALIFY RANK() OVER {self.over(p(False), 'ORDER BY ' + self.keys(False))} <= {self.r.randint(1, 2)})",
            lambda: f"SELECT id FROM t WHERE x IN (SELECT {self.r.choice(('LAG', 'LEAD'))}(y) OVER (ORDER BY id {nl()}) FROM t)",
            lambda: f"SELECT id FROM t WHERE y IN (SELECT MAX(y) OVER {self.over(p(False))} FROM t)",
            lambda: f"SELECT g, COUNT(*) AS n FROM t WHERE id IN (SELECT id FROM t QUALIFY COUNT(*) OVER {self.over(p(False))} > 1) GROUP BY g",
            lambda: f"SELECT id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.g = t.g QUALIFY ROW_NUMBER() OVER (ORDER BY u.w {nl()}) = 1)",
            lambda: f"SELECT id, (SELECT r FROM (SELECT g, RANK() OVER (ORDER BY w DESC {nl()}) AS r FROM u) AS z WHERE z.g = t.g) AS rk FROM t",
            lambda: f"SELECT id FROM t WHERE y > ALL (SELECT MAX(y) OVER {self.over(p(False))} FROM t WHERE g = 'a')",
            lambda: f"SELECT id, (SELECT MAX(w) FROM u) AS m, {self.agg()} OVER {self.over(p())} AS v FROM t",
            lambda: f"SELECT id, (SELECT COUNT(*) FROM u WHERE u.g = t.g) AS c, RANK() OVER {self.over(p(), 'ORDER BY ' + self.keys(False))} AS r FROM t",
        ]

    def scale(self):
        """Queries over the large table whose frames span many rows (integer ordering keys)."""
        p = self.partition
        nl = self.nulls
        r = self.r
        kt = lambda: f"id{r.choice(('', ' DESC'))} {nl()}"  # noqa: E731
        return [
            lambda: (lambda k: f"SELECT id, SUM(y) OVER {self.over(p(), f'ORDER BY {k} {nl()}')} AS v1, COUNT(x) OVER {self.over(p(), f'ORDER BY {k} DESC {nl()}')} AS v2, AVG(y) OVER (ORDER BY {k} {nl()}) AS v3 FROM t")(r.choice(("h", "y"))),
            lambda: f"SELECT id, MIN(y) OVER {self.over(p(False))} AS v1, SUM(x) OVER {self.over(p())} AS v2, MAX(y) OVER () AS v3, COUNT(*) OVER {self.over(p(False))} AS v4 FROM t",
            lambda: (lambda k: f"SELECT id, MIN(y) OVER {self.over(p(), 'ORDER BY ' + k, 'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING')} AS v1, MAX(y) OVER {self.over(p(), 'ORDER BY ' + k, 'ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING')} AS v2, SUM(x) OVER {self.over(p(), 'ORDER BY ' + k, 'ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING')} AS v3 FROM t")(kt()),
            lambda: (lambda a, b, d: f"SELECT id, SUM(x) OVER {self.over(p(), f'ORDER BY y{d} {nl()}', f'RANGE BETWEEN {a} PRECEDING AND {b} FOLLOWING')} AS v1, MAX(x) OVER {self.over(p(), f'ORDER BY y{d} {nl()}', f'RANGE BETWEEN CURRENT ROW AND {a} FOLLOWING')} AS v2, COUNT(*) OVER (ORDER BY y {nl()} RANGE BETWEEN {b} PRECEDING AND UNBOUNDED FOLLOWING) AS v3 FROM t")(
                r.choice((1000, 5000, 20000)), r.choice((0, 1000, 5000)), r.choice(("", " DESC"))),
            lambda: (lambda k: f"SELECT id, COUNT(*) OVER (ORDER BY {k} GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING {self.exclude()}) AS v1, SUM(y) OVER {self.over(p(), 'ORDER BY ' + k, 'RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ' + self.exclude())} AS v2, MIN(x) OVER {self.over(p(), 'ORDER BY ' + k, 'GROUPS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING ' + self.exclude())} AS v3 FROM t")(
                r.choice(("h NULLS LAST", "h DESC NULLS FIRST"))),
            lambda: (lambda k: f"SELECT id, FIRST_VALUE(x IGNORE NULLS) OVER {self.over(p(), 'ORDER BY ' + k, 'ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING')} AS v1, LAST_VALUE(s) OVER {self.over(p(False), 'ORDER BY ' + k, 'ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING')} AS v2, NTH_VALUE(x, {r.randint(2, 5)} IGNORE NULLS) OVER {self.over(p(), 'ORDER BY ' + k)} AS v3, LAST_VALUE(x IGNORE NULLS) OVER {self.over(p(), 'ORDER BY ' + k, 'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING EXCLUDE CURRENT ROW')} AS v4 FROM t")(kt()),
            lambda: (lambda k: f"SELECT id, LAG(x, {r.randint(1, 4)}, -1 IGNORE NULLS) OVER {self.over(p(), 'ORDER BY ' + k)} AS v1, LEAD(x, {r.randint(1, 4)} IGNORE NULLS) OVER {self.over(p(), 'ORDER BY ' + k)} AS v2, LEAD(s, {r.randint(1, 4)}, g) OVER (ORDER BY {k}) AS v3 FROM t")(f"y {nl()}, id {nl()}"),
            lambda: f"SELECT id, RANK() OVER (ORDER BY h {nl()}) AS v1, DENSE_RANK() OVER {self.over(p(), f'ORDER BY h DESC {nl()}')} AS v2, PERCENT_RANK() OVER (ORDER BY h {nl()}) AS v3, CUME_DIST() OVER {self.over(p(), f'ORDER BY h DESC {nl()}')} AS v4, NTILE({r.randint(3, 50)}) OVER (ORDER BY y {nl()}, id {nl()}) AS v5 FROM t",
            lambda: f"SELECT id, y FROM t QUALIFY ROW_NUMBER() OVER {self.over(p(False), f'ORDER BY y DESC {nl()}, id {nl()}')} <= {r.randint(1, 3)} OR SUM(x) OVER (ORDER BY id {nl()}) = {r.randint(5, 50)}",
            lambda: f"SELECT id, s, SUM(x) OVER (PARTITION BY g ORDER BY y {nl()}, id {nl()}) AS v FROM t ORDER BY v DESC NULLS LAST, id {nl()} LIMIT {r.randint(5, 20)}",
        ]


REGRESSION = (
    "SELECT id, x, s FROM t WHERE x > 2",
    "SELECT g, COUNT(*) AS n, SUM(x) AS sx, MIN(s) AS ms FROM t GROUP BY g",
    "SELECT h, AVG(y) AS a FROM t GROUP BY h HAVING COUNT(*) > 1",
    "SELECT id, y FROM t ORDER BY y DESC NULLS LAST, id NULLS LAST LIMIT 3",
    "SELECT DISTINCT g FROM t",
    "SELECT t.id, u.w FROM t JOIN u ON t.g = u.g",
    "SELECT t.id, u.w FROM t LEFT JOIN u ON t.g = u.g",
    "SELECT id FROM t WHERE x IN (SELECT y FROM t)",
    "WITH c AS (SELECT g, MAX(y) AS m FROM t GROUP BY g) SELECT g, m FROM c WHERE m > 2",
    "SELECT id, CASE WHEN x IS NULL THEN 'none' ELSE s END AS v, COALESCE(x, y) AS c FROM t",
    "SELECT g, SUM(x * 2 + y) AS v FROM t GROUP BY g ORDER BY g NULLS FIRST",
    "SELECT y FROM t WHERE y > 3 UNION ALL SELECT w FROM u WHERE w < 25",
)


def generate() -> dict:
    """Returns {"datasets": [...], "cases": [{"id", "tier", "dataset", "sql", "timeout"}]}."""
    rng = random.Random(SEED)
    datasets = [make_dataset(rng, n) for n in DATASET_SIZES]
    small = range(len(datasets))
    datasets.append(make_big_dataset(rng, BIG_ROWS))
    big = len(datasets) - 1
    gen = Gen(rng)
    cases: list[dict] = []
    seen: set = set()

    def add(tier: str, templates, datasets_for, per_dataset: int, timeout: int) -> None:
        k = 0
        for d in datasets_for:
            produced = attempts = 0
            while produced < per_dataset and attempts < 200:
                attempts += 1
                sql = templates[k % len(templates)]()
                k += 1
                if (sql, d) in seen:
                    continue
                seen.add((sql, d))
                cases.append({"id": len(cases), "tier": tier, "dataset": d, "sql": sql, "timeout": timeout})
                produced += 1

    for tier in TIERS:
        if tier == "scale":
            add(tier, gen.scale(), [big], SCALE_CASES, 20)
        else:
            # every template of the group is used equally often (round-robin)
            templates = [t for name in GROUP_TEMPLATES[tier] for t in getattr(gen, name)()]
            per_dataset = 12 if tier == "composition" else CASES_PER_DATASET
            add(tier, templates, small, per_dataset, 10)

    for d in small:
        for sql in REGRESSION:
            cases.append({"id": len(cases), "tier": "regression", "dataset": d, "sql": sql, "timeout": 10})

    return {"datasets": datasets, "cases": cases}
