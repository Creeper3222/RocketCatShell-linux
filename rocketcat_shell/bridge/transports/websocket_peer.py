from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType, web

from rocketcat_shell.logger import logger

from ..json_codec import json_dumps, json_loads
from .action_dispatcher import OneBotActionDispatcher
from .base import heartbeat_event, lifecycle_event
from .codec import OneBotMessageCodec, SerializedOneBotFrame
from .http_common import is_authorized


_WEBSOCKET_SEND_TIMEOUT_SECONDS = 10.0


@dataclass(eq=False, slots=True)
class WebsocketPeer:
    websocket: web.WebSocketResponse
    event_enabled: bool
    queue: asyncio.Queue[SerializedOneBotFrame]
    sender_task: asyncio.Task[Any] | None = None
    closing_task: asyncio.Task[Any] | None = None
    removing: bool = False


class WebsocketPeerHub:
    def __init__(
        self,
        *,
        owner: str,
        token: str,
        self_id: int,
        heartbeat_interval_ms: int,
        queue_capacity: int,
        codec: OneBotMessageCodec,
        dispatcher: OneBotActionDispatcher,
        max_msg_size: int,
    ):
        self.owner = owner
        self.token = str(token or "")
        self.self_id = int(self_id or 0)
        self.heartbeat_interval_ms = max(0, int(heartbeat_interval_ms or 0))
        self.queue_capacity = max(1, int(queue_capacity or 1))
        self.codec = codec
        self.dispatcher = dispatcher
        self.max_msg_size = max(1024, int(max_msg_size or 1024))
        self.peers: set[WebsocketPeer] = set()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self.dropped_event_count = 0
        self.last_error = ""

    @property
    def client_count(self) -> int:
        return len(self.peers)

    @property
    def event_client_count(self) -> int:
        return sum(1 for peer in self.peers if peer.event_enabled)

    @property
    def queue_depth(self) -> int:
        return sum(peer.queue.qsize() for peer in self.peers)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not is_authorized(request, self.token):
            websocket = web.WebSocketResponse(max_msg_size=self.max_msg_size)
            await websocket.prepare(request)
            await websocket.send_str(
                json_dumps(
                    {
                        "status": "failed",
                        "retcode": 1403,
                        "data": None,
                        "wording": "token验证失败",
                        "echo": None,
                    }
                )
            )
            await websocket.close(code=1008, message=b"token verify failed")
            return websocket

        heartbeat_seconds = (
            self.heartbeat_interval_ms / 1000.0
            if self.heartbeat_interval_ms > 0
            else None
        )
        websocket = web.WebSocketResponse(
            heartbeat=heartbeat_seconds,
            autoping=True,
            max_msg_size=self.max_msg_size,
        )
        await websocket.prepare(request)
        event_enabled = request.path.rstrip("/") != "/api"
        peer = WebsocketPeer(
            websocket=websocket,
            event_enabled=event_enabled,
            queue=asyncio.Queue(maxsize=self.queue_capacity),
        )
        self.last_error = ""
        self.peers.add(peer)
        peer.sender_task = asyncio.create_task(
            self._sender_loop(peer), name=f"RocketCatOneBotPeerSender:{self.owner}"
        )
        if event_enabled:
            self._enqueue(
                peer,
                self.codec.serialize_payload(lifecycle_event(self.self_id)),
                count_drop=False,
            )
            self._ensure_heartbeat_task()
        try:
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    await self._handle_message(peer, str(message.data or ""))
                elif message.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                    WSMsgType.ERROR,
                }:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = repr(exc)
            logger.warning(
                "[RocketCatShell] OneBot WebSocket peer failed | owner=%s | error=%r",
                self.owner,
                exc,
            )
        finally:
            await self._remove_peer(peer)
        return websocket

    async def emit_event(self, payload: dict[str, Any]) -> None:
        self.emit_frame(self.codec.serialize_event(payload))

    def emit_frame(self, frame: SerializedOneBotFrame) -> None:
        for peer in tuple(self.peers):
            if peer.event_enabled:
                self._enqueue(peer, frame, count_drop=True)

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        peers = tuple(self.peers)
        if peers:
            await asyncio.gather(
                *(self._remove_peer(peer) for peer in peers),
                return_exceptions=True,
            )

    async def _handle_message(self, peer: WebsocketPeer, raw: str) -> None:
        try:
            payload = json_loads(raw)
        except Exception:
            if not self._enqueue(
                peer,
                self.codec.serialize_payload(
                    {
                        "status": "failed",
                        "retcode": 1400,
                        "data": None,
                        "wording": "json解析失败,请检查数据格式",
                        "echo": None,
                    }
                ),
                count_drop=False,
            ):
                await self._close_slow_action_peer(peer)
            return
        if not isinstance(payload, dict) or not payload.get("action"):
            return

        async def respond(response: dict[str, Any]) -> None:
            if not self._enqueue(
                peer,
                self.codec.serialize_payload(response),
                count_drop=False,
            ):
                await self._close_slow_action_peer(peer)

        await self.dispatcher.submit(
            str(payload.get("action") or ""),
            payload.get("params") if isinstance(payload.get("params"), dict) else {},
            payload.get("echo"),
            respond,
        )

    async def _close_slow_action_peer(self, peer: WebsocketPeer) -> None:
        self.last_error = "action response queue full"
        await peer.websocket.close(
            code=1013,
            message=b"action response queue full",
        )

    async def _sender_loop(self, peer: WebsocketPeer) -> None:
        while not peer.websocket.closed:
            frame = await peer.queue.get()
            try:
                async with asyncio.timeout(_WEBSOCKET_SEND_TIMEOUT_SECONDS):
                    await peer.websocket.send_str(frame.text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                if not peer.websocket.closed:
                    await peer.websocket.close(code=1011, message=b"send failed")
                return
            finally:
                peer.queue.task_done()

    def _enqueue(
        self,
        peer: WebsocketPeer,
        frame: SerializedOneBotFrame,
        *,
        count_drop: bool,
    ) -> bool:
        if peer.websocket.closed:
            if count_drop:
                self.dropped_event_count += 1
            return False
        try:
            peer.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            if count_drop:
                self.dropped_event_count += 1
                self._schedule_slow_peer_close(peer)
            return False

    async def _remove_peer(self, peer: WebsocketPeer) -> None:
        if peer.removing:
            return
        peer.removing = True
        self.peers.discard(peer)
        closing_task = peer.closing_task
        peer.closing_task = None
        if (
            closing_task is not None
            and closing_task is not asyncio.current_task()
            and not closing_task.done()
        ):
            closing_task.cancel()
        if peer.sender_task is not None:
            peer.sender_task.cancel()
            await asyncio.gather(peer.sender_task, return_exceptions=True)
            peer.sender_task = None
        if not peer.websocket.closed:
            await peer.websocket.close()
        if self.event_client_count == 0 and self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None

    def _schedule_slow_peer_close(self, peer: WebsocketPeer) -> None:
        if peer.closing_task is not None or peer.websocket.closed:
            return

        async def close() -> None:
            self.last_error = "event queue full"
            await peer.websocket.close(code=1013, message=b"event queue full")

        peer.closing_task = asyncio.create_task(
            close(), name=f"RocketCatOneBotSlowPeerClose:{self.owner}"
        )

    def _ensure_heartbeat_task(self) -> None:
        if self.heartbeat_interval_ms <= 0:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"RocketCatOneBotHeartbeat:{self.owner}"
        )

    async def _heartbeat_loop(self) -> None:
        interval_seconds = self.heartbeat_interval_ms / 1000.0
        while self.event_client_count > 0:
            await asyncio.sleep(interval_seconds)
            frame = self.codec.serialize_payload(
                heartbeat_event(self.self_id, self.heartbeat_interval_ms)
            )
            for peer in tuple(self.peers):
                if peer.event_enabled:
                    self._enqueue(peer, frame, count_drop=True)
