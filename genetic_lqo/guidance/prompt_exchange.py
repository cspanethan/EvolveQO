from __future__ import annotations

from functools import partial
from pathlib import Path

from genetic_lqo.core.artifacts import write_json
from genetic_lqo.guidance.llm import LLMClient


def save_json(path, value):
    write_json(path, value)


def call_provider(config, messages, directory, *, schema=None):
    directory = Path(directory)
    save_json(directory / "request.json", messages)
    try:
        content, response = LLMClient(config).complete(messages, schema)
    except Exception as exc:
        save_json(directory / "failure.json", {"error_type": type(exc).__name__, "error": str(exc)})
        raise
    save_json(directory / "response.json", response)
    return content


class RecordedProposer:

    def __init__(self, config):
        self.config = config

    def for_call(self, directory):
        return partial(self, directory=directory)

    def __call__(self, messages, *, directory, schema=None):
        return call_provider(self.config, messages, directory, schema=schema)
