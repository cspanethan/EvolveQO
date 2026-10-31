import os

import pytest

from genetic_lqo.execution.executor import PostgresConnectionConfig, PostgresGeneticExecutor
from genetic_lqo.planning.hint_scope import FIXED_PLANNER_SETTINGS

pytestmark = pytest.mark.skipif(
    os.getenv("GENETIC_LQO_RUN_PG_TESTS") != "1",
    reason="requires configured JOB PostgreSQL backend",
)

SQL = """SELECT count(*) FROM title a
JOIN movie_info b ON a.id=b.movie_id
JOIN info_type c ON b.info_type_id=c.id
JOIN movie_keyword d ON a.id=d.movie_id
WHERE a.id<100 AND d.movie_id<100"""


@pytest.fixture
def executor():
    return PostgresGeneticExecutor(PostgresConnectionConfig.from_env("imdb"), planner_settings=FIXED_PLANNER_SETTINGS)


def planned(executor, sql, hints):
    result = executor.explain(sql, hints, analyze=False, timeout_seconds=2.0)
    assert result.success, result.error
    assert result.hint_diagnostics.status == "all_used", result.hint_diagnostics
    return result


def test_joint_bushy_recipe_realizes_both_input_directions(executor):
    for left, right in ((("b", "c"), ("a", "d")), (("a", "d"), ("b", "c"))):
        result = planned(
            executor,
            SQL,
            [
                f"Leading((({' '.join(left)}) ({' '.join(right)})))",
                "HashJoin(b c)",
                "MergeJoin(a d)",
                "NestLoop(a b c d)",
                "SeqScan(b)",
                "IndexScan(a)",
                "IndexOnlyScan(c)",
                "BitmapScan(d)",
                "Rows(b c #10)",
                "Rows(a d +10)",
            ],
        )
        joins = {j.aliases: j for j in result.plan_summary.join_methods}
        assert joins[("a", "b", "c", "d")].child_aliases == (left, right)
        assert joins[("b", "c")].node_type == "Hash Join"
        assert joins[("a", "d")].node_type == "Merge Join"
        assert joins[("a", "b", "c", "d")].node_type == "Nested Loop"
        scans = {s["alias"]: s["node_type"] for s in result.plan_summary.table_accesses if s["alias"]}
        assert scans == {"a": "Index Scan", "b": "Seq Scan", "c": "Index Only Scan", "d": "Bitmap Heap Scan"}


def test_rows_correction_forms_reach_the_requested_join(executor):
    sql = "SELECT count(*) FROM title a JOIN movie_info b ON a.id=b.movie_id WHERE a.id<100"
    leading = "Leading((a b))"
    baseline = planned(executor, sql, [leading])

    def join_rows(result):
        return result.explain_json[0]["Plan"]["Plans"][0]["Plan Rows"]

    estimate = join_rows(baseline)
    for correction, expected in (
        ("#17", 17),
        ("*2", estimate * 2),
        ("+10", estimate + 10),
        ("-10", max(1, estimate - 10)),
    ):
        result = planned(executor, sql, [leading, f"Rows(a b {correction})"])
        assert join_rows(result) == expected


def test_index_selection_and_tidscan_have_native_binding(executor):
    sql = "SELECT a.id FROM title a WHERE a.id<100"
    catalog = executor.fetch_indexes(["title"])["title"]
    names = [i.index_name for i in catalog if i.index_name != "title_pkey"]
    assert names, "Fixture requires the JOB title indexes"
    for method in ("IndexScan", "IndexOnlyScan", "BitmapScan"):
        result = planned(executor, sql, [f"{method}(a title_pkey {names[0]})"])
        assert any(s["index_name"] == "title_pkey" for s in result.plan_summary.table_accesses)
        result = planned(executor, sql, [f"{method}Regexp(a ^title_pkey$)"])
        assert any(s["index_name"] == "title_pkey" for s in result.plan_summary.table_accesses)
    disabled = planned(executor, sql, ["DisableIndex(a title_pkey)"])
    assert all(s["index_name"] != "title_pkey" for s in disabled.plan_summary.table_accesses)
    tid = planned(executor, "SELECT a.id FROM title a WHERE a.ctid='(0,1)'::tid", ["TidScan(a)"])
    assert tid.plan_summary.table_accesses[0]["node_type"] == "Tid Scan"
