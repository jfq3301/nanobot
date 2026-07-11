"""Official QQ Bot channel implementation."""

import asyncio
import inspect
import json
import sys
import time
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import QQConfig


class QQChannel(BaseChannel):
    """
    Official QQ Bot channel using Tencent's WebSocket Gateway and REST APIs.

    The channel receives Gateway dispatch events and sends text replies through
    the official OpenAPI. It supports C2C, group, channel, and channel-DM text
    messages.
    """

    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._access_token: str = ""
        self._access_token_expires_at: float = 0
        self._seq: int | None = None
        self._session_id: str = ""
        self._ws: Any | None = None

    async def start(self) -> None:
        """Connect to the official QQ Bot Gateway and receive events."""
        if not self.config.app_id or not self.config.app_secret:
            logger.error("QQ app_id/app_secret not configured")
            return

        try:
            import websockets
        except ImportError:
            logger.error("QQ channel requires websockets. Run: pip install websockets")
            return

        self._running = True

        while self._running:
            try:
                gateway_url = await self._get_gateway_url()
                logger.info(f"Connecting to QQ Bot Gateway at {gateway_url}...")

                connect_kwargs = self._websocket_connect_kwargs(websockets.connect)
                async with websockets.connect(gateway_url, **connect_kwargs) as ws:
                    self._ws = ws
                    await self._gateway_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"QQ Gateway connection error: {e}")
                if self._running:
                    await asyncio.sleep(5)
            finally:
                self._ws = None

    async def stop(self) -> None:
        """Stop the QQ Gateway connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a text message through the official QQ OpenAPI."""
        route, payload = self._build_send_request(msg)
        payload.setdefault("msg_type", 0)
        payload.setdefault("content", msg.content)

        try:
            async with self._http_client() as client:
                response = await client.post(
                    f"{self.config.api_base.rstrip('/')}{route}",
                    json=payload,
                    headers=await self._api_headers(),
                )
                response.raise_for_status()
        except Exception as e:
            logger.error(f"Error sending QQ message: {e}")
            return

        try:
            result = response.json()
        except json.JSONDecodeError:
            return

        if result.get("code") not in (None, 0):
            logger.error(f"QQ send failed: {result}")

    async def _gateway_loop(self, ws: Any) -> None:
        """Process Gateway payloads for a single WebSocket connection."""
        hello = json.loads(await ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"Expected QQ Gateway Hello, got: {hello}")

        interval_s = (hello.get("d") or {}).get("heartbeat_interval", 45000) / 1000
        await self._identify(ws)
        heartbeat_task = asyncio.create_task(self._heartbeat(ws, interval_s))

        try:
            async for raw in ws:
                payload = json.loads(raw)
                op = payload.get("op")

                if payload.get("s") is not None:
                    self._seq = payload["s"]

                if op == 0:
                    await self._handle_dispatch(payload)
                elif op == 7:
                    logger.info("QQ Gateway requested reconnect")
                    break
                elif op == 9:
                    logger.warning(f"QQ Gateway invalid session: {payload}")
                    break
                elif op == 11:
                    logger.debug("QQ Gateway heartbeat acknowledged")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _identify(self, ws: Any) -> None:
        token = await self._get_access_token()
        await ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": self.config.intents,
                "shard": [0, 1],
                "properties": {
                    "$os": sys.platform,
                    "$browser": "nanobot",
                    "$device": "nanobot",
                },
            },
        }))

    async def _heartbeat(self, ws: Any, interval_s: float) -> None:
        while self._running:
            await asyncio.sleep(interval_s)
            await ws.send(json.dumps({"op": 1, "d": self._seq}))

    async def _handle_dispatch(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("t")
        data = payload.get("d") or {}

        if event_type == "READY":
            self._session_id = str(data.get("session_id") or "")
            user = data.get("user") or {}
            logger.info(f"QQ Bot connected as {user.get('username') or user.get('id')}")
            return

        if event_type in {
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "GROUP_MESSAGE_CREATE",
            "AT_MESSAGE_CREATE",
            "MESSAGE_CREATE",
            "DIRECT_MESSAGE_CREATE",
        }:
            await self._handle_message_event(event_type, data, payload.get("id"))

    async def _handle_message_event(
        self,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None,
    ) -> None:
        sender_id = self._sender_id_from_event(event_type, data)
        chat_id = self._chat_id_from_event(event_type, data)

        if not sender_id or not chat_id:
            logger.warning(f"Ignoring QQ {event_type} without sender/chat id")
            return

        content = str(data.get("content") or "").strip()
        media = self._attachment_urls(data.get("attachments") or [])
        if not content and not media:
            content = "[empty message]"
        elif media:
            content = "\n".join([content, *[f"[media: {url}]" for url in media]]).strip()

        logger.debug(f"QQ message from {sender_id}: {content[:50]}...")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media,
            metadata={
                "event_id": event_id,
                "event_type": event_type,
                "message_id": data.get("id"),
                "timestamp": data.get("timestamp"),
                "author": data.get("author") or {},
                "guild_id": data.get("guild_id"),
                "channel_id": data.get("channel_id"),
                "group_openid": data.get("group_openid"),
            },
        )

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._access_token_expires_at - 60:
            return self._access_token

        async with self._http_client() as client:
            response = await client.post(
                self.config.auth_url,
                json={
                    "appId": self.config.app_id,
                    "clientSecret": self.config.app_secret,
                },
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"QQ access token response missing access_token: {data}")

        expires_in = int(data.get("expires_in") or 7200)
        self._access_token = token
        self._access_token_expires_at = now + expires_in
        return token

    async def _api_headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        return {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }

    async def _get_gateway_url(self) -> str:
        async with self._http_client() as client:
            response = await client.get(
                f"{self.config.api_base.rstrip('/')}/gateway",
                headers=await self._api_headers(),
            )
            response.raise_for_status()

        data = response.json()
        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ gateway response missing url: {data}")
        return str(url)

    def _http_client(self) -> httpx.AsyncClient:
        """Create an HTTP client isolated from shell proxy environment variables."""
        return httpx.AsyncClient(timeout=10, trust_env=False)

    def _websocket_connect_kwargs(self, connect: Any) -> dict[str, Any]:
        """Build WebSocket connect kwargs without relying on environment proxies."""
        if "proxy" in inspect.signature(connect).parameters:
            return {"proxy": None}
        return {}

    def _build_send_request(self, msg: OutboundMessage) -> tuple[str, dict[str, Any]]:
        """Build an official OpenAPI send route and payload."""
        chat_type, target_id = self._parse_chat_id(msg.chat_id)
        payload: dict[str, Any] = {
            "content": msg.content,
            "msg_type": 0,
        }

        reply_to = msg.reply_to or msg.metadata.get("message_id")
        if reply_to:
            payload["msg_id"] = reply_to

        if chat_type == "private":
            self._add_event_id(payload, msg)
            return f"/v2/users/{target_id}/messages", payload
        if chat_type == "group":
            self._add_event_id(payload, msg)
            return f"/v2/groups/{target_id}/messages", payload
        if chat_type == "channel":
            return f"/channels/{target_id}/messages", payload
        if chat_type == "dm":
            return f"/dms/{target_id}/messages", payload

        raise ValueError(f"Unsupported QQ chat_id: {msg.chat_id}")

    def _add_event_id(self, payload: dict[str, Any], msg: OutboundMessage) -> None:
        event_id = msg.metadata.get("event_id")
        if event_id:
            payload["event_id"] = event_id

    def _chat_id_from_event(self, event_type: str, data: dict[str, Any]) -> str:
        if event_type == "C2C_MESSAGE_CREATE":
            openid = (data.get("author") or {}).get("user_openid")
            return f"private:{openid}" if openid else ""
        if event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
            group_openid = data.get("group_openid")
            return f"group:{group_openid}" if group_openid else ""
        if event_type in {"AT_MESSAGE_CREATE", "MESSAGE_CREATE"}:
            channel_id = data.get("channel_id")
            return f"channel:{channel_id}" if channel_id else ""
        if event_type == "DIRECT_MESSAGE_CREATE":
            guild_id = data.get("guild_id")
            return f"dm:{guild_id}" if guild_id else ""
        return ""

    def _sender_id_from_event(self, event_type: str, data: dict[str, Any]) -> str:
        author = data.get("author") or {}
        if event_type == "C2C_MESSAGE_CREATE":
            return str(author.get("user_openid") or "")
        if event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
            return str(author.get("member_openid") or "")
        return str(author.get("id") or "")

    def _parse_chat_id(self, chat_id: str) -> tuple[str, str]:
        for chat_type in ("private", "group", "channel", "dm"):
            prefix = f"{chat_type}:"
            if chat_id.startswith(prefix):
                return chat_type, chat_id.removeprefix(prefix)
        return "private", chat_id

    def _attachment_urls(self, attachments: list[dict[str, Any]]) -> list[str]:
        urls = []
        for attachment in attachments:
            url = attachment.get("url") or attachment.get("voice_wav_url")
            if url:
                urls.append(str(url))
        return urls
