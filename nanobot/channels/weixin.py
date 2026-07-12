"""Personal WeChat (Weixin) channel using HTTP long-poll API."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WeixinConfig

ITEM_TEXT = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
WEIXIN_CHANNEL_VERSION = "2.1.1"
ILINK_APP_ID = "bot"
BASE_INFO: dict[str, str] = {"channel_version": WEIXIN_CHANNEL_VERSION}
DEFAULT_LONG_POLL_TIMEOUT_S = 35
MAX_QR_REFRESH_COUNT = 3
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_S = 2
BACKOFF_DELAY_S = 30


def _build_client_version(version: str) -> int:
    parts = version.split(".")

    def _as_int(idx: int) -> int:
        try:
            return int(parts[idx])
        except Exception:
            return 0

    major = _as_int(0)
    minor = _as_int(1)
    patch = _as_int(2)
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


ILINK_APP_CLIENT_VERSION = _build_client_version(WEIXIN_CHANNEL_VERSION)


class WeixinChannel(BaseChannel):
    """
    Personal WeChat channel using Weixin iLink HTTP long-poll.

    This follows nanobot 0.2.x's Weixin channel shape: QR login stores a token
    under state_dir, getupdates long-polls inbound messages, and replies require
    the per-user context_token from the latest inbound message.
    """

    name = "weixin"

    def __init__(self, config: WeixinConfig, bus: MessageBus | None):
        super().__init__(config, bus)
        self.config: WeixinConfig = config
        self._client: httpx.AsyncClient | None = None
        self._get_updates_buf = ""
        self._context_tokens: dict[str, str] = {}
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._state_dir: Path | None = None
        self._token = ""
        self._next_poll_timeout_s = self.config.poll_timeout

    async def login(self, force: bool = False) -> bool:
        """Perform QR-code login and save account state."""
        if force:
            self._token = ""
            self._get_updates_buf = ""
            state_file = self._get_state_dir() / "account.json"
            if state_file.exists():
                state_file.unlink()

        if self._token or self._load_state():
            logger.info("Weixin login state already exists")
            return True

        self._client = self._http_client(timeout=60)
        self._running = True
        try:
            return await self._qr_login()
        finally:
            self._running = False
            if self._client:
                await self._client.aclose()
                self._client = None

    async def start(self) -> None:
        """Start Weixin long-poll message receiving."""
        self._running = True
        self._next_poll_timeout_s = self.config.poll_timeout
        self._client = self._http_client(timeout=self._next_poll_timeout_s + 10)

        if self.config.token:
            self._token = self.config.token
        elif not self._load_state():
            if not await self._qr_login():
                logger.error("Weixin login failed. Run: nanobot channels login weixin")
                self._running = False
                return

        logger.info("Weixin channel starting with HTTP long-poll...")

        consecutive_failures = 0
        while self._running:
            try:
                await self._poll_once()
                consecutive_failures = 0
            except httpx.TimeoutException:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"Weixin poll loop error: {e}")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    await asyncio.sleep(BACKOFF_DELAY_S)
                else:
                    await asyncio.sleep(RETRY_DELAY_S)

    async def stop(self) -> None:
        """Stop Weixin polling and persist state."""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        self._save_state()

    async def send(self, msg: OutboundMessage) -> None:
        """Send a text message through Weixin."""
        if not self._client or not self._token:
            logger.warning("Weixin client not initialized or not authenticated")
            return

        context_token = self._context_tokens.get(msg.chat_id, "")
        if not context_token:
            logger.warning(f"Weixin context_token missing for chat_id={msg.chat_id}")
            return

        await self._send_text(msg.chat_id, msg.content, context_token)

    async def _poll_once(self) -> None:
        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": BASE_INFO,
        }

        assert self._client is not None
        self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)
        data = await self._api_post("ilink/bot/getupdates", body)

        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"Weixin getupdates failed: ret={ret} errcode={errcode} "
                f"errmsg={data.get('errmsg', '')}"
            )

        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_state()

        for raw_msg in data.get("msgs", []) or []:
            await self._process_message(raw_msg)

    async def _process_message(self, msg: dict[str, Any]) -> None:
        if msg.get("message_type") == MESSAGE_TYPE_BOT:
            return

        from_user_id = str(msg.get("from_user_id") or "")
        if not from_user_id:
            return

        msg_id = str(msg.get("message_id") or msg.get("seq") or "")
        if not msg_id:
            msg_id = f"{from_user_id}_{msg.get('create_time_ms', '')}"
        if msg_id in self._processed_ids:
            return
        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)

        context_token = str(msg.get("context_token") or "")
        if context_token:
            self._context_tokens[from_user_id] = context_token
            self._save_state()

        content = self._extract_content(msg)
        if not content:
            return

        logger.info(f"Weixin inbound from {from_user_id}: {content[:50]}...")
        await self._handle_message(
            sender_id=from_user_id,
            chat_id=from_user_id,
            content=content,
            metadata={
                "message_id": msg_id,
                "context_token": context_token,
                "create_time_ms": msg.get("create_time_ms"),
            },
        )

    def _extract_content(self, msg: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in msg.get("item_list") or []:
            if item.get("type") == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    parts.append(str(text))
            elif item.get("type"):
                parts.append(f"[unsupported weixin item type: {item.get('type')}]")
        return "\n".join(parts).strip()

    async def _send_text(self, to_user_id: str, text: str, context_token: str) -> None:
        item_list = [{"type": ITEM_TEXT, "text_item": {"text": text}}] if text else []
        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"nanobot-{uuid.uuid4().hex[:12]}",
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "context_token": context_token,
        }
        if item_list:
            weixin_msg["item_list"] = item_list

        data = await self._api_post("ilink/bot/sendmessage", {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        })
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            logger.error(f"Weixin send failed: {data}")

    async def _fetch_qr_code(self) -> tuple[str, str]:
        data = await self._api_get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            auth=False,
        )
        qrcode_id = str(data.get("qrcode") or "")
        scan_url = str(data.get("qrcode_img_content") or qrcode_id)
        if not qrcode_id:
            raise RuntimeError(f"Failed to get Weixin QR code: {data}")
        return qrcode_id, scan_url

    async def _qr_login(self) -> bool:
        qrcode_id, scan_url = await self._fetch_qr_code()
        self._print_qr_code(scan_url)
        current_poll_base_url = self.config.base_url
        refresh_count = 0

        while self._running:
            data = await self._api_get_with_base(
                base_url=current_poll_base_url,
                endpoint="ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_id},
                auth=False,
            )
            status = data.get("status")

            if status == "confirmed":
                token = str(data.get("bot_token") or "")
                if not token:
                    logger.error(f"Weixin QR confirmed without bot_token: {data}")
                    return False
                self._token = token
                base_url = str(data.get("baseurl") or "")
                if base_url:
                    self.config.base_url = base_url
                self._save_state()
                logger.info(
                    f"Weixin login successful: bot_id={data.get('ilink_bot_id')} "
                    f"user_id={data.get('ilink_user_id')}"
                )
                return True

            if status == "scaned_but_redirect":
                redirect_host = str(data.get("redirect_host") or "").strip()
                if redirect_host:
                    if redirect_host.startswith(("http://", "https://")):
                        current_poll_base_url = redirect_host
                    else:
                        current_poll_base_url = f"https://{redirect_host}"

            if status == "expired":
                refresh_count += 1
                if refresh_count > MAX_QR_REFRESH_COUNT:
                    logger.warning("Weixin QR code expired too many times")
                    return False
                qrcode_id, scan_url = await self._fetch_qr_code()
                current_poll_base_url = self.config.base_url
                self._print_qr_code(scan_url)

            await asyncio.sleep(1)

        return False

    def _get_state_dir(self) -> Path:
        if self._state_dir:
            return self._state_dir
        if self.config.state_dir:
            state_dir = Path(self.config.state_dir).expanduser()
        else:
            state_dir = Path.home() / ".nanobot" / "weixin"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir = state_dir
        return state_dir

    def _load_state(self) -> bool:
        state_file = self._get_state_dir() / "account.json"
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to load Weixin state: {e}")
            return False

        self._token = str(data.get("token") or "")
        self._get_updates_buf = str(data.get("get_updates_buf") or "")
        context_tokens = data.get("context_tokens")
        if isinstance(context_tokens, dict):
            self._context_tokens = {
                str(user_id): str(token)
                for user_id, token in context_tokens.items()
                if str(user_id).strip() and str(token).strip()
            }
        base_url = str(data.get("base_url") or "")
        if base_url:
            self.config.base_url = base_url
        return bool(self._token)

    def _save_state(self) -> None:
        state_file = self._get_state_dir() / "account.json"
        data = {
            "token": self._token,
            "get_updates_buf": self._get_updates_buf,
            "context_tokens": self._context_tokens,
            "base_url": self.config.base_url,
        }
        state_file.write_text(json.dumps(data, ensure_ascii=False))

    @staticmethod
    def _random_wechat_uin() -> str:
        uint32 = int.from_bytes(os.urandom(4), "big")
        return base64.b64encode(str(uint32).encode()).decode()

    def _make_headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {
            "X-WECHAT-UIN": self._random_wechat_uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self.config.route_tag is not None and str(self.config.route_tag).strip():
            headers["SKRouteTag"] = str(self.config.route_tag).strip()
        return headers

    async def _api_get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        return await self._api_get_with_base(
            base_url=self.config.base_url,
            endpoint=endpoint,
            params=params,
            auth=auth,
        )

    async def _api_get_with_base(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        assert self._client is not None
        response = await self._client.get(
            f"{base_url.rstrip('/')}/{endpoint}",
            params=params,
            headers=self._make_headers(auth=auth),
        )
        response.raise_for_status()
        return response.json()

    async def _api_post(
        self,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        assert self._client is not None
        payload = body or {}
        if "base_info" not in payload:
            payload["base_info"] = BASE_INFO
        response = await self._client.post(
            f"{self.config.base_url.rstrip('/')}/{endpoint}",
            json=payload,
            headers=self._make_headers(auth=auth),
        )
        response.raise_for_status()
        return response.json()

    def _http_client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30),
            follow_redirects=True,
            trust_env=False,
        )

    @staticmethod
    def _print_qr_code(url: str) -> None:
        try:
            import qrcode as qrcode_lib

            qr = qrcode_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nWeixin login URL: {url}\n")
