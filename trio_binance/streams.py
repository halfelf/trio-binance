from contextlib import asynccontextmanager
from random import randint

import trio
import trio_websocket
import orjson
from trio_websocket import open_websocket_url

from trio_binance import AsyncClient


class BinanceSocketManager:
    # Binance WebSocket base URLs.
    #
    # Linear (USD-M futures) uses the new tripartite structure introduced in the
    # 2025 WebSocket migration notice:
    #   /public  — high-frequency order-book data (depth, bookTicker)
    #   /market  — regular market data (aggTrade, markPrice, kline, ticker …)
    #   /private — user account / order-update streams (requires listenKey)
    #
    # Spot, inverse (COIN-M), and portfolio-margin still use the legacy single
    # base URL; the /ws/<listenKey> vs /stream path split is handled at runtime.
    URLS = {
        "main": {
            "spot": "wss://stream.binance.com:9443",
            "linear": {
                "public": "wss://fstream.binance.com/public",
                "market": "wss://fstream.binance.com/market",
                "private": "wss://fstream.binance.com/private",
            },
            "inverse": "wss://dstream.binance.com",
            "portfolio": "wss://fstream.binance.com/pm",
        },
        "test": {},
    }

    # Stream-name suffixes (the part after the first '@', digits stripped) that
    # belong to the high-frequency /public endpoint on the linear market.
    # Everything else goes to /market.
    _LINEAR_PUBLIC_STREAM_TYPES = frozenset({"bookTicker", "depth"})

    def __init__(
        self,
        client: AsyncClient,
        endpoint: str = "spot",
        alternative_net: str = "",
    ):
        self.endpoint: str = endpoint
        self.alternative_net: str = alternative_net if alternative_net else "main"
        self.client: AsyncClient = client
        self.listen_key: str = ""

        # One WebSocket connection per stream-type key ("public", "market",
        # "private", or "_default" for non-linear endpoints).
        self._connections: dict[str, trio_websocket.WebSocketConnection] = {}

        # trio.Event per key, set once the connection is open and ready.
        # Used by _open_connection() to avoid opening duplicates.
        self._connection_ready: dict[str, trio.Event] = {}

        # Nursery and message channel are created in connect() and live for the
        # duration of that context manager.
        self._nursery: trio.Nursery | None = None
        self._message_send: trio.MemorySendChannel | None = None
        self._message_recv: trio.MemoryReceiveChannel | None = None

    @classmethod
    async def create(
        cls,
        client: AsyncClient,
        endpoint: str = "spot",
        alternative_net: str = "",
    ):
        return cls(client, endpoint, alternative_net)

    def _classify_linear_stream(self, stream: str) -> str:
        """Return 'public' or 'market' for a linear market-data stream name.

        Examples
        --------
        "bnbusdt@depth"       → "public"
        "bnbusdt@depth5@500ms"→ "public"   (level suffix stripped)
        "bnbusdt@bookTicker"  → "public"
        "bnbusdt@aggTrade"    → "market"
        "btcusdt@kline_1m"    → "market"
        "!markPrice@arr"      → "market"
        """
        if "@" not in stream:
            return "market"
        # Take the first segment after '@', lowercase it, then strip any
        # trailing speed modifier (e.g. "@500ms") and numeric depth level
        # (e.g. "depth5" → "depth", "depth20" → "depth").
        suffix_base = stream.split("@", 1)[1].lower().split("@")[0]
        suffix_stripped = suffix_base.rstrip("0123456789")
        if suffix_stripped in self._LINEAR_PUBLIC_STREAM_TYPES:
            return "public"
        return "market"

    def _build_url(self, stream_type: str) -> str:
        """Build the WebSocket URL for the given stream_type key.

        Linear private streams use the new query-parameter format:
          wss://fstream.binance.com/private/ws?listenKey=<key>

        All other linear streams use the combined /stream path:
          wss://fstream.binance.com/{public|market}/stream

        Non-linear endpoints use the legacy /ws/<listenKey> or /stream paths.
        """
        endpoint_cfg = self.URLS[self.alternative_net][self.endpoint]
        if isinstance(endpoint_cfg, dict):
            # linear endpoint — new tripartite URL structure
            if stream_type == "private":
                return f"{endpoint_cfg['private']}/ws?listenKey={self.listen_key}"
            return f"{endpoint_cfg[stream_type]}/stream"
        else:
            # spot / inverse / portfolio — legacy URL structure
            if stream_type == "private" and self.listen_key:
                return f"{endpoint_cfg}/ws/{self.listen_key}"
            return f"{endpoint_cfg}/stream"

    async def _connection_task(self, url: str, key: str):
        """Long-running task that owns one WebSocket connection.

        Opens the connection, signals _connection_ready[key], then forwards
        every incoming message to the shared memory channel until the socket
        closes (raising ConnectionClosed) or the nursery is canceled.
        """
        async with open_websocket_url(url) as ws:
            self._connections[key] = ws
            self._connection_ready[key].set()
            while True:
                raw = await ws.get_message()
                await self._message_send.send(orjson.loads(raw))

    async def _open_connection(self, key: str, url: str):
        """Lazily start a _connection_task for *key* and wait until it is ready.

        Idempotent: a second call with the same key returns immediately once the
        first connection's ready event has been set.
        """
        if key not in self._connection_ready:
            self._connection_ready[key] = trio.Event()
            self._nursery.start_soon(self._connection_task, url, key)
        await self._connection_ready[key].wait()

    @asynccontextmanager
    async def connect(self):
        """Context manager that owns the nursery and message channel.

        No WebSocket connection is opened here.  Actual connections are created
        lazily by subscribe() based on which stream types are requested.

        Exception: if self.listen_key is already set (user-data stream), the
        private connection is opened immediately and the keepalive task starts.
        """
        try:
            self.URLS[self.alternative_net][self.endpoint]
        except KeyError:
            raise ValueError(f"endpoint {self.endpoint} with net {self.alternative_net} not supported")

        send_ch, recv_ch = trio.open_memory_channel(100)
        self._message_send = send_ch
        self._message_recv = recv_ch
        self._connections = {}
        self._connection_ready = {}

        async with trio.open_nursery() as nursery:
            self._nursery = nursery
            if self.listen_key:
                # User-data streams push messages without a SUBSCRIBE handshake,
                # so open the private connection immediately.
                await self._open_connection("private", self._build_url("private"))
                nursery.start_soon(self._keepalive_task)
            yield self
            nursery.cancel_scope.cancel()

    async def _keepalive_task(self):
        """Extend the listenKey every 59 minutes to prevent server-side expiry."""
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
        """Subscribe to one or more streams.

        For the linear endpoint each stream name is classified as 'public' or
        'market' and sent to its dedicated WebSocket connection.

        The new /public/stream and /market/stream endpoints require at least one
        stream to be present at connect time; they reject a bare connection with
        1008 POLICY_VIOLATION.  Therefore, on the *first* subscribe call for a
        given stream type the streams are embedded directly in the URL as
        ``?streams=s1/s2``.  Subsequent calls on an already-open connection use
        the JSON SUBSCRIBE message instead.

        For spot / inverse / portfolio a single '_default' connection is used,
        and streams are always added via JSON SUBSCRIBE (the legacy /stream URL
        accepts bare connections).
        """
        if sub_id is None:
            sub_id = randint(1, 2147483647)

        endpoint_cfg = self.URLS[self.alternative_net][self.endpoint]

        if isinstance(endpoint_cfg, dict):
            # Group streams by their target entry point.
            groups: dict[str, list[str]] = {}
            for stream in params:
                stype = self._classify_linear_stream(stream)
                groups.setdefault(stype, []).append(stream)

            for stype, streams in groups.items():
                if stype not in self._connection_ready:
                    # First subscription for this type: include streams in the
                    # URL so the server accepts the connection immediately.
                    url = f"{endpoint_cfg[stype]}/stream?streams={'/'.join(streams)}"
                    await self._open_connection(stype, url)
                    # No JSON SUBSCRIBE needed — streams are already active via URL.
                else:
                    # Connection already open: add extra streams via JSON SUBSCRIBE.
                    await self._connection_ready[stype].wait()
                    await self._connections[stype].send_message(
                        orjson.dumps({"method": "SUBSCRIBE", "params": streams, "id": sub_id})
                    )
        else:
            # spot / inverse / portfolio: single '_default' connection.
            # Like the new linear endpoints, the legacy /stream URL also requires
            # at least one stream to be present at connect time.
            if "_default" not in self._connection_ready:
                base = self._build_url("_default")
                url = f"{base}?streams={'/'.join(params)}"
                await self._open_connection("_default", url)
            else:
                await self._connection_ready["_default"].wait()
                await self._connections["_default"].send_message(
                    orjson.dumps({"method": "SUBSCRIBE", "params": params, "id": sub_id})
                )

    async def list_subscribe(self, sub_id: int | None = None):
        """Query active subscriptions on every open connection."""
        for ws in self._connections.values():
            await ws.send_message(orjson.dumps({"method": "LIST_SUBSCRIPTIONS", "id": sub_id}))

    async def unsubscribe(self, params: list[str], sub_id: int | None = None):
        """Unsubscribe from streams, routing each to the correct connection."""
        if sub_id is None:
            sub_id = randint(1, 2147483647)

        endpoint_cfg = self.URLS[self.alternative_net][self.endpoint]

        if isinstance(endpoint_cfg, dict):
            groups: dict[str, list[str]] = {}
            for stream in params:
                stype = self._classify_linear_stream(stream)
                groups.setdefault(stype, []).append(stream)

            for stype, streams in groups.items():
                if stype in self._connections:
                    await self._connections[stype].send_message(
                        orjson.dumps({"method": "UNSUBSCRIBE", "params": streams, "id": sub_id})
                    )
        else:
            if "_default" in self._connections:
                await self._connections["_default"].send_message(
                    orjson.dumps({"method": "UNSUBSCRIBE", "params": params, "id": sub_id})
                )

    async def get_next_message(self):
        """Yield messages from all open connections in arrival order."""
        async for msg in self._message_recv:
            yield msg
