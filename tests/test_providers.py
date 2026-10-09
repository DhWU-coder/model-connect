"""验证真实请求构造、不同 provider 的分页及检测结论。"""

import asyncio
import json

import httpx
import pytest

from model_connect.providers import Adapter, ProviderError, model_info
from model_connect.schemas import Connection, ModelFilter, ProbeRequest


@pytest.mark.parametrize(
    ("provider", "protocol", "path", "body", "response"),
    [
        (
            "openai",
            "responses",
            "/v1/responses",
            {"input": "只回复hi", "max_output_tokens": 64, "store": False},
            {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
                "status": "completed",
                "model": "actual-model",
            },
        ),
        (
            "openai",
            "chat",
            "/v1/chat/completions",
            {"max_completion_tokens": 64},
            {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
        ),
        (
            "openai_compatible",
            "chat",
            "/v1/chat/completions",
            {"max_tokens": 64},
            {"choices": [{"message": {"content": "hi"}}]},
        ),
        (
            "anthropic",
            "messages",
            "/v1/messages",
            {"max_tokens": 64},
            {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"},
        ),
        (
            "google",
            "generateContent",
            "/v1/models/demo:generateContent",
            {"generationConfig": {"maxOutputTokens": 64}},
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "secret thought", "thought": True}, {"text": "hi"}]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        ),
    ],
)
async def test_native_probe(provider, protocol, path, body, response):
    def handler(request):
        assert request.url.path == path
        payload = json.loads(request.content)
        for key, value in body.items():
            assert payload[key] == value
        if provider == "google":
            assert request.headers["x-goog-api-key"] == "secret-key"
            assert payload["contents"][0]["parts"][0]["text"] == "只回复hi"
        elif provider == "anthropic":
            assert request.headers["x-api-key"] == "secret-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
        else:
            assert request.headers["authorization"] == "Bearer secret-key"
            assert payload["model"] == "demo"
        return httpx.Response(200, json=response)

    config = Connection(provider=provider, base_url="http://provider.test/v1", api_key="secret-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Adapter(config, client).probe(
            "demo", protocol, ProbeRequest(connection=config, models=["demo"])
        )
    assert result.status == "success"
    assert result.text == "hi"
    assert result.attempts == 1


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_pagination(provider):
    calls = []

    def handler(request):
        calls.append(request)
        if provider == "anthropic":
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"}
                )
            assert request.url.params["after_id"] == "first"
            return httpx.Response(200, json={"data": [{"id": "second"}], "has_more": False})
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/first", "supportedGenerationMethods": ["generateContent"]}
                    ],
                    "nextPageToken": "next",
                },
            )
        assert request.url.params["pageToken"] == "next"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/second", "supportedGenerationMethods": ["embedContent"]}
                ]
            },
        )

    config = Connection(provider=provider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await Adapter(config, client).list_models()
    assert [model.id for model in models] == ["first", "second"]
    assert len(calls) == 2
    if provider == "google":
        assert models[1].supported is False


async def test_repeated_cursor_rejected():
    config = Connection(provider="google")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"models": [], "nextPageToken": "same"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderError, match="重复"):
            await Adapter(config, client).list_models()


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (401, {"message": "bad key"}, "authentication_failed"),
        (403, {"message": "no permission"}, "permission_denied"),
        (404, {"message": "model missing"}, "not_found"),
        (429, {"message": "slow down"}, "rate_limited"),
        (429, {"code": "insufficient_quota", "message": "no credits"}, "quota_exceeded"),
        (500, {"message": "server failed"}, "server_error"),
        (400, {"message": "unsupported model"}, "unsupported_request"),
    ],
)
async def test_failure_classification(status, error, expected):
    config = Connection(provider="openai", api_key="secret-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": error}))
    ) as client:
        result = await Adapter(config, client).probe(
            "demo", "chat", ProbeRequest(connection=config, models=["demo"])
        )
    assert result.status == "failed"
    assert result.error_code == expected
    assert result.http_status == status


@pytest.mark.parametrize(
    ("protocol", "response", "expected"),
    [
        (
            "chat",
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            "empty_response",
        ),
        (
            "responses",
            {
                "status": "incomplete",
                "output": [{"type": "reasoning", "summary": []}],
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "empty_response",
        ),
        (
            "responses",
            {
                "output": [
                    {"type": "message", "content": [{"type": "refusal", "refusal": "refused"}]}
                ]
            },
            "refused",
        ),
        ("chat", {"choices": [{"message": {"content": "hi", "refusal": "refused"}}]}, "refused"),
        ("generateContent", {"promptFeedback": {"blockReason": "SAFETY"}}, "refused"),
        (
            "generateContent",
            {"candidates": [{"content": {"parts": [{"text": "only thinking", "thought": True}]}}]},
            "empty_response",
        ),
        ("chat", {"choices": []}, "invalid_response"),
    ],
)
async def test_http_200_is_not_sufficient(protocol, response, expected):
    config = Connection(provider="openai")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        result = await Adapter(config, client).probe(
            "demo", protocol, ProbeRequest(connection=config, models=["demo"])
        )
    assert result.error_code == expected
    assert result.status == "failed"


async def test_token_fallback_and_secret_redaction():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "max_tokens unsupported, use max_completion_tokens"}},
            )
        assert payload["max_completion_tokens"] == 64
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi secret-key header-secret"}}]}
        )

    config = Connection(
        provider="openai_compatible",
        base_url="http://test",
        api_key="secret-key",
        headers={"X-Custom": "header-secret"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Adapter(config, client).probe(
            "claude-anything", "chat", ProbeRequest(connection=config, models=["claude-anything"])
        )
    assert result.status == "success"
    assert result.attempts == 2
    assert result.text == "hi [已隐藏] [已隐藏]"


async def test_total_timeout_and_transient_retry(monkeypatch):
    calls = 0
    real_sleep = asyncio.sleep

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr("model_connect.providers.asyncio.sleep", fast_sleep)
    config = Connection(provider="openai")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Adapter(config, client).probe(
            "demo", "chat", ProbeRequest(connection=config, models=["demo"], retries=1)
        )
    assert result.status == "success"
    assert result.attempts == 2


@pytest.mark.parametrize(
    ("mode", "pattern", "name", "expected"),
    [
        ("contains", "FLASH", "gemini-flash", True),
        ("prefix", "gemini", "gemini-flash", True),
        ("suffix", "preview", "gemini-preview", True),
        ("glob", "**_flash_**", "a_flash_b", True),
        ("glob", "model-?", "model-ab", False),
        ("glob", "model-?", "model-a", True),
        ("exact", "a\nb", "b", True),
        ("contains", "", "anything", True),
    ],
)
def test_filters(mode, pattern, name, expected):
    assert ModelFilter(mode=mode, pattern=pattern).matches(name) is expected
    assert not ModelFilter(pattern="FLASH", case_sensitive=True).matches("flash")


def test_url_and_special_models():
    assert Connection(provider="google").base_url.endswith("/v1beta")
    assert (
        Connection(provider="openai", base_url="https://proxy.test/gateway/v1/").base_url
        == "https://proxy.test/gateway/v1"
    )
    assert Connection(provider="openai", base_url="https://proxy.test").base_url.endswith("/v1")
    assert model_info("text-embedding-3-small", "openai").supported is False
    assert model_info("gemini-flash", "google").supported is None
    with pytest.raises(ValueError):
        Connection(base_url="https://proxy.test?key=secret")


async def test_google_override_usage_redaction_and_http_redirect():
    config = Connection(
        provider="google",
        base_url="http://provider.test/prefix",
        probe_path="custom/{model}:generateContent",
        headers={"Authorization": "Bearer alternate-key"},
    )

    def handler(request):
        assert request.url.path == "/prefix/custom/demo:generateContent"
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {"note": "alternate-key", "large": "a" * 8000},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Adapter(config, client).probe(
            "demo", "generateContent", ProbeRequest(connection=config, models=["demo"])
        )
    assert result.status == "success"
    assert result.usage["note"] == "[已隐藏]"
    assert len(result.usage["large"]) == 6000
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, json={"choices": [{"message": {"content": "hi"}}]})
        )
    ) as client:
        result = await Adapter(config, client).probe(
            "demo", "chat", ProbeRequest(connection=config, models=["demo"])
        )
    assert result.status == "failed"


@pytest.mark.parametrize("key", ["带中文的密钥", "key\nheader", "key secret"])
def test_invalid_key_rejected(key):
    with pytest.raises(ValueError):
        Connection(provider="openai", api_key=key)


async def test_total_timeout_is_enforced():
    async def handler(request):
        await asyncio.sleep(3)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    config = Connection(provider="openai")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await Adapter(config, client).probe(
            "demo", "chat", ProbeRequest(connection=config, models=["demo"], timeout=1)
        )
    assert result.status == "failed"
    assert result.error_code == "timeout"
    assert result.latency_ms < 2000
