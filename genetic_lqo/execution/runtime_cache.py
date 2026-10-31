from contextlib import contextmanager
from threading import RLock

from genetic_lqo.execution.plan_cache import PlanCacheUnavailable, PostgresPlanCache

__all__ = ["PlanCacheUnavailable", "PlanRuntimeCache"]


class PlanRuntimeCache:
    def __init__(self, backend, sql, query_name):
        self.backend, self.sql, self.query_name = backend, sql, query_name
        self._lock = RLock()
        self._plan_locks = {}

    @classmethod
    def open(cls, connection, config, planner_settings, *, sql, query_name,
             measurement_metric="postgres_execution_time_seconds"):
        if not config.cache.enabled:
            raise PlanCacheUnavailable("Experiment execution requires a persistent plan cache")
        backend = PostgresPlanCache.open(
            business_connection=connection, config=config.cache,
            planner_settings={**dict(planner_settings), "measurement_metric": measurement_metric})
        return cls(backend, sql, query_name)

    @contextmanager
    def identity(self, digest, hints=()):
        with self._lock:
            lock = self._plan_locks.setdefault(digest, RLock())
        with lock:
            yield

    def record_plan(self, plan, hints):
        with self._lock:
            self.backend.store_explain(query_name=self.query_name, query_sql=self.sql, hints=hints, result=plan)

    def lookup(self, digest, hints=()):
        with self._lock:
            return self.backend.lookup_runtime(self.sql, digest, hints)

    def record_runtime(self, measured, hints, candidate_id=""):
        with self._lock:
            self.backend.store_runtime(query_name=self.query_name, query_sql=self.sql, hints=hints,
                                       result=measured, individual_id=candidate_id)

    def receipt(self):
        return {"enabled": True, "stats": self.backend.stats_snapshot().as_dict()}

    def close(self):
        self.backend.close()
