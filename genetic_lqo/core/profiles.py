from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from genetic_lqo.core.types import (
    DatabaseConfig,
    GeneticSearchConfig,
    LLMConfig,
    OutputConfig,
    PlanCacheConfig,
    SearchConfig,
)

PROFILE_ROOT = Path(__file__).resolve().parents[1] / "config" / "profiles"
ACTIVE_LLM_CONFIG_PATH = PROFILE_ROOT.parent / "llm.toml"
AVAILABLE_PROFILES = ("smoke", "method")
METHOD_PROFILES = ("method",)
PROVIDER_API_KEY_ENV = {"dashscope": "DASHSCOPE_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
WORKLOAD_SETS = ("JOB", "CEB_3K", "DSB", "STACK_SO_PAST", "STACK_SO_FUTURE", "STACK_SO_SHIFTED")
SECTIONS = {
    "database": DatabaseConfig,
    "llm": LLMConfig,
    "search": SearchConfig,
    "cache": PlanCacheConfig,
    "output": OutputConfig,
}


@dataclass(frozen=True)
class GeneticLQOProfile:
    name: str
    path: Path
    config: GeneticSearchConfig


def load_profile(name: str) -> GeneticLQOProfile:
    if name not in AVAILABLE_PROFILES:
        raise ValueError(f"Unknown genetic_lqo profile {name!r}; available: {', '.join(AVAILABLE_PROFILES)}")
    path = PROFILE_ROOT / f"{name}.toml"
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("llm", {}).update({k: v for k, v in active_llm_config().items() if k != "api_key_env"})
    return GeneticLQOProfile(name, path, config_from_payload(payload))


def active_llm_config() -> dict[str, str]:
    raw = tomllib.loads(ACTIVE_LLM_CONFIG_PATH.read_text(encoding="utf-8"))
    llm = dict(raw["llm"])
    provider = llm["provider"]
    if PROVIDER_API_KEY_ENV.get(provider) != llm.get("api_key_env"):
        raise ValueError(f"Provider {provider!r} requires api_key_env={PROVIDER_API_KEY_ENV.get(provider)!r}")
    return llm


def primary_method_profile_for_workload(workload_set: object, method: str = "hint_search") -> str:
    if workload_set not in WORKLOAD_SETS:
        raise ValueError(f"No method profile for workload {workload_set!r}; available: {', '.join(WORKLOAD_SETS)}")
    if method != "hint_search":
        raise ValueError(f"Unsupported search method: {method!r}")
    return "method"


def config_from_payload(payload: dict[str, Any]) -> GeneticSearchConfig:
    unknown = sorted(set(payload) - set(SECTIONS))
    if unknown:
        raise ValueError(f"Unknown config sections: {', '.join(unknown)}")
    if "api_key" in payload.get("llm", {}):
        raise ValueError("llm.api_key must come from the provider's environment variable, not a config file")
    sections = {}
    for name, cls in SECTIONS.items():
        section = payload.get(name, {})
        valid = {field.name for field in fields(cls)}
        bad = sorted(set(section) - valid)
        if bad:
            raise ValueError(f"Unknown fields in [{name}]: {', '.join(bad)}")
        sections[name] = cls(**section)
    return GeneticSearchConfig(**sections)
