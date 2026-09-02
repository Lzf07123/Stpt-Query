import json

import httpx
import pytest

from app.llm import LLMClient


def _client(handler, **kwargs):
    return LLMClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_chat_uses_thinking_switch_and_system_prompt_override():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "分析正文"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
        })

    client = _client(
        handler,
        model="glm-4.5-flash",
        enable_thinking=False,
        system_prompt="环境变量提示词",
    )
    try:
        content, usage = await client.chat_with_usage("内置提示词", "成绩数据")
        assert content == "分析正文"
    finally:
        await client.aclose()

    assert seen["messages"][0] == {"role": "system", "content": "环境变量提示词"}
    assert seen["thinking"] == {"type": "disabled"}
    assert usage == {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}


@pytest.mark.asyncio
async def test_chat_keeps_builtin_prompt_and_enables_thinking():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "分析正文"}}]
        })

    client = _client(handler, enable_thinking=True)
    try:
        content, usage = await client.chat_with_usage("内置提示词", "成绩数据")
        assert content == "分析正文"
    finally:
        await client.aclose()

    assert seen["messages"][0] == {"role": "system", "content": "内置提示词"}
    assert seen["thinking"] == {"type": "enabled"}
    assert usage is None
