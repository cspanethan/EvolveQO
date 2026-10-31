from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, replace
from pathlib import Path

from genetic_lqo.core.profiles import PROVIDER_API_KEY_ENV, config_from_payload, load_profile
from genetic_lqo.core.types import GeneticSearchConfig


def load_config(path: str | Path | None = None, *, profile: str = "method") -> GeneticSearchConfig:
    base = asdict(load_profile(profile).config)
    if path is not None:
        overlay = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        if "api_key" in overlay.get("llm", {}):
            raise ValueError("llm.api_key must come from the provider's environment variable, not a config file")
        llm = overlay.get("llm", {})
        if llm.get("provider", base["llm"]["provider"]) != base["llm"]["provider"] and "reasoning_effort" not in llm:
            base["llm"]["reasoning_effort"] = None
        for section, values in overlay.items():
            base.setdefault(section, {}).update(values)
    base["llm"].pop("api_key", None)
    return validate_config(apply_environment(config_from_payload(base)))


def apply_environment(config: GeneticSearchConfig) -> GeneticSearchConfig:
    db, llm = config.database, config.llm
    port = os.getenv("DB_PORT")
    database = replace(db, host=os.getenv("DB_HOST") or db.host, port=int(port) if port else db.port,
                       dbname=os.getenv("DB_NAME") or db.dbname, user=os.getenv("DB_USER") or db.user,
                       password=os.getenv("DB_PASSWORD") or db.password)
    llm = replace(llm, api_key=load_llm_api_key_from_environment(llm.provider),
                  base_url=os.getenv("GENETIC_LQO_LLM_BASE_URL") or llm.base_url,
                  model=os.getenv("GENETIC_LQO_LLM_MODEL") or llm.model,
                  reasoning_effort=os.getenv("GENETIC_LQO_LLM_REASONING_EFFORT") or llm.reasoning_effort)
    return replace(config, database=database, llm=llm)


def load_llm_api_key_from_environment(provider: str) -> str:
    key_env = PROVIDER_API_KEY_ENV.get(str(provider).strip().lower())
    return os.getenv(key_env, "") if key_env else ""


def validate_config(config: GeneticSearchConfig) -> GeneticSearchConfig:
    search = config.search
    if search.method != "hint_search":
        raise ValueError("search.method must be hint_search")
    if not search.cutoff_factor > 1:
        raise ValueError("search.cutoff_factor (kappa) must be greater than 1")
    if not search.time_budget_seconds > 0:
        raise ValueError("search.time_budget_seconds must be positive")
    if search.native_seconds is not None and not search.native_seconds > 0:
        raise ValueError("search.native_seconds must be positive")
    if not search.programs or set(search.programs) - {"card", "hint"}:
        raise ValueError("search.programs must be a nonempty subset of card, hint")
    if config.llm.provider not in PROVIDER_API_KEY_ENV:
        raise ValueError(f"Unsupported LLM provider: {config.llm.provider!r}")
    return config
