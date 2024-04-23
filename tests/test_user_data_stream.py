import os

import trio
import pytest_trio

from trio_binance import AsyncClient
from trio_binance.streams import BinanceSocketManager


async def test_private_stream():
    client = AsyncClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
    )
    bsm = await BinanceSocketManager.create(client, endpoint="portfolio", private=True)
    count = 0
    async with bsm.connect():
        async for msg in bsm.get_next_message():
            assert "e" in msg
            count += 1
            if count == 5:
                break
