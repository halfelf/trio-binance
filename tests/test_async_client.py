import trio
import pytest_trio

from trio_binance.client import AsyncClient


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
