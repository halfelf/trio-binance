import trio
import pytest_trio

from trio_binance import AsyncClient
from trio_binance.streams import BinanceSocketManager


async def test_fstream_market_data_stream():
    """USD-M futures (linear): mixed public + market streams via separate connections."""
    client = AsyncClient()
    bsm = await BinanceSocketManager.create(client, endpoint="linear")
    async with bsm.connect():
        symbol = "bnbusdt"
        # @depth → /public/stream, @aggTrade → /market/stream (two connections)
        await bsm.subscribe(params=[f"{symbol}@depth", f"{symbol}@aggTrade"], sub_id=1)
        count = 0
        with trio.fail_after(30):
            async for msg in bsm.get_next_message():
                print(msg)
                # Skip control messages (subscribe acks: {"result": null, "id": 1})
                if "result" in msg:
                    continue
                # Combined stream messages are wrapped: {"stream": ..., "data": {...}}
                data = msg.get("data", msg)
                assert "e" in data
                count += 1
                if count == 10:
                    break


async def test_stream_market_data_stream():
    """Spot: single /stream connection with JSON SUBSCRIBE."""
    client = AsyncClient()
    bsm = await BinanceSocketManager.create(client, endpoint="spot")
    async with bsm.connect():
        symbol = "bnbusdt"
        await bsm.subscribe(params=[f"{symbol}@depth", f"{symbol}@aggTrade"], sub_id=1)
        count = 0
        with trio.fail_after(30):
            async for msg in bsm.get_next_message():
                # Subscribe acks arrive before data: {"result": null, "id": 1}
                if "result" in msg:
                    continue
                # Combined stream messages are wrapped: {"stream": ..., "data": {...}}
                data = msg.get("data", msg)
                assert "e" in data
                count += 1
                if count == 10:
                    break


async def test_dstream_market_data_stream():
    """COIN-M futures (inverse): single /stream connection with JSON SUBSCRIBE."""
    client = AsyncClient()
    bsm = await BinanceSocketManager.create(client, endpoint="inverse")
    async with bsm.connect():
        symbol = "bnbusd_perp"
        await bsm.subscribe(params=[f"{symbol}@depth", f"{symbol}@aggTrade"], sub_id=1)
        count = 0
        with trio.fail_after(30):
            async for msg in bsm.get_next_message():
                if "result" in msg:
                    continue
                data = msg.get("data", msg)
                assert "e" in data
                count += 1
                if count == 10:
                    break
