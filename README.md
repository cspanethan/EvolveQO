# EvolveQO

EvolveQO searches for faster PostgreSQL execution plans by co-evolving two kinds of interventions:

- **C-Engine** (`card`): scales the optimizer's cardinality estimates of join scopes (`Rows(... *f)`).
- **H-Engine** (`hint`): fixes access paths, join methods and join order through `pg_hint_plan`.

An LLM-based interpreter reads the SQL, schemas, statistics and executed plans, and writes a recipe (allele pool and seed individuals) for each engine. Each recipe starts a search task. All tasks share one plan evaluator, and the task scheduler serves the task with the highest sampled discovery rate, cutting executions off at κ × the best latency. Whenever one engine finds a faster plan, the improved configuration is handed to the other engine as the fixed configuration of a new task.

## Repository layout

```
genetic_lqo/
  search/hint_search/   engine, recipe, scheduler, evaluator, interpreter, runtime
  execution/            EXPLAIN / EXPLAIN ANALYZE executor and persistent plan cache
  planning/             hint dialect, plan parsing and fingerprints
  guidance/             LLM client
  prompts/interpreter.md
  workload/             JOB, CEB-3k, DSB and Stack queries and schemas
  config/               run profiles and LLM endpoints
  scripts/              command-line entry points
scripts/prepare_dsb.py  DSB SF50 data generation and import
tasks/                  query lists used in the evaluation
tests/                  offline tests with a deterministic fake database
```

## Requirements

- Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL with [`pg_hint_plan`](https://github.com/ossc-db/pg_hint_plan) in `shared_preload_libraries` (the paper uses PostgreSQL 18.4 and pg_hint_plan 1.8.0)
- An API key for the LLM provider (OpenAI by default; DeepSeek and Qwen are also supported)

```bash
uv sync
```

## Offline check

The following commands need neither a database nor a model.

```bash
.venv/bin/python -m pytest -q tests
uv run python -m genetic_lqo.scripts.prompt check
uv run python -m genetic_lqo.scripts.prompt preview --query JOB_7C --program card --stage transfer
```

## Database setup

| Workload | `--workload-set` | Database | Role | Query names |
| --- | --- | --- | --- | --- |
| JOB | `JOB` | `imdb` | `imdb` | `JOB_1A` … `JOB_33C` |
| CEB-3k | `CEB_3K` | `imdb` | `imdb` | `CEB_8A391`, … |
| DSB SF50 | `DSB` | `dsb_sf50` | `so` | `DSB_013_0`, … |
| Stack | `STACK_SO_PAST` / `STACK_SO_FUTURE` / `STACK_SO_SHIFTED` | `so_past` / `so_future` / `so_shift` | `so` | `STACK_Q1-098`, … |

1. Enable `pg_hint_plan`:

   ```
   shared_preload_libraries = 'pg_hint_plan'
   ```

2. Load the data.
   - **IMDB** (JOB and CEB): create `imdb` with [genetic_lqo/workload/job/schema.sql](genetic_lqo/workload/job/schema.sql) and load the IMDB CSV files described in [genetic_lqo/workload/job/README](genetic_lqo/workload/job/README).
   - **DSB**: build `dsdgen` from [microsoft/dsb](https://github.com/microsoft/dsb), then generate and import SF50 with the schema and indexes in `genetic_lqo/workload/dsb/`:

     ```bash
     .venv/bin/python scripts/prepare_dsb.py --data-dir /path/to/dsb-sf50 --generator-dir /path/to/dsb/code/tools
     ```

   - **Stack**: restore the StackOverflow databases `so_past`, `so_future` and `so_shift` with [genetic_lqo/workload/stack/schema.sql](genetic_lqo/workload/stack/schema.sql).
   - Run `ANALYZE` on every database after loading.

3. Point the code at the server. Defaults are `127.0.0.1:15432`; the database name and role come from the table above.

   ```bash
   export DB_HOST=127.0.0.1 DB_PORT=5432 DB_PASSWORD=...
   ```

4. Create the plan cache database `<db>_lqo_cache` once per business database and verify `pg_hint_plan`:

   ```bash
   uv run python -m genetic_lqo.scripts.init_plan_cache --create-database --db-name imdb
   DB_USER=so uv run python -m genetic_lqo.scripts.init_plan_cache --create-database --db-name dsb_sf50
   uv run python -m genetic_lqo.scripts.optimize --workload-name JOB_1A --verify-db
   ```

## LLM endpoint

The default model is GPT-5.6-Sol ([genetic_lqo/config/llm.toml](genetic_lqo/config/llm.toml)) with temperature 0.7:

```bash
export OPENAI_API_KEY=...
```

The other backbone LLMs are selected with `--config`:

| Model | Config | Key |
| --- | --- | --- |
| GPT-5.6-Sol (default) | — | `OPENAI_API_KEY` |
| DeepSeek-Flash | [genetic_lqo/config/deepseek.toml](genetic_lqo/config/deepseek.toml) | `DEEPSEEK_API_KEY` |
| Qwen-3.8-Max | [genetic_lqo/config/qwen.toml](genetic_lqo/config/qwen.toml) | `DASHSCOPE_API_KEY` |

`GENETIC_LQO_LLM_BASE_URL` and `GENETIC_LQO_LLM_MODEL` override the endpoint and model. Keys are read only from environment variables.

## Running

One query (T_max = 1 hour and κ = 2 by default):

```bash
uv run python -m genetic_lqo.scripts.optimize --workload-name JOB_1A
uv run python -m genetic_lqo.scripts.optimize --workload-name JOB_1A --profile smoke
uv run python -m genetic_lqo.scripts.optimize --workload-name CEB_8A391 --workload-set CEB_3K
uv run python -m genetic_lqo.scripts.optimize --workload-name JOB_1A --config genetic_lqo/config/deepseek.toml
```

A whole workload, one subprocess per query listed in `tasks/`:

```bash
uv run python -m genetic_lqo.scripts.run_method_batch --workload-set JOB --output-dir runs/job
uv run python -m genetic_lqo.scripts.run_method_batch --workload-set DSB --output-dir runs/dsb --resume
uv run python -m genetic_lqo.scripts.run_method_batch --workload-set JOB --output-dir runs/job_one --allow-subset --limit 1 --dry-run
```

| Option | Meaning |
| --- | --- |
| `--time-budget` | T_max in seconds, covering planning, LLM calls and executions (method 3600, smoke 90) |
| `--cutoff-factor` | κ; executions are cut off at κ × the best latency and recorded as censored (default 2.0) |
| `--no-llm` | default allele pools only, without the interpreter |
| `--programs card` / `--programs hint` | run only C-Engine or H-Engine |
| `--seed` | random seed (default 71) |
| `--profile` / `--config` | tracked profile, or a TOML overlay on the method profile |
| `--query-sql-file` + `--db-name` | run an arbitrary SQL file |
| `--resume` | batch only: skip queries that already have `summary.json` |

## Output

Each run writes to `runs/hint_search/<timestamp>/` unless `--output-dir` is given:

- `summary.json`: default-plan latency, best latency, speedup, final hints and counts
- `trace.json`: every search event and every improvement of the best plan
- `context.json`: query, aliases, indexes and search settings
- `llm/NNN/`: each interpreter request and response

A batch writes `batch.json` and `cells/NNNN_<query>/` with one `run.log` and `search/` per query. Latencies are the `Execution Time` of `EXPLAIN ANALYZE`; measured plans are cached in `<db>_lqo_cache` by SQL and plan fingerprint.
