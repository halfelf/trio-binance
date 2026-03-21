import os

import trio
import pytest_trio

from trio_binance import AsyncClient
from trio_binance.streams import BinanceSocketManager


async def test_private_stream():
    client = AsyncClient(
        api_key=os.getenv("BINANCE_ED25519_API_KEY"),
        api_secret=os.getenv("BINANCE_ED25519_PRIVATE_KEY_PATH"),
        sign_style="Ed25519",
    )
    bsm = await BinanceSocketManager.create(client, endpoint="portfolio")
    # Obtain a listenKey before connecting; setting it on the instance causes
    # connect() to open the private WebSocket and start the keepalive task.
    bsm.listen_key = await client.portfolio_margin_stream_get_listen_key()
    count = 0
    async with bsm.connect():
        # Private user-data events are pushed by the server without a SUBSCRIBE
        # handshake, so there are no control messages to skip.
        with trio.fail_after(60):
            async for msg in bsm.get_next_message():
                assert "e" in msg
                count += 1
                if count == 5:
                    break
