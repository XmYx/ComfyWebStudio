"""HTTP routes the pack adds to ComfyUI's own aiohttp server.

Registered explicitly from ``__init__.py`` rather than as an import side effect, so importing the package
for tests does not require a running server.

ComfyUI mirrors every registered route under ``/api`` as well (``server.py:1064-1076``), so each of these is
reachable at both ``/webstudio/...`` and ``/api/webstudio/...``.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from . import kinds
from .constants import PACK_VERSION, PROTOCOL_VERSION
from .inputs import INPUT_KINDS, INPUT_NODES
from .outputs import OUTPUT_KINDS, OUTPUT_NODES
from .paths import sanitize_component, sanitize_run_key

logger = logging.getLogger(__name__)

#: Where staged inputs land inside ComfyUI's ``input/`` directory.
INGEST_ROOT = "webstudio"

#: Refuse uploads above this size; the framework should be linking files, not shipping models.
MAX_INGEST_BYTES = 2 * 1024 * 1024 * 1024


def _manifest_payload() -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "pack_version": PACK_VERSION,
        "kinds": kinds.manifest(),
        "input_nodes": [
            {"class_type": name, "kind": INPUT_KINDS[name], "value_input": _value_input(name)}
            for name in INPUT_NODES
        ],
        "output_nodes": [
            {"class_type": name, "kind": OUTPUT_KINDS[name], "socket": OUTPUT_NODES[name].SOCKET_NAME}
            for name in OUTPUT_NODES
        ],
    }


def _value_input(class_type: str) -> str:
    """Which widget the framework writes a value into for a given input node."""
    return "source" if INPUT_KINDS[class_type] in {"image", "mask", "latent", "audio", "video", "file"} else "value"


def register_routes(prompt_server) -> None:
    """Attach our routes to ``PromptServer.instance``.

    Called during node loading, which runs before ``add_routes()`` collects the table (``main.py:476`` then
    ``main.py:485``), so registering here is early enough for the ``/api`` mirror to be built too.
    """
    routes = prompt_server.routes

    @routes.get("/webstudio/ping")
    async def ping(_request: web.Request) -> web.Response:
        """Handshake. The framework uses this to detect the pack and check protocol compatibility."""
        payload = {"protocol": PROTOCOL_VERSION, "pack_version": PACK_VERSION, "ok": True}
        try:
            import comfyui_version

            payload["comfyui_version"] = comfyui_version.__version__
        except Exception:  # noqa: BLE001 - version reporting must never break the handshake
            pass
        return web.json_response(payload)

    @routes.get("/webstudio/manifest")
    async def manifest(_request: web.Request) -> web.Response:
        """Describes every node and kind, so framework-side discovery follows the installed pack version."""
        return web.json_response(_manifest_payload())

    @routes.post("/webstudio/ingest")
    async def ingest(request: web.Request) -> web.Response:
        """Stage a media file into ComfyUI's ``input/`` directory.

        This is the remote-backend chaining path: ``POST /upload/image`` only accepts images, but a chained
        latent, audio clip or video has to reach the machine running ComfyUI somehow.
        """
        import folder_paths

        reader = await request.multipart()
        run_key = "manual"
        filename = None
        payload_field = None

        async for part in reader:
            if part.name == "run_key":
                run_key = (await part.text()).strip()
            elif part.name == "filename":
                filename = (await part.text()).strip()
            elif part.name == "file":
                payload_field = part
                break  # the file must be read in place, before the reader advances
            else:
                await part.read()

        if payload_field is None:
            return web.json_response({"error": "missing 'file' part"}, status=400)

        name = sanitize_component(
            os.path.basename(filename or payload_field.filename or "upload"), "upload"
        )
        extension = os.path.splitext(filename or payload_field.filename or "")[1]
        if extension and not name.endswith(extension):
            name = f"{name}{extension}"

        subfolder = f"{INGEST_ROOT}/{sanitize_run_key(run_key)}"
        base = os.path.realpath(folder_paths.get_input_directory())
        directory = os.path.realpath(os.path.join(base, *subfolder.split("/")))
        if os.path.commonpath([base, directory]) != base:
            return web.json_response({"error": "invalid destination"}, status=400)
        os.makedirs(directory, exist_ok=True)

        destination = os.path.join(directory, name)
        written = 0
        with open(destination, "wb") as handle:
            while chunk := await payload_field.read_chunk():
                written += len(chunk)
                if written > MAX_INGEST_BYTES:
                    handle.close()
                    os.unlink(destination)
                    return web.json_response({"error": "file too large"}, status=413)
                handle.write(chunk)

        logger.info("WebStudio ingested %s (%d bytes)", destination, written)
        return web.json_response(
            {"name": name, "subfolder": subfolder, "type": "input", "size": written,
             "source": f"{subfolder}/{name}"}
        )
