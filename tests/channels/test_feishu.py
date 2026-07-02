import asyncio

from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel
from nanobot.config.schema import FeishuConfig


def test_feishu_dependency_provides_expected_module() -> None:
    import lark_oapi
    import lark_oapi.ws

    assert hasattr(lark_oapi.ws, "Client")


def test_feishu_ws_client_uses_worker_thread_event_loop() -> None:
    class StoppingClient:
        def start(self) -> None:
            import lark_oapi.ws.client as ws_client

            ws_client.loop.call_soon(ws_client.loop.stop)
            ws_client.loop.run_until_complete(asyncio.sleep(3600))

    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = StoppingClient()
    channel._running = False

    channel._run_ws_client()

    assert channel._ws_loop is not None
    assert channel._ws_loop.is_closed()
