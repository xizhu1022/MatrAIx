"""Deterministic generator of datasets and SQL test cases for the window-function verifier.

Every query is written in the SQL subset that both sqlglot (default dialect) and DuckDB
accept with the same meaning. Queries only use orderings and frames whose result is
uniquely determined (see `total_order`), so there is exactly one correct answer.
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

TIERS = ("ranking", "aggregates", "frames", "navigation", "composition", "scale")

BIG_ROWS = 50_000


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

    def key(self, columns=("x", "y", "s", "g", "h")) -> str:
        return f"{self.r.choice(columns)}{self.direction()} {self.nulls()}"

    def keys(self, total: bool, max_keys: int = 2, columns=("x", "y", "s", "g", "h")) -> str:
        """ORDER BY keys. With total=True a final `id` key makes the order a total order."""
        n = self.r.randint(1, max_keys)
        parts = []
        for _ in range(n):
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
        return self.r.choice(
            (
                "SUM(x)",
                "COUNT(x)",
                "COUNT(*)",
                "AVG(x)",
                "MIN(x)",
                "MAX(x)",
                "MIN(s)",
                "MAX(s)",
                "SUM(x * 2 + y)",
                "MAX(y - x)",
                "AVG(y)",
            )
        )

    def rows_frame(self) -> str:
        # bound types ordered: UP < nP < CR < nF < UF
        while True:
            a, b = self.r.randint(0, 3), self.r.randint(1, 4)
            if a <= b:
                break

        def bound(kind: int) -> str:
            n = self.r.randint(0, 3)
            return {
                0: "UNBOUNDED PRECEDING",
                1: f"{n} PRECEDING",
                2: "CURRENT ROW",
                3: f"{n} FOLLOWING",
                4: "UNBOUNDED FOLLOWING",
            }[kind]

        if self.r.random() < 0.2:
            return "ROWS " + self.r.choice(
                ("UNBOUNDED PRECEDING", f"{self.r.randint(0, 3)} PRECEDING", "CURRENT ROW")
            )
        return f"ROWS BETWEEN {bound(a)} AND {bound(b)}"

    def range_offset_frame(self) -> str:
        while True:
            a, b = self.r.randint(0, 3), self.r.randint(1, 4)
            if a <= b and (a in (1, 3) or b in (1, 3)):
                break

        def bound(kind: int) -> str:
            n = self.r.randint(0, 3)
            return {
                0: "UNBOUNDED PRECEDING",
                1: f"{n} PRECEDING",
                2: "CURRENT ROW",
                3: f"{n} FOLLOWING",
                4: "UNBOUNDED FOLLOWING",
            }[kind]

        return f"RANGE BETWEEN {bound(a)} AND {bound(b)}"

    def groups_frame(self) -> str:
        while True:
            a, b = self.r.randint(0, 3), self.r.randint(1, 4)
            if a <= b:
                break

        def bound(kind: int) -> str:
            n = self.r.randint(0, 2)
            return {
                0: "UNBOUNDED PRECEDING",
                1: f"{n} PRECEDING",
                2: "CURRENT ROW",
                3: f"{n} FOLLOWING",
                4: "UNBOUNDED FOLLOWING",
            }[kind]

        return f"GROUPS BETWEEN {bound(a)} AND {bound(b)}"

    def exclude(self) -> str:
        return self.r.choice(
            ("EXCLUDE CURRENT ROW", "EXCLUDE GROUP", "EXCLUDE TIES", "EXCLUDE NO OTHERS")
        )

    def range_peer_frame(self) -> str:
        return self.r.choice(
            (
                "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
                "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
                "RANGE BETWEEN CURRENT ROW AND CURRENT ROW",
                "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
                "RANGE UNBOUNDED PRECEDING",
            )
        )

    # ------------------------------------------------------------------ tiers

    def ranking(self) -> str:
        r = self.r
        p, k, kt = self.partition(), self.keys(False), self.keys(True)
        choice = r.randint(0, 5)
        if choice == 0:
            return f"SELECT id, ROW_NUMBER() OVER {self.over(p, 'ORDER BY ' + kt)} AS v FROM t"
        if choice == 1:
            p2, k2 = self.partition(), self.keys(False)
            return (
                f"SELECT id, RANK() OVER {self.over(p, 'ORDER BY ' + k)} AS v1, "
                f"DENSE_RANK() OVER {self.over(p2, 'ORDER BY ' + k2)} AS v2 FROM t"
            )
        if choice == 2:
            n = r.randint(1, 7)
            return f"SELECT id, NTILE({n}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v FROM t"
        if choice == 3:
            return (
                f"SELECT id, PERCENT_RANK() OVER {self.over(p, 'ORDER BY ' + k)} AS v1, "
                f"CUME_DIST() OVER {self.over(p, 'ORDER BY ' + k)} AS v2 FROM t"
            )
        if choice == 4:
            return (
                f"SELECT id, g, RANK() OVER {self.over(p)} AS v1, "
                f"DENSE_RANK() OVER (ORDER BY {self.key()}) AS v2, "
                f"ROW_NUMBER() OVER (ORDER BY {self.keys(True, 1)}) AS v3 FROM t"
            )
        k3 = self.keys(False, 3)
        return (
            f"SELECT id, RANK() OVER {self.over(p, 'ORDER BY ' + k3)} AS v1, "
            f"DENSE_RANK() OVER {self.over(p, 'ORDER BY ' + k3)} AS v2, "
            f"PERCENT_RANK() OVER (ORDER BY {k3}) AS v3 FROM t"
        )

    def aggregates(self) -> str:
        r = self.r
        p = self.partition()
        choice = r.randint(0, 4)
        if choice == 0:
            return f"SELECT id, {self.agg()} OVER {self.over(p)} AS v FROM t"
        if choice == 1:
            k = self.keys(False)
            return f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k)} AS v FROM t"
        if choice == 2:
            k1, k2 = self.keys(False), self.keys(False, 1)
            p2 = self.partition()
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k1)} AS v1, "
                f"{self.agg()} OVER {self.over(p2)} AS v2, "
                f"{self.agg()} OVER (ORDER BY {k2}) AS v3 FROM t"
            )
        if choice == 3:
            return f"SELECT id, x, {self.agg()} OVER () AS v1, {self.agg()} OVER () AS v2 FROM t"
        k = self.keys(False, 1, ("x", "s", "g"))
        return (
            f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k)} AS v1, "
            f"COUNT(*) OVER {self.over(p, 'ORDER BY ' + k)} AS v2 FROM t"
        )

    def frames(self) -> str:
        r = self.r
        p = self.partition()
        choice = r.randint(0, 6)
        if choice == 5:
            k = self.keys(False, 2, ("h", "g", "s", "x"))
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k, self.groups_frame())} AS v1, "
                f"{self.agg()} OVER {self.over(p, 'ORDER BY ' + k, self.groups_frame() + ' ' + self.exclude())} AS v2 FROM t"
            )
        if choice == 6:
            k = self.keys(False, 2, ("h", "g", "s", "x"))
            frame = r.choice((self.range_peer_frame(), self.groups_frame(), f"RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"))
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k, frame + ' ' + self.exclude())} AS v1, "
                f"COUNT(*) OVER {self.over(p, 'ORDER BY ' + k, frame + ' ' + self.exclude())} AS v2 FROM t"
            )
        if choice in (0, 1):
            f1, f2 = self.rows_frame(), self.rows_frame()
            kt = self.keys(True)
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + kt, f1)} AS v1, "
                f"{self.agg()} OVER {self.over(p, 'ORDER BY ' + kt, f2)} AS v2 FROM t"
            )
        if choice == 2:
            key = f"y{self.direction()} {self.nulls()}"
            f1, f2 = self.range_offset_frame(), self.range_offset_frame()
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + key, f1)} AS v1, "
                f"COUNT(*) OVER {self.over(p, 'ORDER BY ' + key, f2)} AS v2 FROM t"
            )
        if choice == 3:
            k = self.keys(False)
            return (
                f"SELECT id, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k, self.range_peer_frame())}"
                f" AS v1, {self.agg()} OVER {self.over(p, self.range_peer_frame())} AS v2 FROM t"
            )
        kt = self.keys(True, 1)
        n = r.randint(1, 3)
        return (
            f"SELECT id, SUM(x) OVER {self.over(p, 'ORDER BY ' + kt, f'ROWS BETWEEN {n + 2} PRECEDING AND {n} PRECEDING')} AS v1, "
            f"COUNT(*) OVER {self.over(p, 'ORDER BY ' + kt, f'ROWS BETWEEN {n} FOLLOWING AND {n + 1} FOLLOWING')} AS v2, "
            f"MIN(s) OVER {self.over(p, 'ORDER BY ' + kt, self.rows_frame())} AS v3 FROM t"
        )

    def navigation(self) -> str:
        r = self.r
        p, kt = self.partition(), self.keys(True)
        ign = lambda: " IGNORE NULLS" if r.random() < 0.5 else ""  # noqa: E731
        col = lambda: r.choice(("x", "s", "x + y"))  # noqa: E731
        choice = r.randint(0, 4)
        if choice == 0:
            fn = r.choice(("LAG", "LEAD"))
            args = col()
            if r.random() < 0.7:
                args += f", {r.randint(1, 3)}"
                if r.random() < 0.6 and not args.startswith("s"):
                    args += f", {r.choice(('-1', '0', 'y', '100'))}"
            return f"SELECT id, {fn}({args}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v FROM t"
        if choice == 1:
            return (
                f"SELECT id, LAG(x, 1, -1{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v1, "
                f"LEAD(s, {r.randint(1, 3)}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v2, "
                f"LAG(s, 2, g) OVER {self.over(p, 'ORDER BY ' + kt)} AS v3 FROM t"
            )
        if choice == 2:
            return (
                f"SELECT id, FIRST_VALUE({col()}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v1, "
                f"LAST_VALUE({col()}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v2, "
                f"NTH_VALUE({col()}, {r.randint(1, 3)}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt)} AS v3 FROM t"
            )
        if choice == 3:
            exc = lambda: " EXCLUDE CURRENT ROW" if r.random() < 0.4 else ""  # noqa: E731
            return (
                f"SELECT id, FIRST_VALUE({col()}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt, self.rows_frame() + exc())} AS v1, "
                f"LAST_VALUE({col()}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt, self.rows_frame())} AS v2, "
                f"NTH_VALUE({col()}, {r.randint(1, 3)}{ign()}) OVER {self.over(p, 'ORDER BY ' + kt, self.rows_frame())} AS v3 FROM t"
            )
        key = f"y{self.direction()} {self.nulls()}, id {self.nulls()}"
        return (
            f"SELECT id, FIRST_VALUE(x{ign()}) OVER {self.over(p, 'ORDER BY ' + key, 'ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING')} AS v1, "
            f"LAST_VALUE(s{ign()}) OVER {self.over(p, 'ORDER BY ' + key, 'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING')} AS v2 FROM t"
        )

    def composition(self) -> str:
        r = self.r
        p, k, kt = self.partition(), self.keys(False), self.keys(True)
        choice = r.randint(0, 9)
        if choice == 0:
            pp = self.partition(False)
            return (
                f"SELECT id, g, x FROM t QUALIFY ROW_NUMBER() OVER "
                f"{self.over(pp, 'ORDER BY ' + kt)} <= {r.randint(1, 2)}"
            )
        if choice == 1:
            return (
                f"SELECT id, RANK() OVER {self.over(p, 'ORDER BY ' + k)} AS v, "
                f"{self.agg()} OVER {self.over(p)} AS a FROM t QUALIFY v <= {r.randint(1, 3)}"
            )
        if choice == 2:
            pp = self.partition(False)
            return (
                f"SELECT id, {self.agg()} OVER w AS v1, ROW_NUMBER() OVER w2 AS v2, "
                f"{self.agg()} OVER (w2 {self.rows_frame()}) AS v3, RANK() OVER (w ORDER BY {k}) AS v4 "
                f"FROM t WINDOW w AS ({pp}), w2 AS (w ORDER BY {kt})"
            )
        if choice == 3:
            grp = r.choice(("g", "h"))
            having = r.choice(("", " HAVING COUNT(*) > 1"))
            return (
                f"SELECT {grp}, COUNT(*) AS n, SUM(x) AS sx, "
                f"RANK() OVER (ORDER BY COUNT(*) DESC NULLS LAST) AS r, "
                f"SUM(SUM(x)) OVER (ORDER BY {grp} {self.nulls()} ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS running, "
                f"MAX(y) - MIN(MAX(y)) OVER () AS spread FROM t GROUP BY {grp}{having}"
            )
        if choice == 4:
            return (
                f"SELECT id, x - LAG(x) OVER {self.over(p, 'ORDER BY ' + kt)} AS dx, "
                f"100.0 * x / SUM(x) OVER {self.over(p)} AS pct, "
                f"CASE WHEN ROW_NUMBER() OVER (ORDER BY {kt}) <= 3 THEN 'top' ELSE 'rest' END AS bucket FROM t"
            )
        if choice == 5:
            lim = r.randint(1, 5)
            if r.random() < 0.5:
                return (
                    f"SELECT id, x FROM t ORDER BY SUM(x) OVER {self.over(p)} DESC NULLS LAST, "
                    f"id {self.nulls()} LIMIT {lim}"
                )
            return (
                f"SELECT id, s, DENSE_RANK() OVER {self.over(p, 'ORDER BY ' + k)} AS v FROM t "
                f"ORDER BY v DESC NULLS LAST, id NULLS LAST LIMIT {lim}"
            )
        if choice == 6:
            return f"SELECT DISTINCT g, {self.agg()} OVER (PARTITION BY g) AS m FROM t"
        if choice == 7:
            side = r.choice(("JOIN", "LEFT JOIN"))
            return (
                f"SELECT t.id, u.w, SUM(u.w) OVER (PARTITION BY t.h ORDER BY t.id {self.nulls()}) AS v1, "
                f"ROW_NUMBER() OVER (PARTITION BY u.g ORDER BY t.y {self.nulls()}, t.id {self.nulls()}) AS v2 "
                f"FROM t {side} u ON t.g = u.g"
            )
        if choice == 8:
            if r.random() < 0.5:
                return (
                    f"SELECT q.id, q.r FROM (SELECT id, x, ROW_NUMBER() OVER "
                    f"{self.over(p, 'ORDER BY ' + kt)} AS r FROM t) AS q WHERE q.r <= 3 OR q.x > 4"
                )
            return (
                f"WITH c AS (SELECT g, COUNT(*) AS n FROM t GROUP BY g) "
                f"SELECT g, n, SUM(n) OVER (ORDER BY g {self.nulls()}) AS cum, "
                f"LAG(n) OVER (ORDER BY g {self.nulls()}) AS prev FROM c"
            )
        return (
            f"SELECT id, COUNT(*) OVER () AS c, {self.agg()} OVER {self.over(p, 'ORDER BY ' + k)} AS v "
            f"FROM t WHERE x > {r.randint(1, 4)} OR s IS NULL"
        )


    def scale(self) -> str:
        """Queries over the large table whose frames span many rows."""
        r = self.r
        p = self.partition()
        nl = self.nulls
        choice = r.randint(0, 9)
        if choice == 0:
            k = r.choice(("h", "g", "s"))
            return (
                f"SELECT id, SUM(y) OVER {self.over(p, f'ORDER BY {k} {nl()}')} AS v1, "
                f"COUNT(x) OVER {self.over(p, f'ORDER BY {k} DESC {nl()}')} AS v2, "
                f"AVG(y) OVER (ORDER BY {k} {nl()}) AS v3 FROM t"
            )
        if choice == 1:
            return (
                f"SELECT id, MIN(s) OVER {self.over(self.partition(False))} AS v1, "
                f"SUM(x) OVER {self.over(p)} AS v2, MAX(y) OVER () AS v3, COUNT(*) OVER {self.over(self.partition(False))} AS v4 FROM t"
            )
        if choice == 2:
            kt = f"id{r.choice(('', ' DESC'))} {nl()}"
            return (
                f"SELECT id, MIN(y) OVER {self.over(p, 'ORDER BY ' + kt, 'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING')} AS v1, "
                f"MAX(s) OVER {self.over(p, 'ORDER BY ' + kt, 'ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING')} AS v2, "
                f"SUM(x) OVER {self.over(p, 'ORDER BY ' + kt, 'ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING')} AS v3 FROM t"
            )
        if choice == 3:
            a, b = r.choice((1000, 5000, 20000)), r.choice((0, 1000, 5000))
            d = r.choice(("", " DESC"))
            return (
                f"SELECT id, SUM(x) OVER {self.over(p, f'ORDER BY y{d} {nl()}', f'RANGE BETWEEN {a} PRECEDING AND {b} FOLLOWING')} AS v1, "
                f"MAX(s) OVER {self.over(p, f'ORDER BY y{d} {nl()}', f'RANGE BETWEEN CURRENT ROW AND {a} FOLLOWING')} AS v2, "
                f"COUNT(*) OVER (ORDER BY y {nl()} RANGE BETWEEN {b} PRECEDING AND UNBOUNDED FOLLOWING) AS v3 FROM t"
            )
        if choice == 4:
            k = r.choice(("h NULLS LAST, g NULLS FIRST", "s NULLS LAST", "g DESC NULLS LAST, s NULLS FIRST"))
            return (
                f"SELECT id, COUNT(*) OVER (ORDER BY {k} GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING {self.exclude()}) AS v1, "
                f"SUM(y) OVER {self.over(p, 'ORDER BY ' + k, 'RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING ' + self.exclude())} AS v2, "
                f"MIN(x) OVER {self.over(p, 'ORDER BY ' + k, 'GROUPS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING ' + self.exclude())} AS v3 FROM t"
            )
        if choice == 5:
            kt = f"id{r.choice(('', ' DESC'))} {nl()}"
            return (
                f"SELECT id, FIRST_VALUE(x IGNORE NULLS) OVER {self.over(p, 'ORDER BY ' + kt, 'ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING')} AS v1, "
                f"LAST_VALUE(s) OVER {self.over(self.partition(False), 'ORDER BY ' + kt, 'ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING')} AS v2, "
                f"NTH_VALUE(x, {r.randint(2, 5)} IGNORE NULLS) OVER {self.over(p, 'ORDER BY ' + kt)} AS v3, "
                f"LAST_VALUE(x IGNORE NULLS) OVER {self.over(p, 'ORDER BY ' + kt, 'ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING EXCLUDE CURRENT ROW')} AS v4 FROM t"
            )
        if choice == 6:
            kt = f"y {nl()}, id {nl()}"
            return (
                f"SELECT id, LAG(x, {r.randint(1, 4)}, -1 IGNORE NULLS) OVER {self.over(p, 'ORDER BY ' + kt)} AS v1, "
                f"LEAD(x, {r.randint(1, 4)} IGNORE NULLS) OVER {self.over(p, 'ORDER BY ' + kt)} AS v2, "
                f"LEAD(s, {r.randint(1, 4)}, g) OVER (ORDER BY {kt}) AS v3 FROM t"
            )
        if choice == 7:
            return (
                f"SELECT id, RANK() OVER (ORDER BY h {nl()}) AS v1, DENSE_RANK() OVER {self.over(p, f'ORDER BY s {nl()}')} AS v2, "
                f"PERCENT_RANK() OVER (ORDER BY g {nl()}) AS v3, CUME_DIST() OVER {self.over(p, f'ORDER BY h DESC {nl()}')} AS v4, "
                f"NTILE({r.randint(3, 50)}) OVER (ORDER BY y {nl()}, id {nl()}) AS v5 FROM t"
            )
        if choice == 8:
            return (
                f"SELECT id, y FROM t QUALIFY ROW_NUMBER() OVER {self.over(self.partition(False), f'ORDER BY y DESC {nl()}, id {nl()}')} <= {r.randint(1, 3)} "
                f"OR SUM(x) OVER (ORDER BY id {nl()}) = {r.randint(5, 50)}"
            )
        return (
            f"SELECT id, s, SUM(x) OVER (PARTITION BY g ORDER BY y {nl()}, id {nl()}) AS v FROM t "
            f"ORDER BY v DESC NULLS LAST, id {nl()} LIMIT {r.randint(5, 20)}"
        )


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
    """Returns {"datasets": [...], "cases": [{"id", "tier", "dataset", "sql"}]}."""
    rng = random.Random(SEED)
    datasets = [make_dataset(rng, n) for n in DATASET_SIZES]
    small = range(len(datasets))
    datasets.append(make_big_dataset(rng, BIG_ROWS))
    big = len(datasets) - 1
    gen = Gen(rng)
    cases = []
    seen = set()

    while len([c for c in cases if c["tier"] == "scale"]) < 12:
        sql = gen.scale()
        if sql not in seen:
            seen.add(sql)
            cases.append({"id": len(cases), "tier": "scale", "dataset": big, "sql": sql, "timeout": 20})

    for tier in TIERS[:-1]:
        for d in small:
            produced = 0
            while produced < 8:
                sql = getattr(gen, tier)()
                if (sql, d) in seen:
                    continue
                seen.add((sql, d))
                cases.append({"id": len(cases), "tier": tier, "dataset": d, "sql": sql, "timeout": 10})
                produced += 1

    for d in small:
        for sql in REGRESSION:
            cases.append({"id": len(cases), "tier": "regression", "dataset": d, "sql": sql, "timeout": 10})

    return {"datasets": datasets, "cases": cases}
