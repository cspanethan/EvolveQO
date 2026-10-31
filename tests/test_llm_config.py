import io
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from genetic_lqo.core.config import load_config
from genetic_lqo.core.profiles import active_llm_config, load_profile
from genetic_lqo.guidance.llm import LLMClient, _loopback_endpoint, _streaming_chat_completion
from genetic_lqo.guidance.prompt_exchange import call_provider


def test_default_endpoint_and_credentials_come_only_from_the_provider_variable():
    with patch.dict('os.environ', {'OPENAI_API_KEY': 'openai-key', 'DEEPSEEK_API_KEY': 'deepseek-key'}, clear=True):
        default = load_config()
        deepseek = load_config('genetic_lqo/config/deepseek.toml')
    assert default.llm.model == active_llm_config()['model'] == 'gpt-5.6-sol'
    assert default.llm.provider == 'openai' and default.llm.api_key == 'openai-key'
    assert default.llm.reasoning_effort == 'medium' and default.llm.temperature == 0.7
    assert deepseek.llm.provider == 'deepseek' and deepseek.llm.api_key == 'deepseek-key'
    assert deepseek.llm.reasoning_effort is None
    assert deepseek.search == default.search
    assert load_profile('method').config.llm.api_key == ''
    with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'unrelated'}, clear=True):
        assert load_config().llm.api_key == ''


def test_config_files_cannot_carry_secrets_or_unknown_fields(tmp_path):
    secret = tmp_path / 'secret.toml'
    secret.write_text('[llm]\napi_key = "x"\n')
    with pytest.raises(ValueError, match='api_key'):
        load_config(secret)
    unknown = tmp_path / 'unknown.toml'
    unknown.write_text('[search]\npopulation_size = 12\n')
    with pytest.raises(ValueError, match='population_size'):
        load_config(unknown)
    bad_kappa = tmp_path / 'kappa.toml'
    bad_kappa.write_text('[search]\ncutoff_factor = 1.0\n')
    with pytest.raises(ValueError, match='kappa'):
        load_config(bad_kappa)


def test_provider_payloads():
    config = load_config()
    openai = LLMClient(config)._payload([{'role': 'user', 'content': 'JSON'}])
    assert openai['reasoning_effort'] == 'medium' and openai['max_completion_tokens'] == 4096
    assert openai['stream_options'] == {'include_usage': True}
    assert openai['response_format'] == {'type': 'json_object'}
    deepseek = LLMClient(replace(config, llm=replace(config.llm, provider='deepseek')))._payload([])
    assert deepseek['thinking'] == {'type': 'disabled'} and 'max_tokens' in deepseek
    dashscope = LLMClient(replace(config, llm=replace(config.llm, provider='dashscope')))._payload([])
    assert dashscope['enable_thinking'] is False


def test_streaming_response_is_assembled_with_usage():
    chunks = [{'id': 'r', 'model': 'm', 'created': 1, 'object': 'chat.completion.chunk',
               'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '{"seeds":'}}]},
              {'id': 'r', 'model': 'm', 'created': 1, 'object': 'chat.completion.chunk',
               'choices': [{'index': 0, 'delta': {'content': '[]}'}, 'finish_reason': 'stop'}],
               'usage': {'total_tokens': 9}}]
    stream = io.BytesIO(b''.join(b'data: ' + json.dumps(c).encode() + b'\n\n' for c in chunks) + b'data: [DONE]\n\n')
    response = _streaming_chat_completion(stream)
    assert response['choices'][0]['message']['content'] == '{"seeds":[]}'
    assert response['choices'][0]['finish_reason'] == 'stop' and response['usage'] == {'total_tokens': 9}


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:8318/v1', 'http://localhost:8318/v1', 'http://[::1]:8318/v1'])
def test_loopback_endpoints_bypass_proxies(endpoint):
    assert _loopback_endpoint(endpoint)
    assert not _loopback_endpoint('https://localhost.example/v1')


def test_call_provider_records_request_response_and_failure(tmp_path, monkeypatch):
    config = load_profile('method').config
    config = replace(config, llm=replace(config.llm, api_key='k', stream=False, base_url='https://api.example/v1'))
    assert config.llm.provider == 'openai'
    envelope = {'choices': [{'message': {'role': 'assistant', 'content': '{"seeds": []}'}}]}
    monkeypatch.setattr('urllib.request.urlopen', lambda request, **_: io.BytesIO(json.dumps(envelope).encode()))
    messages = [{'role': 'user', 'content': 'hi'}]
    assert call_provider(config, messages, tmp_path / 'ok') == '{"seeds": []}'
    assert json.loads((tmp_path / 'ok' / 'request.json').read_text()) == messages
    assert (tmp_path / 'ok' / 'response.json').is_file()

    def fail(request, **_):
        raise OSError('connection refused')
    monkeypatch.setattr('urllib.request.urlopen', fail)
    with pytest.raises(RuntimeError):
        call_provider(replace(config, llm=replace(config.llm, retry_max_attempts=1)), messages, tmp_path / 'bad')
    assert 'connection refused' in (tmp_path / 'bad' / 'failure.json').read_text()
