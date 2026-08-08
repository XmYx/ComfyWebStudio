"""The single websocket the frontend listens on.

Everything live — run progress, step results, workflow syncs from ComfyUI, render progress, backend health —
arrives here, so the UI never polls and never talks to ComfyUI directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import get_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

#: Sent when the stream is idle, so a proxy does not silently drop the connection.
HEARTBEAT_S = 25.0


@router.websocket("/api/events")
async def events(websocket: WebSocket, project_id: str | None = None) -> None:
    await websocket.accept()
    state = get_state(websocket)  # type: ignore[arg-type]

    await websocket.send_json(
        {
            "type": "connected",
            "data": {
                "project_id": project_id,
                "active_runs": [r.model_dump(mode="json") for r in state.orchestrator.active_runs(project_id)],
            },
        }
    )

    async with state.events.subscribe(project_id) as stream:
        iterator = stream.__aiter__()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    await websocket.send_json({"type": "ping", "data": {}})
                    continue
                except StopAsyncIteration:
                    return
                await websocket.send_json(event.to_dict())
        except WebSocketDisconnect:
            logger.debug("Event client disconnected")
        except Exception as exc:  # noqa: BLE001 - a dead socket must not surface as a 500
            logger.debug("Event stream ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()
