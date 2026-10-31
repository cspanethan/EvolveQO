
import pytest
from fake_postgres import MemoryCacheBackend

from genetic_lqo.core.profiles import load_profile
from genetic_lqo.core.types import ExecutionResult, PlanCacheConfig
from genetic_lqo.execution.plan_cache import resolve_cache_dbname
from genetic_lqo.execution.runtime_cache import PlanCacheUnavailable, PlanRuntimeCache


def test_profiles_require_cache_and_business_databases_cannot_share_one():
    for profile in ('method', 'smoke'):
        assert load_profile(profile).config.cache.enabled
    assert resolve_cache_dbname('imdb', PlanCacheConfig()) == 'imdb_lqo_cache'
    assert resolve_cache_dbname('dsb_sf50', PlanCacheConfig()) == 'dsb_sf50_lqo_cache'
    with pytest.raises(ValueError, match='own plan cache'):
        resolve_cache_dbname('dsb_sf50', PlanCacheConfig(dbname='imdb_lqo_cache'))
    config = load_profile('method').config
    config.cache.enabled = False
    with pytest.raises(PlanCacheUnavailable):
        PlanRuntimeCache.open(None, config, {}, sql='SELECT 1', query_name='q')


def test_only_finite_successful_runtimes_are_cached():
    cache = PlanRuntimeCache(MemoryCacheBackend(), 'SELECT 1', 'q')
    cache.record_runtime(ExecutionResult('SELECT 1', (), False, True, plan_digest='p', timed_out=True), ())
    assert cache.lookup('p') is None
    cache.record_runtime(ExecutionResult('SELECT 1', (), True, True, plan_digest='p', execution_time_seconds=.2), ())
    assert cache.lookup('p').median_seconds == .2
