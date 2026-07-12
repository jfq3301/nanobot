from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.channels.weixin import ITEM_TEXT, WeixinChannel
from nanobot.config.loader import convert_keys
from nanobot.config.schema import Config, WeixinConfig


def _text_message(from_user: str = "user@im.wechat") -> dict:
    return {
        "message_id": "msg-1",
        "from_user_id": from_user,
        "context_token": "ctx-token",
        "item_list": [
            {"type": ITEM_TEXT, "text_item": {"text": "hello weixin"}},
        ],
    }


async def test_weixin_text_message_publishes_inbound_and_caches_context_token(tmp_path) -> None:
    bus = MessageBus()
    channel = WeixinChannel(WeixinConfig(state_dir=str(tmp_path)), bus)

    await channel._process_message(_text_message())

    msg = await bus.consume_inbound()

    assert msg.channel == "weixin"
    assert msg.sender_id == "user@im.wechat"
    assert msg.chat_id == "user@im.wechat"
    assert msg.content == "hello weixin"
    assert msg.metadata["message_id"] == "msg-1"
    assert channel._context_tokens["user@im.wechat"] == "ctx-token"


async def test_weixin_allow_from_supports_wildcard(tmp_path) -> None:
    bus = MessageBus()
    channel = WeixinChannel(WeixinConfig(allow_from=["*"], state_dir=str(tmp_path)), bus)

    await channel._process_message(_text_message(from_user="anyone@im.wechat"))

    msg = await bus.consume_inbound()
    assert msg.sender_id == "anyone@im.wechat"


async def test_weixin_send_uses_cached_context_token() -> None:
    channel = WeixinChannel(WeixinConfig(), MessageBus())
    channel._token = "bot-token"
    channel._client = object()
    channel._context_tokens["user@im.wechat"] = "ctx-token"
    sent = []

    async def fake_post(endpoint: str, body: dict | None = None, auth: bool = True) -> dict:
        sent.append((endpoint, body, auth))
        return {"ret": 0}

    channel._api_post = fake_post

    await channel.send(OutboundMessage(
        channel="weixin",
        chat_id="user@im.wechat",
        content="reply",
    ))

    endpoint, body, auth = sent[0]
    assert endpoint == "ilink/bot/sendmessage"
    assert auth is True
    assert body["msg"]["to_user_id"] == "user@im.wechat"
    assert body["msg"]["context_token"] == "ctx-token"
    assert body["msg"]["item_list"][0]["text_item"]["text"] == "reply"


def test_weixin_headers_include_route_tag() -> None:
    channel = WeixinChannel(WeixinConfig(route_tag="route-a"), MessageBus())
    channel._token = "bot-token"

    headers = channel._make_headers()

    assert headers["Authorization"] == "Bearer bot-token"
    assert headers["SKRouteTag"] == "route-a"
    assert headers["AuthorizationType"] == "ilink_bot_token"


def test_weixin_config_key_initializes_channel() -> None:
    config = Config.model_validate(convert_keys({
        "channels": {
            "weixin": {
                "enabled": True,
                "allowFrom": ["*"],
            },
        },
    }))

    manager = ChannelManager(config, MessageBus())

    assert manager.enabled_channels == ["weixin"]
    assert manager.get_channel("weixin") is not None
