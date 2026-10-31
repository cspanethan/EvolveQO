#!/usr/bin/env python3
"""Generate DSB data with dsdgen and import it into a new PostgreSQL database."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def schema_steps(text):
    text = re.sub(r"--[^\n]*", "", text)
    tables, primary_keys = {}, {}
    pattern = re.compile(r"create\s+table\s+(\w+)\s*\((.*?)\n\);", re.I | re.S)
    for match in pattern.finditer(text):
        name, columns = match.groups()
        key = re.search(r",\s*primary\s+key\s*\(([^)]+)\)\s*$", columns, re.I)
        if key:
            primary_keys[name + "_pkey"] = (
                f"ALTER TABLE public.{name} ADD CONSTRAINT {name}_pkey "
                f"PRIMARY KEY ({key.group(1)})"
            )
            columns = columns[:key.start()]
        tables[name] = f"CREATE TABLE public.{name} ({columns}\n)"
    if pattern.sub("", text).strip() or len(tables) != 25 or len(primary_keys) != 24:
        raise RuntimeError("Unrecognized DSB schema: expected 25 tables and 24 primary keys")
    return tables, primary_keys


def index_steps(text):
    result = {}
    text = re.sub(r"--[^\n]*", "", text)
    for statement in text.split(";"):
        if not statement.strip():
            continue
        identifier = r'(?:"[a-zA-Z_][a-zA-Z_0-9]*"|[a-zA-Z_][a-zA-Z_0-9]*)'
        match = re.match(rf"\s*create\s+index\s+({identifier})\s+on\s+(?:public\.)?({identifier})\s*(?:using\s+btree\s*)?\(", statement, re.I)
        name = match[1].strip('"') if match else None
        if not match or len(name) > 63 or name in result:
            raise RuntimeError("Index file must contain distinct CREATE INDEX statements with names <=63 bytes")
        result[name] = statement.strip()
    if not result:
        raise RuntimeError("Index file is empty")
    return result


def connect(args, database, owner=False):
    conn = psycopg.connect(host=args.host, port=args.port, dbname=database,
                          user=args.admin_user, connect_timeout=10,
                          application_name="prepare_dsb", autocommit=True)
    conn.execute("SET lock_timeout = '15s'")
    conn.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(f"{args.operation_timeout}s")))
    conn.execute("SET maintenance_work_mem = '1GB'")
    conn.execute("SET max_parallel_maintenance_workers = 1")
    conn.execute("SET temp_file_limit = '48GB'")
    if owner:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(args.owner)))
    conn.execute("SET default_tablespace = ''")
    conn.execute("SET temp_tablespaces = ''")
    return conn


def verify_database_identity(existing_oid, manifest, resume):
    if existing_oid is not None:
        if not resume:
            raise RuntimeError("Refusing to import into an existing database")
        if manifest.get("database_oid") != existing_oid:
            raise RuntimeError("Existing database does not match the run manifest identity")
    elif manifest.get("database_oid") is not None:
        raise RuntimeError("Run database disappeared; refusing to recreate it on resume")


def verify_loaded_table(recorded, actual_count, source):
    if recorded != source or actual_count != recorded["rows"]:
        raise RuntimeError("Committed table/source mismatch; refusing to COPY again")


class Run:
    def __init__(self, args, manifest, state, database_directory):
        self.args, self.manifest, self.state = args, manifest, state
        self.database_directory = database_directory
        self.lock = threading.RLock()

    def update(self, **values):
        with self.lock:
            self.state.update(values, updated_at_unix=time.time())
            atomic_json(self.args.out / "progress.json", self.state)

    def save_manifest(self):
        atomic_json(self.args.out / "manifest.json", self.manifest)

    def sample_disk(self):
        target = self.args.tablespace_dir or self.database_directory
        sample = {"time_unix": time.time(), "stage": self.state.get("stage"),
                  "raw_free_bytes": shutil.disk_usage(self.args.data_dir).free,
                  "database_free_bytes": shutil.disk_usage(target).free,
                  "wal_free_bytes": shutil.disk_usage(self.database_directory).free}
        with (self.args.out / "disk_samples.jsonl").open("a") as stream:
            stream.write(json.dumps(sample) + "\n")
        self.update(last_disk_sample=sample)
        if sample["raw_free_bytes"] < self.args.raw_min_free_gib * GIB:
            raise RuntimeError("Raw-data filesystem free space below safety reserve")
        target_reserve = self.args.raw_min_free_gib if self.args.tablespace_dir else self.args.db_min_free_gib
        if sample["database_free_bytes"] < target_reserve * GIB:
            raise RuntimeError("Database filesystem free space below safety reserve")
        if sample["wal_free_bytes"] < self.args.db_min_free_gib * GIB:
            raise RuntimeError("WAL filesystem free space below safety reserve")

    @contextmanager
    def operation(self, stage, conn=None, process=None, timeout=None):
        self.update(stage=stage, stage_started_at_unix=time.time(), status="running")
        self.sample_disk()
        finished, errors = threading.Event(), []
        deadline = time.monotonic() + (timeout or self.args.operation_timeout)

        def watch():
            while not finished.wait(self.args.sample_seconds):
                try:
                    self.sample_disk()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Operation exceeded deadline: {stage}")
                except BaseException as exc:
                    errors.append(exc)
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    if conn is not None:
                        try:
                            conn.cancel_safe(timeout=5)
                        except Exception:
                            pass
                    return

        watcher = threading.Thread(target=watch, name="dsb-space-guard", daemon=True)
        watcher.start()
        try:
            yield
        finally:
            finished.set()
            watcher.join(timeout=20)
            if errors:
                self.update(guard_error=str(errors[0]))
                raise errors[0]


def source_inventory(path):
    digest, rows, total = hashlib.sha256(), 0, 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
            rows += block.count(b"\n")
            total += len(block)
    return {"bytes": total, "rows": rows, "sha256": digest.hexdigest()}


def generate(run, tables):
    args = run.args
    if run.manifest.get("generation_complete"):
        for table in tables:
            if not (args.data_dir / f"{table}.dat").is_file():
                raise RuntimeError(f"Generated data missing: {table}")
        return
    if any(args.data_dir.iterdir()):
        raise RuntimeError("Incomplete generation retained; review it and use a fresh run/data directory")
    command = [str(args.generator_dir / "dsdgen"), "-scale", str(args.scale),
               "-terminate", "N", "-force", "-dir", str(args.data_dir), "-verbose"]
    run.manifest["generator_command"] = command
    run.save_manifest()
    run.sample_disk()
    with (args.out / "generator.log").open("a") as log:
        process = subprocess.Popen(command, cwd=args.generator_dir, stdout=log, stderr=subprocess.STDOUT)
        run.update(generator_pid=process.pid)
        try:
            with run.operation("generating", process=process, timeout=args.generation_timeout):
                while process.poll() is None:
                    time.sleep(1)
                if process.returncode:
                    raise RuntimeError(f"Generator exited {process.returncode}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    inventory = {}
    with run.operation("checking_generated_files"):
        for table in tables:
            path = args.data_dir / f"{table}.dat"
            inventory[table] = source_inventory(path)
            if inventory[table]["rows"] <= 0:
                raise RuntimeError(f"Generator produced an empty table: {table}")
    version = (args.data_dir / "dbgen_version.dat").read_text()
    if not re.search(rf"-scale\s+{args.scale}(?:\s|\|)", version):
        raise RuntimeError("Generated dbgen_version does not record the requested scale")
    run.manifest.update(generation_complete=True, files=inventory)
    run.save_manifest()


def ledger(conn, name):
    result = conn.execute("SELECT details FROM _dsb_import.steps WHERE name=%s", (name,)).fetchone()
    return result[0] if result else None


def record(conn, name, details):
    conn.execute("INSERT INTO _dsb_import.steps(name, details) VALUES (%s, %s)", (name, Jsonb(details)))


def create_schema(run, tables):
    args = run.args
    with connect(args, "postgres") as admin:
        row = admin.execute("SELECT oid FROM pg_database WHERE datname=%s", (args.database,)).fetchone()
        verify_database_identity(row[0] if row else None, run.manifest, args.resume)
        if row is None:
            statement = sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(args.database), sql.Identifier(args.owner))
            if args.tablespace_dir:
                tablespace = "dsb_" + run.manifest["run_id"].replace("-", "")
                existing = admin.execute(
                    "SELECT oid, pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname=%s",
                    (tablespace,)).fetchone()
                if existing:
                    if (existing[0] != run.manifest.get("tablespace_oid")
                            or existing[1] != str(args.tablespace_dir)):
                        raise RuntimeError("Existing tablespace differs from run identity")
                else:
                    if run.manifest.get("tablespace_oid"):
                        raise RuntimeError("Run tablespace disappeared; refusing to recreate")
                    if any(args.tablespace_dir.iterdir()):
                        raise RuntimeError("Tablespace directory is nonempty; refusing to adopt it")
                    admin.execute(sql.SQL("CREATE TABLESPACE {} OWNER {} LOCATION {}").format(
                        sql.Identifier(tablespace), sql.Identifier(args.owner), sql.Literal(str(args.tablespace_dir))))
                    run.manifest["tablespace_oid"] = admin.execute(
                        "SELECT oid FROM pg_tablespace WHERE spcname=%s", (tablespace,)).fetchone()[0]
                    run.manifest["tablespace_name"] = tablespace
                    run.save_manifest()
                statement += sql.SQL(" TABLESPACE {}").format(sql.Identifier(tablespace))
            admin.execute(statement)
            run.manifest["database_oid"] = admin.execute(
                "SELECT oid FROM pg_database WHERE datname=%s", (args.database,)).fetchone()[0]
            run.save_manifest()
        if args.tablespace_dir:
            actual = admin.execute(
                "SELECT t.oid, pg_tablespace_location(t.oid) FROM pg_database d "
                "JOIN pg_tablespace t ON t.oid=d.dattablespace WHERE d.datname=%s",
                (args.database,)).fetchone()
            if actual != (run.manifest.get("tablespace_oid"), str(args.tablespace_dir)):
                raise RuntimeError("Database tablespace no longer matches the run manifest")
    with connect(args, args.database) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS pg_hint_plan")
    with connect(args, args.database, owner=True) as conn:
        present = conn.execute("SELECT to_regclass('_dsb_import.identity')").fetchone()[0]
        if present is not None:
            if conn.execute("SELECT run_id FROM _dsb_import.identity").fetchall() != [(run.manifest["run_id"],)]:
                raise RuntimeError("Database import identity mismatch")
            return
        with run.operation("creating_schema", conn), conn.transaction():
            conn.execute("CREATE SCHEMA _dsb_import")
            conn.execute("CREATE TABLE _dsb_import.identity(run_id text PRIMARY KEY)")
            conn.execute("INSERT INTO _dsb_import.identity VALUES (%s)", (run.manifest["run_id"],))
            conn.execute("CREATE TABLE _dsb_import.steps(name text PRIMARY KEY, details jsonb NOT NULL)")
            for statement in tables.values():
                conn.execute(statement)


def load_tables(run, tables):
    with connect(run.args, run.args.database, owner=True) as conn:
        for table in tables:
            path = run.args.data_dir / f"{table}.dat"
            expected = run.manifest["files"][table]
            name = "copy:" + table
            recorded = ledger(conn, name)
            if recorded is not None:
                with run.operation("verifying_committed:" + table, conn):
                    source = source_inventory(path)
                    count = conn.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table))).fetchone()[0]
                    verify_loaded_table(recorded, count, source)
                    if source != expected:
                        raise RuntimeError("Source changed from generation manifest")
                run.update(tables_loaded={**run.state.get("tables_loaded", {}), table: expected["rows"]})
                continue
            with run.operation(name, conn):
                with conn.transaction():
                    conn.execute(sql.SQL("LOCK TABLE public.{} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table)))
                    if conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM public.{})").format(sql.Identifier(table))).fetchone()[0]:
                        raise RuntimeError(f"Table {table} has rows without a committed import record")
                    if run.args.resume:
                        conn.execute(sql.SQL("TRUNCATE TABLE public.{}").format(sql.Identifier(table)))
                with conn.transaction():
                    conn.execute(sql.SQL("LOCK TABLE public.{} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table)))
                    if conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM public.{})").format(sql.Identifier(table))).fetchone()[0]:
                        raise RuntimeError(f"Table {table} changed before COPY")
                    digest, rows, total = hashlib.sha256(), 0, 0
                    with conn.cursor() as cursor:
                        statement = sql.SQL("COPY public.{} FROM STDIN WITH (FORMAT csv, DELIMITER '|', NULL '')").format(sql.Identifier(table))
                        with cursor.copy(statement) as copy, path.open("rb") as stream:
                            for block in iter(lambda: stream.read(8 * 1024**2), b""):
                                digest.update(block)
                                rows += block.count(b"\n")
                                total += len(block)
                                copy.write(block)
                        observed = {"bytes": total, "rows": rows, "sha256": digest.hexdigest()}
                        if observed != expected or cursor.rowcount != rows:
                            raise RuntimeError(f"COPY/source row-count mismatch for {table}")
                    record(conn, name, observed)
            run.update(tables_loaded={**run.state.get("tables_loaded", {}), table: expected["rows"]})


def build_indexes(run, steps):
    with connect(run.args, run.args.database, owner=True) as conn:
        for name, statement in steps.items():
            recorded = ledger(conn, "index:" + name)
            if recorded is not None:
                actual = conn.execute(
                    "SELECT indisvalid, indisready FROM pg_index WHERE indexrelid=to_regclass(%s)",
                    ("public." + name,)).fetchone()
                if recorded != {"sql": statement} or actual != (True, True):
                    raise RuntimeError(f"Committed index mismatch: {name}")
                run.update(indexes_built=[*run.state.get("indexes_built", []), name])
                continue
            with run.operation("index:" + name, conn), conn.transaction():
                conn.execute(statement)
                record(conn, "index:" + name, {"sql": statement})
            run.update(indexes_built=[*run.state.get("indexes_built", []), name])


def validate(run, tables, indexes):
    sys.path.insert(0, str(ROOT))
    from genetic_lqo.workload.workloads import get_workload_set

    queries = get_workload_set("DSB").queries
    result = {"database": run.args.database, "scale": run.args.scale, "tables": {},
              "indexes": len(indexes), "queries_explained": 0, "query_errors": {}}
    with connect(run.args, run.args.database, owner=True) as conn:
        for table in tables:
            with run.operation("analyze:" + table, conn):
                conn.execute(sql.SQL("ANALYZE public.{}").format(sql.Identifier(table)))
            with run.operation("validate:" + table, conn):
                count = conn.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table))).fetchone()[0]
                if count != run.manifest["files"][table]["rows"] or count <= 0:
                    raise RuntimeError(f"Final row count mismatch: {table}")
                result["tables"][table] = {"rows": count}
        version = conn.execute("SELECT dv_version, dv_cmdline_args FROM public.dbgen_version").fetchall()
        if len(version) != 1 or not re.search(rf"-scale\s+{run.args.scale}(?:\s|$)", version[0][1]):
            raise RuntimeError("Database scale metadata mismatch")
        result["dbgen_version"] = version
        for name in indexes:
            actual = conn.execute("SELECT indisvalid, indisready FROM pg_index WHERE indexrelid=to_regclass(%s)", ("public." + name,)).fetchone()
            if actual != (True, True):
                raise RuntimeError(f"Final index missing or invalid: {name}")
        result["hint_enabled"] = conn.execute("SHOW pg_hint_plan.enable_hint").fetchone()[0]
        conn.execute("SET statement_timeout = '60s'")
        conn.execute("SET jit=off")
        conn.execute("SET geqo=off")
        for name, query in queries.items():
            with run.operation("explain:" + name, conn):
                try:
                    conn.execute("EXPLAIN (FORMAT JSON) " + query.query_sql).fetchone()
                    result["queries_explained"] += 1
                except psycopg.Error as exc:
                    result["query_errors"][name] = str(exc)
        result["size_bytes"] = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    result["valid"] = (len(tables) == 25 and len(queries) == 90 and result["queries_explained"] == 90
                       and not result["query_errors"] and result["hint_enabled"] == "on")
    result["finished_at_unix"] = time.time()
    atomic_json(run.args.out / "validation.json", result)
    if not result["valid"]:
        raise RuntimeError("Final validation failed; see validation.json")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="dsb_sf50")
    parser.add_argument("--scale", type=int, default=50)
    parser.add_argument("--out", type=Path, default=ROOT / "runs/dsb_sf50_import")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--generator-dir", type=Path, required=True)
    parser.add_argument("--schema-file", type=Path, default=ROOT / "genetic_lqo/workload/dsb/schema.sql")
    parser.add_argument("--indexes-file", type=Path, default=ROOT / "genetic_lqo/workload/dsb/indexes.sql")
    parser.add_argument("--tablespace-dir", type=Path,
                        help="Optional new dedicated tablespace; WAL remains in the server data directory")
    parser.add_argument("--admin-user", default="postgres")
    parser.add_argument("--owner", default="so")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15432)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--raw-min-free-gib", type=float, default=50)
    parser.add_argument("--db-min-free-gib", type=float, default=80)
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--operation-timeout", type=int, default=7200)
    parser.add_argument("--generation-timeout", type=int, default=21600)
    args = parser.parse_args(argv)
    for name in ("out", "data_dir", "generator_dir", "schema_file", "indexes_file"):
        setattr(args, name, getattr(args, name).resolve())
    if args.tablespace_dir:
        args.tablespace_dir = args.tablespace_dir.resolve()
    if min(args.scale, args.sample_seconds, args.operation_timeout, args.generation_timeout,
           args.raw_min_free_gib, args.db_min_free_gib) <= 0:
        parser.error("Scale, deadlines, sample interval and free-space reserves must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    tables, primary_keys = schema_steps(args.schema_file.read_text())
    indexes = index_steps(args.indexes_file.read_text())
    if set(primary_keys) & set(indexes):
        raise RuntimeError("Custom indexes conflict with primary key names")
    identity = {key: str(getattr(args, key)) for key in
                ("database", "scale", "data_dir", "generator_dir", "schema_file", "indexes_file", "tablespace_dir", "host", "port", "owner")}
    identity["source_hashes"] = {str(path): sha256(path) for path in
                                 (args.schema_file, args.indexes_file, args.generator_dir / "dsdgen",
                                  args.generator_dir / "tpcds.idx", ROOT / "genetic_lqo/workload/dsb/dsb.zip")}
    if args.resume:
        manifest = json.loads((args.out / "manifest.json").read_text())
        if manifest["identity"] != identity:
            raise RuntimeError("Resume arguments or input hashes differ from the run manifest")
    else:
        if args.out.exists() and any(args.out.iterdir()):
            raise RuntimeError("Output directory is nonempty; use a new directory or verified --resume")
        if args.data_dir.exists() and any(args.data_dir.iterdir()):
            raise RuntimeError("Data directory is nonempty; refusing to overwrite generated data")
        if args.tablespace_dir and args.tablespace_dir.exists() and any(args.tablespace_dir.iterdir()):
            raise RuntimeError("Tablespace directory is nonempty; refusing to adopt existing storage")
        manifest = {"run_id": str(uuid.uuid4()), "identity": identity, "started_at_unix": time.time()}
    with connect(args, "postgres") as admin:
        row = admin.execute("SELECT oid FROM pg_database WHERE datname=%s", (args.database,)).fetchone()
        verify_database_identity(row[0] if row else None, manifest, args.resume)
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (args.owner,)).fetchone():
            raise RuntimeError(f"Owner role does not exist: {args.owner}")
        data_directory = Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.tablespace_dir:
        args.tablespace_dir.mkdir(parents=True, exist_ok=True)
    with (args.out / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (args.data_dir.parent / ("." + args.data_dir.name + ".import.lock")).open("a") as data_lock:
            fcntl.flock(data_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = {"run_id": manifest["run_id"], "database": args.database,
                     "pid": os.getpid(), "status": "starting", "started_at_unix": time.time()}
            run = Run(args, manifest, state, data_directory)
            run.save_manifest()
            (args.out / "pid").write_text(str(os.getpid()) + "\n")
            (args.out / "exit_code").unlink(missing_ok=True)
            run.update()
            exit_code = 1
            try:
                generate(run, tables)
                create_schema(run, tables)
                load_tables(run, tables)
                build_indexes(run, {**primary_keys, **indexes})
                validate(run, tables, {**primary_keys, **indexes})
                run.update(status="complete", stage="complete", finished_at_unix=time.time())
                exit_code = 0
            except BaseException as exc:
                run.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at_unix=time.time())
                raise
            finally:
                (args.out / "exit_code").write_text(str(exit_code) + "\n")
    return exit_code


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
