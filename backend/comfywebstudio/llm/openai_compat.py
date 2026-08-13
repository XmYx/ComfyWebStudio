"""Anything speaking the OpenAI chat API.

Which is nearly everything self-hosted: vLLM, llama.cpp's server, LM Studio, text-generation-webui, and
the hosted gateways too. One adapter covers them all because the shape is the same; what differs is only
the base URL and whether a key is wanted.

Vision cannot be detected here — the API has no capability endpoint — so a model is taken at its word from
the provider's configuration. That is the honest difference from Ollama, and the UI says so rather than
guessing from the model's name.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from .provider import LlmError, LlmProvider, ModelInfo, Reply, register

logger = logging.getLogger(__name__)


@register("openai")
class OpenAiCompatibleProvider(LlmProvider):
    def _client(self) -> httpx.AsyncClient:
        headers = dict(self.config.headers or {})
        if self.config.api_key:
            headers.setdefault("Authorization", f"Bearer {self.config.api_key}")
        return httpx.AsyncClient(
            base_url=self.config.base_url, timeout=self.config.timeout_s, headers=headers
        )

    async def models(self) -> list[ModelInfo]:
        try:
            async with self._client() as client:
                payload = (await client.get("/v1/models")).raise_for_status().json()
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"Could not reach {self.config.base_url}: {exc}") from exc

        # No capability endpoint exists, so the configuration is the only source of truth for which of
        # these can see. Listing them as text-only and letting the user mark the vision one is honest;
        # inferring it from the name would be a guess dressed up as a fact.
        vision = {name.strip() for name in (self.config.vision_models or [])}
        return [
            ModelInfo(name=str(entry.get("id") or ""), vision=str(entry.get("id")) in vision)
            for entry in (payload.get("data") or [])
            if entry.get("id")
        ]

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        images: list[bytes] | None = None,
        json_object: bool = False,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
    ) -> Reply:
        content: Any = prompt
        if images:
            # The multimodal message shape: a list of parts rather than a string.
            content = [{"type": "text", "text": prompt}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64.b64encode(data).decode()}"},
                }
                for data in images
            ]

        body: dict[str, Any] = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": content}],
            "temperature": temperature,
            "stream": False,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            }
        elif json_object:
            body["response_format"] = {"type": "json_object"}

        try:
            async with self._client() as client:
                response = await client.post("/v1/chat/completions", json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise LlmError(
                f"{self.config.name} refused the request ({exc.response.status_code}): "
                f"{exc.response.text[:200]}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"{self.config.name} at {self.config.base_url} did not answer: {exc}") from exc

        choices = payload.get("choices") or []
        text = str(((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
        if not text:
            raise LlmError(f"{model} returned nothing.")
        return Reply(text=text, model=model, meta=payload.get("usage") or {})
