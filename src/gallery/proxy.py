"""Reverse proxy: gateway <-> per-notebook marimo servers.

Hand-rolled on httpx (HTTP) and websockets (WS) because we need lifecycle
hooks on every request/connection to drive idle reaping, and streaming in
both directions without buffering.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from gallery.manager import ProcessManager

logger = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def make_http_client() -> httpx.AsyncClient:
    # read=None: marimo has long-lived endpoints; never time out mid-stream.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None)
    )


async def proxy_http(
    request: Request,
    client: httpx.AsyncClient,
    manager: ProcessManager,
    slug: str,
    path: str,
    max_upload_bytes: int,
) -> Response:
    app = manager.get(slug)
    assert app is not None
    app.touch()

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_upload_bytes:
        return Response("upload too large", status_code=413)

    backend = manager.base_url(slug)
    url = f"{backend}/apps/{slug}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    headers["host"] = backend.removeprefix("http://")
    headers["x-forwarded-for"] = request.client.host if request.client else ""
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")

    upstream = client.build_request(
        request.method, url, headers=headers, content=request.stream()
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.ConnectError:
        return Response("notebook backend unavailable", status_code=502)

    response_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
    }
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=response_headers,
        background=BackgroundTask(resp.aclose),
    )


async def proxy_ws(
    websocket: WebSocket, manager: ProcessManager, slug: str, path: str
) -> None:
    app = manager.get(slug)
    assert app is not None

    backend = manager.base_url(slug).replace("http://", "ws://")
    url = f"{backend}/apps/{slug}/{path}"
    if websocket.url.query:
        url += f"?{websocket.url.query}"

    headers = {}
    if cookie := websocket.headers.get("cookie"):
        headers["Cookie"] = cookie

    subprotocols = websocket.scope.get("subprotocols") or None
    try:
        upstream = await websockets.connect(
            url,
            additional_headers=headers,
            subprotocols=subprotocols,
            origin=manager.base_url(slug),
            max_size=None,  # marimo file uploads travel over the WS
            ping_interval=20,
            ping_timeout=60,
        )
    except Exception as exc:
        logger.warning("[%s] ws connect to backend failed: %s", slug, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)
    app.active_websockets += 1
    app.touch()

    async def client_to_backend() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close(code=message.get("code") or 1000)
                return
            app.touch()
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def backend_to_client() -> None:
        async for message in upstream:
            app.touch()
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)
        code = upstream.close_code or 1000
        # 1005/1006 mean "no code on the wire"; we can't send those ourselves.
        await websocket.close(code=code if code not in (1005, 1006) else 1000)

    tasks = [
        asyncio.create_task(client_to_backend()),
        asyncio.create_task(backend_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(
                exc, (WebSocketDisconnect, websockets.ConnectionClosed)
            ):
                logger.warning("[%s] ws bridge error: %s", slug, exc)
    finally:
        app.active_websockets -= 1
        app.touch()
        await upstream.close()
