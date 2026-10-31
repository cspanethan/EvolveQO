import json
from dataclasses import replace

import pytest
from fake_postgres import SQL, FakeExecutor, MemoryCacheBackend

import genetic_lqo
from genetic_lqo.core.profiles import load_profile
from genetic_lqo.execution.executor import PostgresConnectionConfig
from genetic_lqo.execution.runtime_cache import PlanRuntimeCache
from genetic_lqo.scripts.optimize import _apply_cli_overrides, _load_cli_config, build_parser
from genetic_lqo.search import optimizer


@pytest.fixture
def fake_database(monkeypatch):
    monkeypatch.setattr(optimizer, "PostgresGeneticExecutor", lambda *_args, **_kwargs: FakeExecutor())
    monkeypatch.setattr(optimizer.PlanRuntimeCache, "open",
                        lambda *_args, **kwargs: PlanRuntimeCache(MemoryCacheBackend(), kwargs["sql"], "q"))


def test_public_api_runs_hint_search_and_writes_artifacts(tmp_path, fake_database):
    config = load_profile("smoke").config
    config = replace(config, search=replace(config.search, use_llm=False, time_budget_seconds=5),
                     output=replace(config.output, output_dir=str(tmp_path)))
    result = genetic_lqo.run_genetic_lqo(SQL, "q", PostgresConnectionConfig(dbname="imdb"), config)
    assert result.summary["method"] == "hint_search"
    assert result.summary["best_so_far"]["seconds"] <= result.summary["native"]["seconds"]
    for name in ("summary", "trace", "context"):
        assert json.loads((tmp_path / f"{name}.json").read_text())
    with pytest.raises(ValueError, match="must be empty"):
        genetic_lqo.run_genetic_lqo(SQL, "q", PostgresConnectionConfig(dbname="imdb"), config)


def test_cli_overrides_only_the_paper_knobs():
    args = build_parser().parse_args(["--workload-name", "JOB_1A", "--profile", "smoke", "--time-budget", "30",
                                      "--cutoff-factor", "1.5", "--no-llm", "--seed", "3"])
    config = _apply_cli_overrides(_load_cli_config(args), args)
    assert (config.search.time_budget_seconds, config.search.cutoff_factor, config.search.use_llm,
            config.search.seed) == (30, 1.5, False, 3)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--workload-name", "JOB_1A", "--sql-workers", "2"])
    with pytest.raises(ValueError, match="kappa"):
        _apply_cli_overrides(_load_cli_config(args), build_parser().parse_args(
            ["--workload-name", "JOB_1A", "--cutoff-factor", "0.5"]))
