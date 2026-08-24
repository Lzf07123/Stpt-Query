"""OpenAI 兼容 LLM 客户端：替代 Dify 的 DeepSeek 插件。

默认直连 DeepSeek（https://api.deepseek.com），通过 LLM_BASE_URL/LLM_MODEL
可切换任意 OpenAI 兼容供应商（阿里云百炼、通义等），不依赖任何供应商 SDK。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .trace import LOG


class LLMError(RuntimeError):
    """LLM 调用失败（网络/上游/无内容）。"""


class LLMClient:
    def __init__(
        self,
        base_url: str = "https://api.deepseek.com",
        api_key: str = "",
        model: str = "deepseek-v4-flash",
        timeout: float = 60.0,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def chat(self, system: str, user: str) -> str:
        """调用 chat/completions，返回纯文本内容。"""
        if not self.api_key:
            raise LLMError("LLM_API_KEY 未配置")
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.base_url + "/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError("LLM 请求失败：%s" % exc.__class__.__name__) from exc
        if resp.status_code >= 400:
            LOG.warning("LLM 上游错误 status=%s body=%s", resp.status_code, resp.text[:200])
            raise LLMError("LLM 上游错误 status=%s" % resp.status_code)
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("LLM 响应缺少 choices[0].message.content") from exc
        return str(content or "").strip()
