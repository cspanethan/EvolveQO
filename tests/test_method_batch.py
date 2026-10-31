import json
import subprocess
from pathlib import Path

import pytest

from genetic_lqo.experiments.method_batch import MethodBatchRequest, build_method_batch_manifest, run_method_batch
from genetic_lqo.scripts.run_method_batch import build_parser, request_from_args


def test_dry_run_plans_commands_for_the_selected_subset(tmp_path):
    args = build_parser().parse_args(["--workload-set", "JOB", "--output-dir", str(tmp_path / "b"),
                                      "--allow-subset", "--limit", "2", "--no-llm", "--time-budget", "60",
                                      "--dry-run"])
    manifest = run_method_batch(request_from_args(args))
    assert manifest["selection"]["selected_cell_count"] == 2 and manifest["arm"] == "no_llm"
    command = manifest["cells"][0]["command"]
    assert command[1:3] == ["-m", "genetic_lqo.scripts.optimize"]
    assert "--no-llm" in command and command[command.index("--time-budget") + 1] == "60.0"
    assert json.loads((tmp_path / "b" / "batch.json").read_text())["status"] == "planned"


def test_subset_selection_requires_allow_subset_and_known_queries(tmp_path):
    with pytest.raises(ValueError, match="allow-subset"):
        build_method_batch_manifest(MethodBatchRequest("JOB", tmp_path, limit=1))
    with pytest.raises(ValueError, match="Unregistered"):
        build_method_batch_manifest(MethodBatchRequest("JOB", tmp_path, allow_subset=True, workload_names=("X",)))


def test_batch_config_overlay_is_forwarded_to_every_query(tmp_path):
    config = Path("genetic_lqo/config/deepseek.toml")
    args = build_parser().parse_args(["--workload-set", "DSB", "--output-dir", str(tmp_path),
                                      "--config", str(config)])
    manifest = build_method_batch_manifest(request_from_args(args))
    assert manifest["selection"]["selected_cell_count"] == 90
    for cell in manifest["cells"]:
        command = cell["command"]
        assert command[command.index("--config") + 1] == str(config.resolve())
        assert "--profile" not in command


def test_failed_cells_are_kept_and_resume_skips_completed_ones(tmp_path):
    calls = []

    def runner(command, **_):
        name = command[command.index("--workload-name") + 1]
        calls.append(name)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1)
        search = Path(command[command.index("--output-dir") + 1])
        search.mkdir(parents=True, exist_ok=True)
        (search / "summary.json").write_text(json.dumps({"native": {"seconds": 1.0},
                                                         "best_so_far": {"seconds": 0.5}}))
        return subprocess.CompletedProcess(command, 0)

    request = MethodBatchRequest("JOB", tmp_path, allow_subset=True, limit=2)
    manifest = run_method_batch(request, runner=runner)
    assert [c["status"] for c in manifest["cells"]] == ["failed", "completed"] and manifest["status"] == "failed"
    assert manifest["cells"][1]["best_seconds"] == 0.5
    with pytest.raises(ValueError, match="resume"):
        run_method_batch(request, runner=runner)
    resumed = run_method_batch(MethodBatchRequest("JOB", tmp_path, allow_subset=True, limit=2, resume=True),
                               runner=runner)
    assert resumed["status"] == "completed" and len(calls) == 3
