"""OpenAI 兼容 LLM 客户端：替代 Dify 的成绩分析插件。

默认直连智谱开放平台（https://open.bigmodel.cn/api/paas/v4），通过
LLM_BASE_URL/LLM_MODEL 可切换任意 OpenAI 兼容供应商，不依赖供应商 SDK。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx

from .trace import LOG


class LLMError(RuntimeError):
    """LLM 调用失败（网络/上游/无内容）。"""


class LLMClient:
    def __init__(
        self,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        api_key: str = "",
        model: str = "glm-4.5-flash",
        timeout: float = 60.0,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        enable_thinking: bool = True,
        system_prompt: str = "",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.system_prompt = system_prompt.strip()
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def chat(self, system: str, user: str) -> str:
        """调用 chat/completions，只返回纯文本内容。"""
        content, _ = await self.chat_with_usage(system, user)
        return content

    async def chat_with_usage(
        self, system: str, user: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """调用 chat/completions，请求局部返回内容与 usage。"""
        if not self.api_key:
            raise LLMError("LLM_API_KEY 未配置")
        effective_system = self.system_prompt or system
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": effective_system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if self.enable_thinking else "disabled"},
        }
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = await self.client.post(
                self.base_url + "/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError("LLM 请求失败：%s" % exc.__class__.__name__) from exc
        if resp.status_code >= 400:
            LOG.warning("LLM 上游错误 status=%s body=%s", resp.status_code, resp.text[:200])
            raise LLMError("LLM 上游错误 status=%s" % resp.status_code)
        try:
            response_payload = resp.json()
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("LLM 响应缺少 choices[0].message.content") from exc
        usage = response_payload.get("usage")
        return str(content or "").strip(), usage if isinstance(usage, dict) else None
