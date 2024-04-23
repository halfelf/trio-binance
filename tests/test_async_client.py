import trio
import pytest_trio

from datetime import datetime, timedelta
from trio_binance.client import AsyncClient
from trio_binance.enums import KLINE_INTERVAL_1HOUR


async def test_exchange_info():
    client = await AsyncClient.create()
    async with client:
        exchange_info = await client.get_exchange_info()
        assert isinstance(exchange_info, dict)
        symbols = exchange_info["symbols"]
        assert isinstance(symbols, list)
        assert "baseAsset" in symbols[0]
        assert "quoteAsset" in symbols[0]
        assert "orderTypes" in symbols[0]
        assert "filters" in symbols[0]


async def test_klines():
    client = await AsyncClient.create()
    async with client:
        start = datetime(2022, 1, 1, 8)
        end = datetime(2022, 1, 1, 12)
        klines = await client.get_historical_klines(
            "BTCUSDT",
            KLINE_INTERVAL_1HOUR,
            start_str=int(start.timestamp() * 1000),
            end_str=int(end.timestamp() * 1000),
        )
        assert isinstance(klines, list)
        assert isinstance(klines[0], list)
        assert len(klines) == (end - start) / timedelta(hours=1) + 1
