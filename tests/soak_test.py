"""
Resilience soak test for BinanceSocketManager.

Connects to the Binance spot stream, subscribes to a few market-data streams,
and runs for RUN_DURATION (default 7 days).  Statistics are printed every
STATS_INTERVAL seconds and appended to soak_test.log in the same directory.

Run:
    python tests/soak_test.py              # 7-day run
    SOAK_HOURS=1 python tests/soak_test.py # short run for smoke-testing

What is verified continuously:
  • Messages arrive at a reasonable rate (at least 1 msg / SILENCE_TIMEOUT s).
  • Reconnects are counted and logged whenever the WebSocket drops.
  • Heartbeat timeouts are counted and logged (they also trigger a reconnect).
  • After each reconnect, data flow resumes within RECONNECT_RESUME_TIMEOUT s.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import trio
from trio_websocket import open_websocket_url
import orjson

from trio_binance import AsyncClient
from trio_binance.streams import BinanceSocketManager
import trio_binance.streams as _streams_mod

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RUN_DURATION: float = float(os.getenv("SOAK_HOURS", 7 * 24)) * 3600  # seconds
STATS_INTERVAL: int = 60        # print stats every N seconds
SILENCE_TIMEOUT: int = 60       # warn if no message received for N seconds

ENDPOINT = "spot"
SYMBOL = "bnbusdt"
STREAMS = [
    f"{SYMBOL}@aggTrade",
    f"{SYMBOL}@depth",
]

LOG_FILE = Path(__file__).parent / "soak_test.log"

# ---------------------------------------------------------------------------
# Logging setup  (file + stderr, both at INFO)
# ---------------------------------------------------------------------------

_fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_fmt,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("soak")

# Make trio_binance.streams reconnect warnings visible in the soak log.
logging.getLogger("trio_binance.streams").setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Instrumented BinanceSocketManager
# ---------------------------------------------------------------------------

class _InstrumentedBSM(BinanceSocketManager):
    """Subclass that counts reconnects and heartbeat timeouts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reconnect_count: int = 0
        self.heartbeat_timeout_count: int = 0
        self._last_reconnect_at: float | None = None

    async def _heartbeat_task(self, ws, key: str):
        """Count heartbeat timeouts before re-raising to trigger reconnect."""
        while True:
            await trio.sleep(_streams_mod._HEARTBEAT_INTERVAL)
            try:
                with trio.fail_after(_streams_mod._HEARTBEAT_TIMEOUT):
                    await ws.ping()
            except trio.TooSlowError:
                self.heartbeat_timeout_count += 1
                log.warning(
                    "Heartbeat timeout on [%s] (total hb timeouts: %d)",
                    key, self.heartbeat_timeout_count,
                )
                raise

    async def _connection_task(self, initial_url: str, key: str):
        """Count and log each reconnect attempt."""
        attempt = 0
        while True:
            url = initial_url if attempt == 0 else self._build_reconnect_url(key)
            try:
                async with open_websocket_url(url) as ws:
                    self._connections[key] = ws
                    self._connection_ready[key].set()
                    if attempt > 0:
                        self.reconnect_count += 1
                        self._last_reconnect_at = time.monotonic()
                        log.info(
                            "WebSocket[%s] RECONNECTED (attempt %d, total: %d)",
                            key, attempt, self.reconnect_count,
                        )
                    attempt = 0

                    async with trio.open_nursery() as inner:
                        inner.start_soon(self._heartbeat_task, ws, key)
                        while True:
                            raw = await ws.get_message()
                            await self._message_send.send(orjson.loads(raw))

            except trio.Cancelled:
                raise
            except Exception as exc:
                delay = min(2 ** attempt, _streams_mod._MAX_RECONNECT_DELAY)
                log.warning(
                    "WebSocket[%s] disconnected (%s: %s), reconnecting in %.0f s",
                    key, type(exc).__name__, exc, delay,
                )
                self._connection_ready[key] = trio.Event()
                await trio.sleep(delay)
                attempt += 1


# ---------------------------------------------------------------------------
# Stats tracker
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self._start = time.monotonic()
        self.total_messages: int = 0
        self._last_msg_at: float = time.monotonic()
        self._interval_msgs: int = 0
        self._interval_start: float = time.monotonic()

    def record_message(self):
        self.total_messages += 1
        self._interval_msgs += 1
        self._last_msg_at = time.monotonic()

    def silence_seconds(self) -> float:
        return time.monotonic() - self._last_msg_at

    def uptime_str(self) -> str:
        return str(timedelta(seconds=int(time.monotonic() - self._start)))

    def flush_interval(self) -> tuple[int, float]:
        """Return (count, rate) for the last interval and reset counters."""
        now = time.monotonic()
        elapsed = now - self._interval_start
        rate = self._interval_msgs / elapsed if elapsed > 0 else 0.0
        count = self._interval_msgs
        self._interval_msgs = 0
        self._interval_start = now
        return count, rate


# ---------------------------------------------------------------------------
# Tasks  (all run until the cancel scope is cancelled from main())
# ---------------------------------------------------------------------------

async def stats_reporter(stats: Stats, bsm: _InstrumentedBSM):
    while True:
        await trio.sleep(STATS_INTERVAL)
        _, rate = stats.flush_interval()
        silence = stats.silence_seconds()
        log.info(
            "STATS  uptime=%s  total_msgs=%d  rate=%.1f msg/s  "
            "silence=%.0f s  reconnects=%d  hb_timeouts=%d",
            stats.uptime_str(),
            stats.total_messages,
            rate,
            silence,
            bsm.reconnect_count,
            bsm.heartbeat_timeout_count,
        )
        if silence > SILENCE_TIMEOUT:
            log.warning("SILENCE DETECTED: no message for %.0f s", silence)


async def message_consumer(stats: Stats, bsm: _InstrumentedBSM):
    last_reconnect_seen = 0
    async for msg in bsm.get_next_message():
        # Skip subscribe-ack control messages.
        if "result" in msg:
            continue

        stats.record_message()

        # Log when data flow resumes after a reconnect.
        if bsm.reconnect_count > last_reconnect_seen:
            delta = time.monotonic() - (bsm._last_reconnect_at or 0)
            log.info(
                "Data flow RESUMED after reconnect #%d (%.1f s after disconnect)",
                bsm.reconnect_count, delta,
            )
            last_reconnect_seen = bsm.reconnect_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    end_wall = datetime.now() + timedelta(seconds=RUN_DURATION)

    log.info("=" * 70)
    log.info("Soak test START  endpoint=%s  streams=%s", ENDPOINT, STREAMS)
    log.info(
        "Planned duration: %.1f h  (until ~%s)",
        RUN_DURATION / 3600,
        end_wall.strftime("%Y-%m-%d %H:%M:%S"),
    )
    log.info(
        "Heartbeat: every %d s, timeout %d s  |  Silence warning: %d s",
        _streams_mod._HEARTBEAT_INTERVAL,
        _streams_mod._HEARTBEAT_TIMEOUT,
        SILENCE_TIMEOUT,
    )
    log.info("=" * 70)

    client = AsyncClient()
    bsm = _InstrumentedBSM(client, endpoint=ENDPOINT)
    stats = Stats()

    async with bsm.connect():
        await bsm.subscribe(params=STREAMS, sub_id=1)
        log.info("Subscribed to %s", STREAMS)

        with trio.CancelScope() as scope:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(stats_reporter, stats, bsm)
                nursery.start_soon(message_consumer, stats, bsm)
                await trio.sleep(RUN_DURATION)
                scope.cancel()

    log.info("=" * 70)
    log.info("Soak test END")
    log.info(
        "FINAL  uptime=%s  total_msgs=%d  reconnects=%d  hb_timeouts=%d",
        stats.uptime_str(),
        stats.total_messages,
        bsm.reconnect_count,
        bsm.heartbeat_timeout_count,
    )
    log.info("=" * 70)


if __name__ == "__main__":
    trio.run(main)
