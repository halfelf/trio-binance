import struct
import random
import posixpath
from typing import Dict, Optional
from contextlib import asynccontextmanager

import trio
import ujson
from trio_websocket import open_websocket_url, WebSocketConnection

from trio_binance import AsyncClient


class StreamName:
    def __init__(self, **kwargs):
        self.symbol = kwargs['symbol'].upper()
        self.stream_type = kwargs.get('stream_type')

        if self.stream_type == 'continuousKline':
            self.params_str = kwargs.get('interval', '1m')
            self.contract_type = kwargs.get('contract_type', 'perpetual')
        elif self.stream_type == 'depth':
            self.params_str = kwargs.get('update_speed')
            self.contract_type = None

    def __str__(self):
        if self.stream_type == 'continuousKline':
            return f"{self.symbol.lower()}_{self.contract_type.lower()}@" \
                   f"{self.stream_type}_{self.params_str}"
        else:
            s = f"{self.symbol.lower()}@{self.stream_type}"
            if self.params_str:
                s += f"@{self.params_str}"
            return s

    def __key(self):
        return self.symbol, self.stream_type, self.params_str, self.contract_type

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        if isinstance(other, StreamName):
            return self.__key() == other.__key()
        return NotImplemented


class KeepAliveWebsocket:
    def __init__(self, base_url, stream_name, client=None):
        """
        DO NOT USE THIS TO CREATE INSTANCE
        """
        self.url = posixpath.join(base_url, str(stream_name))
        self.client: Optional[AsyncClient] = client
        self.conn: Optional[WebSocketConnection] = None
        self.commands_send_chan, self.commands_recv_chan = trio.open_memory_channel(100)
        self.subscribe_id = 0
        self.msg_send_chan: Optional[trio.MemorySendChannel] = None
        self.msg_recv_chan: Optional[trio.MemoryReceiveChannel] = None
        self.requesting = trio.Event()
        self.listen_key = None

    @classmethod
    async def create(cls, base_url, stream_name=None, client=None):
        """
        @param base_url: websocket base URL
        @param stream_name: stream name
        @param client: only used in user data stream
        """
        self = cls(base_url, stream_name, client)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self):
        if self.conn:
            await self.conn.aclose()

    async def _get_message(self, conn: WebSocketConnection):
        msg = ujson.loads(await conn.get_message())
        self.requesting.set()
        self.msg_send_chan, self.msg_recv_chan = trio.open_memory_channel(0)
        while True:
            await self.msg_send_chan.send(msg)
            msg = ujson.loads(await conn.get_message())

    async def _heartbeat(self, conn: WebSocketConnection):
        while True:
            payload = struct.pack('!I', random.getrandbits(32))
            await conn.pong(payload)
            await trio.sleep(seconds=600)  # less than 900 is ok

    async def wait_message(self):
        await self.requesting.wait()

    async def keep_put_listen_key(self):
        while True:
            await trio.sleep(59 * 60)  # in 60 minutes
            await self.client.futures_stream_keepalive(self.listen_key)

    async def start_websocket(self):
        async with open_websocket_url(self.url) as conn:
            async with trio.open_nursery() as nursery:
                if self.client:
                    self.listen_key = self.client.futures_stream_get_listen_key()
                    nursery.start_soon(self.keep_put_listen_key)

                nursery.start_soon(self._heartbeat, conn)
                nursery.start_soon(self._get_message, conn)

                # TODO: reconnect


class BinanceSocketManager:
    # STREAM_URL = 'wss://stream.binance.com:9443/ws'
    # STREAM_TESTNET_URL = 'wss://testnet.binance.vision/ws'
    FSTREAM_URL = 'wss://fstream.binance.com:443/ws'
    FSTREAM_TESTNET_URL = 'wss://stream.binancefuture.com/ws'

    # DSTREAM_URL = 'wss://dstream.binance.com:443/ws'
    # DSTREAM_TESTNET_URL = 'wss://dstream.binancefuture.com/ws'

    def __init__(self, client=None):
        self.client: Optional[AsyncClient] = client
        self.ws: Dict[StreamName, KeepAliveWebsocket] = {}
        self.ws_private: Optional[KeepAliveWebsocket] = None
        self.listen_key = ''

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client:
            await self.client.close_connection()
        if self.ws:
            for stream_name, ws in self.ws.items():
                await ws.__aexit__()

    @asynccontextmanager
    async def subscribe(self, **kwargs) -> trio.MemoryReceiveChannel:
        stream_name = StreamName(**kwargs)
        self.ws[stream_name] = await KeepAliveWebsocket.create(self.FSTREAM_URL, stream_name, self.client)
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.ws[stream_name].start_websocket)
                await self.ws[stream_name].wait_message()  # wait message to synchronize
                yield self.ws[stream_name].msg_recv_chan  # return a handler to user
                nursery.cancel_scope.cancel()  # control has returned from user
        finally:
            await self.ws[stream_name].__aexit__()

    @asynccontextmanager
    async def subscribe_private(self) -> trio.MemoryReceiveChannel:
        self.ws_private = await KeepAliveWebsocket.create(self.FSTREAM_URL, client=self.client)
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.ws_private.start_websocket)
                await self.ws_private.wait_message()
                yield self.ws_private.msg_recv_chan
                nursery.cancel_scope.cancel()
        finally:
            await self.ws_private.__aexit__()
