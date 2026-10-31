from __future__ import annotations

import math
import random
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from genetic_lqo.core.artifacts import write_json
from genetic_lqo.core.progress import emit_progress
from genetic_lqo.core.types import GeneticSearchConfig
from genetic_lqo.planning.plan import extract_alias_map
from genetic_lqo.search.hint_search import scheduler
from genetic_lqo.search.hint_search.engine import Task
from genetic_lqo.search.hint_search.evaluator import Evaluator, Record
from genetic_lqo.search.hint_search.interpreter import Interpreter, describe_config, recipe_summary, seconds_text
from genetic_lqo.search.hint_search.models import CARD, HINT, PROGRAMS, Config, HintGrammar, opposite
from genetic_lqo.search.hint_search.observations import table_summary
from genetic_lqo.search.hint_search.recipe import Recipe, plan_moves


class HintSearchRuntime:
    def __init__(self, config: GeneticSearchConfig, sql: str, query_name: str, executor, output_dir: Path, *,
                 proposer=None, cache=None, clock=time.monotonic):
        self.config, self.sql, self.query_name = config, sql, query_name
        self.executor, self.output = executor, Path(output_dir)
        self.proposer, self.clock = proposer, clock
        self.rng = random.Random(config.search.seed)
        self.evaluator = Evaluator(executor, sql, cache=cache, explain_timeout=config.search.explain_timeout_seconds)
        self.tasks: list[Task] = []
        self.pending: dict[Future, dict] = {}
        self.events: list[dict] = []
        self.improvements: list[dict] = []
        self._scale_cache: dict[str, tuple[int, float, float]] = {}

    def run(self) -> dict:
        search = self.config.search
        self.started = self.clock()
        self.deadline = self.started + search.time_budget_seconds
        aliases = extract_alias_map(self.sql)
        index_rows = self.executor.fetch_indexes(sorted(set(aliases.values())))
        indexes = {alias: [{"index_name": i.index_name, "index_def": i.index_def}
                           for i in index_rows.get(table, [])] for alias, table in aliases.items()}
        self.grammar = HintGrammar(tuple(aliases), indexes)
        statistics = self.executor.fetch_table_statistics(aliases)
        self.interpreter = Interpreter(self.proposer if search.use_llm else None, self.grammar, sql=self.sql,
                                       tables=table_summary(self.sql, aliases, statistics, indexes),
                                       output_dir=self.output)
        write_json(self.output / "context.json", {
            "query_name": self.query_name, "sql": self.sql, "aliases": aliases, "indexes": indexes,
            "search": vars(search), "llm_model": self.config.llm.model if search.use_llm else None})

        self.native = self._native()
        self.best = self.native
        pool = ThreadPoolExecutor(thread_name_prefix="interpreter")
        self.pool = pool
        try:
            initial = {program: self._submit(program, "initial", self.interpreter.initial_values(
                self.native["seconds"], self.native["explain_json"]), origin="initial")
                for program in PROGRAMS if program in search.programs}
            self._await(list(initial.values()))
            self.stop_reason = self._loop()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self.final = self._prune(self.best)
        return self._persist()

    def _native(self) -> dict:
        config = Config()
        plan = self.evaluator.plan(config)
        if not plan.success:
            raise RuntimeError(f"EXPLAIN of the default plan failed: {plan.error}")
        timeout = self.config.search.baseline_timeout_seconds
        executed = self.executor.explain(self.sql, (), analyze=True, timeout_seconds=timeout)
        reference = self.config.search.native_seconds
        if reference is not None:
            record = Record(plan.plan_digest, reference, False, "reference",
                            executed.explain_json if executed.success else plan.explain_json, executed.success,
                            executed.hint_diagnostics.status if executed.hint_diagnostics else "")
            self.evaluator.records[plan.plan_digest] = record
        else:
            record = self.evaluator.measure(config, plan, timeout=timeout, cutoff=timeout, fresh=True)
        if record is None:
            raise RuntimeError("Executing the default plan failed")
        emit_progress(f"[hint_search] native seconds={record.seconds:.4f} censored={record.censored}")
        return self._best_entry(config, record, plan)

    def _best_entry(self, config: Config, record: Record, plan) -> dict:
        return {"config": config, "fingerprint": record.fingerprint, "seconds": record.seconds,
                "censored": record.censored,
                "hint_status": record.hint_status or (plan.hint_diagnostics.status if plan.hint_diagnostics else ""),
                "explain_json": record.explain_json if record.analyzed else plan.explain_json,
                "analyzed": record.analyzed}

    def _submit(self, program: str, stage: str, values: dict, *, origin: str) -> Future:
        best = self.best
        base_plan = best["explain_json"]
        future = self.pool.submit(self.interpreter.request, program, stage, values,
                                  base=best["config"], base_plan=base_plan)
        self.pending[future] = {"program": program, "origin": origin, "base": best}
        return future

    def _await(self, futures: list[Future]) -> None:
        remaining = max(0.0, self.deadline - self.clock())
        wait(futures, timeout=min(remaining, self.config.llm.timeout_seconds))
        self._absorb()

    def _absorb(self) -> None:
        for future in [f for f in self.pending if f.done()]:
            meta = self.pending.pop(future)
            self._launch(future.result(), meta["base"], meta["origin"])

    def _launch(self, recipe: Recipe, base: dict, origin: str) -> Task:
        task = Task(len(self.tasks), recipe, base["seconds"], base["fingerprint"], random.Random(self.rng.random()))
        task.origin = origin
        self.tasks.append(task)
        self._event("task", task=task.id, program=recipe.program, origin=origin, default=recipe.default,
                    diagnosis=recipe.diagnosis, moves=len(recipe.moves), seeds=len(recipe.seeds),
                    base=describe_config(recipe.base))
        return task

    def _loop(self) -> str:
        initial = list(self.tasks)
        seed_turn = 0
        while self.clock() < self.deadline:
            self._absorb()
            seeding = [t for t in initial if t.waiting]
            if seeding:
                task = seeding[seed_turn % len(seeding)]
                seed_turn += 1
            else:
                eligible = [t for t in self.tasks if t.can_breed()]
                if not eligible:
                    if not self.pending:
                        return "exhausted"
                    wait(list(self.pending), timeout=max(0.0, self.deadline - self.clock()),
                         return_when=FIRST_COMPLETED)
                    continue
                task = self._select(eligible)
            self._step(task)
        return "budget"

    def _select(self, eligible: list[Task]) -> Task:
        best, kappa = self.best["seconds"], self.config.search.cutoff_factor
        candidates = []
        for task in eligible:
            sigma, prior = self._scales(task.program)
            observations = [(max(s, 1e-6), c) for s, c in task.observations]
            candidates.append((task, scheduler.Posterior(max(task.base_seconds, 1e-6), observations, sigma, prior),
                               sigma))
        task, _ = scheduler.select(candidates, max(best, 1e-6), max(best, 1e-6) * kappa, self.rng)
        return task

    def _scales(self, program: str) -> tuple[float, float]:
        tasks = [t for t in self.tasks if t.program == program]
        count = sum(len(t.observations) for t in tasks)
        cached = self._scale_cache.get(program)
        if cached is None or cached[0] != count:
            groups = [[(max(s, 1e-6), c) for s, c in t.observations] for t in tasks]
            sigma = scheduler.estimate_sigma(groups)
            prior = scheduler.estimate_prior_scale(
                [(max(t.base_seconds, 1e-6), g) for t, g in zip(tasks, groups)], sigma)
            cached = (count, sigma, prior)
            self._scale_cache[program] = cached
        return cached[1], cached[2]

    def _step(self, task: Task) -> None:
        config = task.next_candidate()
        if config is None:
            return
        plan = self.evaluator.plan(config)
        if not plan.success or not plan.plan_digest:
            self._event("plan_failed", task=task.id, hints=list(config.hints()), error=plan.error)
            return
        cutoff = self.best["seconds"] * self.config.search.cutoff_factor
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            return
        shared = plan.plan_digest in self.evaluator.records
        record = self.evaluator.measure(config, plan, timeout=min(cutoff, remaining), cutoff=cutoff)
        if record is None:
            return
        new_to_task = record.fingerprint not in task.population
        task.observe(config, record.fingerprint, record.seconds, record.censored)
        if new_to_task and not record.censored:
            task.grow(plan_moves(task.program, plan.explain_json, self.grammar))
        improved = not record.censored and record.seconds < self.best["seconds"]
        self._event("evaluation", task=task.id, program=task.program, hints=list(config.hints()),
                    fingerprint=record.fingerprint, seconds=record.seconds, censored=record.censored,
                    source="record" if shared else record.source, cutoff=cutoff, improved=improved)
        if improved:
            self._improve(task, config, record, plan)

    def _improve(self, task: Task, config: Config, record: Record, plan) -> None:
        before = self.best
        self.best = self._best_entry(config, record, plan)
        self.improvements.append({"task": task.id, "program": task.program, "seconds": record.seconds,
                                  "hints": list(config.hints()), "elapsed": self.clock() - self.started})
        emit_progress(f"[hint_search] improvement program={task.program} task={task.id} "
                      f"seconds={record.seconds:.4f} previous={before['seconds']:.4f}")
        receiving = opposite(task.program)
        if receiving not in self.config.search.programs:
            receiving = task.program
        tasks = [f"- task {t.id}: best {seconds_text(t.best_seconds) if math.isfinite(t.best_seconds) else 'none'}"
                 f" after {len(t.observations)} trials; neighborhood {recipe_summary(t.recipe)}"
                 for t in self.tasks if t.program == receiving]
        values = self.interpreter.transfer_values(
            winner=task.program, receiving=receiving, tasks=tasks,
            before=(before["config"], before["seconds"], before["explain_json"], before["analyzed"]),
            after=(config, record.seconds, self.best["explain_json"], self.best["analyzed"]))
        self._submit(receiving, "transfer", values, origin=f"improvement {len(self.improvements)}")
        if self.interpreter.proposer is None:
            self._await(list(self.pending))

    def _prune(self, best: dict) -> dict:
        config = best["config"]
        for program in (HINT, CARD):
            for slot in list(config.component(program)):
                values = {s: v for s, v in config.component(program).items() if s != slot}
                candidate = config.with_component(program, values)
                plan = self.evaluator.plan(candidate)
                if plan.success and plan.plan_digest == best["fingerprint"]:
                    config = candidate
        return {**best, "config": config}

    def _event(self, kind: str, **fields) -> None:
        self.events.append({"event": kind, "elapsed": round(self.clock() - self.started, 4), **fields})

    def _persist(self) -> dict:
        final, native = self.final, self.native
        best = {"seconds": final["seconds"], "complete": not final["censored"], "hint_status": final["hint_status"],
                "hints": list(final["config"].hints()), "unpruned_hints": list(self.best["config"].hints()),
                "configuration": final["config"].describe(), "fingerprint": final["fingerprint"]}
        native_view = {"seconds": native["seconds"], "complete": not native["censored"],
                       "hint_status": native["hint_status"],
                       "status": "reference" if self.config.search.native_seconds is not None
                       else "censored" if native["censored"] else "measured",
                       "fingerprint": native["fingerprint"]}
        summary = {
            "status": "completed", "stop_reason": self.stop_reason, "query_name": self.query_name,
            "native": native_view, "best_so_far": best,
            "speedup": native["seconds"] / final["seconds"] if final["seconds"] > 0 else None,
            "elapsed_seconds": self.clock() - self.started, "cutoff_factor": self.config.search.cutoff_factor,
            "programs": list(self.config.search.programs),
            "counts": {**self.evaluator.counts, "evaluations": sum(e["event"] == "evaluation" for e in self.events),
                       "tasks": len(self.tasks), "llm_calls": self.interpreter.calls,
                       "improvements": len(self.improvements)},
        }
        write_json(self.output / "summary.json", summary)
        write_json(self.output / "trace.json", {"native": native_view, "improvements": self.improvements,
                                                 "events": self.events})
        return summary
