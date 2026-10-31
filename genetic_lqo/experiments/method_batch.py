from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genetic_lqo.core.artifacts import write_json
from genetic_lqo.core.profiles import primary_method_profile_for_workload
from genetic_lqo.core.progress import emit_progress
from genetic_lqo.workload.task_contract import get_task_contract_for_workload
from genetic_lqo.workload.workloads import get_workload_set

METHOD_BATCH_MANIFEST_NAME = "batch.json"


@dataclass(frozen=True)
class MethodBatchRequest:
    workload_set: str
    output_dir: Path
    seed: int = 71
    profile_name: str = "method"
    method: str = "hint_search"
    use_llm: bool = True
    time_budget_seconds: float | None = None
    cutoff_factor: float | None = None
    allow_subset: bool = False
    offset: int = 0
    limit: int | None = None
    dry_run: bool = False
    resume: bool = False
    workload_names: tuple[str, ...] = ()
    config_path: Path | None = None


def select_query_names(ordered: list[str], request: MethodBatchRequest) -> list[str]:
    explicit, sliced = bool(request.workload_names), request.offset != 0 or request.limit is not None
    if (explicit or sliced) and not request.allow_subset:
        raise ValueError("--workload, --offset and --limit require --allow-subset")
    if explicit and sliced:
        raise ValueError("--workload cannot be combined with --offset/--limit")
    if explicit:
        unknown = [name for name in request.workload_names if name not in ordered]
        if unknown:
            raise ValueError(f"Unregistered queries: {unknown!r}")
        return [name for name in ordered if name in request.workload_names]
    end = None if request.limit is None else request.offset + request.limit
    selected = ordered[request.offset:end]
    if not selected:
        raise ValueError("Method batch selection is empty")
    return selected


def build_method_batch_manifest(request: MethodBatchRequest, *, python_executable: str | None = None) -> dict:
    if request.method != "hint_search":
        raise ValueError("Method batch method must be hint_search")
    workload = get_workload_set(request.workload_set)
    task = get_task_contract_for_workload(workload)
    expected = primary_method_profile_for_workload(workload.name, request.method)
    if request.profile_name not in (expected, "smoke"):
        raise ValueError(f"{workload.name} batches use profile {expected!r}, got {request.profile_name!r}")
    names = select_query_names(list(task.ordered_unique_names), request)
    executable = python_executable or sys.executable
    cells = []
    for index, name in enumerate(names):
        directory = f"cells/{index:04d}_{name}"
        configuration = (["--config", str(request.config_path.resolve())] if request.config_path is not None
                         else ["--profile", request.profile_name])
        command = [executable, "-m", "genetic_lqo.scripts.optimize", *configuration,
                   "--workload-name", name, "--workload-set", workload.name, "--seed", str(request.seed),
                   "--output-dir", str(Path(request.output_dir) / directory / "search")]
        if not request.use_llm:
            command.append("--no-llm")
        if request.time_budget_seconds is not None:
            command += ["--time-budget", str(request.time_budget_seconds)]
        if request.cutoff_factor is not None:
            command += ["--cutoff-factor", str(request.cutoff_factor)]
        cells.append({"query_name": name, "task_weight": task.weights[name], "directory": directory,
                      "command": command, "status": "planned"})
    return {"workload_set": workload.name, "profile": request.profile_name, "seed": request.seed,
            "arm": "llm" if request.use_llm else "no_llm",
            "database": {"name": workload.db, "user": workload.db_user},
            "selection": {"selected_cell_count": len(cells),
                          "task_weight_sum": sum(cell["task_weight"] for cell in cells)},
            "status": "planned", "cells": cells}


def run_method_batch(request: MethodBatchRequest, *, runner=subprocess.run) -> dict[str, Any]:
    output = Path(request.output_dir)
    manifest = build_method_batch_manifest(request)
    manifest_path = output / METHOD_BATCH_MANIFEST_NAME
    if output.exists() and any(output.iterdir()) and not request.resume:
        raise ValueError(f"Output directory is not empty (use --resume): {output}")
    write_json(manifest_path, manifest)
    if request.dry_run:
        return manifest
    environment = {**os.environ, "DB_NAME": manifest["database"]["name"], "DB_USER": manifest["database"]["user"]}
    failed = 0
    for cell in manifest["cells"]:
        summary_path = output / cell["directory"] / "search" / "summary.json"
        if request.resume and summary_path.exists():
            cell["status"] = "completed"
            continue
        (output / cell["directory"]).mkdir(parents=True, exist_ok=True)
        emit_progress(f"[batch] start query={cell['query_name']}")
        started = time.monotonic()
        with open(output / cell["directory"] / "run.log", "a", encoding="utf-8") as log:
            returncode = runner(cell["command"], stdout=log, stderr=subprocess.STDOUT, env=environment).returncode
        cell.update(returncode=returncode, elapsed_seconds=round(time.monotonic() - started, 3),
                    status="completed" if returncode == 0 and summary_path.exists() else "failed")
        if cell["status"] == "completed":
            best = json.loads(summary_path.read_text(encoding="utf-8"))
            cell.update(native_seconds=best["native"]["seconds"], best_seconds=best["best_so_far"]["seconds"])
        failed += cell["status"] == "failed"
        write_json(manifest_path, manifest)
    manifest["status"] = "failed" if failed else "completed"
    write_json(manifest_path, manifest)
    return manifest
