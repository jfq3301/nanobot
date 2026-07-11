from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.qq import QQChannel
from nanobot.config.schema import QQConfig


async def test_qq_c2c_message_event_publishes_inbound() -> None:
    bus = MessageBus()
    channel = QQChannel(QQConfig(), bus)

    await channel._handle_dispatch({
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg-1",
            "author": {"user_openid": "user-openid"},
            "content": "hello",
            "timestamp": "2026-07-11T12:00:00+08:00",
        },
    })

    msg = await bus.consume_inbound()

    assert msg.channel == "qq"
    assert msg.sender_id == "user-openid"
    assert msg.chat_id == "private:user-openid"
    assert msg.content == "hello"
    assert msg.metadata["event_id"] == "event-1"
    assert msg.metadata["message_id"] == "msg-1"


async def test_qq_group_message_event_extracts_content_and_media() -> None:
    bus = MessageBus()
    channel = QQChannel(QQConfig(), bus)

    await channel._handle_dispatch({
        "id": "event-2",
        "op": 0,
        "s": 2,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": "msg-2",
            "author": {"member_openid": "member-openid"},
            "content": "look",
            "group_openid": "group-openid",
            "attachments": [{"url": "https://example.test/a.png"}],
        },
    })

    msg = await bus.consume_inbound()

    assert msg.sender_id == "member-openid"
    assert msg.chat_id == "group:group-openid"
    assert msg.content == "look\n[media: https://example.test/a.png]"
    assert msg.media == ["https://example.test/a.png"]


async def test_qq_allow_from_filters_openid() -> None:
    bus = MessageBus()
    channel = QQChannel(QQConfig(allow_from=["allowed-openid"]), bus)

    await channel._handle_dispatch({
        "id": "event-3",
        "op": 0,
        "s": 3,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg-3",
            "author": {"user_openid": "blocked-openid"},
            "content": "blocked",
        },
    })

    assert bus.inbound_size == 0


def test_qq_send_request_supports_private_group_channel_and_dm_chat_ids() -> None:
    channel = QQChannel(QQConfig(), MessageBus())

    private_route, private_payload = channel._build_send_request(OutboundMessage(
        channel="qq",
        chat_id="private:user-openid",
        content="hello",
        metadata={"message_id": "msg-1"},
    ))
    group_route, group_payload = channel._build_send_request(OutboundMessage(
        channel="qq",
        chat_id="group:group-openid",
        content="hello group",
    ))
    channel_route, _ = channel._build_send_request(OutboundMessage(
        channel="qq",
        chat_id="channel:channel-id",
        content="hello channel",
    ))
    dm_route, _ = channel._build_send_request(OutboundMessage(
        channel="qq",
        chat_id="dm:guild-id",
        content="hello dm",
    ))

    assert private_route == "/v2/users/user-openid/messages"
    assert private_payload == {
        "content": "hello",
        "msg_type": 0,
        "msg_id": "msg-1",
    }
    assert group_route == "/v2/groups/group-openid/messages"
    assert group_payload == {
        "content": "hello group",
        "msg_type": 0,
    }
    assert channel_route == "/channels/channel-id/messages"
    assert dm_route == "/dms/guild-id/messages"
