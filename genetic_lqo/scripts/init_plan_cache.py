"""Create (if asked) and initialize the per-database plan cache ``<business_db>_lqo_cache``."""

from __future__ import annotations

import argparse

from genetic_lqo.core.config import load_config
from genetic_lqo.execution.executor import PostgresConnectionConfig
from genetic_lqo.execution.plan_cache import PostgresPlanCache, create_cache_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="", help="TOML overlay on the method profile.")
    parser.add_argument("--db-name", default="", help="Business database name (default: [database].dbname).")
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--maintenance-db-name", default="postgres")
    args = parser.parse_args()

    config = load_config(args.config or None)
    connection = PostgresConnectionConfig.from_config(config.database, args.db_name or config.database.dbname)
    if args.create_database:
        name = create_cache_database(business_connection=connection, config=config.cache,
                                     maintenance_dbname=args.maintenance_db_name)
        print(f"cache_db={name}")
    cache = PostgresPlanCache.open(business_connection=connection, config=config.cache)
    print(f"initialized cache_db={cache.cache_connection.dbname} schema={config.cache.schema}")
    cache.close()


if __name__ == "__main__":
    main()
