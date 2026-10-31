from __future__ import annotations

import re

from genetic_lqo.core.types import ExecutionResult, HintDiagnostics
from genetic_lqo.execution.executor import IndexMetadata
from genetic_lqo.planning.plan import build_plan_summary, plan_digest

SQL = "SELECT count(*) FROM ta a, tb b, tc c WHERE a.id = b.a_id AND b.id = c.b_id AND a.x = 1"
JOIN_NODE = {"HashJoin": "Hash Join", "MergeJoin": "Merge Join", "NestLoop": "Nested Loop"}
SCAN_NODE = {"SeqScan": "Seq Scan", "IndexScan": "Index Scan", "BitmapScan": "Bitmap Heap Scan"}


def _decisions(hints):
    order, joins, scans, rows = None, {}, {}, {}
    for hint in hints:
        head, body = re.match(r"(\w+)\((.*)\)$", hint).groups()
        if head == "Leading":
            order = body
        elif head in JOIN_NODE:
            joins[tuple(sorted(body.split()))] = head
        elif head in SCAN_NODE:
            scans[body.split()[0]] = head
        elif head == "Rows":
            *scope, factor = body.split()
            rows[tuple(sorted(scope))] = float(factor[1:])
    return order, joins, scans, rows


def plan_for(hints):
    order, joins, scans, rows = _decisions(hints)
    if rows.get(("a", "b"), 1) <= 0.1 and ("a", "b") not in joins:
        joins[("a", "b")] = "NestLoop"
    inner = ("b", "a") if order and order.startswith("((b a)") else ("a", "b")

    def scan(alias):
        kind = scans.get(alias, "SeqScan")
        node = {"Node Type": SCAN_NODE[kind], "Alias": alias, "Relation Name": "t" + alias,
                "Plan Rows": 1000, "Actual Rows": 1000, "Actual Loops": 1, "Actual Total Time": 1.0}
        if kind == "IndexScan":
            node["Index Name"] = f"t{alias}_pkey"
        return node

    def join(scope, children):
        method = joins.get(tuple(sorted(scope)), "HashJoin")
        return {"Node Type": JOIN_NODE[method], "Join Type": "Inner", "Plan Rows": 100, "Actual Rows": 5000,
                "Actual Loops": 1, "Actual Total Time": 10.0, "Plans": children}

    ab = join(inner, [scan(inner[0]), scan(inner[1])])
    return {"Plan": join(("a", "b", "c"), [ab, scan("c")])}, joins, scans


def latency(hints) -> float:
    _, joins, scans = plan_for(hints)
    seconds = 1.0
    if joins.get(("a", "b")) == "NestLoop":
        seconds = 0.4
        if scans.get("b") == "IndexScan":
            seconds = 0.1
    if joins.get(("a", "b", "c")) == "MergeJoin":
        seconds *= 3
    return seconds


class FakeExecutor:
    def __init__(self):
        self.executions = 0
        self.explains = 0

    def explain(self, sql, hints=(), *, analyze=False, timeout_seconds=None, **_):
        root, _, _ = plan_for(hints)
        payload = [dict(root)]
        digest = plan_digest(payload)
        diagnostics = HintDiagnostics(status="all_used" if hints else "not_requested", requested_hints=tuple(hints))
        if not analyze:
            self.explains += 1
            return ExecutionResult(sql, tuple(hints), True, False, plan_digest=digest, explain_json=payload,
                                   plan_summary=build_plan_summary(payload, fallback_digest=digest),
                                   hint_diagnostics=diagnostics)
        self.executions += 1
        seconds = latency(hints)
        if timeout_seconds is not None and seconds > timeout_seconds:
            return ExecutionResult(sql, tuple(hints), False, True, timed_out=True, timeout_seconds=timeout_seconds,
                                   hint_diagnostics=diagnostics)
        payload[0]["Execution Time"] = seconds * 1000
        return ExecutionResult(sql, tuple(hints), True, True, plan_digest=digest, explain_json=payload,
                               plan_summary=build_plan_summary(payload, fallback_digest=digest),
                               hint_diagnostics=diagnostics, execution_time_seconds=seconds)

    def fetch_indexes(self, tables):
        return {table: [IndexMetadata(table, f"{table}_pkey", f"CREATE INDEX {table}_pkey")] for table in tables}

    def fetch_table_statistics(self, aliases):
        return [{"node": {"postgresScan": {"alias": alias, "relation": f"public.{table}", "baseConf": {
            "statistics": {"n_rows": 1000.0, "columnStats": [
                {"column": "id", "null_fraction": 0.0, "estimated_distinct_values": 1000.0,
                 "most_common_values": [], "most_common_frequencies": [], "histogram_bounds": []}]}}}}}
            for alias, table in sorted(aliases.items())]


class FakeClock:

    def __init__(self, executor: FakeExecutor, per_explain=0.001):
        self.executor, self.per_explain = executor, per_explain
        self.seconds = 0.0

    def __call__(self):
        return self.seconds + self.executor.explains * self.per_explain + self.executor.executions * 0.5


class MemoryCacheBackend:

    def __init__(self):
        self.runtimes, self.plans, self.warnings = {}, {}, []

    def store_explain(self, *, query_name, query_sql, hints, result):
        self.plans[(query_sql, tuple(sorted(hints)))] = result.plan_digest

    def lookup_runtime(self, query_sql, plan_digest, hints=()):
        from statistics import median

        from genetic_lqo.execution.plan_cache import CachedRuntime
        samples = self.runtimes.get((query_sql, plan_digest))
        return CachedRuntime(median(samples), len(samples)) if samples else None

    def store_runtime(self, *, query_name, query_sql, hints, result, individual_id=""):
        if result.success and result.analyze and not result.timed_out and result.execution_time_seconds:
            self.runtimes.setdefault((query_sql, result.plan_digest), []).append(result.execution_time_seconds)

    def stats_snapshot(self):
        from genetic_lqo.execution.plan_cache import PlanCacheStats
        return PlanCacheStats()

    def close(self):
        pass
