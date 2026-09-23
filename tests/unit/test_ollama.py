import json

import httpx
import pytest

from app.core.config import Settings
from app.services.local_llm import LocalLLMError
from app.services.ollama import OllamaClient

LOCAL_MODEL = {"model_info": {"test.context_length": 32768}, "capabilities": ["completion"]}
REPLY = {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": '{"action_items": []}'}}


def client(handler, **kwargs):
    return OllamaClient(Settings(_env_file=None, ollama_model="local-test", **kwargs), transport=httpx.MockTransport(handler))


def generate(adapter):
    return adapter.generate_json(system="Rules", user="PRIVATE_TRANSCRIPT", schema={"type": "object"})


def test_local_preflight_schema_and_transport(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid")
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            assert body == {"model": "local-test"}
            assert "PRIVATE_TRANSCRIPT" not in request.content.decode()
            return httpx.Response(200, json=LOCAL_MODEL)
        assert request.url.path == "/api/chat"
        assert body["format"] == {"type": "object"}
        assert not body["stream"] and not body["truncate"] and not body["shift"]
        assert "tools" not in body
        assert body["options"]["temperature"] == 0
        return httpx.Response(200, json=REPLY)
    assert json.loads(generate(client(handler, ollama_base_url="http://localhost:11434"))) == {"action_items": []}
    assert len(requests) == 2


@pytest.mark.parametrize("metadata", [{"remote_host": "https://ollama.com", **LOCAL_MODEL},
                                      {"remote_model": "remote", **LOCAL_MODEL}, {"model_info": {}}, {"model_info": [1]}, {}])
def test_cloud_alias_or_unverified_model_never_receives_text(metadata):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=metadata)
    with pytest.raises(LocalLLMError) as error:
        generate(client(handler))
    assert error.value.code == "llm_local_model_unverified"
    assert len(calls) == 1 and calls[0].url.path == "/api/show"


@pytest.mark.parametrize("model", ["", "some-model:cloud", "https://external.invalid/model"])
def test_invalid_model_never_calls_server(model):
    adapter = OllamaClient(Settings(_env_file=None, ollama_model=model), transport=httpx.MockTransport(lambda request: pytest.fail("Must not connect")))
    with pytest.raises(LocalLLMError):
        generate(adapter)


@pytest.mark.parametrize("status,code", [(302, "llm_request_failed"), (404, "llm_model_missing"), (500, "llm_request_failed")])
def test_http_errors_and_no_redirect(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://external.invalid"}, text="private error")
    with pytest.raises(LocalLLMError) as error:
        generate(client(handler))
    assert error.value.code == code
    assert "private error" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "llm_timeout"), (httpx.ConnectError, "llm_unavailable")])
def test_connection_failures(exception, code):
    def handler(request):
        raise exception("private error")
    with pytest.raises(LocalLLMError) as error:
        generate(client(handler))
    assert error.value.code == code


@pytest.mark.parametrize("reply", [{**REPLY, "done": False}, {**REPLY, "done_reason": "length"},
                                  {**REPLY, "message": {"content": "{}", "tool_calls": [1]}}, {**REPLY, "message": None}])
def test_incomplete_or_tool_response_is_not_a_result(reply):
    def handler(request):
        return httpx.Response(200, json=LOCAL_MODEL if request.url.path == "/api/show" else reply)
    with pytest.raises(LocalLLMError) as error:
        generate(client(handler))
    assert error.value.code == "llm_incomplete_response"


def test_context_guard_does_not_truncate_or_send_transcript():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model_info": {"test.context_length": 128}})
    with pytest.raises(LocalLLMError) as error:
        generate(client(handler))
    assert error.value.status == 413
    assert len(calls) == 1
