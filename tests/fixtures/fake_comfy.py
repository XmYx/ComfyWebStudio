"""A fake ComfyUI server.

Implements enough of the real API — ``/prompt``, ``/history``, ``/view``, ``/upload/image``,
``/object_info``, ``/userdata``, ``/webstudio/*`` and the websocket — for the orchestrator, the chaining
logic, the cache and every error path to be exercised without a GPU or a real ComfyUI.

Event ordering matches ComfyUI 0.24.1 exactly, including the detail the runner depends on: the
``executing: node=null`` sentinel is emitted **after** history is written, while ``execution_success``
arrives before it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

PROTOCOL_VERSION = 1


@dataclass
class FakeComfy:
    """A running fake server. Use :func:`fake_comfy` (the pytest fixture) rather than constructing it."""

    root: Path
    app: web.Application = field(default_factory=web.Application)
    runner: web.AppRunner | None = None
    site: web.TCPSite | None = None
    port: int = 0

    sockets: list[web.WebSocketResponse] = field(default_factory=list)
    history: dict[str, Any] = field(default_factory=dict)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    ingested: list[dict[str, Any]] = field(default_factory=list)

    #: Test knobs.
    fail_on_class: str | None = None
    reject_prompt: bool = False
    node_pack_installed: bool = True
    hang: bool = False
    interrupt_next: bool = False
    execution_delay: float = 0.0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def output_dir(self) -> Path:
        path = self.root / "output"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def input_dir(self) -> Path:
        path = self.root / "input"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> FakeComfy:
        # A real ComfyUI root always has these; their presence is what makes a backend "local".
        for name in ("output", "input", "temp", "user"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._register_routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        for socket in list(self.sockets):
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()

    def _register_routes(self) -> None:
        routes = [
            web.get("/ws", self._ws),
            web.get("/system_stats", self._system_stats),
            web.get("/object_info", self._object_info),
            web.get("/object_info/{class_type}", self._object_info_one),
            web.post("/prompt", self._prompt),
            web.get("/history/{prompt_id}", self._history_one),
            web.get("/queue", self._queue),
            web.post("/queue", self._queue_post),
            web.post("/interrupt", self._interrupt),
            web.get("/view", self._view),
            web.post("/upload/image", self._upload_image),
            web.get("/userdata", self._list_userdata),
            web.get("/userdata/{file}", self._read_userdata),
            web.post("/userdata/{file}", self._write_userdata),
            web.get("/webstudio/ping", self._ws_ping),
            web.get("/webstudio/manifest", self._ws_manifest),
            web.post("/webstudio/ingest", self._ws_ingest),
        ]
        # ComfyUI registers every route twice, bare and /api-prefixed; the client uses /api.
        for route in routes:
            self.app.router.add_route(route.method, route.path, route.handler)
            self.app.router.add_route(route.method, "/api" + route.path, route.handler)

    # -- websocket ---------------------------------------------------------------------------------

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        sid = request.query.get("clientId", "fake-sid")

        await socket.send_json(
            {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}, "sid": sid}}
        )
        try:
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("type") == "feature_flags":
                    await socket.send_json(
                        {"type": "feature_flags", "data": {"supports_preview_metadata": True}}
                    )
        finally:
            if socket in self.sockets:
                self.sockets.remove(socket)
        return socket

    async def _broadcast(self, type_: str, data: dict[str, Any]) -> None:
        for socket in list(self.sockets):
            if not socket.closed:
                await socket.send_json({"type": type_, "data": data})

    # -- introspection -----------------------------------------------------------------------------

    async def _system_stats(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "system": {"comfyui_version": "0.24.1", "python_version": "3.14", "os": "posix"},
                "devices": [{"name": "fake-gpu", "type": "cuda", "vram_total": 1 << 34, "vram_free": 1 << 33}],
            }
        )

    def _node_schemas(self) -> dict[str, Any]:
        def node(name, required, output=(), output_node=False):
            return {
                "name": name,
                "display_name": name,
                "input": {"required": required, "optional": {}},
                "output": list(output),
                "output_name": list(output),
                "output_node": output_node,
                "category": "test",
                "python_module": "fake",
            }

        return {
            "KSampler": node(
                "KSampler",
                {
                    "model": ["MODEL"],
                    "seed": ["INT", {"default": 0, "min": 0, "max": 2**32}],
                    "steps": ["INT", {"default": 20, "min": 1, "max": 100}],
                    "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 30.0, "step": 0.1}],
                    "sampler_name": [["euler", "dpmpp_2m"], {"default": "euler"}],
                },
                ("LATENT",),
            ),
            "EmptyImage": node(
                "EmptyImage",
                {"width": ["INT", {"default": 512}], "height": ["INT", {"default": 512}]},
                ("IMAGE",),
            ),
            "WSStringInput": node(
                "WSStringInput",
                {"port_name": ["STRING", {"default": ""}], "value": ["STRING", {"multiline": True}]},
                ("STRING",),
            ),
            "WSSeedInput": node(
                "WSSeedInput",
                {
                    "port_name": ["STRING", {"default": ""}],
                    "value": ["INT", {"default": 0, "min": 0, "max": 2**64 - 1}],
                },
                ("INT",),
            ),
            "WSImageInput": node(
                "WSImageInput",
                {"port_name": ["STRING", {"default": ""}], "source": ["STRING", {"default": ""}]},
                ("IMAGE",),
            ),
            "WSImageOutput": node(
                "WSImageOutput",
                {
                    "image": ["IMAGE"],
                    "port_name": ["STRING", {"default": ""}],
                    "format": [["png", "webp"], {"default": "png"}],
                    "run_key": ["STRING", {"default": ""}],
                },
                (),
                output_node=True,
            ),
            "WSTextOutput": node(
                "WSTextOutput",
                {
                    "text": ["STRING", {"forceInput": True}],
                    "port_name": ["STRING", {"default": ""}],
                    "run_key": ["STRING", {"default": ""}],
                },
                (),
                output_node=True,
            ),
        }

    async def _object_info(self, _request: web.Request) -> web.Response:
        return web.json_response(self._node_schemas())

    async def _object_info_one(self, request: web.Request) -> web.Response:
        class_type = request.match_info["class_type"]
        schemas = self._node_schemas()
        if class_type not in schemas:
            return web.json_response({}, status=404)
        return web.json_response({class_type: schemas[class_type]})

    # -- execution ---------------------------------------------------------------------------------

    async def _prompt(self, request: web.Request) -> web.Response:
        body = await request.json()
        prompt = body.get("prompt") or {}
        self.submitted.append(body)

        # Same rule as the real server (server.py, post_prompt): a caller-supplied prompt_id has to be a
        # canonical UUID. Enforcing it here is what stops us regressing to our own prefixed ids, which
        # ComfyUI 0.31 rejects outright with "prompt_id must be a valid UUID".
        supplied = body.get("prompt_id")
        if supplied is None:
            prompt_id = str(uuid.uuid4())
        else:
            try:
                prompt_id = str(uuid.UUID(str(supplied)))
            except ValueError:
                return web.json_response(
                    {
                        "error": {
                            "type": "invalid_prompt_id",
                            "message": "prompt_id must be a valid UUID",
                        },
                        "node_errors": {},
                    },
                    status=400,
                )

        if self.reject_prompt:
            return web.json_response(
                {
                    "error": {"type": "prompt_outputs_failed_validation", "message": "invalid prompt"},
                    "node_errors": {
                        next(iter(prompt), "1"): {
                            "class_type": "KSampler",
                            "errors": [{"type": "value_not_in_list", "message": "bad sampler"}],
                        }
                    },
                },
                status=400,
            )

        unknown = [nid for nid, n in prompt.items() if n.get("class_type") not in self._node_schemas()]
        if unknown:
            return web.json_response(
                {
                    "error": {"type": "missing_node_type", "message": "node not found"},
                    "node_errors": {
                        unknown[0]: {
                            "class_type": prompt[unknown[0]].get("class_type"),
                            "errors": [{"type": "missing_node_type", "message": "not installed"}],
                        }
                    },
                },
                status=400,
            )

        asyncio.get_running_loop().create_task(self._execute(prompt_id, prompt))
        return web.json_response({"prompt_id": prompt_id, "number": 0, "node_errors": {}})

    async def _execute(self, prompt_id: str, prompt: dict[str, Any]) -> None:
        """Replay ComfyUI's real event ordering."""
        await asyncio.sleep(0)
        await self._broadcast("execution_start", {"prompt_id": prompt_id, "timestamp": 0})
        await self._broadcast("execution_cached", {"prompt_id": prompt_id, "nodes": []})

        if self.hang:
            return  # never terminates: exercises the step timeout

        outputs: dict[str, Any] = {}
        for node_id, node in prompt.items():
            class_type = node.get("class_type")

            await self._broadcast("executing", {"prompt_id": prompt_id, "node": node_id})
            await self._broadcast(
                "progress_state",
                {
                    "prompt_id": prompt_id,
                    "nodes": {node_id: {"value": 1, "max": 1, "state": "running", "node_id": node_id}},
                },
            )
            if self.execution_delay:
                await asyncio.sleep(self.execution_delay)

            if self.interrupt_next:
                self.interrupt_next = False
                await self._broadcast(
                    "execution_interrupted",
                    {"prompt_id": prompt_id, "node_id": node_id, "node_type": class_type},
                )
                return

            if self.fail_on_class and class_type == self.fail_on_class:
                await self._broadcast(
                    "execution_error",
                    {
                        "prompt_id": prompt_id,
                        "node_id": node_id,
                        "node_type": class_type,
                        "exception_message": "deliberate test failure",
                        "exception_type": "RuntimeError",
                        "traceback": ["fake traceback"],
                    },
                )
                return

            payload = self._produce(node_id, node, prompt)
            if payload is not None:
                outputs[node_id] = payload
                await self._broadcast(
                    "executed", {"prompt_id": prompt_id, "node": node_id, "output": payload}
                )

        # Real ordering: execution_success fires before history exists...
        await self._broadcast("execution_success", {"prompt_id": prompt_id, "timestamp": 0})
        self.history[prompt_id] = {
            "prompt": [0, prompt_id, prompt, {}, []],
            "outputs": outputs,
            "status": {"status_str": "success", "completed": True, "messages": []},
        }
        # ...and only then the node=null sentinel. A client that reads history on execution_success races.
        await self._broadcast("executing", {"prompt_id": prompt_id, "node": None})

    def _resolve(self, value: Any, prompt: dict[str, Any], depth: int = 0) -> Any:
        """Follow a ``[node_id, slot]`` link to the value its producer emits.

        ComfyUI resolves links before a node's function is called, so a fake that handed the raw link
        through would let a broken injection look like it worked.
        """
        if not isinstance(value, list) or len(value) != 2 or depth > 8:
            return value
        source = prompt.get(str(value[0]))
        if not isinstance(source, dict):
            return value
        source_inputs = source.get("inputs") or {}
        if source.get("class_type") in {"WSStringInput", "WSIntInput", "WSFloatInput", "WSBooleanInput"}:
            return self._resolve(source_inputs.get("value"), prompt, depth + 1)
        return value

    def _produce(
        self, node_id: str, node: dict[str, Any], prompt: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Write a real file for each output node, mirroring the node pack's payload."""
        class_type = node.get("class_type")
        inputs = {k: self._resolve(v, prompt) for k, v in (node.get("inputs") or {}).items()}
        if class_type not in {"WSImageOutput", "WSTextOutput"}:
            return None

        port_name = str(inputs.get("port_name") or f"node_{node_id}")
        run_key = str(inputs.get("run_key") or "manual")
        subfolder = f"webstudio/{run_key}/{port_name}"
        directory = self.output_dir / subfolder
        directory.mkdir(parents=True, exist_ok=True)

        if class_type == "WSImageOutput":
            from PIL import Image

            filename = f"{port_name}_00001_.png"
            # Content varies with the resolved inputs, so cache tests see real hash changes.
            seed = abs(hash(json.dumps(inputs, sort_keys=True, default=str))) % 200
            Image.new("RGB", (16, 16), (seed, 120, 200)).save(directory / filename)
            kind, meta = "image", {"count": 1, "width": 16, "height": 16, "format": "png"}
        else:
            filename = f"{port_name}_00001_.txt"
            value = str(inputs.get("text", ""))
            (directory / filename).write_text(value, encoding="utf-8")
            kind, meta = "string", {"count": 1, "value": value, "format": "txt"}

        files = [{"filename": filename, "subfolder": subfolder, "type": "output"}]
        return {
            "webstudio": [
                {
                    "protocol": PROTOCOL_VERSION,
                    "port_name": port_name,
                    "kind": kind,
                    "run_key": run_key,
                    "files": files,
                    "meta": meta,
                }
            ],
            "images" if kind == "image" else "text": files if kind == "image" else [meta["value"]],
        }

    async def _history_one(self, request: web.Request) -> web.Response:
        prompt_id = request.match_info["prompt_id"]
        entry = self.history.get(prompt_id)
        return web.json_response({prompt_id: entry} if entry else {})

    async def _queue(self, _request: web.Request) -> web.Response:
        return web.json_response({"queue_running": [], "queue_pending": []})

    async def _queue_post(self, _request: web.Request) -> web.Response:
        return web.json_response({})

    async def _interrupt(self, _request: web.Request) -> web.Response:
        return web.json_response({})

    # -- media -------------------------------------------------------------------------------------

    async def _view(self, request: web.Request) -> web.Response:
        filename = request.query.get("filename", "")
        subfolder = request.query.get("subfolder", "")
        folder = {"output": self.output_dir, "input": self.input_dir}.get(
            request.query.get("type", "output"), self.output_dir
        )
        path = folder / subfolder / filename
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.Response(body=path.read_bytes(), content_type="application/octet-stream")

    async def _upload_image(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        name, subfolder, data = "upload.png", "", b""
        async for part in reader:
            if part.name == "image":
                name = part.filename or name
                data = await part.read()
            elif part.name == "subfolder":
                subfolder = await part.text()
            else:
                await part.read()

        directory = self.input_dir / subfolder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
        record = {"name": name, "subfolder": subfolder, "type": "input", "size": len(data)}
        self.uploads.append(record)
        return web.json_response(record)

    async def _ws_ingest(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        name, run_key, data = "file.bin", "manual", b""
        async for part in reader:
            if part.name == "file":
                name = part.filename or name
                data = await part.read()
            elif part.name == "run_key":
                run_key = await part.text()
            else:
                await part.read()

        subfolder = f"webstudio/{run_key}"
        directory = self.input_dir / subfolder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
        record = {
            "name": name,
            "subfolder": subfolder,
            "type": "input",
            "size": len(data),
            "source": f"{subfolder}/{name}",
        }
        self.ingested.append(record)
        return web.json_response(record)

    # -- userdata ----------------------------------------------------------------------------------

    def _userdata_dir(self) -> Path:
        path = self.root / "user" / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _list_userdata(self, request: web.Request) -> web.Response:
        directory = self._userdata_dir() / request.query.get("dir", "workflows")
        if not directory.is_dir():
            return web.json_response([])
        entries = [
            {
                "path": str(p.relative_to(directory)).replace(os.sep, "/"),
                "size": p.stat().st_size,
                "modified": int(p.stat().st_mtime * 1000),
            }
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        ]
        return web.json_response(entries)

    async def _read_userdata(self, request: web.Request) -> web.Response:
        from urllib.parse import unquote

        path = self._userdata_dir() / unquote(request.match_info["file"])
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json")

    async def _write_userdata(self, request: web.Request) -> web.Response:
        from urllib.parse import unquote

        relative = unquote(request.match_info["file"])
        path = self._userdata_dir() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(await request.text(), encoding="utf-8")
        return web.json_response(relative)

    # -- node pack ---------------------------------------------------------------------------------

    async def _ws_ping(self, _request: web.Request) -> web.Response:
        if not self.node_pack_installed:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(
            {"protocol": PROTOCOL_VERSION, "pack_version": "0.1.0", "ok": True, "comfyui_version": "0.24.1"}
        )

    async def _ws_manifest(self, _request: web.Request) -> web.Response:
        if not self.node_pack_installed:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(
            {
                "protocol": PROTOCOL_VERSION,
                "pack_version": "0.1.0",
                "kinds": [
                    {"kind": "image", "comfy_type": "IMAGE", "formats": ["png", "webp"], "default_format": "png"},
                    {"kind": "string", "comfy_type": "STRING", "formats": ["txt"], "default_format": "txt"},
                ],
                "input_nodes": [
                    {"class_type": "WSStringInput", "kind": "string", "value_input": "value"},
                    {"class_type": "WSImageInput", "kind": "image", "value_input": "source"},
                ],
                "output_nodes": [
                    {"class_type": "WSImageOutput", "kind": "image", "socket": "image"},
                    {"class_type": "WSTextOutput", "kind": "string", "socket": "text"},
                ],
            }
        )
