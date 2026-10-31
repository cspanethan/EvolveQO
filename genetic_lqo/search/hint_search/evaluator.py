from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from genetic_lqo.core.types import ExecutionResult
from genetic_lqo.search.hint_search.models import Config


@dataclass
class Record:
    fingerprint: str
    seconds: float
    censored: bool
    source: str
    explain_json: Any = None
    analyzed: bool = False
    hint_status: str = ""


class Evaluator:
    def __init__(self, executor, sql: str, *, cache=None, explain_timeout: float = 5.0):
        self.executor, self.sql, self.cache = executor, sql, cache
        self.explain_timeout = explain_timeout
        self.records: dict[str, Record] = {}
        self.plans: dict[Config, ExecutionResult] = {}
        self.counts = {"explains": 0, "executions": 0, "timeouts": 0, "cache_hits": 0, "cancelled": 0, "failures": 0}

    def plan(self, config: Config) -> ExecutionResult:
        if config not in self.plans:
            self.counts["explains"] += 1
            result = self.executor.explain(self.sql, config.hints(), analyze=False,
                                           timeout_seconds=self.explain_timeout)
            if result.success and self.cache is not None:
                self.cache.record_plan(result, config.hints())
            self.plans[config] = result
        return self.plans[config]

    def measure(self, config: Config, plan: ExecutionResult, *, timeout: float, cutoff: float,
                fresh: bool = False) -> Record | None:
        fingerprint = plan.plan_digest
        if fingerprint in self.records:
            return self.records[fingerprint]
        hints = config.hints()
        cached = self.cache.lookup(fingerprint, hints) if self.cache is not None and not fresh else None
        if cached is not None:
            self.counts["cache_hits"] += 1
            record = Record(fingerprint, cached.median_seconds, False, "cache", plan.explain_json, False,
                            plan.hint_diagnostics.status if plan.hint_diagnostics else "")
            self.records[fingerprint] = record
            return record
        self.counts["executions"] += 1
        result = self.executor.explain(self.sql, hints, analyze=True, timeout_seconds=timeout)
        status = result.hint_diagnostics.status if result.hint_diagnostics else ""
        if result.success and result.execution_time_seconds is not None:
            result.plan_digest = fingerprint
            if self.cache is not None:
                self.cache.record_runtime(result, hints)
            record = Record(fingerprint, result.execution_time_seconds, False, "execution",
                            result.explain_json, True, status)
        elif result.timed_out and timeout >= cutoff:
            self.counts["timeouts"] += 1
            record = Record(fingerprint, cutoff, True, "execution", plan.explain_json, False, status)
        else:
            self.counts["cancelled" if result.timed_out else "failures"] += 1
            return None
        self.records[fingerprint] = record
        return record
