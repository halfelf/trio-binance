from contextlib import asynccontextmanager
import logging
from random import randint

import trio
import trio_websocket
import orjson
from trio_websocket import open_websocket_url

from trio_binance import AsyncClient

logger = logging.getLogger(__name__)

# Seconds between WebSocket pings.
_HEARTBEAT_INTERVAL = 20
# Seconds to wait for a pong before treating the connection as dead.
_HEARTBEAT_TIMEOUT = 10
# Exponential-backoff cap (seconds) between reconnect attempts.
_MAX_RECONNECT_DELAY = 60
# listenKey keepalive: max retry attempts and pause between them.
_KEEPALIVE_MAX_RETRIES = 5
_KEEPALIVE_RETRY_DELAY = 10  # seconds


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
        # Reset to a new (unset) Event each time a connection drops so that
        # callers waiting in subscribe() pause until the reconnect completes.
        self._connection_ready: dict[str, trio.Event] = {}

        # All streams currently subscribed per connection key.  Used to
        # rebuild the ?streams= URL when reconnecting.
        self._subscribed_streams: dict[str, list[str]] = {}

        # Base stream URL (no query string) per key, stored on first connect
        # so that reconnect can reassemble the full URL.
        self._stream_base_urls: dict[str, str] = {}

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

    def _build_reconnect_url(self, key: str) -> str:
        """Build the URL to use when reconnecting an existing connection.

        For private connections the listenKey URL is rebuilt from scratch so
        that any key refresh performed by _keepalive_task is picked up.

        For market-data connections all currently subscribed streams are
        embedded in the ?streams= query parameter so that the server starts
        pushing them immediately without a separate SUBSCRIBE handshake.
        """
        if key == "private":
            return self._build_url("private")
        streams = self._subscribed_streams.get(key, [])
        base = self._stream_base_urls[key]
        if streams:
            return f"{base}?streams={'/'.join(streams)}"
        return base

    async def _heartbeat_task(self, ws: trio_websocket.WebSocketConnection, key: str):
        """Send a WebSocket ping every _HEARTBEAT_INTERVAL seconds.

        If the server does not respond within _HEARTBEAT_TIMEOUT seconds the
        fail_after block raises TooSlowError, which propagates out of the inner
        nursery in _connection_task and triggers a reconnect.
        """
        while True:
            await trio.sleep(_HEARTBEAT_INTERVAL)
            with trio.fail_after(_HEARTBEAT_TIMEOUT):
                await ws.ping()

    async def _connection_task(self, initial_url: str, key: str):
        """Long-running task that owns one WebSocket connection.

        Opens the connection, signals _connection_ready[key], then forwards
        every incoming message to the shared memory channel.

        On any connection error (including a heartbeat timeout) the task waits
        with exponential back-off and reconnects automatically.  The outer
        nursery's cancel scope is the only way to stop the loop.
        """
        attempt = 0
        while True:
            url = initial_url if attempt == 0 else self._build_reconnect_url(key)
            try:
                async with open_websocket_url(url) as ws:
                    self._connections[key] = ws
                    self._connection_ready[key].set()
                    attempt = 0  # reset counter after a successful connect

                    async with trio.open_nursery() as inner:
                        inner.start_soon(self._heartbeat_task, ws, key)
                        while True:
                            raw = await ws.get_message()
                            await self._message_send.send(orjson.loads(raw))

            except trio.Cancelled:
                # Outer nursery is shutting down — propagate immediately.
                raise
            except Exception as exc:
                delay = min(2 ** attempt, _MAX_RECONNECT_DELAY)
                logger.warning(
                    "WebSocket[%s] disconnected (%s: %s), reconnecting in %.0fs",
                    key, type(exc).__name__, exc, delay,
                )
                # Replace the ready event so that any concurrent subscribe()
                # calls will block until the new connection is established.
                self._connection_ready[key] = trio.Event()
                await trio.sleep(delay)
                attempt += 1

    async def _open_connection(self, key: str, url: str, base_url: str):
        """Lazily start a _connection_task for *key* and wait until it is ready.

        Idempotent: a second call with the same key returns immediately once the
        first connection's ready event has been set.
        """
        if key not in self._connection_ready:
            self._connection_ready[key] = trio.Event()
            self._stream_base_urls[key] = base_url
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
        self._subscribed_streams = {}
        self._stream_base_urls = {}

        async with trio.open_nursery() as nursery:
            self._nursery = nursery
            if self.listen_key:
                # User-data streams push messages without a SUBSCRIBE handshake,
                # so open the private connection immediately.
                url = self._build_url("private")
                await self._open_connection("private", url, url)
                nursery.start_soon(self._keepalive_task)
            yield self
            nursery.cancel_scope.cancel()

    async def _keepalive_task(self):
        """Extend the listenKey every 59 minutes to prevent server-side expiry.

        Each renewal attempt is retried up to _KEEPALIVE_MAX_RETRIES times
        (with a _KEEPALIVE_RETRY_DELAY second pause between attempts) before
        giving up on that cycle.  Failure is logged but never crashes the task:
        losing the WebSocket connection due to a stale listenKey is far less
        harmful than tearing down the whole nursery.
        """
        while True:
            await trio.sleep(59 * 60)
            for attempt in range(1, _KEEPALIVE_MAX_RETRIES + 1):
                try:
                    with trio.fail_after(5):
                        if self.endpoint == "spot":
                            await self.client.stream_keepalive()
                        elif self.endpoint == "linear":
                            await self.client.futures_stream_keepalive()
                        elif self.endpoint == "inverse":
                            await self.client.futures_coin_stream_keepalive()
                        elif self.endpoint == "portfolio":
                            await self.client.portfolio_margin_stream_keepalive()
                    break  # success — no more retries needed
                except trio.Cancelled:
                    raise
                except Exception as exc:
                    if attempt == _KEEPALIVE_MAX_RETRIES:
                        logger.critical(
                            "listenKey keepalive FAILED after %d attempts (%s: %s); "
                            "stream may expire within 1 minute",
                            attempt, type(exc).__name__, exc,
                        )
                    else:
                        logger.warning(
                            "listenKey keepalive attempt %d/%d failed (%s: %s), retrying in %d s",
                            attempt, _KEEPALIVE_MAX_RETRIES,
                            type(exc).__name__, exc, _KEEPALIVE_RETRY_DELAY,
                        )
                        await trio.sleep(_KEEPALIVE_RETRY_DELAY)

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

        All subscribed stream names are recorded in _subscribed_streams so that
        they can be re-sent automatically after a reconnect.
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
                # Record streams for reconnect before opening the connection.
                self._subscribed_streams.setdefault(stype, []).extend(streams)

                if stype not in self._connection_ready:
                    # First subscription for this type: include streams in the
                    # URL so the server accepts the connection immediately.
                    base_url = f"{endpoint_cfg[stype]}/stream"
                    url = f"{base_url}?streams={'/'.join(streams)}"
                    await self._open_connection(stype, url, base_url)
                    # No JSON SUBSCRIBE needed — streams are already active via URL.
                else:
                    # Connection already open (or reconnecting): wait for it,
                    # then add extra streams via JSON SUBSCRIBE.
                    await self._connection_ready[stype].wait()
                    await self._connections[stype].send_message(
                        orjson.dumps({"method": "SUBSCRIBE", "params": streams, "id": sub_id})
                    )
        else:
            # spot / inverse / portfolio: single '_default' connection.
            # Like the new linear endpoints, the legacy /stream URL also requires
            # at least one stream to be present at connect time.
            self._subscribed_streams.setdefault("_default", []).extend(params)

            if "_default" not in self._connection_ready:
                base_url = self._build_url("_default")
                url = f"{base_url}?streams={'/'.join(params)}"
                await self._open_connection("_default", url, base_url)
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
                # Remove from reconnect tracking.
                if stype in self._subscribed_streams:
                    for s in streams:
                        try:
                            self._subscribed_streams[stype].remove(s)
                        except ValueError:
                            pass
                if stype in self._connections:
                    await self._connections[stype].send_message(
                        orjson.dumps({"method": "UNSUBSCRIBE", "params": streams, "id": sub_id})
                    )
        else:
            if "_default" in self._subscribed_streams:
                for s in params:
                    try:
                        self._subscribed_streams["_default"].remove(s)
                    except ValueError:
                        pass
            if "_default" in self._connections:
                await self._connections["_default"].send_message(
                    orjson.dumps({"method": "UNSUBSCRIBE", "params": params, "id": sub_id})
                )

    async def get_next_message(self):
        """Yield messages from all open connections in arrival order."""
        async for msg in self._message_recv:
            yield msg
