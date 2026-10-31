from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from genetic_lqo.core.types import GeneticSearchConfig


class LLMRequestError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, config: GeneticSearchConfig) -> None:
        self.config = config

    def complete(self, messages: list[dict[str, str]], schema: dict | None = None) -> tuple[str, dict[str, Any]]:
        response = self._post_chat_completions(self._payload(messages, schema))
        try:
            return str(response["choices"][0]["message"]["content"] or ""), response
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMRequestError(f"Malformed chat completion: {str(response)[:500]}") from exc

    def _payload(self, messages: list[dict[str, str]], schema: dict | None = None) -> dict[str, Any]:
        llm = self.config.llm
        payload: dict[str, Any] = {"model": llm.model, "messages": messages,
                                   "response_format": {"type": "json_object"}}
        if schema is not None and llm.provider == "openai":
            payload["response_format"] = {"type": "json_schema", "json_schema": schema}
        if llm.provider == "openai":
            payload["max_completion_tokens"] = llm.max_tokens
            if llm.reasoning_effort:
                payload["reasoning_effort"] = llm.reasoning_effort
        else:
            payload["max_tokens"] = llm.max_tokens
        if llm.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        elif llm.provider == "dashscope":
            payload["enable_thinking"] = False
        if llm.temperature is not None:
            payload["temperature"] = llm.temperature
        if llm.stream:
            payload["stream"] = True
            if llm.provider == "openai":
                payload["stream_options"] = {"include_usage": True}
        return payload

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        llm = self.config.llm
        if not llm.api_key:
            raise LLMRequestError(f"No API key for provider {llm.provider!r} in the environment")
        endpoint = llm.base_url.rstrip("/") + "/chat/completions"
        attempts = max(1, llm.retry_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return self._post_chat_completions_once(endpoint, payload)[0]
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
                error = LLMRequestError(f"LLM HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:1000]}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                retryable, error = True, LLMRequestError(f"LLM request failed: {exc}")
            if not retryable or attempt == attempts:
                raise error
            time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        raise AssertionError("unreachable")

    def _post_chat_completions_once(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        llm = self.config.llm
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"})
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                  if _loopback_endpoint(endpoint) else urllib.request.urlopen)
        with opener(request, timeout=llm.timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            if llm.stream:
                return _streaming_chat_completion(response), status
            return json.loads(response.read().decode("utf-8")), status


def _loopback_endpoint(endpoint: str) -> bool:
    hostname = urlsplit(endpoint).hostname or ""
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _streaming_chat_completion(lines: Iterable[bytes], *, provider: str = "") -> dict[str, Any]:
    content: list[str] = []
    reasoning: list[str] = []
    response: dict[str, Any] = {}
    finish_reason = None
    usage = None
    done = False
    for raw_line in lines:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        response.setdefault("id", chunk.get("id"))
        response.setdefault("model", chunk.get("model"))
        response.setdefault("created", chunk.get("created"))
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
            finish_reason = choice.get("finish_reason") or finish_reason
    if not done:
        raise LLMRequestError("LLM stream ended before data: [DONE]")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if any(reasoning):
        message["reasoning_content"] = "".join(reasoning)
    return {**response, "object": "chat.completion", "usage": usage or {},
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
