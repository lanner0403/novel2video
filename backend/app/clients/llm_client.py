"""LLM 客戶端：OpenAI 相容 chat completions。

mock=True（預設）時不連網，改呼叫各階段提供的 mock_builder 產生離線假資料，
讓整條流水線在沒有任何 API key 的情況下也能跑通做展示。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

from ..config import settings


class LLMClient:
    def __init__(self) -> None:
        self.cfg = settings.llm
        # 尊重明確的 mock 開關；Ollama 等本地端點不需要 api_key
        self.mock = self.cfg.mock

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        mock_builder: Callable[[], Any],
    ) -> Any:
        """回傳解析後的 JSON 物件。離線時走 mock_builder。"""
        if self.mock:
            return mock_builder()

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }
        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{self.cfg.base_url}/chat/completions",
                       json=payload, headers=headers)
            # 部分本地端點（某些 Ollama 版本/模型）不支援 json_object，退回重試
            if r.status_code >= 400 and "response_format" in payload:
                payload.pop("response_format")
                r = c.post(f"{self.cfg.base_url}/chat/completions",
                           json=payload, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        return _loads_loose(content)


def _loads_loose(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise
