=================================
Welcome to trio-binance
=================================

This is an unofficial Python wrapper for the `Binance exchange REST API v3 <https://binance-docs.github.io/apidocs/spot/en>`_. I am in no way affiliated with Binance, use at your own risk.

And this repository is forked from `python-binance <https://github.com/sammchardy/python-binance>`_, with trio as the asynchronous framework.

Source code
  https://github.com/halfelf/trio-binance

Binance API Telegram
  https://t.me/binance_api_english

Features
--------

- Trio and async client implementation only.
- Simple handling of authentication
- No need to generate timestamps yourself, the wrapper does it for you
- Response exception handling
- Websocket handling with reconnection and multiplexed connections
- Symbol Depth Cache
- Historical Kline/Candle fetching function
- Futures Trading

Quick Start
-----------

`Register an account with Binance <https://accounts.binance.com/en/register?ref=10099792>`_.

`Generate an API Key <https://www.binance.com/en/my/settings/api-management>`_ and assign relevant permissions.

If you are using an exchange from the US, Japan or other TLD then make sure pass `tld='us'` when creating the
client.


.. code:: bash

    pip install trio-binance


Async Example
-------------

Read `Async basics for Binance <https://sammchardy.github.io/binance/2021/05/01/async-binance-basics.html>`_
for more information.

.. code:: python

    import trio
    import json

    from binance import AsyncClient, DepthCacheManager, BinanceSocketManager

    async def main():

        # initialise the client
        client = await AsyncClient.create()

        # run some simple requests
        print(json.dumps(await client.get_exchange_info(), indent=2))

        print(json.dumps(await client.get_symbol_ticker(symbol="BTCUSDT"), indent=2))

        # initialise websocket factory manager
        bsm = BinanceSocketManager(client)

        # create listener using async with
        # this will exit and close the connection after 5 messages
        async with bsm.trade_socket('ETHBTC') as ts:
            for _ in range(5):
                res = await ts.recv()
                print(f'recv {res}')

        # get historical kline data from any date range

        # fetch 1 minute klines for the last day up until now
        klines = client.get_historical_klines("BNBBTC", AsyncClient.KLINE_INTERVAL_1MINUTE, "1 day ago UTC")

        # use generator to fetch 1 minute klines for the last day up until now
        async for kline in await client.get_historical_klines_generator("BNBBTC", AsyncClient.KLINE_INTERVAL_1MINUTE, "1 day ago UTC"):
            print(kline)

        # fetch 30 minute klines for the last month of 2017
        klines = client.get_historical_klines("ETHBTC", Client.KLINE_INTERVAL_30MINUTE, "1 Dec, 2017", "1 Jan, 2018")

        # fetch weekly klines since it listed
        klines = client.get_historical_klines("NEOBTC", Client.KLINE_INTERVAL_1WEEK, "1 Jan, 2017")

        # setup an async context the Depth Cache and exit after 5 messages
        async with DepthCacheManager(client, symbol='ETHBTC') as dcm_socket:
            for _ in range(5):
                depth_cache = await dcm_socket.recv()
                print(f"symbol {depth_cache.symbol} updated:{depth_cache.update_time}")
                print("Top 5 asks:")
                print(depth_cache.get_asks()[:5])
                print("Top 5 bids:")
                print(depth_cache.get_bids()[:5])

        # Vanilla options Depth Cache works the same, update the symbol to a current one
        options_symbol = 'BTC-210430-36000-C'
        async with OptionsDepthCacheManager(client, symbol=options_symbol) as dcm_socket:
            for _ in range(5):
                depth_cache = await dcm_socket.recv()
                count += 1
                print(f"symbol {depth_cache.symbol} updated:{depth_cache.update_time}")
                print("Top 5 asks:")
                print(depth_cache.get_asks()[:5])
                print("Top 5 bids:")
                print(depth_cache.get_bids()[:5])

        await client.close_connection()

    if __name__ == "__main__":
        trio.run(main)


Donate
------

If this library helps, feel free to donate.

- ETH:
- BTC:

