import trio
import pytest_trio

from trio_binance import AsyncClient
from trio_binance.streams import BinanceSocketManager


async def test_market_data_stream():
    client = AsyncClient()
    bsm = await BinanceSocketManager.create(client, endpoint="linear")
    async with bsm.connect():
        symbol = "bnbusdt"
        bsm.subscribe(params=[f"{symbol}@depth", f"{symbol}@aggTrade"], sub_id=1)
        count = 1
        async for msg in bsm.get_next_message():
            assert "e" in msg
            count += 1
            if count == 500:
                break
