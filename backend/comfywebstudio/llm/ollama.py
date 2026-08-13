"""Ollama.

The obvious local choice, and the one worth supporting properly: it reports what each model can *do*
(`/api/show` returns a capabilities list), so the app can offer only models that can actually see when it
needs to look at a picture, instead of letting the user pick one that will quietly ignore the image.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from .provider import LlmError, LlmProvider, ModelInfo, Reply, register

logger = logging.getLogger(__name__)


@register("ollama")
class OllamaProvider(LlmProvider):
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
            headers=self.config.headers or {},
        )

    async def models(self) -> list[ModelInfo]:
        try:
            async with self._client() as client:
                listing = (await client.get("/api/tags")).raise_for_status().json()
        except Exception as exc:  # noqa: BLE001 - the reason belongs in the UI, not a traceback
            raise LlmError(f"Could not reach Ollama at {self.config.base_url}: {exc}") from exc

        found: list[ModelInfo] = []
        async with self._client() as client:
            for entry in listing.get("models") or []:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                details = entry.get("details") or {}
                info = ModelInfo(
                    name=name,
                    size=str(details.get("parameter_size") or ""),
                    family=str(details.get("family") or ""),
                )
                # Asked per model rather than guessed from the name: "llava" in a tag is a convention,
                # not a contract, and a renamed local build would be misjudged either way.
                try:
                    shown = (
                        await client.post("/api/show", json={"model": name})
                    ).raise_for_status().json()
                    info.vision = "vision" in (shown.get("capabilities") or [])
                except Exception as exc:  # noqa: BLE001 - one unreadable model must not hide the rest
                    logger.debug("Could not read capabilities for %s: %s", name, exc)
                found.append(info)
        return found

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
        message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            message["images"] = [base64.b64encode(data).decode() for data in images]

        body: dict[str, Any] = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else []) + [message],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if schema is not None:
            # Ollama constrains decoding to the schema itself, so the answer cannot have the wrong keys
            # or the wrong types. This is what stops a model returning {"light": …} where a sentence was
            # asked for, or echoing "shot_prompt" back as the value of image_prompt.
            body["format"] = schema
        elif json_object:
            body["format"] = "json"

        try:
            async with self._client() as client:
                response = await client.post("/api/chat", json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise LlmError(_explain(exc, model)) from exc
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"Ollama at {self.config.base_url} did not answer: {exc}") from exc

        text = str((payload.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LlmError(f"{model} returned nothing.")
        return Reply(
            text=text,
            model=model,
            meta={k: payload.get(k) for k in ("total_duration", "eval_count") if k in payload},
        )


    async def pull(self, model: str, on_progress=None) -> None:
        """Download a model, reporting progress as it goes.

        Ollama streams newline-delimited JSON: a line per layer, each carrying how many bytes of that
        layer have arrived. Nothing here buffers the body — a six-gigabyte download read into memory to
        find out how far along it is would be its own kind of joke.
        """
        seen: dict[str, tuple[int, int]] = {}
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST", "/api/pull", json={"model": model}, timeout=None
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("error"):
                            raise LlmError(f"Ollama could not pull {model}: {event['error']}")

                        digest = str(event.get("digest") or "")
                        if digest and event.get("total"):
                            seen[digest] = (int(event.get("completed") or 0), int(event["total"]))

                        done = sum(part for part, _ in seen.values())
                        total = sum(whole for _, whole in seen.values())
                        if on_progress:
                            on_progress(
                                str(event.get("status") or ""),
                                done / total if total else 0.0,
                                done,
                                total,
                            )
        except httpx.HTTPStatusError as exc:
            raise LlmError(_explain(exc, model)) from exc
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"Pulling {model} failed: {exc}") from exc


def _explain(exc: httpx.HTTPStatusError, model: str) -> str:
    """Ollama's errors are readable; surfacing the text beats surfacing a status code."""
    detail = ""
    try:
        detail = str(json.loads(exc.response.text).get("error") or "")
    except Exception:  # noqa: BLE001
        detail = exc.response.text[:200]
    if "not found" in detail.lower():
        return f"Ollama has no model called {model!r}. Pull it first: ollama pull {model}"
    return f"Ollama refused the request ({exc.response.status_code}): {detail}"
