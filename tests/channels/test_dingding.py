from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.dingding import DingDingChannel
from nanobot.config.schema import DingDingConfig


def _text_payload(sender_staff_id: str = "staff-1") -> dict:
    return {
        "conversationId": "cid-1",
        "chatbotCorpId": "ding-corp",
        "chatbotUserId": "bot-user",
        "msgId": "msg-1",
        "senderNick": "Alice",
        "senderStaffId": sender_staff_id,
        "senderCorpId": "sender-corp",
        "conversationType": "2",
        "senderId": "encrypted-sender",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
        "sessionWebhookExpiredTime": 4102444800000,
        "robotCode": "robot-code",
        "msgtype": "text",
        "text": {"content": " hello DingTalk "},
    }


async def test_dingding_text_callback_publishes_inbound() -> None:
    bus = MessageBus()
    channel = DingDingChannel(DingDingConfig(), bus)

    await channel._handle_callback_data(_text_payload())

    msg = await bus.consume_inbound()

    assert msg.channel == "dingding"
    assert msg.sender_id == "staff-1"
    assert msg.chat_id == "cid-1"
    assert msg.content == "hello DingTalk"
    assert msg.metadata["message_id"] == "msg-1"
    assert msg.metadata["session_webhook"].endswith("session=abc")


async def test_dingding_allow_from_filters_sender_ids() -> None:
    bus = MessageBus()
    channel = DingDingChannel(DingDingConfig(allow_from=["allowed-staff"]), bus)

    await channel._handle_callback_data(_text_payload(sender_staff_id="blocked-staff"))

    assert bus.inbound_size == 0


async def test_dingding_audio_uses_recognition_text() -> None:
    bus = MessageBus()
    channel = DingDingChannel(DingDingConfig(), bus)
    payload = _text_payload()
    payload["msgtype"] = "audio"
    payload.pop("text")
    payload["content"] = {"recognition": "voice to text"}

    await channel._handle_callback_data(payload)

    msg = await bus.consume_inbound()

    assert msg.content == "voice to text"


async def test_dingding_send_uses_cached_session_webhook() -> None:
    bus = MessageBus()
    channel = DingDingChannel(DingDingConfig(), bus)
    sent = []

    async def fake_post(session_webhook: str, payload: dict) -> None:
        sent.append((session_webhook, payload))

    channel._post_session_webhook = fake_post
    await channel._handle_callback_data(_text_payload())

    await channel.send(OutboundMessage(
        channel="dingding",
        chat_id="cid-1",
        content="reply",
    ))

    assert sent == [(
        "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
        {"msgtype": "text", "text": {"content": "reply"}},
    )]
