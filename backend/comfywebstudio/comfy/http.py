"""Async HTTP client for one ComfyUI instance.

Every ComfyUI HTTP call in the application goes through here, so version drift is contained to one file.

Two things worth knowing about the endpoints, both verified in ComfyUI 0.24.1:
  * Routes are registered twice, bare and ``/api``-prefixed (``server.py:1064-1076``). We always use ``/api``.
  * The default origin-only middleware (``server.py:147-185``) 403s browser requests but not server-to-server
    ones, which is why the frontend must never call ComfyUI directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ComfyError(RuntimeError):
    """A ComfyUI request failed."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class ComfyPromptRejected(ComfyError):
    """``POST /prompt`` returned 400 — the graph did not validate.

    ``node_errors`` maps node id to the specific problem, which is what the UI needs in order to point at
    the offending node rather than showing a wall of text.
    """

    def __init__(self, message: str, node_errors: dict[str, Any] | None = None, payload: Any = None):
        super().__init__(message, status=400, payload=payload)
        self.node_errors = node_errors or {}


class ComfyHttpClient:
    """Thin, typed wrapper over ComfyUI's HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ComfyHttpClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- low level ---------------------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ComfyError(f"{method} {path} failed: {exc}") from exc
        return response

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ComfyError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}",
                status=response.status_code,
                payload=response.text,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ComfyError(f"{method} {path} returned non-JSON: {response.text[:200]}") from exc

    # -- introspection -----------------------------------------------------------------------------

    async def system_stats(self) -> dict[str, Any]:
        return await self._json("GET", "/api/system_stats")

    async def object_info(self) -> dict[str, Any]:
        return await self._json("GET", "/api/object_info")

    async def object_info_for(self, class_type: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/object_info/{class_type}")

    async def features(self) -> dict[str, Any]:
        return await self._json("GET", "/api/features")

    # -- execution ---------------------------------------------------------------------------------

    async def post_prompt(
        self,
        prompt: dict[str, Any],
        *,
        client_id: str,
        prompt_id: str,
        extra_data: dict[str, Any] | None = None,
        partial_execution_targets: list[str] | None = None,
        front: bool = False,
    ) -> dict[str, Any]:
        """Queue a graph.

        We always supply ``prompt_id`` ourselves (accepted at ``server.py:945``) so the websocket consumer
        can start filtering for it before the HTTP response even arrives.
        """
        body: dict[str, Any] = {"prompt": prompt, "client_id": client_id, "prompt_id": prompt_id}
        if extra_data:
            body["extra_data"] = extra_data
        if partial_execution_targets:
            body["partial_execution_targets"] = list(partial_execution_targets)
        if front:
            body["front"] = True

        response = await self._request("POST", "/api/prompt", json=body)
        if response.status_code == 400:
            payload = response.json() if response.content else {}
            error = payload.get("error") or {}
            raise ComfyPromptRejected(
                error.get("message") or error.get("type") or "prompt rejected",
                node_errors=payload.get("node_errors"),
                payload=payload,
            )
        if response.status_code >= 400:
            raise ComfyError(
                f"POST /prompt -> {response.status_code}: {response.text[:400]}",
                status=response.status_code,
            )
        return response.json()

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        payload = await self._json("GET", f"/api/history/{prompt_id}")
        return (payload or {}).get(prompt_id)

    async def queue(self) -> dict[str, Any]:
        return await self._json("GET", "/api/queue")

    async def interrupt(self, prompt_id: str | None = None) -> None:
        """Interrupt the *running* prompt.

        Only affects a prompt that is currently executing (``server.py:1003-1024``); a queued one has to be
        removed with :meth:`cancel_queued` instead.
        """
        await self._request("POST", "/api/interrupt", json={"prompt_id": prompt_id} if prompt_id else {})

    async def cancel_queued(self, prompt_ids: list[str]) -> None:
        await self._request("POST", "/api/queue", json={"delete": list(prompt_ids)})

    async def free(self, *, unload_models: bool = False, free_memory: bool = True) -> None:
        await self._request(
            "POST", "/api/free", json={"unload_models": unload_models, "free_memory": free_memory}
        )

    # -- media -------------------------------------------------------------------------------------

    async def view(self, filename: str, *, subfolder: str = "", folder_type: str = "output") -> bytes:
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        response = await self._request("GET", "/api/view", params=params)
        if response.status_code >= 400:
            raise ComfyError(
                f"GET /view {subfolder}/{filename} -> {response.status_code}",
                status=response.status_code,
            )
        return response.content

    async def upload_image(
        self,
        data: bytes,
        filename: str,
        *,
        subfolder: str = "",
        folder_type: str = "input",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Upload an image into ComfyUI's input directory.

        Note ComfyUI renames on collision unless ``overwrite`` is set, so the returned ``name`` is
        authoritative — never assume it matches what was sent.
        """
        files = {"image": (filename, data, "application/octet-stream")}
        form = {"type": folder_type, "subfolder": subfolder, "overwrite": "true" if overwrite else "false"}
        return await self._json("POST", "/api/upload/image", files=files, data=form)

    # -- userdata (workflow storage) ---------------------------------------------------------------

    async def list_workflows(self, directory: str = "workflows") -> list[dict[str, Any]]:
        """List stored workflows with sizes and modification times.

        ``full_info`` gives ``[{path, size, modified}]`` (``app/user_manager.py:149-211``), which is what
        the polling fallback uses to notice edits made in the ComfyUI UI.
        """
        return await self._json(
            "GET",
            "/api/userdata",
            params={"dir": directory, "recurse": "true", "full_info": "true"},
        )

    async def read_userdata(self, relative_path: str) -> str:
        response = await self._request("GET", f"/api/userdata/{_encode_userdata(relative_path)}")
        if response.status_code >= 400:
            raise ComfyError(f"GET userdata {relative_path} -> {response.status_code}", status=response.status_code)
        return response.text

    async def write_userdata(self, relative_path: str, content: str, *, overwrite: bool = True) -> Any:
        """Write a workflow file. The body is raw content, not multipart (``user_manager.py:341``)."""
        return await self._json(
            "POST",
            f"/api/userdata/{_encode_userdata(relative_path)}",
            params={"overwrite": "true" if overwrite else "false"},
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    async def delete_userdata(self, relative_path: str) -> None:
        await self._request("DELETE", f"/api/userdata/{_encode_userdata(relative_path)}")

    # -- our node pack -----------------------------------------------------------------------------

    async def webstudio_ping(self) -> dict[str, Any] | None:
        """Detect the companion node pack. Returns None when it is not installed."""
        try:
            return await self._json("GET", "/api/webstudio/ping")
        except ComfyError:
            return None

    async def webstudio_manifest(self) -> dict[str, Any] | None:
        try:
            return await self._json("GET", "/api/webstudio/manifest")
        except ComfyError:
            return None

    async def webstudio_ingest(self, data: bytes, filename: str, *, run_key: str) -> dict[str, Any]:
        """Stage a non-image file into ComfyUI's input directory.

        ``POST /upload/image`` only accepts images, so a chained latent, audio clip or video needs this.
        """
        return await self._json(
            "POST",
            "/api/webstudio/ingest",
            files={"file": (filename, data, "application/octet-stream")},
            data={"run_key": run_key, "filename": filename},
        )


def _encode_userdata(relative_path: str) -> str:
    """``/userdata/{file}`` takes the path as a single URL-encoded segment."""
    from urllib.parse import quote

    return quote(relative_path.replace("\\", "/"), safe="")
