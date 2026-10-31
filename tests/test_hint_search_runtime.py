import json
from dataclasses import replace

from fake_postgres import SQL, FakeClock, FakeExecutor, MemoryCacheBackend

from genetic_lqo.core.profiles import load_profile
from genetic_lqo.execution.runtime_cache import PlanRuntimeCache
from genetic_lqo.search.hint_search.interpreter import render_messages
from genetic_lqo.search.hint_search.runtime import HintSearchRuntime


def config(budget=60.0, use_llm=False):
    base = load_profile("method").config
    return replace(base, search=replace(base.search, time_budget_seconds=budget, use_llm=use_llm))


class ScriptedProposer:

    def __init__(self):
        self.messages = []

    def for_call(self, directory):
        def call(messages, schema=None):
            self.messages.append(messages)
            if "cardinality program" in messages[1]["content"].split("\n")[-1]:
                return json.dumps({"diagnosis": "(a b) is overestimated",
                                   "neighborhood": [{"a b": [-2, -1]}], "seeds": [{"a b": -2}]})
            return json.dumps({"diagnosis": "b should be probed by index",
                               "neighborhood": {"access_path": ["IndexScan(b tb_pkey)"],
                                                "coupled": [["NestLoop(a b)", "IndexScan(b tb_pkey)"]]},
                               "seeds": [["IndexScan(b tb_pkey)"]]})
        return call


def run(tmp_path, cfg, proposer=None, cache=None):
    executor = FakeExecutor()
    runtime = HintSearchRuntime(cfg, SQL, "Q", executor, tmp_path, proposer=proposer, cache=cache,
                                clock=FakeClock(executor))
    return runtime, runtime.run()


def test_no_llm_control_finds_the_card_then_hint_improvement_and_prunes(tmp_path):
    runtime, summary = run(tmp_path, config())
    assert summary["native"]["seconds"] == 1.0
    assert summary["best_so_far"]["seconds"] == 0.1 and summary["best_so_far"]["complete"]
    programs = [i["program"] for i in runtime.improvements]
    assert programs[0] == "card" and "hint" in programs
    origins = [e["origin"] for e in runtime.events if e["event"] == "task"]
    assert origins[:2] == ["initial", "initial"] and any(o.startswith("improvement") for o in origins)
    pruned = runtime.final["config"]
    assert pruned.size <= runtime.best["config"].size
    assert runtime.evaluator.plan(pruned).plan_digest == runtime.best["fingerprint"]
    trace = json.loads((tmp_path / "trace.json").read_text())
    assert trace["native"]["complete"] and json.loads((tmp_path / "summary.json").read_text())["stop_reason"]


def test_cutoff_censors_slow_plans_and_shared_records_avoid_reexecution(tmp_path):
    runtime, summary = run(tmp_path, config())
    evaluations = [e for e in runtime.events if e["event"] == "evaluation"]
    censored = [e for e in evaluations if e["censored"]]
    assert censored and all(e["seconds"] >= e["cutoff"] for e in censored)
    assert any(e["source"] == "record" for e in evaluations)
    assert summary["counts"]["executions"] < len(evaluations)


def test_interpreter_recipes_drive_the_search_and_transfers_inherit_the_best_exactly(tmp_path):
    proposer = ScriptedProposer()
    runtime, summary = run(tmp_path, config(use_llm=True), proposer=proposer)
    assert summary["best_so_far"]["seconds"] == 0.1
    assert summary["counts"]["llm_calls"] >= 3
    transfer = [m[1]["content"] for m in proposer.messages if "## Before" in m[1]["content"]]
    assert any("## After: the cardinality program found a faster plan" in text for text in transfer)
    transferred = [t for t in runtime.tasks if t.origin.startswith("improvement")]
    assert transferred
    for task in transferred:
        source = runtime.improvements[int(task.origin.split()[-1]) - 1]
        assert list(task.recipe.base.hints()) == source["hints"]
        assert task.program != source["program"]
    assert (tmp_path / "llm" / "001" / "recipe.json").is_file()


def test_native_is_warmed_up_and_measured_fresh(tmp_path):
    backend = MemoryCacheBackend()
    cache = PlanRuntimeCache(backend, SQL, "Q")
    run(tmp_path / "1", config(budget=5), cache=cache)
    executor = FakeExecutor()
    calls = []
    original = executor.explain

    def explain(sql, hints=(), **kwargs):
        calls.append((tuple(hints), kwargs.get("analyze", False)))
        return original(sql, hints, **kwargs)

    executor.explain = explain
    HintSearchRuntime(config(budget=5), SQL, "Q", executor, tmp_path / "2", cache=cache,
                      clock=FakeClock(executor)).run()
    analyzed = [hints for hints, analyze in calls if analyze]
    assert analyzed[:2] == [(), ()]


def test_persistent_cache_serves_a_second_run_without_execution(tmp_path):
    backend = MemoryCacheBackend()
    cache = PlanRuntimeCache(backend, SQL, "Q")
    first, _ = run(tmp_path / "1", config(budget=20), cache=cache)
    second, summary = run(tmp_path / "2", config(budget=20), cache=cache)
    assert summary["counts"]["cache_hits"] > 0
    assert second.improvements[-1]["elapsed"] < first.improvements[-1]["elapsed"]


def test_prompts_are_plain_language_without_host_internals():
    for program in ("card", "hint"):
        values = {"sql": SQL, "tables": "- a", "seconds": "1.00 s", "plan": "-> Hash Join"}
        system, user = (m["content"] for m in render_messages(program, "initial", values))
        text = system + user
        for jargon in ("individuals", "genes", "best_so_far", "budget", "version", "channel", "provenance"):
            assert jargon not in text
        assert "diagnose" in text.lower() and '"neighborhood"' in system


def test_single_program_ablation_keeps_every_task_in_that_program(tmp_path):
    for program in ("card", "hint"):
        cfg = config()
        cfg = replace(cfg, search=replace(cfg.search, programs=(program,)))
        runtime, summary = run(tmp_path / program, cfg)
        assert {t.program for t in runtime.tasks} == {program} and summary["programs"] == [program]
        assert any(t.origin.startswith("improvement") for t in runtime.tasks)
        other = "hint" if program == "card" else "card"
        assert not runtime.final["config"].component(other)
        assert summary["best_so_far"]["seconds"] < summary["native"]["seconds"]


def test_reference_native_latency_sets_the_starting_best_and_cutoff(tmp_path):
    cfg = config(budget=10)
    cfg = replace(cfg, search=replace(cfg.search, native_seconds=30.0))
    runtime, summary = run(tmp_path, cfg)
    assert summary["native"]["seconds"] == 30.0 and summary["native"]["status"] == "reference"
    first = next(e for e in runtime.events if e["event"] == "evaluation")
    assert first["cutoff"] == 60.0
    assert runtime.native["analyzed"]
