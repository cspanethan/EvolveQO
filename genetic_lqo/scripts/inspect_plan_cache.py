"""List the measured plans cached for one workload query."""
import argparse
import json

from genetic_lqo.core.profiles import load_profile
from genetic_lqo.execution.executor import PostgresConnectionConfig
from genetic_lqo.execution.plan_cache import PostgresPlanCache
from genetic_lqo.search.optimizer import PLANNER_SETTINGS
from genetic_lqo.workload.workloads import get_workload_set, infer_implicit_workload_set


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', required=True)
    parser.add_argument('--workload-set')
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    workload = get_workload_set(args.workload_set or infer_implicit_workload_set(args.query))
    query = workload.queries[args.query]
    config = load_profile('method').config
    cache = PostgresPlanCache.open(
        business_connection=PostgresConnectionConfig.from_config(config.database, query.db), config=config.cache,
        planner_settings={**PLANNER_SETTINGS, 'measurement_metric': 'postgres_execution_time_seconds'})
    try:
        print(json.dumps({'query': args.query, 'cache_database': cache.cache_connection.dbname,
                          'plans': cache.list_runtime_records(query.query_sql, limit=args.limit)},
                         indent=2, default=str))
    finally:
        cache.close()


if __name__ == '__main__':
    main()
