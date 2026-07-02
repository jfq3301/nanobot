"""Feishu/Lark channel implementation using event WebSocket."""

import asyncio
import json
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using the official SDK's event WebSocket client.

    This channel supports text messages first. Incoming Feishu messages are routed
    to the nanobot bus with the Feishu chat_id, and replies are sent back to that
    same chat.
    """

    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any | None = None
        self._im_client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._client_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the Feishu event WebSocket client."""
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id/app_secret not configured")
            return

        try:
            import lark_oapi as lark
        except ImportError:
            logger.error("Feishu channel requires lark-oapi. Run: pip install lark-oapi")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.config.verification_token,
                self.config.encrypt_key,
            )
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )

        self._im_client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        self._client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("Starting Feishu event WebSocket client...")
        self._client_task = asyncio.create_task(asyncio.to_thread(self._run_ws_client))

        try:
            await self._client_task
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                logger.error(f"Feishu WebSocket client stopped with error: {e}")
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop the Feishu event WebSocket client."""
        self._running = False

        if self._client and self._ws_loop:
            disconnect = getattr(self._client, "_disconnect", None)
            if disconnect:
                try:
                    future = asyncio.run_coroutine_threadsafe(disconnect(), self._ws_loop)
                    await asyncio.to_thread(future.result, 5)
                except Exception as e:
                    logger.debug(f"Error disconnecting Feishu client: {e}")

            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)

        if self._client_task and not self._client_task.done():
            try:
                await self._client_task
            except asyncio.CancelledError:
                pass

        self._client = None
        self._im_client = None
        self._ws_loop = None
        self._client_task = None

    def _run_ws_client(self) -> None:
        """Run the Lark SDK WebSocket client on its own event loop."""
        import lark_oapi.ws.client as ws_client

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client.loop = loop
        self._ws_loop = loop

        try:
            self._client.start()
        except RuntimeError as e:
            if self._running or "Event loop stopped before Future completed" not in str(e):
                raise
        finally:
            loop.close()

    async def send(self, msg: OutboundMessage) -> None:
        """Send a text message through Feishu."""
        if not self._im_client:
            logger.warning("Feishu client not running")
            return

        try:
            await asyncio.to_thread(self._send_text, msg.chat_id, msg.content)
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    def _send_text(self, chat_id: str, content: str) -> None:
        """Send text using the synchronous Feishu SDK client."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": content}, ensure_ascii=False))
                .build()
            )
            .build()
        )

        response = self._im_client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                f"Feishu send failed: code={response.code} "
                f"msg={response.msg} request_id={response.get_request_id()}"
            )

    def _on_message(self, data: Any) -> None:
        """Handle Feishu message events from the SDK callback thread."""
        if not self._loop or not self._running:
            return

        future = asyncio.run_coroutine_threadsafe(self._handle_feishu_message(data), self._loop)
        future.add_done_callback(self._log_callback_error)

    async def _handle_feishu_message(self, data: Any) -> None:
        """Convert a Feishu SDK event into an inbound bus message."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)

        if not event or not message:
            logger.debug("Ignoring Feishu event without message payload")
            return

        message_type = getattr(message, "message_type", "")
        if message_type != "text":
            logger.info(f"Ignoring unsupported Feishu message type: {message_type}")
            return

        content = self._extract_text(getattr(message, "content", ""))
        if not content:
            content = "[empty message]"

        sender_id = self._allowed_sender_id(sender)
        if not sender_id:
            return

        chat_id = str(getattr(message, "chat_id", "") or "")

        if not chat_id:
            logger.warning("Ignoring Feishu message without chat_id")
            return

        logger.debug(f"Feishu message from {sender_id}: {content[:50]}...")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata={
                "message_id": getattr(message, "message_id", None),
                "chat_type": getattr(message, "chat_type", None),
                "message_type": message_type,
                "open_id": self._sender_id_value(sender, "open_id"),
                "user_id": self._sender_id_value(sender, "user_id"),
                "union_id": self._sender_id_value(sender, "union_id"),
            },
        )

    def _extract_text(self, raw_content: str) -> str:
        """Extract text from Feishu message.content JSON."""
        if not raw_content:
            return ""

        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content

        text = content.get("text")
        if isinstance(text, str):
            return text
        return raw_content

    def _allowed_sender_id(self, sender: Any) -> str:
        """Return the matched Feishu sender ID, or an empty string when denied."""
        sender_ids = [
            self._sender_id_value(sender, "open_id"),
            self._sender_id_value(sender, "user_id"),
            self._sender_id_value(sender, "union_id"),
        ]
        sender_ids = [sender_id for sender_id in sender_ids if sender_id]

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            return sender_ids[0] if sender_ids else "unknown"

        for sender_id in sender_ids:
            if sender_id in allow_list:
                return sender_id
        return ""

    def _sender_id_value(self, sender: Any, field: str) -> str:
        sender_id = getattr(sender, "sender_id", None)
        value = getattr(sender_id, field, None)
        return str(value) if value else ""

    def _log_callback_error(self, future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception as e:
            logger.error(f"Error handling Feishu message: {e}")
