=================================
Welcome to trio-binance
=================================

This is an unofficial Python wrapper for the `Binance exchange REST API v3 <https://binance-docs.github.io/apidocs/spot/en>`_. I am in no way affiliated with Binance, use at your own risk.

And this repository is forked from `python-binance <https://github.com/sammchardy/python-binance>`_, but has only async client, and works **only** with `trio <https://trio.readthedocs.io/en/stable/index.html>`_ or `trio-compatible <https://trio.readthedocs.io/en/stable/awesome-trio-libraries.html#trio-asyncio-interoperability>`_ asynchronous frameworks. I have kept the REST API as close to the original as possible, but using a completely different approach for websocket streams, due to the design of trio and structured concurrency.

Source code
  https://github.com/halfelf/trio-binance

Quick Start
-----------

`Register an account with Binance <https://accounts.binance.com/en/register?ref=10099792>`_.

`Generate an API Key <https://www.binance.com/en/my/settings/api-management>`_ and assign relevant permissions.

.. code:: bash

    pip install trio-binance
    # or, I suggest using uv to manage your Python dependencies.
    uv add trio-binance


Example
-------------

Check pytest file under ``tests``.

Donate
------

If this library helps, feel free to donate.

- ETH: 0xf560e5F7F234307A20670ed8A5778F350a8366d1
