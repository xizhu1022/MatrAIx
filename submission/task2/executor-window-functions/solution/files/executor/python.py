import bisect
import collections
import itertools
import math

from sqlglot import exp, planner, tokens
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ExecuteError
from sqlglot.executor.context import Context
from sqlglot.executor.env import ENV
from sqlglot.executor.table import RowReader, Table
from sqlglot.generators.python import PythonGenerator
from sqlglot.optimizer.scope import build_scope

SUBQUERY_NODES = (exp.Subquery, exp.Exists, exp.All, exp.Any)


class PythonExecutor:
    def __init__(self, env=None, tables=None):
        self.generator = Python().generator(identify=True, comments=False)
        self.env = {**ENV, **(env or {})}
        self.tables = tables or {}
        self._subquery_plans = {}
        self._plan_names_by_sql = {}
        self._ctes = None
        self._outer_scope = None
        self.env.update(
            SUBQUERY_COMPARISON=self._subquery_comparison,
            SUBQUERY_EXISTS=self._subquery_exists,
            SUBQUERY_SCALAR=self._subquery_scalar,
        )

    def execute(self, plan, outer_scope=None):
        ctes, scope = self._ctes, self._outer_scope
        self._ctes, self._outer_scope = plan.ctes, outer_scope
        try:
            return self._execute(plan)
        finally:
            self._ctes, self._outer_scope = ctes, scope

    def _execute(self, plan):
        finished = set()
        queue = set(plan.leaves)
        contexts = {}

        while queue:
            node = queue.pop()
            try:
                context = self.context(
                    {
                        name: table
                        for dep in node.dependencies
                        for name, table in contexts[dep].tables.items()
                    }
                )

                if isinstance(node, planner.Scan):
                    contexts[node] = self.scan(node, context)
                elif isinstance(node, planner.Aggregate):
                    contexts[node] = self.aggregate(node, context)
                elif isinstance(node, planner.Join):
                    contexts[node] = self.join(node, context)
                elif isinstance(node, planner.Sort):
                    contexts[node] = self.sort(node, context)
                elif isinstance(node, planner.SetOperation):
                    contexts[node] = self.set_operation(node, context)
                elif isinstance(node, planner.Window):
                    contexts[node] = self.window(node, context)
                else:
                    raise NotImplementedError

                if node.offset:
                    table = contexts[node].tables[node.name]
                    table.rows = table.rows[node.offset :]

                finished.add(node)

                for dep in node.dependents:
                    if all(d in contexts for d in dep.dependencies):
                        queue.add(dep)

                for dep in node.dependencies:
                    if all(d in finished for d in dep.dependents):
                        contexts.pop(dep)
            except Exception as e:
                raise ExecuteError(f"Step '{node.id}' failed: {e}") from e

        root = plan.root
        return contexts[root].tables[root.name]

    def generate(self, expression):
        """Convert a SQL expression into literal Python code and compile it into bytecode."""
        if not expression:
            return None

        expression = self._replace_subqueries(expression)
        sql = self.generator.generate(expression)
        return compile(sql, sql, "eval", optimize=2)

    def _replace_subqueries(self, expression):
        if not expression.find(*SUBQUERY_NODES):
            return expression

        expression = expression.copy()

        while True:
            subquery = expression.find(*SUBQUERY_NODES)

            if subquery is None:
                return expression

            target, replacement = self._compile_subquery(subquery)

            if target is expression:
                expression = replacement
            else:
                target.replace(replacement)

    def _compile_subquery(self, subquery):
        query = subquery.this.unnest()
        scope = build_scope(query)

        outer_columns = list(scope.external_columns if scope else [])

        plan = self._register_subquery(query)
        parent = subquery.parent

        if isinstance(subquery, exp.Exists):
            return subquery, exp.func(
                "SUBQUERY_EXISTS", plan, exp.var("scope"), *outer_columns, copy=False
            )

        if len(query.selects) != 1:
            raise ExecuteError(
                f"Subquery used as an expression returned {len(query.selects)} columns"
            )

        if isinstance(subquery, (exp.All, exp.Any)):
            return self._compile_quantified(parent, subquery.key.upper(), plan, outer_columns)

        if isinstance(parent, exp.In) and subquery is parent.args.get("query"):
            return self._compile_quantified(parent, "ANY", plan, outer_columns, op="EQ")

        return subquery, exp.func(
            "SUBQUERY_SCALAR", plan, exp.var("scope"), *outer_columns, copy=False
        )

    def _compile_quantified(self, comparison, quantifier, plan, outer_columns, op=None):
        if not isinstance(comparison, (exp.Binary, exp.In)):
            raise ExecuteError(f"Unsupported {quantifier} subquery: expected a comparison")

        return comparison, exp.func(
            "SUBQUERY_COMPARISON",
            comparison.this,
            plan,
            exp.var("scope"),
            exp.Literal.string(op or comparison.key.upper()),
            exp.Literal.string(quantifier),
            *outer_columns,
            copy=False,
        )

    def _register_subquery(self, query):
        if self._ctes is not None and not query.args.get("with_"):
            query.set("with_", self._ctes)

        sql = query.sql()
        name = self._plan_names_by_sql.get(sql)

        if name is None:
            name = self._plan_names_by_sql[sql] = f"_sq_{len(self._subquery_plans)}"
            self._subquery_plans[name] = (planner.Plan(query), {})

        return exp.Literal.string(name)

    def _subquery_table(self, plan_name, scope, args):
        plan, cache = self._subquery_plans[plan_name]

        try:
            return cache[args]
        except KeyError:
            pass
        except TypeError:  # an unhashable correlated value can't be memoized
            cache = None

        table = self.execute(plan, scope)

        if cache is not None:
            cache[args] = table

        return table

    def _subquery_exists(self, plan_name, scope, *args):
        return bool(self._subquery_table(plan_name, scope, args).rows)

    def _subquery_scalar(self, plan_name, scope, *args):
        rows = self._subquery_table(plan_name, scope, args).rows

        if len(rows) > 1:
            raise ExecuteError("More than one row returned by a subquery used as an expression")

        return rows[0][0] if rows else None

    def _subquery_comparison(self, value, plan_name, scope, op, quantifier, *args):
        compare = self.env[op]
        is_any = quantifier == "ANY"
        saw_null = False

        for row in self._subquery_table(plan_name, scope, args).rows:
            result = compare(value, row[0])

            if result is None:
                saw_null = True
            elif bool(result) is is_any:
                return is_any

        return None if saw_null else not is_any

    def generate_tuple(self, expressions):
        """Convert an array of SQL expressions into tuple of Python byte code."""
        if not expressions:
            return tuple()
        return tuple(self.generate(expression) for expression in expressions)

    def context(self, tables):
        return Context(tables, env=self.env, outer=self._outer_scope)

    def table(self, expressions):
        return Table(
            expression.alias_or_name if isinstance(expression, exp.Expr) else expression
            for expression in expressions
        )

    def scan(self, step, context):
        source = step.source

        if source and isinstance(source, exp.Expr):
            source = source.name or source.alias

        if source is None:
            context, table_iter = self.static()
        elif source in context:
            if not step.projections and not step.condition:
                return self.context({step.name: context.tables[source]})
            table_iter = context.table_iter(source)
        else:
            context, table_iter = self.scan_table(step)

        return self.context({step.name: self._project_and_filter(context, step, table_iter)})

    def _project_and_filter(self, context, step, table_iter):
        sink = self.table(step.projections if step.projections else context.columns)
        condition = self.generate(step.condition)
        projections = self.generate_tuple(step.projections)

        for reader in table_iter:
            if len(sink) >= step.offset + step.limit:
                break

            if condition and not context.eval(condition):
                continue

            if projections:
                sink.append(context.eval_tuple(projections))
            else:
                sink.append(reader.row)

        return sink

    def static(self):
        return self.context({}), [RowReader(())]

    def scan_table(self, step):
        table = self.tables.find(step.source)
        context = self.context({step.source.alias_or_name: table})
        return context, iter(table)

    def join(self, step, context):
        source = step.source_name

        source_table = context.tables[source]
        source_context = self.context({source: source_table})
        column_ranges = {source: range(0, len(source_table.columns))}

        for name, join in step.joins.items():
            table = context.tables[name]
            start = max(r.stop for r in column_ranges.values())
            column_ranges[name] = range(start, len(table.columns) + start)
            join_context = self.context({name: table})
            condition = self.generate(join["condition"])
            condition_context = (
                self.context(
                    {
                        name: Table(
                            source_context.columns + join_context.columns,
                            column_range=column_range,
                        )
                        for name, column_range in column_ranges.items()
                    }
                )
                if condition
                else None
            )

            if join.get("source_key"):
                table = self.hash_join(
                    join, source_context, join_context, condition, condition_context
                )
            else:
                table = self.nested_loop_join(
                    join, source_context, join_context, condition, condition_context
                )

            source_context = self.context(
                {
                    name: Table(table.columns, table.rows, column_range)
                    for name, column_range in column_ranges.items()
                }
            )
        if not step.condition and not step.projections:
            return source_context

        sink = self._project_and_filter(
            source_context,
            step,
            (reader for reader, _ in iter(source_context)),
        )

        if step.projections:
            return self.context({step.name: sink})
        else:
            return self.context(
                {
                    name: Table(table.columns, sink.rows, table.column_range)
                    for name, table in source_context.tables.items()
                }
            )

    @staticmethod
    def _join_matches(row, condition, condition_context):
        if not condition:
            return True

        condition_context.set_row(row)
        return condition_context.eval(condition) is True

    def nested_loop_join(self, join, source_context, join_context, condition, condition_context):
        table = Table(source_context.columns + join_context.columns)
        source_rows = source_context.table.rows
        join_rows = join_context.table.rows
        matched_source = set()
        matched_join = set()

        for source_index, source_row in enumerate(source_rows):
            for join_index, join_row in enumerate(join_rows):
                row = source_row + join_row
                if self._join_matches(row, condition, condition_context):
                    table.append(row)
                    matched_source.add(source_index)
                    matched_join.add(join_index)

        self._append_unmatched_join_rows(
            table, join, source_rows, join_rows, matched_source, matched_join
        )

        return table

    def hash_join(self, join, source_context, join_context, condition, condition_context):
        source_key = self.generate_tuple(join["source_key"])
        join_key = self.generate_tuple(join["join_key"])
        results = collections.defaultdict(lambda: ([], []))

        for index, (reader, ctx) in enumerate(source_context):
            key = ctx.eval_tuple(source_key)
            if all(value is not None for value in key):
                results[key][0].append((index, reader.row))
        for index, (reader, ctx) in enumerate(join_context):
            key = ctx.eval_tuple(join_key)
            if all(value is not None for value in key):
                results[key][1].append((index, reader.row))

        table = Table(source_context.columns + join_context.columns)
        matched_source = set()
        matched_join = set()

        for source_group, join_group in results.values():
            for (source_index, source_row), (join_index, join_row) in itertools.product(
                source_group, join_group
            ):
                row = source_row + join_row
                if self._join_matches(row, condition, condition_context):
                    table.append(row)
                    matched_source.add(source_index)
                    matched_join.add(join_index)

        self._append_unmatched_join_rows(
            table,
            join,
            source_context.table.rows,
            join_context.table.rows,
            matched_source,
            matched_join,
        )

        return table

    @staticmethod
    def _append_unmatched_join_rows(
        table, join, source_rows, join_rows, matched_source, matched_join
    ):
        side = join.get("side")
        if side in ("LEFT", "FULL"):
            join_nulls = (None,) * (len(table.columns) - len(source_rows[0]) if source_rows else 0)
            for index, row in enumerate(source_rows):
                if index not in matched_source:
                    table.append(row + join_nulls)

        if side in ("RIGHT", "FULL"):
            source_width = len(table.columns) - (len(join_rows[0]) if join_rows else 0)
            source_nulls = (None,) * source_width
            for index, row in enumerate(join_rows):
                if index not in matched_join:
                    table.append(source_nulls + row)

    def aggregate(self, step, context):
        group_by = self.generate_tuple(step.group.values())
        aggregations = self.generate_tuple(step.aggregations)
        operands = self.generate_tuple(step.operands)

        if operands:
            operand_table = Table(self.table(step.operands).columns)

            for reader, ctx in context:
                operand_table.append(ctx.eval_tuple(operands))

            for i, (a, b) in enumerate(zip(context.table.rows, operand_table.rows)):
                context.table.rows[i] = a + b

            width = len(context.columns)
            context.add_columns(*operand_table.columns)

            operand_table = Table(
                context.columns,
                context.table.rows,
                range(width, width + len(operand_table.columns)),
            )

            context = self.context(
                {
                    None: operand_table,
                    **context.tables,
                }
            )

        context.sort(group_by)

        group = None
        start = 0
        end = 1
        length = len(context.table)
        table = self.table(list(step.group) + step.aggregations)

        def add_row():
            table.append(group + context.eval_tuple(aggregations))

        if length:
            for i in range(length):
                context.set_index(i)
                key = context.eval_tuple(group_by)
                group = key if group is None else group
                end += 1
                if key != group:
                    context.set_range(start, end - 2)
                    add_row()
                    group = key
                    start = end - 2
                if not step.condition and len(table.rows) >= step.offset + step.limit:
                    break
                if i == length - 1:
                    context.set_range(start, end - 1)
                    add_row()
        elif step.limit > 0 and not group_by:
            context.set_range(0, 0)
            table.append(context.eval_tuple(aggregations))

        context = self.context({step.name: table, **{name: table for name in context.tables}})

        if step.projections or step.condition:
            return self.context(
                {step.name: self._project_and_filter(context, step, context.table_iter(step.name))}
            )
        return context

    def sort(self, step, context):
        projections = self.generate_tuple(step.projections)
        projection_columns = [p.alias_or_name for p in step.projections]
        all_columns = list(context.columns) + projection_columns
        sink = self.table(all_columns)
        for reader, ctx in context:
            sink.append(reader.row + ctx.eval_tuple(projections))

        sort_ctx = self.context(
            {
                None: sink,
                **{table: sink for table in context.tables},
            }
        )
        sort_ctx.sort(self.generate_tuple(step.key))

        if not math.isinf(step.limit):
            sort_ctx.table.rows = sort_ctx.table.rows[0 : step.offset + step.limit]

        rows = sort_ctx.table.rows

        if projection_columns:
            rows = [row[len(context.columns) : len(all_columns)] for row in rows]

        output = Table(projection_columns or context.columns, rows=rows)
        return self.context({step.name: output})

    def window(self, step, context):
        table = context.tables[step.source]
        columns = tuple(table.columns)
        computed = [
            self._window_values(window, step.source, columns, table.rows)
            for window in step.windows.values()
        ]
        rows = [row + tuple(values) for row, values in zip(table.rows, zip(*computed))]
        output = Table(columns + tuple(step.windows), rows)

        if step.projections or step.condition or not math.isinf(step.limit):
            # projections and QUALIFY reference the input relation, which keeps its name even
            # when this step is renamed (e.g. as the last step of a CTE)
            context = self.context({step.source: output})
            return self.context(
                {step.name: self._project_and_filter(context, step, context.table_iter(step.source))}
            )
        return self.context({step.name: output})

    def _window_values(self, window, source, columns, rows):
        """Evaluates one window function for every row; returns values in input row order.

        Rows are partitioned and sorted once. Frames are computed per row as position ranges
        in O(1) or O(log n), and frame aggregates are answered from prefix sums (SUM, COUNT,
        AVG) or sparse tables (MIN, MAX), so the cost is O(n log n) per window regardless of
        frame sizes.
        """
        func = window.this
        ignore_nulls = isinstance(func, exp.IgnoreNulls)
        if isinstance(func, (exp.IgnoreNulls, exp.RespectNulls)):
            func = func.this

        order = window.args.get("order")
        ordered = order.expressions if order else []
        spec = window.args.get("spec")
        if spec is not None and spec.args.get("kind") not in (None, "ROWS", "RANGE", "GROUPS"):
            raise ExecuteError(f"Unsupported window frame kind: {spec.args.get('kind')}")

        n = len(rows)
        input_ctx = self.context({source: Table(columns, rows)})

        def per_row(code):
            values = []
            for i in range(n):
                input_ctx.set_index(i)
                values.append(input_ctx.eval(code))
            return values

        def per_row_tuple(codes):
            values = []
            for i in range(n):
                input_ctx.set_index(i)
                values.append(input_ctx.eval_tuple(codes))
            return values

        partition_keys = per_row_tuple(self.generate_tuple(window.args.get("partition_by") or []))
        sort_keys = per_row_tuple(self.generate_tuple(ordered))

        partitions = {}
        for i, key in enumerate(partition_keys):
            partitions.setdefault(key, []).append(i)

        kind = WINDOW_AGGREGATES.get(type(func))
        if isinstance(func, (exp.Lag, exp.Lead, exp.FirstValue, exp.LastValue, exp.NthValue)):
            values = per_row(self.generate(func.this))
        elif kind is not None:
            arg = func.this
            if isinstance(arg, exp.Distinct):
                raise ExecuteError("DISTINCT is not supported in window aggregates")
            values = per_row(self.generate(arg))
        else:
            values = None

        def row_constant(arg, i, default):
            if arg is None:
                return default
            input_ctx.set_index(i)
            return input_ctx.eval(self.generate(arg))

        range_keys = None
        if spec is not None and spec.args.get("kind") == "RANGE":
            if any(isinstance(spec.args.get(b), exp.Expr) for b in ("start", "end")):
                if len(ordered) != 1:
                    raise ExecuteError("RANGE with an offset requires exactly one ORDER BY key")
                range_keys = per_row(self.generate(ordered[0].this))
        descending = bool(ordered and ordered[0].args.get("desc"))

        fallback_code = None
        if values is None and not isinstance(func, planner.NON_AGGREGATE_WINDOW_FUNCTIONS):
            fallback_code = self.generate(func)

        result = [None] * n

        for indices in partitions.values():
            indices.sort(key=lambda i: sort_keys[i])
            m = len(indices)

            # peer groups (runs of equal ORDER BY keys)
            group_of = [0] * m
            group_starts = [0]
            for p in range(1, m):
                if sort_keys[indices[p]] != sort_keys[indices[p - 1]]:
                    group_starts.append(p)
                group_of[p] = len(group_starts) - 1
            group_ends = group_starts[1:] + [m]

            vals = [values[i] for i in indices] if values is not None else None
            frames = _FrameBuilder(
                spec, m, group_of, group_starts, group_ends, bool(ordered),
                [range_keys[i] for i in indices] if range_keys is not None else None,
                descending,
            )

            if isinstance(func, exp.RowNumber):
                out = list(range(1, m + 1))
            elif isinstance(func, exp.Rank):
                out = [group_starts[group_of[p]] + 1 for p in range(m)]
            elif isinstance(func, exp.DenseRank):
                out = [group_of[p] + 1 for p in range(m)]
            elif isinstance(func, exp.PercentRank):
                out = [group_starts[group_of[p]] / (m - 1) if m > 1 else 0.0 for p in range(m)]
            elif isinstance(func, exp.CumeDist):
                out = [group_ends[group_of[p]] / m for p in range(m)]
            elif isinstance(func, exp.Ntile):
                out = []
                for p in range(m):
                    buckets = row_constant(func.this, indices[p], None)
                    out.append(None if buckets is None else self._ntile(p, m, int(buckets)))
            elif isinstance(func, (exp.Lag, exp.Lead)):
                non_null = [p for p in range(m) if vals[p] is not None] if ignore_nulls else None
                out = []
                for p in range(m):
                    offset = row_constant(func.args.get("offset"), indices[p], 1)
                    default = row_constant(func.args.get("default"), indices[p], None)
                    if offset is None:
                        out.append(None)
                        continue
                    if isinstance(func, exp.Lag):
                        offset = -offset
                    out.append(_navigate(vals, non_null, p, offset, default))
            elif isinstance(func, (exp.FirstValue, exp.LastValue, exp.NthValue)):
                non_null = [p for p in range(m) if vals[p] is not None] if ignore_nulls else None
                out = []
                for p in range(m):
                    pieces = frames.pieces(p)
                    if isinstance(func, exp.FirstValue):
                        out.append(_nth_in_pieces(vals, non_null, pieces, 1))
                    elif isinstance(func, exp.LastValue):
                        out.append(_nth_in_pieces(vals, non_null, pieces, -1))
                    else:
                        nth = row_constant(func.args.get("offset"), indices[p], None)
                        out.append(
                            _nth_in_pieces(vals, non_null, pieces, nth)
                            if nth is not None and nth >= 1
                            else None
                        )
            elif kind is not None:
                out = _frame_aggregates(kind, vals, frames, m)
            else:
                # any other aggregate: evaluate it over each frame with the engine's own code
                frame_ctx = self.context({source: Table(columns, [rows[i] for i in indices])})
                out = []
                for p in range(m):
                    pieces = frames.pieces(p)
                    if len(pieces) > 1:
                        raise ExecuteError("EXCLUDE is only supported for SUM/COUNT/AVG/MIN/MAX")
                    lo, hi = pieces[0] if pieces else (0, 0)
                    frame_ctx.set_range(lo, hi)
                    out.append(frame_ctx.eval(fallback_code))

            for p, value in enumerate(out):
                result[indices[p]] = value

        return result

    @staticmethod
    def _ntile(index, count, buckets):
        if buckets <= 0:
            raise ExecuteError("NTILE requires a positive number of buckets")
        size, remainder = divmod(count, buckets)
        large = remainder * (size + 1)
        if index < large:
            return index // (size + 1) + 1
        return (index - large) // size + remainder + 1

    def set_operation(self, step, context):
        left = context.tables[step.left]
        right = context.tables[step.right]

        sink = self.table(left.columns)

        if issubclass(step.op, exp.Intersect):
            right_counts = collections.Counter(right.rows)
            seen = set()
            for row in left.rows:
                if right_counts[row] and (not step.distinct or row not in seen):
                    sink.append(row)
                    seen.add(row)
                    if not step.distinct:
                        right_counts[row] -= 1
        elif issubclass(step.op, exp.Except):
            right_counts = collections.Counter(right.rows)
            seen = set()
            for row in left.rows:
                if right_counts[row] and not step.distinct:
                    right_counts[row] -= 1
                elif not right_counts[row] and (not step.distinct or row not in seen):
                    sink.append(row)
                    seen.add(row)
        elif issubclass(step.op, exp.Union) and step.distinct:
            sink.rows = list(set(left.rows).union(set(right.rows)))
        else:
            sink.rows = left.rows + right.rows

        if not math.isinf(step.limit):
            sink.rows = sink.rows[0 : step.offset + step.limit]

        return self.context({step.name: sink})


WINDOW_AGGREGATES = {
    exp.Sum: "SUM",
    exp.Count: "COUNT",
    exp.Avg: "AVG",
    exp.Min: "MIN",
    exp.Max: "MAX",
}


def _frame_offset(value):
    amount = value.to_py() if isinstance(value, exp.Literal) else None
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
        raise ExecuteError(f"Unsupported window frame offset: {value.sql()}")
    return amount


class _FrameBuilder:
    """Computes the frame of each row of one sorted partition as position ranges [lo, hi)."""

    def __init__(self, spec, m, group_of, group_starts, group_ends, has_order, keys, desc):
        self.spec = spec
        self.m = m
        self.group_of = group_of
        self.group_starts = group_starts
        self.group_ends = group_ends
        self.has_order = has_order
        self.kind = (spec.args.get("kind") or "ROWS") if spec is not None else None
        self.exclude = spec.args.get("exclude") if spec is not None else None
        if isinstance(self.exclude, exp.Expr):
            self.exclude = self.exclude.name
        self.exclude = (self.exclude or "NO OTHERS").upper()

        if keys is not None:
            non_null = [p for p in range(m) if keys[p] is not None]
            self.nn_start = non_null[0] if non_null else 0
            self.nn_end = non_null[-1] + 1 if non_null else 0
            # ascending search keys over the non-NULL block
            self.search = [(-keys[p] if desc else keys[p]) for p in non_null]
        self.keys = keys
        self.desc = desc

    def bounds(self, p):
        if self.spec is None:
            if self.has_order:
                return 0, self.group_ends[self.group_of[p]]
            return 0, self.m

        spec = self.spec
        lo = self._bound(p, spec.args.get("start"), spec.args.get("start_side"), True)
        end = spec.args.get("end")
        if end is None:
            hi = self._bound(p, "CURRENT ROW", None, False)
        else:
            hi = self._bound(p, end, spec.args.get("end_side"), False)

        lo = min(max(lo, 0), self.m)
        hi = min(max(hi, 0), self.m)
        return lo, max(lo, hi)

    def pieces(self, p):
        """The frame of row p as ordered, non-empty, disjoint ranges (EXCLUDE splits it)."""
        lo, hi = self.bounds(p)
        if self.exclude == "NO OTHERS":
            keep = ((0, self.m),)
        else:
            g = self.group_of[p]
            gs, ge = self.group_starts[g], self.group_ends[g]
            if self.exclude == "CURRENT ROW":
                keep = ((0, p), (p + 1, self.m))
            elif self.exclude == "GROUP":
                keep = ((0, gs), (ge, self.m))
            elif self.exclude == "TIES":
                keep = ((0, gs), (p, p + 1), (ge, self.m))
            else:
                raise ExecuteError(f"Unsupported EXCLUDE option: {self.exclude}")

        out = []
        for a, b in keep:
            a, b = max(a, lo), min(b, hi)
            if a < b:
                out.append((a, b))
        return out

    def _bound(self, p, value, side, is_start):
        m = self.m
        if value == "UNBOUNDED":
            return 0 if side == "PRECEDING" else m

        kind = self.kind
        g = self.group_of[p]

        if value == "CURRENT ROW":
            if kind == "ROWS":
                return p if is_start else p + 1
            return self.group_starts[g] if is_start else self.group_ends[g]

        amount = _frame_offset(value)
        signed = -amount if side == "PRECEDING" else amount

        if kind == "ROWS":
            return p + signed if is_start else p + signed + 1

        if kind == "GROUPS":
            target = g + int(signed)
            groups = len(self.group_starts)
            if is_start:
                return self.group_starts[target] if 0 <= target < groups else (0 if target < 0 else m)
            return self.group_ends[target] if 0 <= target < groups else (0 if target < 0 else m)

        # RANGE with a value offset over the single ORDER BY key
        current = self.keys[p]
        if current is None:
            return self.group_starts[g] if is_start else self.group_ends[g]
        search_current = -current if self.desc else current
        target = search_current + signed
        if is_start:
            return self.nn_start + bisect.bisect_left(self.search, target)
        return self.nn_start + bisect.bisect_right(self.search, target)


def _navigate(vals, non_null, p, offset, default):
    if non_null is None:
        target = p + offset
        return vals[target] if 0 <= target < len(vals) else default
    if offset == 0:
        return vals[p]
    if offset < 0:
        j = bisect.bisect_left(non_null, p) + offset
    else:
        j = bisect.bisect_right(non_null, p) + offset - 1
    return vals[non_null[j]] if 0 <= j < len(non_null) else default


def _nth_in_pieces(vals, non_null, pieces, nth):
    """The nth value (1-based; -1 means last) of the frame, skipping NULLs if non_null is given."""
    if non_null is None:
        if nth == -1:
            return vals[pieces[-1][1] - 1] if pieces else None
        for a, b in pieces:
            if nth <= b - a:
                return vals[a + nth - 1]
            nth -= b - a
        return None

    if nth == -1:
        for a, b in reversed(pieces):
            j = bisect.bisect_left(non_null, b) - 1
            if j >= 0 and non_null[j] >= a:
                return vals[non_null[j]]
        return None
    for a, b in pieces:
        first = bisect.bisect_left(non_null, a)
        count = bisect.bisect_left(non_null, b) - first
        if nth <= count:
            return vals[non_null[first + nth - 1]]
        nth -= count
    return None


def _frame_aggregates(kind, vals, frames, m):
    out = []
    if kind in ("SUM", "COUNT", "AVG"):
        exact = all(v is None or (isinstance(v, int) and not isinstance(v, bool)) for v in vals)
        sums, counts = [0], [0]
        for v in vals:
            sums.append(sums[-1] + (v if v is not None and exact else 0))
            counts.append(counts[-1] + (v is not None))
        for p in range(m):
            total, count = 0, 0
            pieces = frames.pieces(p)
            for a, b in pieces:
                count += counts[b] - counts[a]
                if exact:
                    total += sums[b] - sums[a]
                else:
                    total += math.fsum(v for v in vals[a:b] if v is not None)
            if kind == "COUNT":
                out.append(count)
            elif count == 0:
                out.append(None)
            else:
                out.append(total if kind == "SUM" else total / count)
        return out

    better = (lambda a, b: a if b is None or (a is not None and a < b) else b) if kind == "MIN" else (
        lambda a, b: a if b is None or (a is not None and a > b) else b
    )
    table = _SparseTable(vals, better)
    for p in range(m):
        best = None
        for a, b in frames.pieces(p):
            best = better(best, table.query(a, b)) if best is not None else table.query(a, b)
        out.append(best)
    return out


class _SparseTable:
    """O(1) range queries for an idempotent combine function (MIN/MAX skipping NULLs)."""

    def __init__(self, values, combine):
        self.combine = combine
        self.levels = [list(values)]
        width = 1
        while 2 * width <= len(values):
            prev = self.levels[-1]
            self.levels.append(
                [combine(prev[i], prev[i + width]) for i in range(len(values) - 2 * width + 1)]
            )
            width *= 2

    def query(self, lo, hi):
        if lo >= hi:
            return None
        level = (hi - lo).bit_length() - 1
        row = self.levels[level]
        return self.combine(row[lo], row[hi - (1 << level)])


class Python(Dialect):
    class Tokenizer(tokens.Tokenizer):
        STRING_ESCAPES = ["\\"]

    Generator = PythonGenerator
