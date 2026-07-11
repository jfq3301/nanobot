from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import convert_keys
from nanobot.config.schema import Config


def test_dingtalk_config_key_initializes_dingding_channel() -> None:
    config = Config.model_validate(convert_keys({
        "channels": {
            "dingtalk": {
                "enabled": True,
                "clientId": "client-id",
                "clientSecret": "client-secret",
            },
        },
    }))

    manager = ChannelManager(config, MessageBus())

    assert manager.enabled_channels == ["dingding"]
    assert manager.get_channel("dingding") is not None


def test_channels_config_does_not_expose_dingding_alias() -> None:
    config = Config()

    assert not hasattr(config.channels, "dingding")
