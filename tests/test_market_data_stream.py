import trio
import pytest_trio

from trio_binance.streams import BinanceSocketManager


async def test_market_data_stream():
    async with BinanceSocketManager() as bsm:
        symbol = 'BNBUSDT'
        async with bsm.subscribe(symbol=symbol, stream_type='continuousKline') as recv_chan:
            async for kline in recv_chan:
                assert kline.get('e') == 'continuous_kline'
                assert kline.get('ps') == symbol
                assert kline.get('ct') == 'PERPETUAL'
                k = kline.get('k')
                assert k.get('i') == '1m'
                assert 'o' in k
                assert 'h' in k
                assert 'c' in k
                assert 'l' in k
                break
