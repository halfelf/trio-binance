from contextlib import asynccontextmanager
from random import randint

import trio
import trio_websocket
import orjson
from trio_websocket import open_websocket_url

from trio_binance import AsyncClient


class BinanceSocketManager:
    URLS = {
        "main": {
            "spot": "wss://stream.binance.com:9443",
            "linear": "wss://fstream.binance.com",
            "inverse": "wss://dstream.binance.com",
            "portfolio": "wss://fstream.binance.com/pm",
        },
        "test": {},
    }

    def __init__(
        self,
        client: AsyncClient,
        endpoint: str = "spot",
        alternative_net: str = "",
        private=False,
    ):
        self.ws: trio_websocket.WebSocketConnection | None = None
        self.endpoint: str = endpoint
        self.alternative_net: str = alternative_net if alternative_net else "main"
        self.client: AsyncClient = client
        self.listen_key: str = ""
        self.user_data_stream = private

    @classmethod
    async def create(
        cls,
        client: AsyncClient,
        endpoint: str = "spot",
        alternative_net: str = "",
        private=False,
    ):
        self = cls(client, endpoint, alternative_net, private)
        if self.endpoint == "spot":
            self.listen_key = await self.client.stream_get_listen_key()
        elif self.endpoint == "linear":
            self.listen_key = await self.client.futures_stream_get_listen_key()
        elif self.endpoint == "inverse":
            self.listen_key = await self.client.futures_coin_stream_get_listen_key()
        elif self.endpoint == "portfolio":
            self.listen_key = await self.client.portfolio_margin_stream_get_listen_key()
        return self

    @asynccontextmanager
    async def connect(self):
        try:
            base_url = self.URLS[self.alternative_net][self.endpoint]
            if self.user_data_stream:
                url = f"{base_url}/ws/{self.listen_key}"
            else:
                url = f"{base_url}/stream"
        except KeyError:
            raise ValueError(f"endpoint {self.endpoint} with net {self.alternative_net} not supported")
        async with open_websocket_url(url) as ws:
            self.ws = ws
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.keepalive)
                yield self.ws
                nursery.cancel_scope.cancel()

    async def keepalive(self):
        while True:
            await trio.sleep(59 * 60)
            with trio.fail_after(5):
                if self.endpoint == "spot":
                    await self.client.stream_keepalive()
                elif self.endpoint == "linear":
                    await self.client.futures_stream_keepalive()
                elif self.endpoint == "inverse":
                    await self.client.futures_coin_stream_keepalive()
                elif self.endpoint == "portfolio":
                    await self.client.portfolio_margin_stream_keepalive()

    async def subscribe(self, params: list[str], sub_id: int | None = None):
        if sub_id is None:
            sub_id = randint(1, 2147483647)
        await self.ws.send_message(orjson.dumps({"method": "SUBSCRIBE", "params": params, "id": sub_id}))

    async def list_subscribe(self, sub_id: int | None = None):
        await self.ws.send_message(orjson.dumps({"method": "LIST_SUBSCRIPTIONS", "id": sub_id}))

    async def unsubscribe(self, params: list[str], sub_id: int | None = None):
        await self.ws.send_message(orjson.dumps({"method": "UNSUBSCRIBE", "params": params, "id": sub_id}))

    async def get_next_message(self):
        while True:
            raw_message = await self.ws.get_message()
            message = orjson.loads(raw_message)
            yield message
