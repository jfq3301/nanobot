"""DingTalk/DingDing channel implementation using Stream Mode."""

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote_plus

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DingDingConfig


class DingDingChannel(BaseChannel):
    """
    DingTalk channel using the official dingtalk-stream SDK.

    Stream Mode opens an outbound connection to DingTalk, so local development
    does not require a public HTTP callback URL. Replies are sent through the
    sessionWebhook included in incoming robot messages.
    """

    name = "dingding"

    def __init__(self, config: DingDingConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DingDingConfig = config
        self._client: Any | None = None
        self._client_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._session_webhooks: dict[str, tuple[str, int]] = {}

    async def start(self) -> None:
        """Start the DingTalk Stream client."""
        if not self.config.client_id or not self.config.client_secret:
            logger.error("DingTalk client_id/client_secret not configured")
            return

        try:
            import dingtalk_stream
        except ImportError:
            logger.error("DingTalk channel requires dingtalk-stream. Run: pip install dingtalk-stream")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        handler = _NanobotDingDingHandler(self)
        credential = dingtalk_stream.Credential(
            self.config.client_id,
            self.config.client_secret,
        )
        self._client = dingtalk_stream.DingTalkStreamClient(credential)
        self._client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
            handler,
        )

        logger.info("Starting DingTalk Stream client...")
        self._client_task = asyncio.create_task(asyncio.to_thread(self._run_stream_client))

        try:
            await self._client_task
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                logger.error(f"DingTalk Stream client stopped with error: {e}")
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop the DingTalk Stream client."""
        self._running = False

        if self._client:
            websocket = getattr(self._client, "websocket", None)
            if websocket and self._sdk_loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(websocket.close(), self._sdk_loop)
                    await asyncio.to_thread(future.result, 5)
                except Exception as e:
                    logger.debug(f"Error closing DingTalk websocket: {e}")

        if self._client_task and not self._client_task.done():
            try:
                await asyncio.wait_for(self._client_task, timeout=5)
            except asyncio.TimeoutError:
                self._client_task.cancel()
                try:
                    await self._client_task
                except asyncio.CancelledError:
                    pass

        self._client = None
        self._client_task = None
        self._loop = None
        self._sdk_loop = None

    def _run_stream_client(self) -> None:
        """Run the DingTalk SDK client on a worker-thread event loop."""
        asyncio.run(self._stream_loop())

    async def _stream_loop(self) -> None:
        """Run a stoppable DingTalk Stream loop around the official SDK primitives."""
        import websockets

        if not self._client:
            return

        self._sdk_loop = asyncio.get_running_loop()
        self._client.pre_start()

        while self._running:
            try:
                connection = self._client.open_connection()
                if not connection:
                    logger.error("DingTalk open connection failed")
                    await self._sleep_while_running(10)
                    continue

                logger.info(f"DingTalk endpoint is {connection.get('endpoint')}")
                uri = f"{connection['endpoint']}?ticket={quote_plus(connection['ticket'])}"

                async with websockets.connect(uri) as websocket:
                    self._client.websocket = websocket
                    keepalive_task = asyncio.create_task(self._client.keepalive(websocket))
                    try:
                        async for raw_message in websocket:
                            if not self._running:
                                break
                            json_message = json.loads(raw_message)
                            asyncio.create_task(self._client.background_task(json_message))
                    finally:
                        keepalive_task.cancel()
                        try:
                            await keepalive_task
                        except asyncio.CancelledError:
                            pass
                        self._client.websocket = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"DingTalk Stream connection error: {e}")
                    await self._sleep_while_running(3)

    async def _sleep_while_running(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(min(0.5, deadline - time.monotonic()))

    async def send(self, msg: OutboundMessage) -> None:
        """Send a text message through the current DingTalk session webhook."""
        session_webhook = self._session_webhook_for_message(msg)
        if not session_webhook:
            logger.warning(f"No DingTalk sessionWebhook available for chat_id: {msg.chat_id}")
            return

        payload = {
            "msgtype": "text",
            "text": {
                "content": msg.content,
            },
        }

        try:
            await self._post_session_webhook(session_webhook, payload)
        except Exception as e:
            logger.error(f"Error sending DingTalk message: {e}")

    async def _post_session_webhook(self, session_webhook: str, payload: dict[str, Any]) -> None:
        async with self._http_client() as client:
            response = await client.post(session_webhook, json=payload)
            response.raise_for_status()

        try:
            result = response.json()
        except ValueError:
            return

        if result.get("errcode") not in (None, 0):
            logger.error(f"DingTalk send failed: {result}")

    def _http_client(self) -> httpx.AsyncClient:
        """Create an HTTP client isolated from shell proxy environment variables."""
        return httpx.AsyncClient(timeout=10, trust_env=False)

    def _session_webhook_for_message(self, msg: OutboundMessage) -> str:
        session_webhook = msg.metadata.get("session_webhook")
        if session_webhook:
            return str(session_webhook)

        session = self._session_webhooks.get(msg.chat_id)
        if not session:
            return ""

        session_webhook, expires_at = session
        now_ms = int(time.time() * 1000)
        if expires_at and now_ms > expires_at:
            self._session_webhooks.pop(msg.chat_id, None)
            return ""

        return session_webhook

    def _on_callback(self, data: dict[str, Any]) -> None:
        """Handle a DingTalk callback from the SDK worker thread."""
        if not self._loop or not self._running:
            return

        future = asyncio.run_coroutine_threadsafe(self._handle_callback_data(data), self._loop)
        future.add_done_callback(self._log_callback_error)

    async def _handle_callback_data(self, data: dict[str, Any]) -> None:
        """Convert DingTalk callback JSON into an inbound bus message."""
        chat_id = str(data.get("conversationId") or "")
        if not chat_id:
            logger.warning("Ignoring DingTalk message without conversationId")
            return

        sender_id = self._allowed_sender_id(data)
        if not sender_id:
            return

        msg_type = str(data.get("msgtype") or "")
        content = self._extract_content(data)
        if not content:
            content = "[empty message]"

        session_webhook = str(data.get("sessionWebhook") or "")
        expires_at = int(data.get("sessionWebhookExpiredTime") or 0)
        if session_webhook:
            self._session_webhooks[chat_id] = (session_webhook, expires_at)

        logger.debug(f"DingTalk message from {sender_id}: {content[:50]}...")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata={
                "message_id": data.get("msgId"),
                "message_type": msg_type,
                "conversation_type": data.get("conversationType"),
                "conversation_title": data.get("conversationTitle"),
                "sender_id": data.get("senderId"),
                "sender_staff_id": data.get("senderStaffId"),
                "sender_nick": data.get("senderNick"),
                "sender_corp_id": data.get("senderCorpId"),
                "chatbot_corp_id": data.get("chatbotCorpId"),
                "chatbot_user_id": data.get("chatbotUserId"),
                "robot_code": data.get("robotCode"),
                "session_webhook": session_webhook,
                "session_webhook_expired_time": expires_at,
            },
        )

    def _extract_content(self, data: dict[str, Any]) -> str:
        msg_type = str(data.get("msgtype") or "")

        if msg_type == "text":
            text = data.get("text") or {}
            return str(text.get("content") or "").strip()

        content = data.get("content") or {}

        if msg_type == "audio":
            recognition = content.get("recognition")
            if recognition:
                return str(recognition).strip()

        if msg_type == "richText":
            parts = []
            for item in content.get("richText") or []:
                text = item.get("text")
                if text:
                    parts.append(str(text))
                elif item.get("type"):
                    parts.append(f"[{item['type']} message]")
            return "".join(parts).strip()

        if msg_type in {"picture", "video", "file"}:
            label = msg_type
            file_name = content.get("fileName")
            if file_name:
                return f"[{label}: {file_name}]"
            return f"[{label} message]"

        unknown = content.get("unknownMsgType")
        if unknown:
            return str(unknown).strip()

        return ""

    def _allowed_sender_id(self, data: dict[str, Any]) -> str:
        sender_ids = [
            str(data.get("senderStaffId") or ""),
            str(data.get("senderId") or ""),
            str(data.get("senderNick") or ""),
        ]
        sender_ids = [sender_id for sender_id in sender_ids if sender_id]

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            return sender_ids[0] if sender_ids else "unknown"

        for sender_id in sender_ids:
            if sender_id in allow_list:
                return sender_id
        return ""

    def _log_callback_error(self, future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception as e:
            logger.error(f"Error handling DingTalk message: {e}")


class _NanobotDingDingHandler:
    """Small adapter around dingtalk_stream.ChatbotHandler."""

    def __new__(cls, channel: DingDingChannel):
        try:
            import dingtalk_stream
        except ImportError:
            return super().__new__(cls)

        class Handler(dingtalk_stream.ChatbotHandler):
            def __init__(self, dingding_channel: DingDingChannel):
                super().__init__()
                self._dingding_channel = dingding_channel

            async def process(self, callback: Any) -> tuple[Any, str]:
                self._dingding_channel._on_callback(callback.data)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        return Handler(channel)
